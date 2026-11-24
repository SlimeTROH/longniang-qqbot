"""星澜 NovaAI · 若娜瓦 Web 版后端（复用 chat_bot 的 ERP 逻辑）。

启动后手机浏览器访问 http://<电脑局域网IP>:8090 即可。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from network_config import configure_process_proxy
from shared_history import atomic_json, read_session
from web_conversations import Conversations, InvalidConversationSetting
configure_process_proxy()

import nonebot  # noqa: E402

nonebot.init()
import plugins.chat_bot as cb  # noqa: E402

# 同时把日志写一份到文件，方便事后查生成耗时/重试原因
try:
    from nonebot.log import logger as _nb_logger

    (HERE / "logs").mkdir(exist_ok=True)
    _nb_logger.add(
        HERE / "logs" / "web.log",
        rotation="5 MB",
        retention="3 days",
        encoding="utf-8",
    )
except Exception:  # noqa: BLE001
    pass

erp = cb.erp

WEB_DIR = HERE / "web"
# 测试实例可以用 CHAT_WEB_UID / CHAT_WEB_SESSION 指到独立会话，绝不碰正式存档
UID = os.environ.get("CHAT_WEB_UID", "web")
SESS_FILE = cb.STATE_FILE.parent / os.environ.get(
    "CHAT_WEB_SESSION", "erp_web_session.json"
)
# 正常模式（与 QQ 共用记忆）只给管理员用：设了口令就必须带对
WEB_TOKEN = str(cb._env("CHAT_WEB_TOKEN", "") or "")

# 后台事件循环：所有生成都丢到同一个 loop 里跑，
# 避免每次请求新建循环导致 vertex 的 httpx 连接池绑在已关闭的循环上。
_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, daemon=True).start()


def _run(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _LOOP)
    try:
        return future.result(timeout=180)
    except TimeoutError:
        future.cancel()
        raise


_REQUEST_LOCK = threading.Lock()
_ERP_COMPRESSION = None
_TASKS = set()


def _background(coro):
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    def done(completed):
        _TASKS.discard(completed)
        if not completed.cancelled() and completed.exception():
            nonebot.logger.warning("[web] 后台记忆整理未完成，原文保留")
    task.add_done_callback(done)
    return task


async def _compress_erp_later(uid, session):
    await erp._compress_if_needed(uid, session)
    _save_sess()


async def _wait_erp_compression():
    if _ERP_COMPRESSION is not None:
        try:
            await asyncio.shield(_ERP_COMPRESSION)
        except Exception:
            nonebot.logger.warning("[web] 上次整理未成功，继续保留原文对话")


async def _schedule_erp_compression(uid=None, session=None):
    global _ERP_COMPRESSION
    session = session if session is not None else _sess
    if _ERP_COMPRESSION is None or _ERP_COMPRESSION.done():
        if len(session.get("history") or []) >= erp._erp_compress_trigger():
            _ERP_COMPRESSION = _background(_compress_erp_later(uid or UID, session))


def _load_sess() -> dict:
    try:
        data = json.loads(SESS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("history"), list):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"history": [], "n": 0, "temp": 0.9, "normal": [], "normal_n": 0}


_sess = _load_sess()
_conversations = Conversations(SESS_FILE.with_name("erp_web_conversations.json"), _sess, UID, erp._get_aff(UID))
_sess = _conversations.get("legacy")


def _chat_aff() -> float:
    """主对话的独立好感（只读，供正常模式参考）。"""
    try:
        data = json.loads(
            (cb.STATE_FILE.parent / "chat_affection.json").read_text(encoding="utf-8")
        )
        return float((data.get(ADMIN_UID) or {}).get("affection") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


_FACT_HINTS = (
    "是谁", "什么人", "什么时候", "哪年", "多少钱", "价格", "最新", "新闻",
    "推荐", "在哪", "怎么买", "官网", "版本", "上市", "发布会", "评测",
)


async def _maybe_search(text: str) -> str:
    """正常模式的联网：只在这句话确实像在问事实/时效类问题时搜。"""
    if not cb.WEB_ENABLED:
        return ""
    try:
        q = cb.web_search.clean_query(text)
        need = cb.web_search.detect_force_search(text) or cb.web_search.wants_recency(text)
        if not need:
            need = any(h in text for h in _FACT_HINTS)
        if not need or not q:
            return ""
        res = await cb.web_search.search(
            q,
            cb.DEEPSEEK_KEY,
            count=cb.WEB_MAX_HITS,
            ori_question=text,
            recency=cb.web_search.wants_recency(text),
            baidu_key=cb.BAIDU_SEARCH_KEY,
            tavily_key=cb.TAVILY_KEY,
            proxy=cb.SEARCH_PROXY,
        )
        block = cb.web_search.format_for_prompt(res, cb.WEB_MAX_HITS)
        if block:
            nonebot.logger.info(f"[web] 已联网：{q} → {len(block)} 字")
        return block or ""
    except Exception as exc:  # noqa: BLE001
        nonebot.logger.warning(f"[web] 联网失败：{exc!r}")
        return ""


ADMIN_UID = str(cb._env("CHAT_WEB_ADMIN", ""))


_SCREEN_CACHE = {}
_SCREEN_POSITIVE = ("屏幕", "截图", "截屏", "这个报错", "这个界面", "这个窗口", "我画的", "看看这个", "这个代码")


async def _judge_need_screen(text: str) -> bool:
    """判断这句话需不需要看屏幕（只给桌面端用）。"""
    t = (text or "").strip()
    if not t:
        return False

    if any(word in t for word in _SCREEN_POSITIVE):
        return True
    if not any(word in t for word in ("这", "那", "它", "看", "画", "作品", "当前", "正在")):
        return False
    cached = _SCREEN_CACHE.get(t)
    if cached and cached[1] > time.monotonic():
        return cached[0]

    sys_p = (
        "判断回答这句话是否需要查看用户的屏幕。只回答一个词，yes 或 no。\n"
        "yes 的情况：提到屏幕上的东西、报错、界面、窗口、正在看的画面；"
        "要你评价他正在做的东西（画、代码、作品、照片、游戏画面）；"
        "让你「看看」「看一下」什么。\n"
        "no 的情况：普通聊天、问知识、问身份、让说点什么、告别、打招呼。\n"
        "例子：\n"
        "帮我看看这个报错 → yes\n"
        "你看看我画的怎么样 → yes\n"
        "这个界面怎么点 → yes\n"
        "今天有点累 → no\n"
        "你是谁 → no"
    )
    try:
        r = await cb.vertex.vertex_chat(
            [{"role": "user", "content": t}],
            sys_p,
            auth_file=cb.VERTEX_AUTH,
            model=cb.GEMINI_CHAT_MODEL or "gemini-4-hydrogen",
            proxy=cb.VERTEX_PROXY,
            timeout=4.0,
            thinking_level="low",
            cache_scope="",
            plain_text=True,
            temperature=0.0,
            total_budget=4.0,
            per_try=4.0,
            max_output=32,
            include_refs=False,
        )
        out = str(r.get("text") or "").strip().lower() if r.get("ok") else ""
        nonebot.logger.info(f"[web] need_screen 判断 {t[:20]!r} -> {out[:20]!r}")
        decision = out.startswith("y")
        if out.startswith(("y", "n")):
            if len(_SCREEN_CACHE) >= 128:
                _SCREEN_CACHE.pop(next(iter(_SCREEN_CACHE)))
            _SCREEN_CACHE[t] = (decision, time.monotonic() + 300.0)
        return decision
    except Exception as exc:  # noqa: BLE001
        nonebot.logger.warning(f"[web] need_screen 判定失败：{exc!r}")
        return False


async def _normal_chat(text: str, images: list[bytes] | None = None):
    async with cb._history_scope("private:" + ADMIN_UID):
        return await _normal_chat_unlocked(text, images)


async def _normal_chat_unlocked(text: str, images: list[bytes] | None = None) -> dict:
    """正常模式使用 QQ 主对话的关系档位、记忆与好感结算。"""
    persona = erp.persona_obj
    static = cb.llm.build_static_system(persona.base, persona.name)
    store_obj = getattr(cb, "store", None)
    aff_v = float(store_obj.peek(ADMIN_UID)) if hasattr(store_obj, "peek") else _chat_aff()
    context_key = "private:" + ADMIN_UID
    session = cb.histories.setdefault(context_key, cb.Session())

    aff_mod = getattr(cb, "aff", None)
    tiers = getattr(persona, "tiers", None) or []
    tier = aff_mod.tier_of(aff_v, tiers) if (aff_mod and tiers) else {}
    tier_name = str(tier.get("name", "")) if tier else ""
    tier_desc = str(tier.get("desc", "")) if tier else ""
    tier_range = f"{tier.get('min')}~{tier.get('max')}" if tier and "min" in tier else ""

    search_task = _background(_maybe_search(text))
    hint = None
    target_chars = 0
    try:
        openai_key = getattr(cb, "OPENAI_KEY", "")
        intent_key = openai_key or getattr(cb, "DEEPSEEK_KEY", "")
        intent_model = (
            getattr(cb, "INTENT_MODEL", "")
            if openai_key
            else getattr(cb, "DEEPSEEK_MODEL", "")
        )
        if getattr(cb, "LENGTH_ENABLED", False) and intent_key:
            extra_kw = {}
            if openai_key:
                extra_kw["reasoning_effort"] = getattr(cb, "INTENT_EFFORT", "low")
                extra_kw["fallback_key"] = getattr(cb, "DEEPSEEK_KEY", "")
                extra_kw["fallback_model"] = getattr(cb, "DEEPSEEK_MODEL", "")
            hint = await cb.llm.decide_length(
                text,
                intent_key,
                intent_model,
                farm_enabled=False,
                web_enabled=getattr(cb, "WEB_ENABLED", False),
                context="",
                history=list(session.recent[-8:]),
                **extra_kw,
            )
            target_chars = max(
                10,
                min(500, int(hint.max_chars * getattr(cb, "LENGTH_MULTIPLIER", 1.0))),
            )
            nonebot.logger.info(
                f"[web] 前置意图审查 {hint.level} {target_chars}字 ({hint.reason})"
            )
    except Exception as exc:  # noqa: BLE001
        nonebot.logger.warning(f"[web] 长度判定失败：{exc!r}")

    mem_block = ""
    try:
        mem_block = cb.memory_store.render(ADMIN_UID, aff_v) or ""
    except Exception as exc:  # noqa: BLE001
        nonebot.logger.warning(f"[web] 记忆注入失败：{exc!r}")

    search_block = await search_task

    dynamic = cb.llm.build_dynamic_block(
        affection=aff_v,
        tier_name=tier_name,
        tier_desc=tier_desc,
        tier_range=tier_range,
        max_chars=target_chars,
        length_level=hint.level if hint else "",
        memory_block=mem_block,
        search_block=search_block,
        has_image=bool(images),
    )
    user_head = (f"{text}\n\n（对方发来 {len(images)} 张图）" if images else text).strip()
    pub_ctx = cb._group_context(cb.histories, ADMIN_UID, cb._group_history_messages)
    tail = (
        f"{user_head}\n\n"
        "──────── 系统运行时状态（本轮自动附加，不要向对方复述）────────\n"
        f"{dynamic}"
    )
    if pub_ctx:
        tail += "\n\n" + pub_ctx
    msgs = cb._history_messages(session) + [{"role": "user", "content": tail}]

    res = await cb.llm.chat(
        msgs,
        static,
        api_key=cb.ARK_KEY,
        model=cb.ARK_MODEL,
        gemini_key=cb.GEMINI_CHAT_KEY,
        gemini_model=cb.GEMINI_CHAT_MODEL,
        gemini_base=cb.GEMINI_CHAT_BASE,
        vertex_auth=cb.VERTEX_AUTH,
        vertex_proxy=cb.VERTEX_PROXY,
        vertex_thinking=cb.VERTEX_THINKING,
        vertex_cache_scope=context_key if cb.VERTEX_SESSION_CACHE else "",
        images=images or None,
        min_chars=target_chars,
    )
    last_reply = ""
    for m in reversed(session.recent):
        if m.get("role") == "assistant":
            last_reply = str(m.get("content") or "").strip()
            break
    if getattr(res, "ok", False):
        content = str(getattr(res, "reply", "") or "").strip()
        if content and last_reply and content == last_reply:
            nonebot.logger.warning("[web] 正常模式复读了上一句，重生成一次")
            retry_msgs = msgs + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "（别重复上一句，换个说法，接住我这句。）"},
            ]
            res2 = await cb.llm.chat(
                retry_msgs,
                static,
                api_key=cb.ARK_KEY,
                model=cb.ARK_MODEL,
                gemini_key=cb.GEMINI_CHAT_KEY,
                gemini_model=cb.GEMINI_CHAT_MODEL,
                gemini_base=cb.GEMINI_CHAT_BASE,
                vertex_auth=cb.VERTEX_AUTH,
                vertex_proxy=cb.VERTEX_PROXY,
                vertex_thinking=cb.VERTEX_THINKING,
                vertex_cache_scope=context_key if cb.VERTEX_SESSION_CACHE else "",
                images=images or None,
                min_chars=target_chars,
            )
            c2 = str(getattr(res2, "reply", "") or "").strip()
            if getattr(res2, "ok", False) and c2 and c2 != content:
                res = res2
                content = c2
    if getattr(res, "ok", False):
        content = str(getattr(res, "reply", "") or "").strip()
        if content:
            img_desc = str(getattr(res, "image_desc", "") or "").strip()
            stored = user_head + (f"\n\n（图上内容：{img_desc}）" if img_desc else "")
            session.recent.append({"role": "user", "content": stored})
            session.recent.append({"role": "assistant", "content": content})
            cb._save_histories(context_key)
            if len(session.recent) >= cb.HISTORY_COMPRESS_AT:
                _background(cb._maybe_compress_history(context_key))

    if not getattr(res, "ok", False) or not str(getattr(res, "reply", "") or "").strip():
        return res

    try:
        daily_hint = None
        if hint is not None and getattr(hint, "has_daily", False):
            daily_hint = hint.daily_affection
        event = getattr(res, "event", "none") or "none"
        ar = cb._apply_shared_affection(
            cb.store, ADMIN_UID, event, rng=random, daily_hint=daily_hint
        )
        aff_daily = getattr(cb, "affection_daily", None)
        if aff_daily is not None and hasattr(ar, "daily_delta"):
            aff_daily.record(
                ADMIN_UID,
                daily_delta=ar.daily_delta,
                praise=1 if (getattr(ar, "praise_hit", False) and getattr(ar, "event_gain", 0) > 0) else 0,
                offense=1 if (getattr(ar, "offense_hit", False) and getattr(ar, "event_loss", 0) > 0) else 0,
                total_delta=ar.delta,
            )
        nonebot.logger.info(
            f"[web] 好感 event={event} {ar.before:.2f}→{ar.after:.2f} ({ar.delta:+.2f})"
            f" | {getattr(res, 'reason', '')}"
        )
    except Exception as exc:  # noqa: BLE001
        nonebot.logger.warning(f"[web] 好感结算失败：{exc!r}")
    return res


def _save_sess() -> None:
    try:
        SESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _conversations.save()
        atomic_json(SESS_FILE, _conversations.get("legacy"))
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 保存会话失败：{exc!r}")


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


class Handler(BaseHTTPRequestHandler):
    server_version = "NovaAI/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        print("[web]", fmt % args)

    def _token_ok(self) -> bool:
        """正常模式（与 QQ 共用记忆）只给管理员：配了口令就要求带上。"""
        if not WEB_TOKEN:
            return True
        return self.headers.get("X-Nova-Token", "") == WEB_TOKEN

    def _json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError, OSError):
            # 手机端等不及先断开了，回复丢了就算了，别刷堆栈
            print("[web] 客户端已断开（回复未送达）")

    def _html(self, text: str) -> None:
        body = text.encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, BrokenPipeError, OSError):
            pass

    def _conversation_id(self):
        return self.headers.get("X-Nova-Conversation", "legacy") or "legacy"

    def _session(self):
        row = _conversations.get(self._conversation_id())
        erp._erp_aff[row["uid"]] = float(row.get("aff", 0))
        return row

    def _uid(self):
        return self._session()["uid"]

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/conversations":
            if not self._token_ok():
                self._json({"ok": False, "error": "需要管理员口令"}, 401)
                return
            self._json({"ok": True, "conversations": _conversations.listing()})
            return
        if self._conversation_id() not in _conversations.data["conversations"]:
            self._json({"ok": False, "error": "对话不存在，请重新选择"}, 404)
            return
        if self._conversation_id() != "legacy" and not self._token_ok():
            self._json({"ok": False, "error": "需要管理员口令"}, 401)
            return
        if path in ("/", "/index.html"):
            page = WEB_DIR / "index.html"
            if not page.exists():
                self._html("<h1>web/index.html 不存在</h1>")
                return
            self._html(page.read_text(encoding="utf-8"))
            return
        if path == "/nova-icon.png":
            body = (WEB_DIR / "nova-icon.png").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/health":
            self._json({"ok": True, "ready": True})
            return
        if path == "/api/summary":
            if "mode=normal" in self.path.partition("?")[2]:
                if not self._token_ok():
                    self._json({"ok": False, "error": "需要管理员口令"}, 401)
                    return
                row = read_session(cb.HISTORY_FILE, "private:" + ADMIN_UID) or {}
                sections = row.get("sections") or []
                self._json({"ok": True, "summary": "\n\n".join(sections) or row.get("summary", ""),
                            "compressed": len(sections), "rounds": len(row.get("recent") or []) // 2,
                            "archive_blocks": len(sections)})
                return
            blocks = 0
            try:
                arc = json.loads(
                    (cb.STATE_FILE.parent / "erp_web_archive.json").read_text(encoding="utf-8")
                )
                blocks = len(arc.get("blocks") or [])
            except Exception:  # noqa: BLE001
                pass
            self._json(
                {
                    "ok": True,
                    "summary": self._session().get("summary") or "",
                    "compressed": int(self._session().get("compressed") or 0),
                    "rounds": len(self._session().get("history") or []) // 2,
                    "archive_blocks": blocks,
                }
            )
            return
        if path == "/api/status":
            self._json(
                {
                    "ok": True,
                    "turns": int(self._session().get("n") or 0),
                    "aff": float(self._session().get("aff", 0)),
                    "conversation_id": self._conversation_id(),
                    "conversation_title": self._session().get("title", "原有对话"),
                    "pre_prompt": self._session().get("pre_prompt", ""),
                    "model": cb.GEMINI_CHAT_MODEL,
                    "fallback": erp._erp_models()[1:],
                    "length": len(self._session().get("history") or []),
                    "temp": self._session().get("temp", 0.9),
                    "normal_turns": int(self._session().get("normal_n") or 0),
                    "normal_aff": _chat_aff(),
                    "memory": {"trigger": cb.HISTORY_COMPRESS_AT, "recent": cb.HISTORY_KEEP_RECENT,
                               "sections": cb.HISTORY_SECTIONS_MAX},
                }
            )
            return
        if path == "/api/history":
            q = self.path.partition("?")[2]
            if "mode=normal" in q:
                if not self._token_ok():
                    self._json({"ok": False, "error": "需要管理员口令"}, 401)
                    return
                # 正常模式与 QQ 共用记忆：直接给 QQ 那边的原文
                row = read_session(cb.HISTORY_FILE, "private:" + ADMIN_UID) or {}
                self._json({"ok": True, "history": row.get("recent") or []})
            else:
                self._json({"ok": True, "history": self._session().get("history") or []})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/need_screen":
            return self._dispatch_post()
        if not _REQUEST_LOCK.acquire(blocking=False):
            self._json({"ok": False, "error": "上一条请求还在处理，请稍等片刻"}, 409)
            return
        try:
            if path not in ("/api/chat_normal", "/api/reset_normal"):
                _run(_wait_erp_compression())
            self._dispatch_post()
        except TimeoutError:
            self._json({"ok": False, "error": "本次请求等待超时，请查看历史确认结果后再试"}, 504)
        except Exception as exc:
            nonebot.logger.warning(f"[web] 请求未完成：{type(exc).__name__}")
            self._json({"ok": False, "error": "请求暂未完成，请稍后重试"}, 500)
        finally:
            _REQUEST_LOCK.release()

    def _dispatch_post(self):
        path = self.path.split("?")[0]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            self._json({"ok": False, "error": "bad request"}, 400)
            return

        if path in ("/api/conversations/new", "/api/conversations/config"):
            if not self._token_ok():
                self._json({"ok": False, "error": "需要管理员口令"}, 401)
                return
            if not isinstance(data, dict):
                self._json({"ok": False, "error": "会话设置需要是一个对象"}, 400)
                return
            try:
                if path.endswith("/new"):
                    row = _conversations.create(data.get("title", "新对话"), data.get("aff", 0), data.get("pre_prompt", ""))
                else:
                    row = _conversations.configure(self._conversation_id(), data)
            except InvalidConversationSetting as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
                return
            except KeyError:
                self._json({"ok": False, "error": "对话不存在，请重新选择"}, 404)
                return
            self._json({"ok": True, "conversation": _conversations.summary(row)})
            return
        if self._conversation_id() not in _conversations.data["conversations"]:
            self._json({"ok": False, "error": "对话不存在，请重新选择"}, 404)
            return
        if self._conversation_id() != "legacy" and not self._token_ok():
            self._json({"ok": False, "error": "需要管理员口令"}, 401)
            return
        if path == "/api/need_screen":
            if not self._token_ok():
                self._json({"ok": False, "error": "需要管理员口令"}, 401)
                return
            txt = str(data.get("text") or "").strip()
            need = _run(_judge_need_screen(txt))
            self._json({"ok": True, "need": bool(need)})
            return

        if path == "/api/chat_normal":
            if not self._token_ok():
                self._json({"ok": False, "error": "需要管理员口令"}, 401)
                return
            text = str(data.get("text") or "").strip()
            # 图片：前端传 base64 数组，最多 4 张
            raw_imgs = data.get("images") or []
            images: list[bytes] = []
            if isinstance(raw_imgs, list):
                for b64 in raw_imgs[:4]:
                    try:
                        if isinstance(b64, str) and b64:
                            images.append(base64.b64decode(b64, validate=False))
                    except Exception:  # noqa: BLE001
                        continue
            if not text and not images:
                self._json({"ok": False, "error": "空消息"})
                return
            res = _run(_normal_chat(text or "（看看这张图）", images or None))
            reply = str(getattr(res, "reply", "") or "")
            if not reply:
                self._json({"ok": False, "error": str(getattr(res, "error", "") or "没接上")})
                return
            # 历史已由 _normal_chat 写进 QQ 那份记忆里，这里不再重复存
            self._session()["normal_n"] = int(self._session().get("normal_n") or 0) + 1
            _save_sess()
            sess_qq = cb.histories.get("private:" + ADMIN_UID)
            turns = (len(sess_qq.recent) // 2) if sess_qq else 0
            usage = getattr(res, "usage", {}) or {}
            prompt = int(usage.get("_prompt_tokens") or usage.get("prompt_tokens") or 0)
            cached = int(usage.get("_cached_tokens") or 0)
            self._json({"ok": True, "reply": reply, "turns": turns,
                        "elapsed": round(float(getattr(res, "elapsed", 0)), 2),
                        "cache_ratio": round(cached / prompt, 3) if prompt else 0,
                        "memory_pending": bool(_TASKS)})
            return

        if path == "/api/reset_normal":
            if not self._token_ok():
                self._json({"ok": False, "error": "需要管理员口令"}, 401)
                return
            # 正常模式与 QQ 共用记忆，这里不清 QQ 那边；只重置网页计数
            self._session()["normal"] = []
            self._session()["normal_n"] = 0
            _save_sess()
            self._json({"ok": True, "note": "正常模式与 QQ 共用记忆，未清空 QQ 记录"})
            return

        if path == "/api/chat":
            text = str(data.get("text") or "").strip()
            if not text:
                self._json({"ok": False, "error": "空消息"})
                return
            hist = self._session().setdefault("history", [])
            if hist and hist[-1].get("role") == "user":
                hist[-1]["content"] = text
            else:
                hist.append({"role": "user", "content": text})
            reply, err = _run(
                erp._do_generate(self._uid(), self._session(), text, self._session().get("temp"))
            )
            if not reply:
                _save_sess()
                self._json({"ok": False, "error": err or "被判定成非自愿/露骨，卡住了"})
                return
            hist.append({"role": "assistant", "content": reply})
            self._session()["n"] = int(self._session().get("n") or 0) + 1
            # 到 40 轮自动压缩：前段 + 旧摘要 → 新摘要，只留最近 20 轮原文
            _save_sess()
            _run(_schedule_erp_compression(self._uid(), self._session()))
            self._json({"ok": True, "reply": reply, "turns": self._session()["n"]})
            return

        if path == "/api/retry":
            hist = self._session().get("history") or []
            last_user = next((m for m in reversed(hist) if m.get("role") == "user"), None)
            if last_user is None:
                self._json({"ok": False, "error": "没有可重抽的内容"})
                return
            if hist and hist[-1].get("role") == "assistant":
                hist.pop()
            reply, err = _run(
                erp._do_generate(self._uid(), self._session(), str(last_user.get("content") or ""), self._session().get("temp"))
            )
            if not reply:
                _save_sess()
                self._json({"ok": False, "error": err or "还是被判定成非自愿/露骨"})
                return
            hist.append({"role": "assistant", "content": reply})
            self._session()["n"] = max(int(self._session().get("n") or 0), len(hist) // 2)
            _save_sess()
            self._json({"ok": True, "reply": reply, "turns": self._session()["n"]})
            return

        if path == "/api/undo":
            hist = self._session().get("history") or []
            if not hist:
                self._json({"ok": False, "error": "没有可撤回的内容"})
                return
            hist.pop()
            self._session()["n"] = len(hist) // 2
            _save_sess()
            self._json({"ok": True, "history": hist, "turns": self._session()["n"]})
            return

        if path == "/api/truncate":
            try:
                keep = max(0, int(data.get("len")))
            except Exception:  # noqa: BLE001
                self._json({"ok": False, "error": "bad len"})
                return
            hist = self._session().get("history") or []
            del hist[keep:]
            self._session()["n"] = len(hist) // 2
            _save_sess()
            self._json({"ok": True, "history": hist, "turns": self._session()["n"]})
            return

        if path == "/api/temp":
            try:
                val = float(data.get("value"))
            except Exception:  # noqa: BLE001
                self._json({"ok": False, "error": "bad value"})
                return
            self._session()["temp"] = max(0.0, min(1.5, round(val, 2)))
            _save_sess()
            self._json({"ok": True, "temp": self._session()["temp"]})
            return

        if path == "/api/reset":
            row = _conversations.create()
            self._json({"ok": True, "conversation": _conversations.summary(row)})
            return

        if path == "/api/aff":
            try:
                val = float(data.get("value"))
            except Exception:  # noqa: BLE001
                self._json({"ok": False, "error": "bad value"})
                return
            self._session()["aff"] = max(-50, min(100, val))
            self._session()["stage_sent"] = 0      # 好感变了，下一次重新注入一次状态块
            _save_sess()
            self._json({"ok": True, "aff": self._session().get("aff", 0)})
            return

        if path == "/api/scene":
            words = str(data.get("words") or "").strip()
            if not words:
                self._json({"ok": False, "error": "给几个描述词，比如：雪夜 火盆 强吻"})
                return
            prompt = (
                f"【场景搭建】根据这些描述词写开场：{words}\n"
                "要求：先写 2~4 句环境与她的状态（括号内、第三人称），再写她的第一句台词或动作收尾。"
                "由用户决定自己的动作、台词和剧情选择。"
                "只把场景摆好、让她先落到这个场景里。不要解释设定。"
            )
            hist = self._session().setdefault("history", [])
            nxt = int(self._session().get("n") or 0) + 1
            msgs = [
                {"role": "user", "content": erp._stage_block(self._uid(), nxt)},
                {"role": "assistant", "content": "（当前关系状态已归档。）"},
                {"role": "user", "content": prompt},
            ]

            async def _gen() -> dict:
                return await erp.vertex.vertex_chat(
                    msgs,
                    erp._system_for_session(self._session()),
                    auth_file=cb.VERTEX_AUTH,
                    model=cb.GEMINI_CHAT_MODEL,
                    proxy=erp._erp_proxy(),
                    timeout=70.0,
                    thinking_level=cb.VERTEX_THINKING or "low",
                    cache_scope="",
                    plain_text=True,
                    temperature=self._session().get("temp"),
                    total_budget=70.0,
                    per_try=45.0,
                    max_output=1400,
                )

            r = _run(_gen())
            text = erp._strip_wrapper(str(r.get("text") or "")) if r.get("ok") else ""
            if not text or erp._looks_refusal(text):
                self._json({"ok": False, "error": "没写出来，换个描述词再试"})
                return
            hist.append({"role": "user", "content": f"（场景：{words}）"})
            hist.append({"role": "assistant", "content": text})
            self._session()["n"] = nxt
            _save_sess()
            self._json({"ok": True, "reply": text, "turns": self._session()["n"]})
            return

        self._json({"ok": False, "error": "not found"}, 404)


def main() -> None:
    port = int(cb._env("CHAT_WEB_PORT", "8090") or 8090)
    ip = _lan_ip()
    print("=" * 60)
    print("  星澜 NovaAI · 若娜瓦 Web 版")
    print(f"  本机访问 : http://127.0.0.1:{port}")
    print(f"  手机访问 : http://{ip}:{port}")
    print(f"  当前模型 : {cb.GEMINI_CHAT_MODEL}")
    print(f"  会话轮数 : {_sess.get('n') or 0}")
    print("=" * 60)
    async def warm():
        await cb.vertex.warmup(cb.VERTEX_AUTH, cb.GEMINI_CHAT_MODEL,
                              cb.llm.build_static_system(erp.persona_obj.base, erp.persona_obj.name))
    asyncio.run_coroutine_threadsafe(warm(), _LOOP)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
