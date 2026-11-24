"""Compact public help, quoted topic selection, and a private operations manual."""
from __future__ import annotations

import math
from time import monotonic

from nonebot import get_plugin, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent, MessageSegment, PrivateMessageEvent
from nonebot.rule import Rule
from nonebot.typing import T_State

GROUP_COOLDOWN_SEC = 60.0
PRIVATE_COOLDOWN_SEC = 10.0
TOPIC_COOLDOWN_SEC = 15.0
CHOICE_COOLDOWN_SEC = 5.0
ADMIN_COOLDOWN_SEC = 3.0
MENU_TTL_SEC = 60.0

TRIGGERS = frozenset({"机器帮助", "机器人帮助", "帮助", "功能", "help", "菜单", "指令"})
ADMIN_TRIGGERS = frozenset({"管理帮助", "运维帮助", "管理员帮助", "admin help", "admin"})
TOPICS = ("对话", "做题", "生图", "好感", "游戏", "工具")
_ALIASES = {
    alias: topic
    for topic, aliases in {
        "对话": ("聊天", "互动", "识图", "搜索"),
        "做题": ("答题", "解题", "源文件"),
        "生图": ("画图", "图片", "图像", "图像生成", "gif", "配额"),
        "好感": ("好感度", "记忆"),
        "游戏": ("无尽电梯", "电梯", "电梯玩法"),
        "工具": ("小工具", "骰子", "私聊"),
    }.items()
    for alias in (topic, *aliases)
}
_DEFAULT_TIERS = ("戒备", "疏离", "初识", "眼熟", "聊得来", "熟络", "信任", "亲近", "默契")
_cooldowns: dict[tuple[str, ...], float] = {}
_menus: dict[tuple[str, ...], float] = {}


def _loaded(name: str):
    plugin = get_plugin(name)
    return plugin.module if plugin else None


def _number(module, name: str, default: int | float) -> int | float:
    value = getattr(module, name, default)
    return value if type(value) in (int, float) and math.isfinite(value) else default


def _display_name(runtime) -> str:
    persona = getattr(runtime, "persona", None)
    return " ".join(str(getattr(persona, "name", "")).split())[:30] or "GPT龙娘"


def _overview(runtime) -> str:
    title = _display_name(runtime)
    game_loaded = _loaded("endless_elevator") is not None
    game = "跨群搜打撤，发「电梯」上车" if game_loaded else "当前暂未加载"
    poke = "，也可以戳我的头像" if _loaded("poke") else ""
    shortcuts = "好感度 · 记忆 · 图像配额" + (" · 电梯" if game_loaded else "")
    return "\n".join((
        f"🐉 {title} · 功能总览",
        f"在群里 @龙娘 或引用我的消息开聊{poke}。",
        "支持带图提问和联网搜索。",
        "",
        "1. 💬 对话互动 · 聊天、识图、图像溯源",
        "2. 📝 智能做题 · 拍题图、看解答、领源文件",
        "3. 🎨 图像生成 · 画图、改图、动态 GIF",
        "4. 💗 好感与记忆 · 查分、历史、重置",
        f"5. 🛗 无尽电梯 · {game}",
        "6. 🎲 小工具 · 骰子、选择、抛硬币",
        "",
        "发「帮助 做题」等，或在 60 秒内引用本条回复 1～6。",
        f"常用直达　{shortcuts}",
    ))


def _conversation(runtime) -> str:
    poke = _loaded("poke")
    cooldown = _number(poke, "COOLDOWN_SEC", 5)
    check_awareness = getattr(getattr(runtime, "awareness", None), "is_enabled", None)
    awareness = callable(check_awareness) and check_awareness()
    lines = [
        "💬 对话与互动",
        "",
        "• 群里 @龙娘 或引用我的消息就能聊，可以带图，也可以引用群友发言。",
    ]
    if poke:
        lines.append(f"• 戳一戳我的头像，我会按当前好感回应。同群间隔 {cooldown:g} 秒。")
    lines.extend((
        "• 主动接话已开启，合适时我会像群友一样插一句。" if awareness else "• 主动接话当前关闭。",
        "• 联网搜索可查资料、核对新闻和截图里的说法。",
        "• 附图问内容，或让我找图像出处。",
        "• 可以回查本群原话，也可以问某位群友的头像。",
        "",
        "👉 发「帮助」回总览，发「帮助 好感」看相处规则。",
    ))
    return "\n".join(lines)


