"""chat_bot —— 模块化主对话协调器（缓存极限命中 + GPT Sol 前置意图审查 + 农场/钓鱼直控与自动化派单）。

重构要点：
  1. 拆分原 3,215 行单文件巨石，配置、人设、好感日志、限流冷却、消息/CQ 解析、三路识图、
     游戏读写控制、自动化流程精算各自独立成模块；
  2. 前置意图审查默认走 OpenAI GPT Sol（`gpt-6.1-sol`，`/v1/responses`，`reasoning.effort="low"`），
     配合 ≥1024 tokens 纯静态提示词与 `prompt_cache_key` 吃满 90%+ 缓存命中并极速返回；
  3. 主对话、重试、续写全链路保持 `system_prompt` 与 `REPLY_SCHEMA` 100% 恒定，
     动态状态（好感/记忆/农场/钓鱼/代执行回执/待确认派单/搜索/字数约束）严格锚定在当轮消息末尾；
  4. 支持自然语言直接操控农场与钓鱼，以及自动识别托管需求、精算最优流程并生成唯一派单号（`ORD-XXXXXX`）供玩家确认。
"""
from __future__ import annotations

import asyncio
import math
import random
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

from network_config import get_proxy
from nonebot import get_driver, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import ActionFailed, Bot, Message, MessageEvent, MessageSegment
from nonebot.params import Command, CommandArg, CommandStart
from nonebot.rule import Rule
from qq_commands import AFF_QUERY_WORDS, AFF_RESET_SUBS, is_affection_history, is_image_quota_query, split_aff_query
from shared_history import (
    apply_affection as _apply_shared_affection,
    merge_sessions as _merge_shared_histories,
    read_session as _read_shared_history,
    read_sessions as _read_shared_histories,
    scope_lock as _scope_lock,
)

from . import affection as aff
from . import groups as group_store
from . import (
    affection_daily,
    affection_log,
    automation_planner,
    awareness,
    context_tools,
    elevator_knowledge,
    env_config,
    farm_bridge,
    game_controller,
    guard,
    image_generation,
    image_quota,
    llm,
    memory,
    message_io,
    persona_loader,
    problem_math,
    problem_solver,
    problem_sources,
    rate_guard,
    search as web_search,
    vertex,
    vision,
    vision_router,
)
from .conversation_scope import (
    attributed_message,
    group_context as _group_context,
    group_history_tail,
    quote_allowed as _quote_allowed,
    sender_name,
    session_key as _session_key,
    speaker_label,
)

HERE = Path(__file__).resolve().parent
PERSONA_FILE = HERE / "persona.toml"

# 兼容外部调用（erp_server.py / bot.py / poke.py）
_parse_env_file = env_config.parse_env_file
_read_env_file = env_config.read_env_file
_env = env_config.get_env
_admin_set = env_config.admin_set
_is_admin = env_config.is_admin
_is_private = message_io.is_private_event
_extract = message_io.extract_message
_fetch_quote = message_io.fetch_quote
_fetch_image = message_io.fetch_image
_reply_to = message_io.reply_to
_send_as_forward = message_io.send_as_forward


def _baidu_creds() -> dict:
    return {
        "ak": _env("CHAT_BAIDU_AK", ""),
        "sk": _env("CHAT_BAIDU_SK", ""),
        "ocr": env_config.env_bool("CHAT_BAIDU_OCR", "0"),
        "qianfan_key": _env("CHAT_BAIDU_SEARCH_KEY", ""),
        "qianfan_model": _env("CHAT_BAIDU_VISION_MODEL", "internvl3-38b"),
        "clue": env_config.env_bool("CHAT_BAIDU_CLUE", "1"),
    }


VISION_WEB_FREE_TIER = 1000
VISION_WEB_RESERVE = 50

