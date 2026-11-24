"""戳一戳响应插件（复用主对话静态人设前缀与 Vertex 缓存，命中率 90%+）。"""
from __future__ import annotations

import random
import time

from nonebot import logger, on_notice
from nonebot.adapters.onebot.v11 import Bot, NoticeEvent
from nonebot.rule import Rule

COOLDOWN_SEC = 5.0
_last_poke: dict[str, float] = {}
_poke_count: dict[str, int] = {}

FREQUENT_POKE = 3
CLOSE_AFFECTION = 80.0

POKE_INPUTS = [
    "（有人没说话，突然伸手戳了你一下）",
    "（有人趁你不注意，戳了戳你）",
    "（有人在你旁边戳了你一下，没出声）",
]


def _is_poke(event: NoticeEvent) -> bool:
    ntype = str(getattr(event, "notice_type", "") or "")
    sub = str(getattr(event, "sub_type", "") or "")
    return ntype == "notify" and sub == "poke"


async def _only_poke_me(bot: Bot, event: NoticeEvent) -> bool:
    if not _is_poke(event):
        return False
    target = str(getattr(event, "target_id", "") or "")
    self_id = str(getattr(event, "self_id", "") or "")
    return bool(target) and target == self_id


poke_cmd = on_notice(rule=Rule(_only_poke_me), priority=4, block=True)


@poke_cmd.handle()
async def _(bot: Bot, event: NoticeEvent) -> None:
    from plugins.chat_bot import (
        FARM_ENABLED,
        FISHING_ENABLED,
        GEMINI_CHAT_BASE,
        GEMINI_CHAT_KEY,
        GEMINI_CHAT_MODEL,
        VERTEX_AUTH,
        VERTEX_PROXY,
        VERTEX_THINKING,
        _generate_reply,
        _openai_chat_params,
        aff as aff_mod,
        llm,
        memory_store,
        persona,
        store,
    )

    raw_gid = getattr(event, "group_id", None)
    gid = str(raw_gid) if raw_gid else "private"
    mem_scope = "private" if gid == "private" else "group"
    sender = str(getattr(event, "user_id", "") or "")
    now = time.time()

    last = _last_poke.get(gid, 0.0)
    if now - last < COOLDOWN_SEC:
        return
    _last_poke[gid] = now

    if not persona.base or not persona.tiers:
        persona.load()
    score = store.peek(sender)
    tier = aff_mod.tier_of(score, persona.tiers)
    if tier is None:
        return

    # 保持静态人设 100% 不变，与主聊天共享同一份 Vertex 隐式/显式人设缓存！
    static_system = llm.build_static_system(
        persona.base, persona.name, games_enabled=FARM_ENABLED or FISHING_ENABLED
    )
    dynamic_tail = llm.build_dynamic_block(
        affection=score,
        tier_name=str(tier.get("name", "?")),
        tier_desc=str(tier.get("desc", "")),
        tier_range=f"{tier.get('min')}~{tier.get('max')}",
        max_chars=32,
        length_level="short",
    )
    poke_guide = (
        "\n──────── 当前突发互动 ────────\n"
        "对方没说话，突然伸手戳了你一下。\n"
        "· 用 15~35 字简短作出符合当前好感档位（"
        f"「{tier.get('name', '?')}」"
        "）的反应：负/低好感冷斥赶人，中好感挑眉微讽，高好感嘴硬纵容。\n"
    )
    user_text = random.choice(POKE_INPUTS)
    model_input = (
        f"{user_text}\n\n"
        "──────── 系统运行时状态（本轮自动附加，不要向对方复述）────────\n"
        f"{dynamic_tail}{poke_guide}"
    )

    result = await _generate_reply(
        [{"role": "user", "content": model_input}],
        static_system,
        min_chars=30,
        gemini_key=GEMINI_CHAT_KEY,
        gemini_model=GEMINI_CHAT_MODEL,
        gemini_base=GEMINI_CHAT_BASE,
        vertex_auth=VERTEX_AUTH,
        vertex_proxy=VERTEX_PROXY,
        vertex_thinking=VERTEX_THINKING,
        **_openai_chat_params(),
    )

    if not result.ok or not result.reply.strip():
        logger.warning(f"poke: 生成失败 {result.error[:100]}")
        await poke_cmd.finish("……")

    reply = result.reply.strip()
    logger.info(f"poke: via={result.via} {result.elapsed:.1f}s → {reply[:60]!r}")

    try:
        count = _poke_count.get(sender, 0) + 1
        _poke_count[sender] = count
        if score >= CLOSE_AFFECTION:
            value = "他喜欢用戳一下来打招呼"
        elif count >= FREQUENT_POKE:
            value = f"他经常戳我（已经 {count} 次了）"
        else:
            value = "他伸手戳过我一下，没说话"
        memory_store.set(sender, "互动", value, score, scope=mem_scope, group_id=str(raw_gid or ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"poke: 写记忆失败 {exc!r}")

    await poke_cmd.finish(reply)