def _solving(runtime) -> str:
    solver = getattr(runtime, "problem_solver", None)
    summary_every = _number(solver, "PROGRESS_INTERVAL_SECONDS", 60) / 60
    confirm_after = _number(solver, "CONFIRM_AFTER_SECONDS", 240) / 60
    grace = _number(solver, "CONFIRM_GRACE_SECONDS", 60) / 60
    source_ttl = _number(getattr(runtime, "problem_sources", None), "TTL_SECONDS", 600) / 60
    return "\n".join((
        "📝 智能做题",
        "",
        "• @龙娘 发题目或拍题图。短题快答，复杂题会推导并把公式渲染成图。",
        f"• 长题每 {summary_every:g} 分钟报进度，每做 {confirm_after:g} 分钟问一次。"
        f"请在 {grace:g} 分钟内回「继续」。",
        "• 想停时直接发「不做了」或「取消做题」。",
        "",
        f"• 解答发出后 {source_ttl:g} 分钟内，发「源文件」或 @我回「要」。",
        "• 可领取解答文本，有代码时一起打包。有多题时按列表选编号。",
        "",
        "👉 发「帮助」回总览。",
    ))


def _images(runtime) -> str:
    quota = getattr(runtime, "image_quota", None)
    zero = _number(quota, "ZERO_AFFECTION_LIMIT", 2000)
    minimum = _number(quota, "MIN_DAILY_LIMIT", 8000)
    maximum = _number(quota, "MAX_DAILY_LIMIT", 30_000)
    frames = _number(getattr(runtime, "image_settings", None), "MAX_GIF_FRAMES", 16)
    return "\n".join((
        "🎨 图像生成与每日配额",
        "",
        f"• @龙娘 描述画面，可做静态 PNG 或 2～{frames:g} 帧 GIF。",
        "• 带图或引用图片可以改图。额度紧张时，会尝试调整画质或尺寸。",
        "",
        "额度随好感变化，每天北京时间零点重置。",
        "• 负好感暂不可生图。",
        f"• 0 分每天 {zero:,} token。正好感从 {minimum:,} 起，最高 {maximum:,}。",
        "",
        "👉 群聊或私聊发「图像配额」，查今日剩余、已用和累计用量。",
    ))


def _affection(runtime) -> str:
    affection = getattr(runtime, "aff", None)
    memory = getattr(runtime, "memory", None)
    tiers = getattr(getattr(runtime, "persona", None), "tiers", None) or []
    names = tuple(" ".join(str(tier["name"]).split()) for tier in tiers if isinstance(tier, dict) and tier.get("name"))
    names = names or _DEFAULT_TIERS
    bonus_low = _number(affection, "DAILY_BONUS_MIN", 0)
    bonus_high = _number(affection, "DAILY_BONUS_MAX", 0.4)
    praise = _number(affection, "PRAISE_BONUS", 2)
    offense = _number(affection, "OFFENSE_PENALTY", -3)
    gain = _number(affection, "DAILY_GAIN_CAP", 10)
    loss = _number(affection, "DAILY_LOSS_CAP", 15)
    score_min = _number(affection, "AFFECTION_MIN", -50)
    score_max = _number(affection, "AFFECTION_MAX", 100)
    cooldown = _number(affection, "COOLDOWN_SECONDS", 30)
    reset_days = _number(affection, "RESET_INTERVAL_SECONDS", 7 * 86400) / 86400
    threshold = _number(memory, "MEMORY_THRESHOLD", 50)
    memory_max = _number(memory, "MAX_MEMORIES", 30)
    return "\n".join((
        "💗 好感与记忆",
        f"按 QQ 号计分，初始 0 分，范围 {score_min:g}～{score_max:g}。共 {len(names)} 档。",
        " → ".join(names),
        "",
        f"• 非冒犯互动随机 +{bonus_low:g}～{bonus_high:g}。明显夸赞 {praise:+g}，冒犯 {offense:+g}，这些有 {cooldown:g} 秒冷却。",
        f"• 每天最多加 {gain:g} 分、扣 {loss:g} 分。",
        f"• 超过 {threshold:g} 分会挑选长期记忆，每人最多 {memory_max:g} 条，群聊只用本群的记忆。",
        "",
        "• 「好感度」查档位、距下一档和近期变动。",
        "• 「好感度 历史」把记录发到私聊。",
        "• 「记忆」查看记下的事。",
        f"• 「好感度 重置」在 60 秒内回「是」，每 {reset_days:g} 天一次，同时清空私聊上下文、好感记录和长期记忆。",
    ))