# ====================================================================== 运行时配置
ARK_KEY = _env("CHAT_ARK_API_KEY") or _env("ARK_API_KEY")
GLM_KEY = _env("CHAT_GLM_API_KEY") or _env("GLM_API_KEY")
OPENAI_KEY = _env("CHAT_OPENAI_API_KEY") or _env("OPENAI_API_KEY")
OPENAI_CHAT_MODEL, OPENAI_CHAT_EFFORT = env_config.load_chat_model(
    llm.DEFAULT_OPENAI_CHAT_MODEL, llm.DEFAULT_OPENAI_CHAT_EFFORT,
)
INTENT_MODEL = _env("CHAT_INTENT_MODEL", llm.DEFAULT_INTENT_MODEL)
INTENT_EFFORT = _env("CHAT_INTENT_EFFORT", llm.DEFAULT_INTENT_EFFORT)
DEEPSEEK_KEY = _env("CHAT_DEEPSEEK_API_KEY") or _env("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL = _env("CHAT_DEEPSEEK_MODEL", llm.DEEPSEEK_MODEL)

ARK_MODEL = _env("CHAT_MODEL", llm.DEFAULT_MODEL)
VISION_MODEL = _env("CHAT_VISION_MODEL", vision.DEFAULT_VISION_MODEL)
VISION_FALLBACK = _env("CHAT_VISION_FALLBACK", vision.FALLBACK_VISION_MODEL)
VISION_GEMINI_BASE = _env("CHAT_VISION_GEMINI_BASE", "")
VISION_GEMINI_KEY = _env("CHAT_VISION_GEMINI_KEY", "")
VISION_GEMINI_MODEL = _env("CHAT_VISION_GEMINI_MODEL", "")
VISION_WEB_ENABLED = env_config.env_bool("CHAT_VISION_WEB", "1")
VISION_WEB_MAX = env_config.env_int("CHAT_VISION_WEB_MAX", 10, minimum=1, maximum=10)
VISION_WEB_CAP = env_config.env_int(
    "CHAT_VISION_WEB_MONTHLY_CAP", 950, minimum=0, maximum=VISION_WEB_FREE_TIER - VISION_WEB_RESERVE
)

GEMINI_CHAT_BASE = _env("CHAT_GEMINI_BASE", "")
GEMINI_CHAT_KEY = _env("CHAT_GEMINI_KEY", "")
GEMINI_CHAT_MODEL = _env("CHAT_GEMINI_MODEL", "")
VERTEX_AUTH = _env("CHAT_VERTEX_AUTH", "")
VERTEX_PROXY = get_proxy()
VERTEX_THINKING = _env("CHAT_VERTEX_THINKING", "low")
VERTEX_SESSION_CACHE = env_config.env_bool("CHAT_VERTEX_SESSION_CACHE", "1")

LENGTH_ENABLED = env_config.env_bool("CHAT_LENGTH_ENABLED", "1")
LENGTH_MULTIPLIER = env_config.env_float("CHAT_LENGTH_MULTIPLIER", 1.0, minimum=0.5, maximum=3.0)
TRIGGER_NAMES = [n.strip() for n in _env("CHAT_TRIGGER_NAMES", "GPT龙娘").split(",") if n.strip()]
HISTORY_TURNS = env_config.env_int("CHAT_HISTORY_TURNS", 12, minimum=2)

STATE_FILE = Path(_env("CHAT_STATE_FILE", str(HERE.parent.parent / "state" / "chat_affection.json")))
MEMORY_FILE = Path(_env("CHAT_MEMORY_FILE", str(HERE.parent.parent / "state" / "chat_memory.json")))

PRIVATE_ENABLED = env_config.env_bool("CHAT_PRIVATE_ENABLED", "1")
FARM_ENABLED = env_config.env_bool("CHAT_FARM_ENABLED", "0")
FISHING_ENABLED = env_config.env_bool("CHAT_FISHING_ENABLED", "0")
WEB_ENABLED = env_config.env_bool("CHAT_WEB_ENABLED", "1")
BAIDU_SEARCH_KEY = _env("CHAT_BAIDU_SEARCH_KEY", "")
TAVILY_KEY = _env("CHAT_TAVILY_KEY", "")
SEARCH_PROXY = get_proxy()
WEB_MAX_HITS = env_config.env_int("CHAT_WEB_MAX_HITS", 3, minimum=1, maximum=5)

HISTORY_FILE = Path(_env("CHAT_HISTORY_FILE", str(HERE.parent.parent / "state" / "chat_history.json")))
HISTORY_KEEP_RECENT = env_config.env_int("CHAT_HISTORY_KEEP_RECENT", 40, minimum=20)
HISTORY_COMPRESS_AT = env_config.env_int("CHAT_HISTORY_COMPRESS_AT", 64, minimum=HISTORY_KEEP_RECENT + 2)
HISTORY_KEEP = HISTORY_KEEP_RECENT
HISTORY_RECENT_BYTES = 24 * 1024
HISTORY_SECTIONS_MAX = env_config.env_int("CHAT_HISTORY_SECTIONS_MAX", 5, minimum=1)
HISTORY_COMPRESS_BATCH = env_config.env_int(
    "CHAT_HISTORY_COMPRESS_BATCH", 16, minimum=2, maximum=16
)

AFFECTION_HISTORY_FILE = Path(
    _env("CHAT_AFFECTION_HISTORY", str(HERE.parent.parent / "state" / "affection_history.jsonl"))
)
AFFECTION_HISTORY_QUERY = 15

COOLDOWN_FILE = Path(_env("CHAT_COOLDOWN_FILE", str(HERE.parent.parent / "state" / "chat_cooldown.json")))
OFFENSE_LIMIT = env_config.env_int("CHAT_OFFENSE_LIMIT", 3, minimum=1)
COOLDOWN_HOURS = env_config.env_int("CHAT_COOLDOWN_HOURS", 24, minimum=1)
COOLDOWN_SECONDS = env_config.env_int("CHAT_COOLDOWN_SECONDS", COOLDOWN_HOURS * 3600, minimum=1)

RATE_MAX = env_config.env_int("CHAT_RATE_LIMIT_MAX", 8, minimum=1)
RATE_WINDOW = env_config.env_float("CHAT_RATE_LIMIT_WINDOW", 300.0, minimum=10.0)
RATE_ADMIN_EXEMPT = env_config.env_bool("CHAT_RATE_LIMIT_ADMIN_EXEMPT", "1")

# ====================================================================== 状态存储
store = aff.AffectionStore(STATE_FILE)
memory_store = memory.MemoryStore(MEMORY_FILE)
image_quota_store = image_quota.ImageQuota(HERE.parent.parent / "state" / "image_quota.sqlite3")
_cooldown_store = rate_guard.OffenseCooldownStore(COOLDOWN_FILE)
_rate_limiter = rate_guard.SlidingRateLimiter()
_model_change_lock = asyncio.Lock()
Persona = persona_loader.Persona
persona = Persona(PERSONA_FILE)
persona.load()


def reload_runtime_config() -> None:
    """热重算模块级配置（/chat reload 调用）。"""
    global ARK_KEY, GLM_KEY, OPENAI_KEY, INTENT_MODEL, INTENT_EFFORT, DEEPSEEK_KEY, DEEPSEEK_MODEL
    global OPENAI_CHAT_MODEL, OPENAI_CHAT_EFFORT
    global ARK_MODEL, VISION_MODEL, VISION_FALLBACK
    global VISION_GEMINI_BASE, VISION_GEMINI_KEY, VISION_GEMINI_MODEL
    global VISION_WEB_ENABLED, VISION_WEB_MAX, VISION_WEB_CAP
    global GEMINI_CHAT_BASE, GEMINI_CHAT_KEY, GEMINI_CHAT_MODEL
    global VERTEX_AUTH, VERTEX_PROXY, VERTEX_THINKING, VERTEX_SESSION_CACHE
    global LENGTH_ENABLED, LENGTH_MULTIPLIER, TRIGGER_NAMES, HISTORY_TURNS
    global HISTORY_COMPRESS_AT, HISTORY_KEEP_RECENT, HISTORY_COMPRESS_BATCH
    global PRIVATE_ENABLED, FARM_ENABLED, FISHING_ENABLED, WEB_ENABLED, WEB_MAX_HITS
    global BAIDU_SEARCH_KEY, TAVILY_KEY, SEARCH_PROXY
    global OFFENSE_LIMIT, COOLDOWN_HOURS, COOLDOWN_SECONDS
    global RATE_MAX, RATE_WINDOW, RATE_ADMIN_EXEMPT

    ARK_KEY = _env("CHAT_ARK_API_KEY") or _env("ARK_API_KEY")
    GLM_KEY = _env("CHAT_GLM_API_KEY") or _env("GLM_API_KEY")
    OPENAI_KEY = _env("CHAT_OPENAI_API_KEY") or _env("OPENAI_API_KEY")
    OPENAI_CHAT_MODEL, OPENAI_CHAT_EFFORT = env_config.load_chat_model(
        llm.DEFAULT_OPENAI_CHAT_MODEL, llm.DEFAULT_OPENAI_CHAT_EFFORT,
    )
    INTENT_MODEL = _env("CHAT_INTENT_MODEL", llm.DEFAULT_INTENT_MODEL)
    INTENT_EFFORT = _env("CHAT_INTENT_EFFORT", llm.DEFAULT_INTENT_EFFORT)
    DEEPSEEK_KEY = _env("CHAT_DEEPSEEK_API_KEY") or _env("DEEPSEEK_API_KEY")
    DEEPSEEK_MODEL = _env("CHAT_DEEPSEEK_MODEL", llm.DEEPSEEK_MODEL)

    ARK_MODEL = _env("CHAT_MODEL", llm.DEFAULT_MODEL)
    VISION_MODEL = _env("CHAT_VISION_MODEL", vision.DEFAULT_VISION_MODEL)
    VISION_FALLBACK = _env("CHAT_VISION_FALLBACK", vision.FALLBACK_VISION_MODEL)
    VISION_GEMINI_BASE = _env("CHAT_VISION_GEMINI_BASE", "")
    VISION_GEMINI_KEY = _env("CHAT_VISION_GEMINI_KEY", "")
    VISION_GEMINI_MODEL = _env("CHAT_VISION_GEMINI_MODEL", "")
    VISION_WEB_ENABLED = env_config.env_bool("CHAT_VISION_WEB", "1")
    VISION_WEB_MAX = env_config.env_int("CHAT_VISION_WEB_MAX", 10, minimum=1, maximum=10)
    VISION_WEB_CAP = env_config.env_int(
        "CHAT_VISION_WEB_MONTHLY_CAP", 950, minimum=0, maximum=VISION_WEB_FREE_TIER - VISION_WEB_RESERVE
    )
    GEMINI_CHAT_BASE = _env("CHAT_GEMINI_BASE", "")
    GEMINI_CHAT_KEY = _env("CHAT_GEMINI_KEY", "")
    GEMINI_CHAT_MODEL = _env("CHAT_GEMINI_MODEL", "")
    VERTEX_AUTH = _env("CHAT_VERTEX_AUTH", "")
    VERTEX_PROXY = get_proxy()
    VERTEX_THINKING = _env("CHAT_VERTEX_THINKING", "low")
    VERTEX_SESSION_CACHE = env_config.env_bool("CHAT_VERTEX_SESSION_CACHE", "1")
    LENGTH_ENABLED = env_config.env_bool("CHAT_LENGTH_ENABLED", "1")
    LENGTH_MULTIPLIER = env_config.env_float("CHAT_LENGTH_MULTIPLIER", 1.0, minimum=0.5, maximum=3.0)
    TRIGGER_NAMES = [n.strip() for n in _env("CHAT_TRIGGER_NAMES", "GPT龙娘").split(",") if n.strip()]
    HISTORY_TURNS = env_config.env_int("CHAT_HISTORY_TURNS", 12, minimum=2)
    HISTORY_KEEP_RECENT = env_config.env_int("CHAT_HISTORY_KEEP_RECENT", 40, minimum=20)
    HISTORY_COMPRESS_AT = env_config.env_int("CHAT_HISTORY_COMPRESS_AT", 64, minimum=HISTORY_KEEP_RECENT + 2)
    HISTORY_COMPRESS_BATCH = env_config.env_int(
        "CHAT_HISTORY_COMPRESS_BATCH", 16, minimum=2, maximum=16
    )
    PRIVATE_ENABLED = env_config.env_bool("CHAT_PRIVATE_ENABLED", "1")
    FARM_ENABLED = env_config.env_bool("CHAT_FARM_ENABLED", "0")
    FISHING_ENABLED = env_config.env_bool("CHAT_FISHING_ENABLED", "0")
    WEB_ENABLED = env_config.env_bool("CHAT_WEB_ENABLED", "1")
    BAIDU_SEARCH_KEY = _env("CHAT_BAIDU_SEARCH_KEY", "")
    TAVILY_KEY = _env("CHAT_TAVILY_KEY", "")
    SEARCH_PROXY = get_proxy()
    WEB_MAX_HITS = env_config.env_int("CHAT_WEB_MAX_HITS", 3, minimum=1, maximum=5)
    OFFENSE_LIMIT = env_config.env_int("CHAT_OFFENSE_LIMIT", 3, minimum=1)
    COOLDOWN_HOURS = env_config.env_int("CHAT_COOLDOWN_HOURS", 24, minimum=1)
    COOLDOWN_SECONDS = env_config.env_int("CHAT_COOLDOWN_SECONDS", COOLDOWN_HOURS * 3600, minimum=1)
    RATE_MAX = env_config.env_int("CHAT_RATE_LIMIT_MAX", 8, minimum=1)
    RATE_WINDOW = env_config.env_float("CHAT_RATE_LIMIT_WINDOW", 300.0, minimum=10.0)
    RATE_ADMIN_EXEMPT = env_config.env_bool("CHAT_RATE_LIMIT_ADMIN_EXEMPT", "1")
    logger.info("chat_bot: 运行时配置已重算（.env 生效）")


def _aux_llm_params() -> tuple[str, str, dict]:
    """返回当前生效的辅助/前置意图模型配置（优先 OpenAI GPT Sol，DeepSeek 自动兜底）。"""
    if OPENAI_KEY:
        return (
            OPENAI_KEY,
            INTENT_MODEL or llm.DEFAULT_INTENT_MODEL,
            {
                "reasoning_effort": INTENT_EFFORT or llm.DEFAULT_INTENT_EFFORT,
                "fallback_key": DEEPSEEK_KEY,
                "fallback_model": DEEPSEEK_MODEL,
            },
        )
    return DEEPSEEK_KEY, DEEPSEEK_MODEL, {}


def _openai_chat_params() -> dict:
    return {
        "api_key": OPENAI_KEY,
        "model": OPENAI_CHAT_MODEL,
        "openai_effort": OPENAI_CHAT_EFFORT,
    }


# ====================================================================== 会话与分块锚定归档
# 注意：Session / _load_histories / _save_histories / _refresh_history_scope / _history_messages
# 被 tests/test_conversation_scope.py 通过 AST 顶层提取验证，保持纯净依赖。
@dataclass
class Session:
    summary: str = ""
    recent: list[dict] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    meta_mode: bool = False
    compaction: dict | None = None

    @classmethod
    def from_row(cls, row: dict, *, meta_mode: bool = False):
        return cls(
            summary=row["summary"].strip(),
            sections=[s.strip() for s in row["sections"]],
            recent=[dict(message) for message in row["recent"]],
            meta_mode=meta_mode,
            compaction=row.get("compaction"),
        )

    def history_row(self) -> dict:
        row = {
            "summary": self.summary,
            "sections": list(self.sections),
            "recent": [dict(message) for message in self.recent],
        }
        if self.compaction:
            row["compaction"] = self.compaction
        return row

    def archive_text(self) -> str:
        parts = [s for s in self.sections if s.strip()]
        if not parts and self.summary.strip():
            parts = [self.summary.strip()]
        return "\n\n".join(parts)


histories: dict[str, Session] = {}
_HISTORY_BASELINE: dict[str, dict] = {}


def _load_histories() -> bool:
    """从磁盘恢复对话上下文。"""
    try:
        rows = _read_shared_histories(HISTORY_FILE, migrate_groups=True)
        restored = {
            key: Session.from_row(row)
            for key, row in rows.items()
            if row["summary"] or row["sections"] or row["recent"] or row.get("compaction")
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"chat_bot: 读取对话上下文失败 {exc!r}")
        return False
    histories.clear()
    histories.update(restored)
    _HISTORY_BASELINE.clear()
    _HISTORY_BASELINE.update({key: session.history_row() for key, session in restored.items()})
    if restored:
        total = sum(len(s.recent) for s in histories.values())
        with_summary = sum(1 for s in histories.values() if s.summary or s.compaction)
        logger.success(
            f"chat_bot: 已恢复 {len(restored)} 个会话的对话上下文"
            f"（{total} 条消息，{with_summary} 人有归档摘要）"
        )
    return True


def _save_histories(*keys: str) -> bool:
    """按作用域原子合并保存会话历史。"""
    baseline = _HISTORY_BASELINE
    if keys:
        baseline = {key: baseline[key] for key in keys if key in baseline}
    payload = {}
    for key in keys or histories:
        session = histories.get(key)
        if session and (session.summary or session.sections or session.recent or session.compaction):
            payload[key] = session.history_row()
    try:
        saved = _merge_shared_histories(HISTORY_FILE, payload, baseline)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"chat_bot: 保存上下文暂未成功：{type(exc).__name__}")
        return False
    for key in baseline.keys() - saved.keys():
        _HISTORY_BASELINE.pop(key, None)
    _HISTORY_BASELINE.update(saved)
    return True


def _refresh_history_scope(key: str) -> None:
    previous = histories.get(key)
    row = _read_shared_history(HISTORY_FILE, key)
    if row is None:
        histories.pop(key, None)
        _HISTORY_BASELINE.pop(key, None)
        return
    session = Session.from_row(row, meta_mode=bool(previous and previous.meta_mode))
    histories[key] = session
    _HISTORY_BASELINE[key] = session.history_row()


def _history_messages(session: Session) -> list[dict]:
    """拼出要发给模型的历史消息（归档摘要固定在前 + 近期原文追加在后）。"""
    msgs: list[dict] = []
    archive = session.archive_text().strip()
    if archive:
        msgs.append(
            {
                "role": "user",
                "content": f"【前序剧情归档（这是你们之前聊过的事）】\n{archive}",
            }
        )
        msgs.append({"role": "assistant", "content": "（这些我都记着。）"})
    msgs.extend(session.recent)
    return msgs


def _group_history_messages(session: Session) -> list[dict]:
    return [attributed_message(message) for message in group_history_tail(session, _history_messages)]


_load_histories()
_compressing_histories: set[str] = set()


@asynccontextmanager
async def _history_scope(key: str):
    async with _scope_lock(HISTORY_FILE, key):
        _refresh_history_scope(key)
        store.load()
        memory_store.load()
        yield


def _history_cut(session: Session) -> int:
    messages = session.recent
    count = len(messages)
    if count <= HISTORY_KEEP_RECENT:
        return 0
    if count < HISTORY_COMPRESS_AT and sum(len(m["content"].encode("utf-8")) for m in messages) <= HISTORY_RECENT_BYTES:
        return 0
    cut = min(HISTORY_COMPRESS_BATCH, count - HISTORY_KEEP_RECENT)
    while cut and messages[cut].get("role") == "assistant":
        cut -= 1
    return cut


async def _maybe_compress_history(key: str) -> None:
    if not OPENAI_KEY or key in _compressing_histories:
        return
    _compressing_histories.add(key)
    try:
        while True:
            async with _scope_lock(HISTORY_FILE, key):
                row = _read_shared_history(HISTORY_FILE, key)
                if row is None:
                    return
                snapshot = Session.from_row(row)
                cut = _history_cut(snapshot)
                if not cut:
                    return
            old = snapshot.recent[:cut]
            archive = snapshot.archive_text()
            messages = []
            if archive:
                messages.append({"role": "user", "content": "【以前的对话摘要】\n" + archive})
            for message in old:
                rendered = attributed_message(message)
                rendered["content"] = llm.clip_text(rendered["content"], 3500)
                messages.append(rendered)

            # Network I/O stays outside the scope lock so another turn can read the old state.
            compacted, error = await llm.compact_history(messages, OPENAI_KEY, OPENAI_CHAT_MODEL, snapshot.compaction)
            if not compacted:
                logger.warning(f"chat_bot: 上下文压缩失败，原文仍可用（{error[:100]}）")
                return

            async with _scope_lock(HISTORY_FILE, key):
                _refresh_history_scope(key)
                session = histories.get(key)
                if (session is None or session.recent[:cut] != old or session.compaction != snapshot.compaction
                        or session.sections != snapshot.sections or session.summary != snapshot.summary):
                    return
                session.compaction = compacted
                session.summary = ""
                session.sections = []
                session.recent = session.recent[cut:]
                if not _save_histories(key):
                    _refresh_history_scope(key)
                    return
                logger.success(f"chat_bot: GPT 已压缩 {cut} 条旧消息，保留 {len(session.recent)} 条原文，"
                               f"压缩上下文 {compacted['tokens']} tokens")
    except (OSError, ValueError) as exc:
        logger.warning(f"chat_bot: 压缩上下文失败 {type(exc).__name__}")
    finally:
        _compressing_histories.discard(key)


# ====================================================================== 辅助封装
_reply_slots = asyncio.Semaphore(env_config.env_int("CHAT_CONCURRENT_REPLIES", 6, minimum=1, maximum=16))
_pending_images: dict[str, list[tuple[float, bytes]]] = {}
PENDING_IMAGE_TTL = 300.0

_META_HINTS = (
    "机器人", "你是ai", "你是 ai", "模型", "提示词", "prompt", "出戏", "扮演",
    "程序", "配置", "代码", "python", "nonebot", "gemini", "api", "token",
)
_META_BACK_HINTS = (
    "回去", "继续演", "回到原来", "恢复扮演", "变回来", "别出戏", "入戏",
)


def _take_pending_images(scope_key: str) -> list[bytes]:
    items = _pending_images.pop(str(scope_key), [])
    now = time.time()
    return [data for ts, data in items if now - ts <= PENDING_IMAGE_TTL]


async def _generate_reply(messages: list[dict], system_prompt: str, **options) -> llm.ChatResult:
    async with _reply_slots:
        return await llm.chat(messages, system_prompt, **options)


def _configured_groups() -> set[str]:
    raw = _env("CHAT_ENABLED_GROUPS", "")
    return {g.strip() for g in raw.split(",") if g.strip()}


def _group_status() -> str:
    try:
        return group_store.load().describe(_configured_groups())
    except (OSError, ValueError):
        return "名单读取失败"


def _group_allowed(event: MessageEvent) -> bool:
    if _is_private(event):
        return True
    try:
        return group_store.load().allows(_current_group(event), _configured_groups())
    except (OSError, ValueError) as exc:
        logger.warning(f"chat_bot: 群名单读取失败 {type(exc).__name__}")
        return False


async def _require_private_admin(matcher, event: MessageEvent) -> bool:
    if _is_private(event) and str(event.user_id) in _admin_set():
        return True
    await matcher.finish()
    return False


def _in_cooldown(user_id: str) -> bool:
    return _cooldown_store.in_cooldown(user_id, is_admin=_is_admin(user_id))


def _should_reply(event: MessageEvent, command_start: str | None = CommandStart()) -> bool:
    if not _group_allowed(event):
        return False
    if command_start:
        return False
    try:
        uid = str(event.get_user_id())
    except Exception:  # noqa: BLE001
        uid = ""
    if uid and _in_cooldown(uid):
        if not _is_private(event):
            try:
                if not message_io.addressed_to_bot(event):
                    return False
            except Exception:  # noqa: BLE001
                return False
        return True
    if _is_private(event):
        return PRIVATE_ENABLED
    try:
        return message_io.addressed_to_bot(event)
    except Exception:  # noqa: BLE001
        return False


def _clean_text(text: str) -> str:
    return message_io.clean_trigger_text(text, TRIGGER_NAMES)


# ====================================================================== 管理与查询命令
def _is_aff_summary(event: MessageEvent) -> bool:
    text = event.get_plaintext()
    head, _ = split_aff_query(text)
    return head in {"affection", "查好感"} and not is_affection_history(text)


def _image_quota_tier(user_id: str) -> tuple[int, str]:
    store.load()
    tiers = persona.tiers
    score = store.peek(user_id)
    tier = aff.tier_of(score, tiers)
    highest = float(tiers[-1]["min"]) if tiers else 0.0
    limit = image_quota.daily_limit(score, float(tier.get("min", 0)), highest)
    return limit, str(tier.get("name") or "当前")


def _is_image_quota_query(event: MessageEvent) -> bool:
    return is_image_quota_query(event.get_plaintext()) and _group_allowed(event)


image_quota_cmd = on_message(rule=Rule(_is_image_quota_query), priority=15, block=True)


@image_quota_cmd.handle()
async def handle_image_quota(bot: Bot, event: MessageEvent) -> None:
    uid = str(event.user_id)
    try:
        limit, tier = _image_quota_tier(uid)
        balance = image_quota_store.balance(uid, limit)
        reply = (
            f"{image_generation.balance_line(balance)}（{tier}）\n"
            f"今日已用 {balance.used:,} · 累计输出 {balance.lifetime:,} 图像 token"
        )
        if limit == 0:
            reply = image_quota.UNAVAILABLE_TEXT + f"\n今日已用 {balance.used:,} · 累计输出 {balance.lifetime:,} 图像 token"
        if balance.reserved:
            reply += f"\n生图任务已预留 {balance.reserved:,} token"
    except (OSError, sqlite3.Error) as exc:
        logger.warning(f"chat_bot: 图像配额查询失败 {type(exc).__name__}")
        reply = "图像配额暂时查不到，稍后再试。"
    if _is_private(event):
        await bot.adapter.send_image_quota(bot, event, reply)
        await image_quota_cmd.finish()
    await image_quota_cmd.finish(_reply_to(event, reply))


status_cmd = on_command("chat", aliases={"聊天"}, force_whitespace=True, priority=1, block=True)
solve_model_cmd = on_command(
    "做题 模型", aliases={"答题 模型", "solve model"}, force_whitespace=True, priority=1, block=True,
)
solve_effort_cmd = on_command(
    "做题 思考", aliases={"做题 思考等级", "答题 思考", "答题 思考等级", "solve effort"},
    force_whitespace=True, priority=1, block=True,
)
aff_cmd = on_command("affection", aliases={"查好感"}, rule=_is_aff_summary, priority=1, block=True)
memory_cmd = on_command("memory", aliases={"记忆"}, force_whitespace=True, priority=1, block=True)
add_group_cmd = on_command("加群", aliases={"添加群", "加群组"}, force_whitespace=True, priority=1, block=True)
del_group_cmd = on_command("删群", aliases={"退群", "删除群", "减群"}, force_whitespace=True, priority=1, block=True)
list_group_cmd = on_command("群列表", aliases={"群清单", "所有群"}, force_whitespace=True, priority=1, block=True)


_EFFORT_ALIASES = {"关闭": "none", "低": "low", "中": "medium", "高": "high", "极高": "xhigh", "最高": "max"}
_MODEL_MENU_TIMEOUT = 60
_MODEL_MENU_CANCEL = {"取消", "取消选择", "0", "０"}


@dataclass(frozen=True)
class _ChatModelMenu:
    choices: tuple[str, ...]
    change_model: bool
    model: str
    expires_at: float
    for_solve: bool = False


_chat_model_menus: dict[tuple[str, str, str], _ChatModelMenu] = {}


def _model_menu_key(event: MessageEvent) -> tuple[str, str, str]:
    return str(event.self_id), str(event.user_id), str(getattr(event, "group_id", ""))