def _elevator(_runtime) -> str:
    if _loaded("endless_elevator") is None:
        return "🛗 无尽电梯当前暂未加载。\n\n👉 发「帮助」查看其他功能。"
    return "\n".join((
        "🛗 无尽电梯 · 跨群搜打撤",
        "",
        "• 在群里发「电梯」，首次先确认昵称。角色和仓库跨群共用。",
        "• 免费通行证每天零点补足至 20 张，优先消耗。商店每天限购 20 张，付费票最多持有 20 张，剩余可留到次日。",
        "• 武器、护甲、挂件、核心各穿一件。「图鉴进度」能看到长期增益和四种虹彩背包技能。",
        "• 越往下宝物越贵。发「撤离」把战利品带回家。",
        "• 前 3 局有萌新保险。之后翻车时安全箱保底，可能留遗物盒。",
        "",
        "• 局内可发「进 / 打 / 跑 / 撤 / 存」，也可引用游戏菜单回序号。",
        "• 遇精英可「摇人」，遇遗物盒可「舔」或「捞」。",
        "• 8、18、30 层和此后每 20 层是首领房。击败回满血，正式局另有 0.5% 虹彩机会。",
        "• 「局内道具」查看本局拾取物，「地图道具」查各地图手册。临时道具随局结束清空。",
        "• 发「帮助 地图」「帮助 道具」「帮助 词条」「帮助 合成」查百科。",
        "",
        "👉 发「电梯帮助」看现在能做什么。",
    ))


def _tools(_runtime) -> str:
    return "\n".join((
        "🎲 群内小工具",
        "",
        "• 掷骰子　「骰子」默认 1d6，也可发「骰子 2d6」或「/roll」。",
        "  支持 1～20 颗、2～1,000 面。",
        "• 做选择　「选择 火锅 烧烤 日料」，支持 2～20 项，也可用「/pick」。",
        "• 抛硬币　发「抛硬币」或「/coin」。",
        "",
        "私聊可以查「图像配额」，也能接收好感度历史记录。",
        "聊天和其他功能请在群里使用。",
        "",
        "👉 发「帮助」回总览。",
    ))


_RENDER = {
    "对话": _conversation, "做题": _solving, "生图": _images,
    "好感": _affection, "游戏": _elevator, "工具": _tools,
}

ADMIN_HELP = """🛠️ 龙娘运维速查

【模型与思考档位】
「聊天 模型 [模型名] [档位]」　「聊天 思考 [档位]」
「做题 模型 [模型名] [档位]」　「做题 思考 [档位]」
群聊或私聊都可用，省略参数打开 60 秒编号菜单。
档位可用关闭／低／中／高／极高／最高，或模型支持的英文档位。

【运行与会话】
「聊天 状态」查看主模型、记忆、搜源和群范围。
「聊天 重读」重读人设、配置和会话。
「聊天 意识层 [状态/开启/关闭]」管理主动接话。
「聊天 生图间隔 [秒数]」查看或调整每人的生图间隔，0 为关闭。
「聊天 清空上下文 [群号]」在私聊清空当前私聊或指定群会话。
「查好感 [@某人]」查看自己或指定群友的好感详情。

【群和机器人】
「加群 [群号]」　「删群 [群号]」　「群列表」
群里省略群号时操作本群，群列表在私聊查看。
「加机器人 <QQ>」　「删机器人 <QQ>」　「机器人列表」

【插件（私聊）】
「/plugins」列出插件（别名「插件」）。
「/reload [插件名]」重载指定插件，省略时重载全部。
「/unload <插件名>」卸载插件。

【私聊扩展】
私聊讨论代码、配置等可进入元对话。发「回去」或「继续演」恢复角色。
发「色色 帮助」查看独立剧情模式的指令。"""


def _normalize(text: str) -> str:
    text = text.strip().removeprefix("/").strip(" \t\r\n　?？!！。.,，、~～")
    return " ".join(text.casefold().split())


def _private_admin(event: MessageEvent) -> bool:
    if not isinstance(event, PrivateMessageEvent):
        return False
    load = getattr(_loaded("chat_bot"), "_admin_set", None)
    if load is None:
        load = getattr(_loaded("hot_reload"), "_allowlist", None)
    try:
        return callable(load) and str(event.user_id) in load()
    except (OSError, TypeError, ValueError):
        return False


def _scope(event: MessageEvent) -> tuple[str, ...]:
    if isinstance(event, GroupMessageEvent):
        return "group", str(event.self_id), str(event.group_id)
    return "private", str(event.self_id), str(event.user_id)


def _quoted_id(event: MessageEvent) -> str:
    for segment in event.original_message or event.get_message():
        if segment.type == "reply":
            return str(segment.data.get("id") or "")
    return ""


def _help_route(event: MessageEvent) -> tuple[str, bool] | None:
    if not isinstance(event, GroupMessageEvent) and not _private_admin(event):
        return None
    text = _normalize(event.get_plaintext())
    first, _, rest = text.partition(" ")
    if first in TRIGGERS:
        if not rest:
            return "", False
        topic = _ALIASES.get(rest)
        return (topic, False) if topic else None
    if text in ("1", "2", "3", "4", "5", "6"):
        quote = _quoted_id(event)
        if quote and monotonic() < _menus.get((*_scope(event), quote), 0):
            return TOPICS[int(text) - 1], True
    return None


def _match_help(event: MessageEvent, state: T_State) -> bool:
    route = _help_route(event)
    if route is None:
        return False
    state["bot_help_route"] = route
    return True


def _match_admin_help(event: MessageEvent) -> bool:
    return _normalize(event.get_plaintext()) in ADMIN_TRIGGERS and _private_admin(event)


bot_help = on_message(rule=Rule(_match_help), priority=3, block=True)
admin_help = on_message(rule=Rule(_match_admin_help), priority=2, block=True)


async def _deliver(
    bot: Bot, event: MessageEvent, text: str, key: tuple[str, ...], seconds: float, *, numbered: bool = False,
) -> None:
    now = monotonic()
    if now < _cooldowns.get(key, 0):
        return
    for settings in (_cooldowns, _menus):
        for item, until in tuple(settings.items()):
            if until <= now:
                settings.pop(item)
    deadline = now + seconds
    _cooldowns[key] = deadline
    message = MessageSegment.text(text)
    if isinstance(event, GroupMessageEvent):
        message = MessageSegment.reply(event.message_id) + message
    try:
        sent = await bot.send(event, message)
    except Exception as exc:  # noqa: BLE001
        if _cooldowns.get(key) == deadline:
            _cooldowns.pop(key)
        logger.warning(f"bot_help: 帮助消息发送失败（{type(exc).__name__}）")
        return
    if numbered and isinstance(sent, dict) and sent.get("message_id") is not None:
        _menus[(*_scope(event), str(sent["message_id"]))] = monotonic() + MENU_TTL_SEC


@bot_help.handle()
async def send_public_help(bot: Bot, event: MessageEvent, state: T_State) -> None:
    if not isinstance(event, GroupMessageEvent) and not _private_admin(event):
        return
    topic, quoted = state["bot_help_route"]
    scope = _scope(event)
    runtime = _loaded("chat_bot")
    if not topic:
        cooldown = GROUP_COOLDOWN_SEC if isinstance(event, GroupMessageEvent) else PRIVATE_COOLDOWN_SEC
        await _deliver(bot, event, _overview(runtime), ("menu", *scope), cooldown, numbered=True)
    elif quoted:
        await _deliver(
            bot, event, _RENDER[topic](runtime), ("choice", *scope, str(event.user_id), topic), CHOICE_COOLDOWN_SEC,
        )
    else:
        await _deliver(bot, event, _RENDER[topic](runtime), ("topic", *scope, topic), TOPIC_COOLDOWN_SEC)


@admin_help.handle()
async def send_admin_help(bot: Bot, event: MessageEvent) -> None:
    if _private_admin(event):
        await _deliver(bot, event, ADMIN_HELP, ("admin", *_scope(event)), ADMIN_COOLDOWN_SEC)