async def _expire_model_menu(bot: Bot, event: MessageEvent, menu: _ChatModelMenu) -> None:
    await asyncio.sleep(max(0, menu.expires_at - time.monotonic()))
    key = _model_menu_key(event)
    if _chat_model_menus.get(key) is menu:
        _chat_model_menus.pop(key)
        try:
            await bot.send(event, _reply_to(event, "选择超时，已取消。"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"chat_bot: 发送模型菜单超时通知失败（{type(exc).__name__}）")


def _open_model_menu(bot: Bot, event: MessageEvent, *, change_model: bool, for_solve: bool = False) -> str:
    if not OPENAI_KEY:
        return "请先配置 OpenAI 主对话通道。"
    if for_solve:
        model, effort = problem_solver.model_settings()
        choices = problem_solver.SOLVE_MODELS if change_model else problem_solver.model_efforts(model)
        feature = "做题"
    else:
        model, effort = OPENAI_CHAT_MODEL, OPENAI_CHAT_EFFORT
        choices = tuple(env_config.CHAT_MODEL_EFFORTS) if change_model else env_config.chat_model_efforts(model)
        feature = "聊天"
    if not choices:
        return f"请先发「{feature} 模型」选择一个支持的模型。"
    menu = _ChatModelMenu(choices, change_model, model, time.monotonic() + _MODEL_MENU_TIMEOUT, for_solve=for_solve)
    _chat_model_menus[_model_menu_key(event)] = menu
    asyncio.create_task(_expire_model_menu(bot, event, menu))
    current = model if change_model else effort
    model_name = "做题模型" if for_solve else "主模型"
    title = f"切换{model_name}" if change_model else f"切换{model_name} {model} 的思考档位"
    lines = [title, f"当前 {current}"]
    lines.extend(f"{index}. {value}{'（当前）' if value == current else ''}" for index, value in enumerate(choices, 1))
    lines.append(f"{_MODEL_MENU_TIMEOUT} 秒内回复序号，发「取消」退出。")
    return "\n".join(lines)


def _is_model_menu_reply(event: MessageEvent) -> bool:
    if _model_menu_key(event) not in _chat_model_menus or str(event.user_id) not in _admin_set():
        return False
    text = event.get_plaintext().strip()
    return text.isdecimal() or text in _MODEL_MENU_CANCEL


model_menu_reply = on_message(rule=Rule(_is_model_menu_reply), priority=2, block=True)


@model_menu_reply.handle()
async def handle_model_menu_reply(event: MessageEvent) -> None:
    key = _model_menu_key(event)
    menu = _chat_model_menus.get(key)
    if menu is None or str(event.user_id) not in _admin_set():
        await model_menu_reply.finish()
        return
    text = event.get_plaintext().strip()
    if time.monotonic() >= menu.expires_at or text in _MODEL_MENU_CANCEL:
        _chat_model_menus.pop(key)
        message = "已取消选择。" if text in _MODEL_MENU_CANCEL else "选择超时，已取消。"
        await model_menu_reply.finish(_reply_to(event, message))
        return
    index = int(text) if text.isdecimal() and len(text) <= 3 else 0
    if not 1 <= index <= len(menu.choices):
        await model_menu_reply.finish(_reply_to(event, f"请输入 1 到 {len(menu.choices)}，或发「取消」。"))
        return
    _chat_model_menus.pop(key)
    result = await _change_chat_model(
        menu.choices[index - 1], change_model=menu.change_model, expected_model="" if menu.change_model else menu.model,
        for_solve=menu.for_solve,
    )
    await model_menu_reply.finish(_reply_to(event, result))


async def _change_chat_model(
    target: str, *, change_model: bool, expected_model: str = "", for_solve: bool = False,
) -> str:
    global OPENAI_CHAT_MODEL, OPENAI_CHAT_EFFORT
    if not OPENAI_KEY:
        return "请先配置 OpenAI 主对话通道。"

    feature = "做题" if for_solve else "聊天"
    model_name = "做题模型" if for_solve else "主模型"
    parts = target.lower().split()
    if not parts or len(parts) > (2 if change_model else 1):
        return f"用法「{feature} 模型 模型名 [思考档位]」或「{feature} 思考 档位」。"

    async with _model_change_lock:
        current_model, current_effort = (
            problem_solver.model_settings() if for_solve else (OPENAI_CHAT_MODEL, OPENAI_CHAT_EFFORT)
        )
        if expected_model and current_model != expected_model:
            return f"{model_name}已切换，请重新发送「{feature} 思考」查看新档位。"
        model = parts[0] if change_model else current_model
        levels = problem_solver.model_efforts(model) if for_solve else env_config.chat_model_efforts(model)
        if not levels:
            return f"请发送「{feature} 模型」查看可切换的模型。"
        explicit_effort = parts[-1] if not change_model or len(parts) == 2 else ""
        effort = _EFFORT_ALIASES.get(explicit_effort, explicit_effort)
        if not effort:
            effort = current_effort if current_effort in levels else levels[0]
        if effort not in levels:
            return f"{model} 的可用档位是 {'、'.join(levels)}。"

        if model != current_model:
            error = await llm.check_openai_model_access(OPENAI_KEY, model)
            if error:
                return error
        try:
            if for_solve:
                env_config.save_solve_model(model, effort)
            else:
                env_config.save_chat_model(model, effort)
        except OSError as exc:
            logger.warning(f"chat_bot: 保存模型设置失败（{type(exc).__name__}）")
            return "模型设置保存失败，请稍后再试。"
        if for_solve:
            return f"已设置做题模型 {model}，思考档位 {effort}。新开的题目开始生效。"
        OPENAI_CHAT_MODEL, OPENAI_CHAT_EFFORT = model, effort
        return f"已设置主模型 {model}，思考档位 {effort}。下一轮回复开始生效。"


async def _handle_model_command(
    matcher, bot: Bot, event: MessageEvent, arg: Message, target: str, *,
    change_model: bool, for_solve: bool = False,
) -> None:
    if str(event.user_id) not in _admin_set():
        await matcher.finish()
        return
    if any(not segment.is_text() for segment in arg):
        await matcher.finish(_reply_to(event, "模型名和思考档位请用文字填写。"))
        return
    if target:
        _chat_model_menus.pop(_model_menu_key(event), None)
        reply = await _change_chat_model(target, change_model=change_model, for_solve=for_solve)
    else:
        reply = _open_model_menu(bot, event, change_model=change_model, for_solve=for_solve)
    await matcher.finish(_reply_to(event, reply))


@solve_model_cmd.handle()
async def handle_solve_model_cmd(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    await _handle_model_command(
        solve_model_cmd, bot, event, arg, arg.extract_plain_text().strip(), change_model=True, for_solve=True,
    )


@solve_effort_cmd.handle()
async def handle_solve_effort_cmd(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    await _handle_model_command(
        solve_effort_cmd, bot, event, arg, arg.extract_plain_text().strip(), change_model=False, for_solve=True,
    )


@status_cmd.handle()
async def handle_chat_cmd(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    parts = arg.extract_plain_text().strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    target = parts[1].strip() if len(parts) == 2 else ""

    if sub in ("模型", "model", "思考", "思考等级", "effort"):
        await _handle_model_command(status_cmd, bot, event, arg, target, change_model=sub in ("模型", "model"))
        return

    if sub in ("生图间隔", "图像间隔"):
        if str(event.user_id) not in _admin_set():
            await status_cmd.finish()
            return
        try:
            if any(not segment.is_text() for segment in arg) or (target and not (target.isascii() and target.isdecimal())):
                raise ValueError("Invalid cooldown argument")
            if target:
                seconds = int(target)
                await asyncio.to_thread(image_quota_store.set_cooldown_seconds, seconds)
            else:
                seconds = await asyncio.to_thread(image_quota_store.cooldown_seconds)
            label = "生图间隔已更新" if target else "当前生图间隔"
            reply = f"{label} · {seconds} 秒/次（每个 QQ 号）" if seconds else f"{label} · 关闭"
        except ValueError:
            reply = "用法「聊天 生图间隔 秒数」，0 为关闭。"
        except (OSError, sqlite3.Error) as exc:
            logger.warning(f"chat_bot: 生图间隔设置失败（{type(exc).__name__}）")
            reply = "暂时无法读取或保存生图间隔。"
        await status_cmd.finish(_reply_to(event, reply))
        return

    if sub == "意识层":
        if str(event.user_id) not in _admin_set():
            await status_cmd.finish()
            return
        if target in ("", "状态"):
            reply = f"群聊意识层当前{'开启' if awareness.is_enabled() else '关闭'}"
        elif target in ("开启", "开", "关闭", "关"):
            enabled = target in ("开启", "开")
            try:
                await awareness.set_enabled(enabled)
                reply = f"已{'开启' if enabled else '关闭'}群聊意识层"
            except OSError as exc:
                logger.warning(f"chat_bot: 群聊意识层开关保存失败（{type(exc).__name__}）")
                reply = "意识层开关保存失败"
        else:
            reply = "用法「聊天 意识层 开启」「聊天 意识层 关闭」"
        await status_cmd.finish(_reply_to(event, reply))
        return

    if not await _require_private_admin(status_cmd, event):
        return

    if not target and sub in ("reload", "重载", "重读"):
        persona.load()
        try:
            reload_runtime_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"chat_bot: 配置重算失败 {exc!r}")
            await status_cmd.finish("配置重读失败，请检查配置和运行日志。")
        if not _load_histories():
            await status_cmd.finish("对话上下文重读失败，当前会话仍能继续。")
        if persona.error:
            await status_cmd.finish("人设加载失败，请检查 persona.toml 和运行日志。")
        groups = _group_status()
        ctx_people = len(histories)
        ctx_msgs = sum(len(s.recent) for s in histories.values())
        ctx_arch = sum(1 for s in histories.values() if s.summary or s.compaction)
        intent_desc = (
            f"{INTENT_MODEL} (effort={INTENT_EFFORT})"
            if OPENAI_KEY
            else (DEEPSEEK_MODEL if DEEPSEEK_KEY else "本地规则")
        )
        main_model = f"{OPENAI_CHAT_MODEL}（{OPENAI_CHAT_EFFORT}）" if OPENAI_KEY else GEMINI_CHAT_MODEL or "待配置"
        await status_cmd.finish(
            f"已重读人设和配置 · {len(persona.tiers)} 档好感\n"
            f"主模型　{main_model}\n"
            f"前置判断　{intent_desc}\n"
            f"启用群　{groups}\n"
            f"上下文　{ctx_people} 个会话，{ctx_msgs} 条消息，{ctx_arch} 个含归档"
        )

    if sub in ("clear", "清空上下文", "忘记对话"):
        uid = str(event.user_id)
        gid = _group_number(target) if target else None
        if any(not segment.is_text() for segment in arg) or (target and not gid):
            await status_cmd.finish("清空指定群会话时，请发「聊天 清空上下文 群号」。")
            return
        key = f"group:{gid}" if gid else _session_key(event, uid)
        label = f"群 {gid} 的会话" if gid else "当前私聊"
        async with _history_scope(key):
            session_old = histories.pop(key, None)
            n = len(session_old.recent) if session_old else 0
            had_summary = bool(session_old and (session_old.archive_text() or session_old.compaction))
            if not _save_histories(key):
                if session_old is not None:
                    histories[key] = session_old
                await status_cmd.finish("上下文清空失败，请稍后重试。")
        await status_cmd.finish(
            f"已清空{label}，共 {n} 条消息{'和归档上下文' if had_summary else ''}。"
        )

    if not target and sub in ("", "status", "状态"):
        if OPENAI_KEY:
            main_model = f"{OPENAI_CHAT_MODEL}（{OPENAI_CHAT_EFFORT}）"
        elif VERTEX_AUTH and GEMINI_CHAT_MODEL:
            main_model = f"{GEMINI_CHAT_MODEL}（Vertex）"
        elif GEMINI_CHAT_KEY and GEMINI_CHAT_MODEL:
            main_model = f"{GEMINI_CHAT_MODEL}（中转）"
        else:
            main_model = "待配置"
        bcfg = _baidu_creds()
        lines = [
            f"运行状态 · {len(persona.tiers)} 档好感",
            f"主模型　{main_model}",
            f"工具选择　{'由主模型判断' if OPENAI_KEY else '当前通道只回复'}",
            f"百度识图　{vision_router.baidu_status_text(bcfg)}",
            f"以图搜源　{vision_router.vision_web_status_text(enabled=VISION_WEB_ENABLED, vertex_auth=VERTEX_AUTH, cap=VISION_WEB_CAP, reserve=VISION_WEB_RESERVE, free_tier=VISION_WEB_FREE_TIER)}",
            f"记忆　好感超过 {memory.MEMORY_THRESHOLD:.0f} 分开始记录，上限 {memory.MAX_MEMORIES} 条",
            f"群聊　{_group_status()}",
        ]
        if persona.error:
            lines.append("人设加载失败，请检查 persona.toml 和运行日志。")
        await status_cmd.finish("\n".join(lines))

    await status_cmd.finish(
        "「聊天 状态」查看模型和启用范围\n"
        "「聊天 模型」打开模型切换菜单\n"
        "「聊天 思考」打开思考档位菜单\n"
        "「聊天 重读」重新读取人设和配置\n"
        "「聊天 清空上下文」清空当前私聊\n"
        "「聊天 清空上下文 群号」清空指定群会话"
    )


def _affection_card(target_id: str, label: str, *, show_actions: bool = False) -> list[str]:
    score = store.peek(target_id)
    tier = aff.tier_of(score, persona.tiers)
    state = store.get(target_id)
    tier_lo = float(tier.get("min", 0))
    heading = "好感度" if label == "你" else f"{label} 的好感度"
    lines = [f"{heading} {score:.2f} 分 · {tier.get('name', '未知')}"]
    next_tier = next((t for t in persona.tiers if float(t.get("min", 0)) > tier_lo), None)
    if next_tier is not None:
        need = max(0.0, float(next_tier["min"]) - score)
        lines.append(f"距「{next_tier['name']}」还差 {need:.2f} 分")
    elif tier:
        lines.append("已是最高档")

    today = affection_daily.day(target_id)
    if today["turns"]:
        lines.append(
            f"今日 {today['total']:+.2f} 分 · 夸奖 {today['praise']} 次 · 冒犯 {today['offense']} 次"
        )
        lines.append(
            f"今日加分 {state.daily_gain:.2f}/{aff.DAILY_GAIN_CAP:.0f}，"
            f"扣分 {state.daily_loss:.2f}/{aff.DAILY_LOSS_CAP:.0f}"
        )
    else:
        lines.append("今天还没有好感变化记录。")

    days = affection_daily.recent(target_id, days=7)
    if days:
        recent = " · ".join(f"{d['date'][5:]} {d['total']:+.2f}" for d in days[:3])
        lines.append(f"近期　{recent}")

    if show_actions:
        reset_left = store.reset_ready_in(target_id)
        if reset_left > 0:
            reset_hint = f"还要等 {_reset_wait(reset_left)}"
        elif abs(score) < 1e-9:
            reset_hint = "当前 0 分"
        else:
            reset_hint = "7 天一次，需确认"
        lines.extend(
            [
                "",
                "「好感度 历史」在私聊接收自己的记录",
                f"「好感度 重置」{reset_hint}",
            ]
        )
    return lines


def _reset_wait(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    days, hours = divmod(minutes // 60, 24)
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes % 60} 分钟"
    return f"{minutes} 分钟"


@aff_cmd.handle()
async def handle_aff_cmd(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    if not _group_allowed(event) or str(event.user_id) not in _admin_set():
        await aff_cmd.finish()
        return
    target_id = str(event.user_id)
    target_label = "你"
    for seg in arg:
        if seg.type == "at":
            qq = str(seg.data.get("qq") or "").strip()
            if qq:
                target_id = qq
                target_label = str(seg.data.get("name") or qq)
            break
    lines = _affection_card(target_id, target_label, show_actions=(target_label == "你"))
    await aff_cmd.finish("\n".join(lines))


@memory_cmd.handle()
async def handle_memory_cmd(event: MessageEvent, arg: Message = CommandArg()) -> None:
    uid = str(event.user_id)
    items = memory_store.list(uid, scope=None if _is_private(event) else "group", group_id=_current_group(event))
    score = store.peek(uid)

    if not items:
        if score <= memory.MEMORY_THRESHOLD:
            await memory_cmd.finish(
                f"你现在有 {score:.1f} 分好感。超过 {memory.MEMORY_THRESHOLD:.0f} 分后，我就会开始记。"
            )
        await memory_cmd.finish("我在这里还没记下什么。")

    lines = [f"我记得这 {len(items)} 件事", ""]
    grouped: dict[str, list[str]] = {}
    for m in items:
        mark = "（私聊）" if m.scope == "private" else ""
        grouped.setdefault(m.key, []).append(m.value + mark)
    for key, values in grouped.items():
        lines.append(f"【{key}】")
        for v in values:
            lines.append(f"  · {v}")
    await memory_cmd.finish("\n".join(lines))


def _group_number(text: str) -> str | None:
    number = text.strip()
    return number if re.fullmatch(r"[1-9][0-9]*", number) else None


def _current_group(event: MessageEvent) -> str:
    gid = str(getattr(event, "group_id", "") or "")
    return gid if gid.isdigit() else ""


@add_group_cmd.handle()
async def handle_add_group(event: MessageEvent, arg: Message = CommandArg()) -> None:
    await _change_group(add_group_cmd, event, arg, enable=True)


@del_group_cmd.handle()
async def handle_del_group(event: MessageEvent, arg: Message = CommandArg()) -> None:
    await _change_group(del_group_cmd, event, arg, enable=False)


async def _change_group(matcher, event: MessageEvent, arg: Message, *, enable: bool) -> None:
    if str(event.user_id) not in _admin_set():
        await matcher.finish()
        return
    command = "加群" if enable else "退群"
    private = _is_private(event)
    text = arg.extract_plain_text().strip()
    gid = _group_number(text) if private or text else _current_group(event)
    if not gid or any(not segment.is_text() for segment in arg):
        await matcher.finish(f"请带上完整群号，例如「{command} 123456789」。" if private else "请填写有效群号。")
        return
    try:
        change = group_store.add if enable else group_store.remove
        changed = change(gid, _configured_groups())
    except (OSError, ValueError) as exc:
        logger.warning(f"chat_bot: 群名单更新失败 {type(exc).__name__}")
        await matcher.finish("群聊范围保存失败，请查看运行日志。")
        return
    state = "启用" if enable else "停用"
    if private:
        await matcher.finish(f"群 {gid} {'已' if changed else '当前已'}{state}。")
        return
    await matcher.finish(f"{command}成功。" if changed else f"当前已{state}。")


@list_group_cmd.handle()
async def handle_list_group(event: MessageEvent) -> None:
    if not await _require_private_admin(list_group_cmd, event):
        return
    env_groups = _configured_groups()
    try:
        settings = group_store.load()
    except (OSError, ValueError):
        await list_group_cmd.finish("群名单读取失败，请检查配置和运行日志。")
        return
    lines = [
        "群聊范围",
        f"当前生效　{settings.describe(env_groups)}",
        f"QQ 内添加　{'、'.join(sorted(settings.added)) or '无'}",
        f"QQ 内停用　{'、'.join(sorted(settings.disabled)) or '无'}",
        f"配置文件　{'、'.join(sorted(env_groups)) or '无'}",
    ]
    await list_group_cmd.finish("\n".join(lines))


# ====================================================================== 好感度快捷查询与重置
def _is_aff_query(event: MessageEvent) -> bool:
    if not _group_allowed(event):
        return False
    text = event.get_plaintext()
    head, sub = split_aff_query(text)
    return is_affection_history(text) or (head in AFF_QUERY_WORDS and (not sub or sub in AFF_RESET_SUBS))


aff_query = on_message(rule=Rule(_is_aff_query), priority=15, block=True)


@aff_query.handle()
async def handle_aff_query(bot: Bot, event: MessageEvent) -> None:
    uid = str(event.user_id)
    text = event.get_plaintext()
    _, sub = split_aff_query(text)

    if sub in AFF_RESET_SUBS:
        ready_in = store.reset_ready_in(uid)
        if ready_in > 0:
            await aff_query.finish(_reply_to(event, f"再等 {_reset_wait(ready_in)} 就能重置。每 7 天可重置一次。"))

        current = store.peek(uid)
        if abs(current) < 1e-9:
            await aff_query.finish(_reply_to(event, "你现在就是 0 分，不用重置。"))

        confirm_msg = (
            f"确认重置好感度（当前 {current:.2f} 分）？\n"
            "这也会清空你的私聊上下文、好感记录和长期记忆。\n"
            "60 秒内回复「是」确认，回复其他内容取消。"
        )
        await aff_query.send(_reply_to(event, confirm_msg))

        from nonebot_plugin_waiter import waiter

        @waiter(waits=["message"], keep_session=True)
        async def _confirm(e: MessageEvent):
            if str(e.user_id) != uid:
                return None
            if str(getattr(e, "group_id", "")) != str(getattr(event, "group_id", "")):
                return None
            return e.get_plaintext().strip()

        resp = await _confirm.wait(timeout=60)
        if resp is None:
            await aff_query.finish(_reply_to(event, "确认超时，重置已取消。"))
        if resp not in {"是", "确认", "确定", "对", "yes", "y", "YES", "Y"}:
            await aff_query.finish(_reply_to(event, "已取消重置。"))

        ok, before, _, note = store.reset(uid)
        if not ok:
            await aff_query.finish(_reply_to(event, f"重置失败（{note}）。"))

        private_key = "private:" + uid
        async with _history_scope(private_key):
            old_session = histories.pop(private_key, None)
            n_msgs = len(old_session.recent) if old_session else 0
            had_summary = bool(old_session and (old_session.archive_text() or old_session.compaction))
            if old_session and not _save_histories(private_key):
                histories[private_key] = old_session
                await aff_query.finish(_reply_to(event, "好感度已重置，对话上下文清空失败，请稍后重试。"))

        n_hist = affection_log.clear_affection_history(AFFECTION_HISTORY_FILE, uid)
        n_mem = memory_store.clear(uid)
        cleared = [
            f"· 好感度 {before:.2f} → 0.00",
            f"· 私聊上下文 {n_msgs} 条" + ("（含归档摘要）" if had_summary else ""),
            f"· 好感度历史 {n_hist} 条",
            f"· 长期记忆 {n_mem} 条",
        ]
        await aff_query.finish(
            _reply_to(event, "好感已重置\n" + "\n".join(cleared))
        )

    if is_affection_history(text):
        events = affection_log.read_affection_events(AFFECTION_HISTORY_FILE, uid, limit=AFFECTION_HISTORY_QUERY)
        blocks: list[list[str]] = []
        for e in events:
            ts = time.strftime("%m-%d %H:%M", time.localtime(e.get("ts", 0)))
            d = e.get("delta", 0)
            mark = "↑" if d > 0 else ("↓" if d < 0 else "·")
            block = [f"{ts} {mark}{d:+.2f} → {e.get('after', 0):.2f}"]
            mine = str(e.get("user") or "").replace("\n", " ").strip()
            hers = str(e.get("reply") or "").replace("\n", " ").strip()
            if mine:
                block.append(f"你：{mine[:36]}")
            if hers:
                block.append(f"我：{hers[:36]}")
            block.append("")
            blocks.append(block)

        chunks: list[list[str]] = []
        for i in range(0, len(blocks), 4):
            node_lines: list[str] = [f"最近 {len(events)} 条好感记录", ""] if i == 0 else []
            for b in blocks[i : i + 4]:
                node_lines.extend(b)
            chunks.append(node_lines)
        try:
            messages = ["\n".join(chunk) for chunk in chunks] or ["目前还没有好感变化记录。"]
            await bot.adapter.send_affection_history(bot, event, messages)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"chat_bot: 好感历史私聊发送失败 {type(exc).__name__}")
            if not _is_private(event):
                await aff_query.finish(_reply_to(event, "记录暂时发不过去。加我为好友，或打开临时会话后再发「好感度 历史」。"))
            await aff_query.finish()
        if not _is_private(event):
            await aff_query.finish(_reply_to(event, "已把你的好感记录发到私聊。"))
        await aff_query.finish()

    lines = _affection_card(uid, "你", show_actions=True)
    await aff_query.finish(_reply_to(event, "\n".join(lines)))


# ====================================================================== 做题与续答
def _problem_session_key(event: MessageEvent) -> str:
    return f"{event.self_id}:{event.get_session_id()}"


def _image_reply(event: MessageEvent, text: str, image: bytes) -> Message:
    return Message(_reply_to(event, text)) + MessageSegment.image(image)


async def _send_problem_delivery(bot: Bot, event: MessageEvent, delivery: problem_solver.Delivery) -> None:
    if delivery.exhausted_notice is not None:
        sticker_file = HERE.parent.parent / "assets" / "problem-exhausted-sticker.png"
        sticker = await asyncio.to_thread(sticker_file.read_bytes)
        outgoing = _image_reply(event, delivery.exhausted_notice, sticker)
        outgoing.append(MessageSegment.text(f"\n{delivery.text}"))
        await bot.send(event, outgoing)
        return
    if not delivery.answer:
        plain = _reply_to(event, delivery.text)
        outgoing = MessageSegment.text(plain) if isinstance(plain, str) else plain
        if delivery.check_in and not _is_private(event):
            outgoing = Message(outgoing)
            index = 1 if outgoing and outgoing[0].type == "reply" else 0
            outgoing[index:index] = MessageSegment.at(event.user_id) + MessageSegment.text(" ")
        await bot.send(event, outgoing)
        return
    answer = delivery.formatted_answer or delivery.answer
    if not delivery.pages:
        delivery.pages = await asyncio.to_thread(problem_math.render_answer, answer)
        if not delivery.pages:
            raise ValueError("解答图为空")

    total = len(delivery.pages)
    while delivery.sent_pages < total:
        index = delivery.sent_pages
        title = f"解答 {index + 1}/{total}" if total > 1 else "解答见图"
        outgoing = _image_reply(event, title, delivery.pages[index])
        if index + 1 == total:
            tail = delivery.text
            if delivery.question:
                tail += f"\n\n{problem_sources.OFFER}"
            outgoing.append(MessageSegment.text(f"\n{tail}"))
        try:
            await bot.send(event, outgoing)
        except ActionFailed as exc:
            logger.warning(
                f"chat_bot: 解答图发送失败 retcode={exc.info.get('retcode')} page={index + 1}/{total}"
            )
            raise
        delivery.sent_pages += 1
    if delivery.question and not delivery.source_saved:
        problem_sources.remember(_problem_session_key(event), delivery.question, answer, delivery.files)
        delivery.source_saved = True


async def _send_problem_source(bot: Bot, event: MessageEvent, source: problem_sources.SourceAnswer) -> str:
    if source.sending:
        return "这份源文件正在发送"
    source.sending = True
    try:
        name, data = problem_sources.attachment(source)
        with TemporaryDirectory(prefix="qqbot-source-") as directory:
            path = (Path(directory) / ("source" + Path(name).suffix)).resolve()
            path.write_bytes(data)
            if _is_private(event):
                await bot.call_api("upload_private_file", user_id=event.user_id, file=path.as_uri(), name=name)
            else:
                await bot.call_api("upload_group_file", group_id=event.group_id, file=path.as_uri(), name=name)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"chat_bot: 源文件上传失败（{type(exc).__name__}）")
        return "源文件暂时没发出去。保留期内可以再发「源文件」重试"
    finally:
        source.sending = False
    return ""


async def _save_problem_solution(event: MessageEvent, delivery: problem_solver.Delivery) -> None:
    context_key = _session_key(event)
    async with _scope_lock(HISTORY_FILE, context_key):
        if delivery.history_saved:
            return
        _refresh_history_scope(context_key)
        session = histories.setdefault(context_key, Session())
        session.recent.append({
            "role": "assistant",
            "content": f"【已完成的做题解答】\n题目：\n{delivery.question}\n\n解答：\n{delivery.answer}",
            "speaker_id": str(event.user_id),
            "speaker_name": sender_name(getattr(event, "sender", None)),
            "at": time.time(),
        })
        if not _save_histories(context_key):
            session.recent.pop()
            return
        delivery.history_saved = True
        if _history_cut(session):
            asyncio.create_task(_maybe_compress_history(context_key))


async def _deliver_problem(bot: Bot, event: MessageEvent, delivery: problem_solver.Delivery) -> None:
    try:
        await _send_problem_delivery(bot, event, delivery)
    finally:
        if delivery.answer and delivery.question and not delivery.history_saved:
            try:
                await _save_problem_solution(event, delivery)
            except (OSError, ValueError) as exc:
                logger.warning(f"chat_bot: 写入题目和解答失败（{type(exc).__name__}）")


def _problem_reply_text(event: MessageEvent) -> str:
    return event.get_plaintext().strip().rstrip("。！？.!?").rstrip()


def _is_problem_reply(event: MessageEvent) -> bool:
    text = _problem_reply_text(event)
    if text not in problem_solver.CONTINUE_REPLIES and text not in problem_solver.CANCEL_REPLIES:
        return False
    if not _group_allowed(event):
        return False
    if text in {"继续做题", "取消做题", "停止做题"}:
        return True
    if text in {"是", "否"} and not (_is_private(event) or message_io.addressed_to_bot(event)):
        return False
    key = _problem_session_key(event)
    return problem_solver.waiting_for_reply(key) or (
        text != "否" and text in problem_solver.CANCEL_REPLIES and problem_solver.is_solving(key)
    )


problem_reply = on_message(rule=Rule(_is_problem_reply), priority=4, block=True)


@problem_reply.handle()
async def handle_problem_reply(bot: Bot, event: MessageEvent) -> None:
    result = await problem_solver.handle_reply(
        _problem_session_key(event), _problem_reply_text(event), OPENAI_KEY,
        lambda delivery: _deliver_problem(bot, event, delivery),
    )
    if result:
        await problem_reply.finish(_reply_to(event, result))


def _is_problem_source_reply(event: MessageEvent) -> bool:
    if not _group_allowed(event):
        return False
    return problem_sources.recognizes_reply(
        _problem_session_key(event), event.get_plaintext().strip(),
        addressed=_is_private(event) or message_io.addressed_to_bot(event),
    )


problem_source_reply = on_message(rule=Rule(_is_problem_source_reply), priority=5, block=True)


@problem_source_reply.handle()
async def handle_problem_source_reply(bot: Bot, event: MessageEvent) -> None:
    selected = problem_sources.select(_problem_session_key(event), event.get_plaintext().strip())
    text = selected if isinstance(selected, str) else await _send_problem_source(bot, event, selected)
    if text:
        await problem_source_reply.finish(_reply_to(event, text))


# ====================================================================== 主对话入口与流程编排
def _can_observe_group(event: MessageEvent) -> bool:
    return bool(
        awareness.is_enabled() and OPENAI_KEY and not _is_private(event) and _current_group(event)
        and str(event.user_id) != str(event.self_id) and _group_allowed(event)
    )


group_observer = on_message(rule=Rule(_can_observe_group), priority=0, block=False)


@group_observer.handle()
async def observe_group_message(bot: Bot, event: MessageEvent, command: tuple[str, ...] | None = Command()) -> None:
    if not _can_observe_group(event):
        return
    direct = message_io.addressed_to_bot(event)
    eligible = not (direct or command or context_tools.mentioned_members(event))
    awareness.rooms.observe(bot, event, OPENAI_KEY, persona.name, eligible=eligible, direct=direct)


def _may_join_group(event: MessageEvent) -> bool:
    return _can_observe_group(event) and awareness.rooms.candidate(event)


group_join_matcher = on_message(rule=Rule(_may_join_group), priority=100, block=False)


@group_join_matcher.handle()
async def handle_group_join(bot: Bot, event: MessageEvent) -> None:
    async def respond(focus: str) -> None:
        user_id = str(event.user_id)
        if not _can_observe_group(event) or _in_cooldown(user_id):
            return
        if not persona.base or not persona.tiers:
            persona.load()
        if not persona.base or not persona.tiers:
            return
        text, images = _extract(event)
        async with _history_scope(_session_key(event, user_id)):
            quoted_text, quoted_images, quoted_author = await _fetch_quote(bot, event)
            await _run_turn(
                bot, event, user_id, text, images, quoted_text, quoted_images, quoted_author,
                received_at=float(event.time), proactive_focus=focus,
            )

    await awareness.rooms.join(event, respond)


chat_matcher = on_message(rule=Rule(_should_reply), priority=99, block=False)

_SIMPLE_CHAT_MESSAGES = frozenset({
    "你好", "您好", "嗨", "哈喽", "在吗", "在嘛", "在不在", "早安", "晚安",
    "早上好", "中午好", "下午好", "晚上好", "hi", "hello", "hey", "test", "测试", "ping",
})
IMAGE_PREFLIGHT_WAIT = 5.0


def _local_length_hint(text: str, games_enabled: bool) -> llm.LengthHint:
    local = llm.parse_local_game_intent(text if games_enabled else "")
    return llm.LengthHint(
        need_farm=bool(local["need_farm"]) if FARM_ENABLED else False,
        need_fishing=bool(local["need_fishing"]) if FISHING_ENABLED else False,
        direct_actions=local["direct_actions"],
        automation=local["automation"],
        order_op=local["order_op"],
        order_id=local["order_id"],
    )


@chat_matcher.handle()
async def handle_chat(bot: Bot, event: MessageEvent) -> None:
    received_at = time.time()
    if not persona.tiers or not persona.base:
        persona.load()
    if not persona.base or not persona.tiers:
        return
    if not (OPENAI_KEY or VERTEX_AUTH or GEMINI_CHAT_KEY):
        logger.warning("chat_bot: 未配置任何可用主对话通道，跳过")
        return

    user_id = str(event.user_id)

    if _in_cooldown(user_id):
        notice = _cooldown_store.take_notice(user_id)
        if notice:
            await chat_matcher.send(_reply_to(event, notice), event=event)
        return

    text, image_segs = _extract(event)
    text = _clean_text(text)

    if image_segs and not text.strip() and _is_private(event):
        saved: list[bytes] = []
        for seg in image_segs[:3]:
            data = await _fetch_image(seg, bot)
            if data:
                saved.append(data)
        if saved:
            bucket = _pending_images.setdefault(_session_key(event, user_id), [])
            now_ts = time.time()
            bucket.extend((now_ts, d) for d in saved)
            del bucket[:-llm.CHAT_IMAGE_LIMIT]
            logger.info(f"chat_bot: user={user_id} 暂存 {len(saved)} 张图，等待下一句合并")
            return

    if not (RATE_ADMIN_EXEMPT and _is_admin(event.user_id)):
        if not _rate_limiter.take(user_id, max_hits=RATE_MAX, window_sec=RATE_WINDOW):
            if _rate_limiter.should_notify(user_id, cooldown=30.0):
                mins = max(1, int(RATE_WINDOW // 60))
                await chat_matcher.finish(
                    _reply_to(event, f"……够了，先歇会儿。（{mins} 分钟最多 {RATE_MAX} 次）")
                )
            return

    async with _history_scope(_session_key(event, user_id)):
        quote_text, quote_images, quote_author = await _fetch_quote(bot, event)
        await _run_turn(
            bot, event, user_id, text, image_segs, quote_text, quote_images, quote_author,
            received_at=received_at,
        )


async def _run_turn(
    bot: Bot,
    event: MessageEvent,
    user_id: str,
    text: str,
    image_segs: list[dict],
    quote_text: str = "",
    quote_images: list[dict] | None = None,
    quote_author: str = "",
    *,
    received_at: float | None = None,
    proactive_focus: str = "",
) -> None:
    score = store.peek(user_id)
    tier = aff.tier_of(score, persona.tiers)
    mem_scope = "private" if _is_private(event) else "group"
    group_id = _current_group(event)
    display_name = sender_name(getattr(event, "sender", None))
    speaker = speaker_label(user_id, display_name)
    author_fields = {"speaker_id": user_id, "speaker_name": display_name, "at": received_at or time.time()}

    context_key = _session_key(event, user_id)
    _sess = histories.get(context_key)
    context_messages = [attributed_message(m) for m in _history_messages(_sess)] if _sess else []

    suspicious = guard.scan(text)
    if suspicious:
        logger.warning(f"chat_bot: 可疑输入 user={user_id} 类型={suspicious} 原文={text[:120]!r}")

    # ---- 1. 下载当前消息有权访问的图片，交给主模型判断如何使用 ----
    parts = [text] if text else []
    if mem_scope == "group":
        mentioned = context_tools.mentioned_members(event)
        if mentioned:
            parts.append("［本条正文提及的群成员 QQ " + "、".join(mentioned) + "］")
    pending = _take_pending_images(context_key)
    quoted_segs = (quote_images or [])[:3]
    current_segs = image_segs[:3]
    downloaded = await asyncio.gather(*(_fetch_image(seg, bot) for seg in [*quoted_segs, *current_segs]))
    quoted = [
        ("引用消息里的表情包" if seg.get("_is_sticker") else "引用消息里的配图", data)
        for seg, data in zip(quoted_segs, downloaded[:len(quoted_segs)]) if data is not None
    ]
    current = [
        ("当前消息附带的表情包" if seg.get("_is_sticker") else "当前消息附图", data)
        for seg, data in zip(current_segs, downloaded[len(quoted_segs):]) if data is not None
    ]
    dropped = max(0, len(pending) + len(quoted) + len(current) - llm.CHAT_IMAGE_LIMIT)
    pending = pending[dropped:]
    attachments = [("先前单独发来的图", data) for data in pending] + quoted + current
    raw_images = [data for _, data in attachments]
    quote_image_count = len(quoted)

    if quote_text or quote_images:
        q_parts: list[str] = []
        if quote_text:
            q_parts.append(f"> {quote_text}")
        if quote_image_count:
            q_parts.append("[引用消息附有图片]")
        elif quote_images:
            q_parts.append("[引用的图片暂未取到]")
        if q_parts:
            parts.append(f"【引用原作者 {quote_author or '未知'}】\n" + "\n".join(q_parts))

    if image_segs and not current:
        parts.append("[这条消息附带的图片暂未取到]")

    notice_sent = False

    async def announce_tool(message: str) -> None:
        nonlocal notice_sent
        if notice_sent:
            return
        notice_sent = True
        try:
            await chat_matcher.send(_reply_to(event, message))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"chat_bot: 工具进度发送失败 {type(exc).__name__}")

    image_context = ""
    if attachments:
        image_context = "本轮附件编号：" + "，".join(
            f"{index}＝{origin}" for index, (origin, _) in enumerate(attachments, 1)
        ) + "。\n"
        if not text.strip() and not quote_text.strip():
            image_context += "当前发言人没有另写文字。上文有明确问题时把相关图当补充，否则按分享接话。\n"

    user_content = "\n\n".join(parts).strip()
    if not user_content:
        user_content = "（对方发了图片附件）" if raw_images else "（对方只是@了你一下，什么也没说）"

    # ---- 2. 兼容可选的旧农场功能。聊天、联网和识图交给主模型统一处理。----
    aux_key, aux_model, aux_kw = _aux_llm_params()
    games_enabled = not proactive_focus and (FARM_ENABLED or FISHING_ENABLED)
    simple_chat = (
        not (games_enabled or quote_text or quote_images or image_segs or raw_images)
        and text.strip().rstrip("!?！？。.~～ ").casefold() in _SIMPLE_CHAT_MESSAGES
    )

    intent = _local_length_hint(text, games_enabled) if games_enabled else llm.LengthHint()
    if LENGTH_ENABLED and games_enabled and (aux_key or aux_kw.get("fallback_key")):
        decision = llm.decide_length(
            text,
            aux_key,
            aux_model,
            farm_enabled=FARM_ENABLED,
            web_enabled=False,
            games_enabled=True,
            context=f"引用原作者 {quote_author or '未知'}：{quote_text}" if quote_text else "",
            history=context_messages[-8:],
            **aux_kw,
        )
        try:
            model_intent = await asyncio.wait_for(decision, timeout=IMAGE_PREFLIGHT_WAIT if raw_images else None)
        except asyncio.TimeoutError:
            model_intent = llm.LengthHint(elapsed=IMAGE_PREFLIGHT_WAIT, error="带图意图判断超时")
        if model_intent.ok:
            intent = model_intent
        u = model_intent.usage or {}
        in_tok = int(u.get("_prompt_tokens", 0) or 0)
        ca_tok = int(u.get("_cached_tokens", 0) or 0)
        rs_tok = int(u.get("_reasoning_tokens", 0) or 0)
        m_used = str(u.get("_model_used") or aux_model)
        cache_tag = f" cache={ca_tok}/{in_tok}" if in_tok else ""
        think_tag = f" think={rs_tok}" if rs_tok else ""
        logger.info(
            f"chat_bot: 按需意图判断[{m_used}] farm={intent.need_farm} fish={intent.need_fishing} "
            f"actions={len(intent.direct_actions)} auto={bool(intent.automation.get('is_automation'))} "
            f"{cache_tag}{think_tag} [{model_intent.elapsed:.2f}s]"
            + (f" ⚠️{model_intent.error[:100]}" if not model_intent.ok and model_intent.error else "")
        )

    # ---- 2.2 游戏操控 / 自动化派单精算 / 派单确认执行 ----
    action_block = ""
    order_block = ""
    append_card = ""

    if text and games_enabled:
        # 情况 A：对话中带唯一标识符确认或取消派单（如「没问题，确认执行 ORD-7K9M2X」）
        if intent.order_op in ("confirm", "cancel") and intent.order_id:
            if intent.order_op == "confirm":
                ok_op, op_msg = await automation_planner.runner.confirm_and_start(
                    user_id, intent.order_id, is_admin=_is_admin(user_id)
                )
            else:
                ok_op, op_msg = automation_planner.runner.cancel_order(
                    user_id, intent.order_id, is_admin=_is_admin(user_id)
                )
            action_block = (
                f"【派单指令已处理（{'成功' if ok_op else '未生效'}）】\n{op_msg}\n"
                "（请以你的口吻简短回应；下方系统会自动附上完整回执，你不必逐字复述步骤。）"
            )
            append_card = op_msg

        # 情况 B：玩家需要自动化托管管理 → 自主精算最优流程并生成带唯一标识符的待确认派单
        elif intent.automation.get("is_automation"):
            local_spec = automation_planner.parse_automation_from_text(text)
            merged_spec = automation_planner.merge_automation_specs(local_spec, intent.automation)
            if not FARM_ENABLED and merged_spec.domain == "both":
                merged_spec.domain = "fishing"
            if not FISHING_ENABLED and merged_spec.domain == "both":
                merged_spec.domain = "farm"
            created_order = await automation_planner.compute_and_create_order(
                user_id, group_id, merged_spec, raw_request=text
            )
            order_block = automation_planner.format_order_for_prompt(created_order)
            append_card = automation_planner.format_order_card(created_order)
            logger.success(
                f"chat_bot: 已为 user={user_id} 生成自动化派单 {created_order.order_id} "
                f"（{len(created_order.steps)} 步，domain={created_order.domain}）"
            )

        # 情况 C：玩家要求单次即时直接操控农场/钓鱼 → 先真实执行落盘，再读最新状态给主模型
        elif intent.direct_actions:
            filtered_actions = [
                act
                for act in intent.direct_actions
                if (act.get("domain") == "farm" and FARM_ENABLED)
                or (act.get("domain") == "fishing" and FISHING_ENABLED)
            ]
            if filtered_actions:
                action_block, receipt = await game_controller.execute_direct_actions(
                    user_id, filtered_actions
                )
                if receipt:
                    append_card = f"📋 操作回执\n{receipt}"
                for act in filtered_actions:
                    if act.get("domain") == "farm":
                        intent.need_farm = True
                    elif act.get("domain") == "fishing":
                        intent.need_fishing = True

    # ---- 2.5 并行拉取启用的游戏状态 ----
    farm_block = ""
    fishing_block = ""

    jobs: list[tuple[str, object]] = []
    if games_enabled and FARM_ENABLED and intent.need_farm:
        jobs.append(("farm", farm_bridge.build_farm_block(user_id)))
    if games_enabled and FISHING_ENABLED and intent.need_fishing:
        jobs.append(("fishing", game_controller.build_fishing_block(user_id)))

    if jobs:
        outcomes = await asyncio.gather(*(coro for _, coro in jobs), return_exceptions=True)
        for (kind, _), out in zip(jobs, outcomes):
            if isinstance(out, Exception):
                logger.warning(f"chat_bot: {kind} 子任务异常 {out!r}")
                continue
            if kind == "farm":
                farm_block = str(out)
            elif kind == "fishing":
                fishing_block = str(out)

    tools: dict[str, llm.ChatTool] = {}
    task_notice = ""
    turn_lookup_notes: list[str] = []
    if OPENAI_KEY and not append_card and not proactive_focus:
        tools = vision_router.image_tools(
            raw_images, baidu_cfg=_baidu_creds(),
            vision_web_enabled=VISION_WEB_ENABLED, vertex_auth=VERTEX_AUTH, vertex_proxy=VERTEX_PROXY,
            vision_web_max=VISION_WEB_MAX, vision_web_cap=VISION_WEB_CAP,
        )
        if "trace_image" in tools:
            _orig_trace = tools["trace_image"]
            async def _wrapped_trace(args: dict, _run=_orig_trace.run) -> str | list[dict]:
                out = await _run(args)
                if isinstance(out, str) and "Google Vision 图像匹配线索" in out:
                    compact = " ".join(out.splitlines()[1:]).strip()
                    if compact:
                        turn_lookup_notes.append(f"以图搜源 → {compact[:300]}")
                return out
            tools["trace_image"] = llm.ChatTool(
                _orig_trace.description, _orig_trace.parameters, _wrapped_trace,
                defer_notice=_orig_trace.defer_notice, silent=_orig_trace.silent,
            )

        async def send_image_delivery(delivery: image_generation.Delivery) -> None:
            nonlocal task_notice
            outgoing = (
                _image_reply(event, delivery.text, delivery.image)
                if delivery.image else _reply_to(event, delivery.text)
            )
            await bot.send(event, outgoing)
            if delivery.started:
                task_notice = delivery.text

        async def send_solver_delivery(delivery: problem_solver.Delivery) -> None:
            nonlocal task_notice
            if delivery.started:
                sticker_name = "problem-follow-up-sticker.jpg" if delivery.follow_up else "problem-start-sticker.jpg"
                sticker_file = HERE.parent.parent / "assets" / sticker_name
                sticker = await asyncio.to_thread(sticker_file.read_bytes)
                await bot.send(event, _image_reply(event, delivery.text, sticker))
                task_notice = delivery.text
            else:
                await _deliver_problem(bot, event, delivery)

        tools["solve_problem"] = problem_solver.problem_tool(
            key=_problem_session_key(event), api_key=OPENAI_KEY, images=tuple(raw_images),
            notify=send_solver_delivery, request_text=text or quote_text or "",
        )

        tools["generate_image"] = image_generation.image_tool(
            ledger=image_quota_store,
            message_key=f"{event.self_id}:{event.get_session_id()}:{event.message_id}:{event.time}",
            user_id=user_id,
            api_key=OPENAI_KEY,
            get_limit=lambda: _image_quota_tier(user_id)[0],
            notify=send_image_delivery,
            images=tuple(raw_images),
        )
        if WEB_ENABLED:
            async def search_tool(args: dict) -> str:
                query = args["query"].strip()[:128]
                result = await web_search.search(
                    query, DEEPSEEK_KEY, count=WEB_MAX_HITS, ori_question=query,
                    recency=args["recent"], foreign=args["foreign"],
                    baidu_key=BAIDU_SEARCH_KEY, tavily_key=TAVILY_KEY,
                    proxy=SEARCH_PROXY or VERTEX_PROXY.split(",")[0].strip(),
                )
                if not result.ok or result.empty:
                    return "这次联网查询没找到可用资料。"
                formatted = web_search.format_for_prompt(result, max_hits=WEB_MAX_HITS)
                compact = " ".join(formatted.split())
                if compact:
                    turn_lookup_notes.append(f"搜「{query}」→ {compact[:360]}")
                return formatted

            tools["search_web"] = llm.ChatTool(
                "根据文字线索查找公开网页。用于核实公开事实、时效性信息、截图中的说法或文字原帖。"
                "评价一张包含事实主张的截图时，可以用图里的关键说法寻找可靠报道。",
                {
                    "query": {"type": "string", "description": "写要核对的具体说法，使用当前消息或截图中的公开人名、事件、标题、日期或原文关键词。"},
                    "recent": {"type": "boolean", "description": "是否需要最新资料。"},
                    "foreign": {"type": "boolean", "description": "是否优先找外语来源。"},
                },
                search_tool,
            )

    lookup = None
    if OPENAI_KEY and not append_card:
        saved_history = [
            {**item, "speaker_id": str(event.self_id), "speaker_name": persona.name}
            if item.get("role") == "assistant" else item
            for item in (_sess.recent if _sess else [])
        ]
        if mem_scope == "group":
            saved_history.extend(
                item for item in awareness.rooms.history(event) if item["message_id"] != str(event.message_id)
            )
        lookup = context_tools.ConversationLookup(bot, event, saved_history)
        tools.update(lookup.tools())
        tools.update(elevator_knowledge.elevator_tools(
            user_id, turn_lookup_notes,
            group_id=str(group_id or ""),
            nickname=(getattr(event.sender, "card", None) or getattr(event.sender, "nickname", None) or "群友"),
        ))

    # ---- 3. 组装恒定静态前缀 + 尾部动态运行时状态 ----
    memory_block = memory_store.render(
        user_id, score, scope="group" if mem_scope == "group" else None, group_id=group_id,
    )
    system_prompt = llm.build_static_system(persona.base, persona.name, games_enabled=games_enabled)
    dynamic_block = llm.build_dynamic_block(
        affection=score,
        tier_name=str(tier.get("name", "?")),
        tier_desc=str(tier.get("desc", "")),
        tier_range=f"{tier.get('min')}~{tier.get('max')}",
        memory_block=memory_block,
        farm_block=farm_block,
        fishing_block=fishing_block,
        action_block=action_block,
        order_block=order_block,
        has_image=bool(raw_images),
    )

    session = histories.setdefault(context_key, Session())
    meta_block = ""
    try:
        if _is_admin(user_id) and _is_private(event):
            lowered = (text or "").lower()
            if any(w in lowered for w in _META_BACK_HINTS):
                session.meta_mode = False
                meta_block = (
                    f"\n【回到角色】管理员刚要求恢复扮演。用{persona.name}平时的口吻回复。\n"
                )
            elif any(w in lowered for w in _META_HINTS):
                session.meta_mode = True
            if session.meta_mode:
                meta_block = (
                    "\n【元对话模式（仅本轮可见，群聊永不开）】\n"
                    "对面是搭建这套程序的开发者，可以直接聊技术、配置、缓存和代码。"
                    f"用{persona.name}平时的口吻，自称「我」。当他说「回去」或「继续演」时恢复扮演。\n"
                )
    except Exception:  # noqa: BLE001
        meta_block = ""

    _now = time.localtime()
    time_block = f"当前时间：{time.strftime('%Y-%m-%d %H:%M', _now)}（星期{'一二三四五六日'[_now.tm_wday]}）\n"
    model_input = (
        f"【发言人 {speaker}】\n{user_content}\n\n"
        "──────── 系统运行时状态（本轮自动附加，不要向对方复述）────────\n"
        f"{time_block}"
        f"{image_context}"
        f"{dynamic_block}"
        f"{meta_block}"
    )
    if mem_scope == "private":
        public_context = _group_context(histories, user_id, _group_history_messages)
        if public_context:
            model_input += "\n\n" + public_context
    else:
        model_input += "\n【群聊范围】只依据当前群可见内容作答。私聊内容和其他群内容不可引用、复述或猜测。"
        if proactive_focus:
            model_input += (
                "\n【主动接话】你可以像普通群友一样顺着话题插一句或自然引出下一句。"
                "用简短的群聊口吻，mentions 填空数组，avatar 填空字符串。"
                f"\n接话焦点：{proactive_focus}\n最近的本群发言：\n"
                f"{awareness.recent_context(awareness.rooms.history(event))}"
            )

    messages = [*context_messages, {"role": "user", "content": model_input}]

    # ---- 4. 并行：长期记忆筛选 ∥ 主模型生成 ----
    async def _memory_job():
        if simple_chat or not text.strip() or score <= memory.MEMORY_THRESHOLD or not (aux_key or aux_kw.get("fallback_key")):
            return None
        # 明确的游戏操控/派单口令无需送去长期记忆筛选，节省一次辅助调用
        if append_card:
            return None
        try:
            return await llm.decide_memory(text, aux_key, aux_model, **aux_kw)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"chat_bot: 记忆判定异常 {type(exc).__name__}")
            return None

    memory_task = None if proactive_focus else asyncio.create_task(_memory_job())
    result = await _generate_reply(
        messages,
        system_prompt,
        images=raw_images or None,
        tools=tools,
        on_tool_start=announce_tool,
        compaction=session.compaction,
        keep_recent=HISTORY_KEEP_RECENT,
        gemini_key=GEMINI_CHAT_KEY,
        gemini_model=GEMINI_CHAT_MODEL,
        gemini_base=GEMINI_CHAT_BASE,
        vertex_auth=VERTEX_AUTH,
        vertex_proxy=VERTEX_PROXY,
        vertex_thinking=VERTEX_THINKING,
        vertex_cache_scope=context_key if VERTEX_SESSION_CACHE else "",
        **_openai_chat_params(),
    )
    _pt = int(result.usage.get("_prompt_tokens", 0) or 0)
    _ct = int(result.usage.get("_cached_tokens", 0) or 0)
    _ratio = (_ct / _pt * 100) if _pt else 0.0
    logger.info(
        f"chat_bot: 主模型 via={result.via} model={result.model_used} {result.elapsed:.1f}s"
        + (f" | 工具 {result.tool_calls} 次" if result.tool_calls else "")
        + (f" | 输入 {_pt} 命中缓存 {_ct}（{_ratio:.1f}%）" if _pt else "")
        + (f" ⚠️{result.error[:100]}" if result.error else "")
    )

    if proactive_focus:
        reply = result.reply.strip() if result.ok else ""
        if reply and _can_observe_group(event):
            sent = await bot.send(event, MessageSegment.text(reply), at_sender=False, reply_message=False)
            awareness.rooms.record_reply(event, persona.name, reply, sent)
            session.recent.append({"role": "user", "content": user_content, **author_fields})
            session.recent.append({"role": "assistant", "content": reply, **author_fields})
            _save_histories(context_key)
            if _history_cut(session):
                asyncio.create_task(_maybe_compress_history(context_key))
        return

    if not result.ok and not task_notice:
        memory_task.cancel()
        if append_card:
            # 即便主聊天模型偶发抖动，已完成的游戏动作回执或生成的唯一派单绝不丢失！
            await chat_matcher.finish(_reply_to(event, append_card))
            return
        logger.error(f"chat_bot: 主模型调用失败 {result.error}")
        if result.tool_calls:
            failure_text = "这次查询没能生成完整的回复。"
        elif "超时" in result.error or "Timeout" in result.error:
            failure_text = "这次模型回复超时了。"
        elif "HTTP 429" in result.error:
            failure_text = "模型接口这会儿限流了。"
        elif "RemoteProtocolError" in result.error:
            failure_text = "连接断了，这次没收到模型回复。"
        else:
            failure_text = "这次没拿到模型回复。"
        await chat_matcher.finish(_reply_to(event, failure_text))
        return

    # ---- 5. 结算好感度与冷却 ----
    meta_mode = bool(session.meta_mode)
    apply_result = _apply_shared_affection(
        store,
        user_id,
        "none" if meta_mode else result.event,
        rng=random,
        daily_hint=0.0 if meta_mode else None,
    )
    logger.info(
        f"chat_bot: user={user_id} event={result.event} "
        f"aff {apply_result.before:.2f}→{apply_result.after:.2f} ({apply_result.delta:+.2f})"
    )

    if result.event == "offense":
        _cooldown_store.bump_offense(str(user_id), limit=OFFENSE_LIMIT, cooldown_seconds=COOLDOWN_SECONDS)
    else:
        _cooldown_store.reset_offense(str(user_id))

    try:
        affection_daily.record(
            user_id,
            daily_delta=apply_result.daily_delta,
            praise=1 if (apply_result.praise_hit and apply_result.event_gain > 0) else 0,
            offense=1 if (apply_result.offense_hit and apply_result.event_loss > 0) else 0,
            total_delta=apply_result.delta,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"chat_bot: 写每日好感度汇总失败 {exc!r}")

    if apply_result.event in ("praise", "offense"):
        try:
            affection_log.record_affection_event(
                AFFECTION_HISTORY_FILE,
                user_id=user_id,
                before=apply_result.before,
                after=apply_result.after,
                delta=apply_result.delta,
                event=apply_result.event,
                reason=result.reason,
                user_text=user_content,
                reply=result.reply,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"chat_bot: 写好感度历史失败 {exc!r}")

    # ---- 6. 写入纯对话历史（不含尾部动态块，使后续轮次前缀 100% 连续对齐）----
    if result.image_desc:
        user_content = f"{user_content}\n\n（图上内容：{result.image_desc}）"

    stored_assistant = task_notice or result.reply.strip() or "嗯……"
    stored_assistant_history = stored_assistant
    if turn_lookup_notes and not task_notice:
        digest = "；".join(turn_lookup_notes)[:720]
        stored_assistant_history = f"{stored_assistant}\n\n（本轮已检索核实：{digest}）"
    session.recent.append({"role": "user", "content": user_content, **author_fields})
    session.recent.append({"role": "assistant", "content": stored_assistant_history, **author_fields})
    _save_histories(context_key)

    if _history_cut(session):
        asyncio.create_task(_maybe_compress_history(context_key))

    # ---- 7. 发送最终消息（若有派单卡片或代操作回执则清晰附于台词下方）----
    final_reply = stored_assistant
    if append_card:
        final_reply = f"{stored_assistant}\n\n{append_card}"

    try:
        if not task_notice:
            await asyncio.sleep(random.uniform(0.0, 2.0))
            outgoing = _reply_to(event, final_reply)
            if mem_scope == "group" and lookup is not None and (result.mentions or result.avatar):
                outgoing = await lookup.reply(final_reply, mentions=result.mentions, avatar=result.avatar)
            sent = await chat_matcher.send(outgoing)
            if mem_scope == "group":
                awareness.rooms.record_reply(event, persona.name, final_reply, sent)
        mem_hint = await memory_task
        if mem_hint is not None and mem_hint.should_save:
            remember = memory_store.set if mem_hint.action == "update" else memory_store.add
            remember(
                user_id, mem_hint.key, mem_hint.value, apply_result.after,
                now=author_fields["at"], scope=mem_scope, group_id=group_id,
            )
    finally:
        memory_task.cancel()
    await chat_matcher.finish()


# ====================================================================== 生命周期
@get_driver().on_startup
async def _chat_startup() -> None:
    persona.load()
    logger.info(
        f"chat_bot: 启动完成 | 前置意图模型={'OpenAI ' + INTENT_MODEL if OPENAI_KEY else DEEPSEEK_MODEL} | "
        f"好感记录={len(store._users)}"
    )


# 注册 ERP 私密通道 matcher
from . import erp  # noqa: E402,F401
