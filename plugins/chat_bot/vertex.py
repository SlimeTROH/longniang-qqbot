"""Vertex AI 直连与上下文缓存引擎（隐式前缀缓存 + 显式人设/会话缓存双模优化）。

核心缓存优化设计：
  1. Gemini 4（如 gemini-4-hydrogen）采用隐式前缀缓存（最低 4096 tokens）：
     - 人设放入 `contents[0]`（`systemInstruction` 实测隐式缓存命中为 0）；
     - `include_refs=True` 时紧随人设固定注入压缩立绘，工具/分类器调用传 `include_refs=False` 避免无谓上传 60KB 图片；
     - 历史消息做严格 user/model 交替合并，与显式缓存路径保持 100% 字节级一致；
     - 字数、时间、好感、游戏状态全部严格置于最后一条 user 消息末尾，保证前 N-1 条消息跨轮完全不变。
  2. Gemini 3.x 采用 `cachedContents` 显式人设缓存 + 分块边界（8/20/40/60）会话缓存：
     - 后台异步建缓存（`CACHE_START_DELAY` 错峰上传，失败冷却 15 分钟），绝不阻塞当前回复；
     - 跨进程原子共享缓存注册表（`state/vertex_cache.json`）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
import weakref
from functools import partial
from pathlib import Path

import cache_registry as registry_io
import httpx
from network_config import get_proxy
from nonebot import logger
from shared_history import scope_lock as shared_cache_lock

# 按凭据文件隔离缓存 OAuth2 token；单个调用方取消不中断共享刷新任务
_TOKENS: dict[str, tuple[str, float]] = {}
_TOKEN_REFRESH: dict[tuple[object, str], asyncio.Task] = {}


def _load_service_account(auth_file: str) -> tuple[dict, str, str]:
    """读取服务账号 JSON，提取 (service_account_dict, project_id, location)。"""
    raw = json.loads(Path(auth_file).read_text(encoding="utf-8"))
    sa = raw.get("service_account") or {}
    if not sa.get("private_key"):
        raise ValueError(f"{auth_file} 里没有 service_account.private_key")
    project = str(raw.get("project_id") or sa.get("project_id") or "")
    location = str(raw.get("location") or "global")
    return sa, project, location


def _refresh_token_sync(auth_file: str, proxy: str = "") -> tuple[str, float]:
    """在工作线程中通过代理刷新 Google Cloud OAuth2 access_token。"""
    from datetime import timezone

    import requests
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    sa, _, _ = _load_service_account(auth_file)
    creds = service_account.Credentials.from_service_account_info(
        sa, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )

    use_proxy = proxy or get_proxy()
    with requests.Session() as session:
        session.trust_env = False
        session.proxies = {"http": use_proxy, "https": use_proxy}
        creds.refresh(partial(Request(session=session), timeout=(5.0, 10.0)))

    if creds.expiry is not None:
        exp = creds.expiry
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        expiry = exp.timestamp()
    else:
        expiry = time.time() + 3000
    return creds.token or "", expiry


async def _get_token(auth_file: str, proxy: str = "") -> str:
    """按凭据路径复用有效 token，并发请求共享同一次刷新。"""
    key = os.path.normcase(str(Path(auth_file).resolve()))
    cached = _TOKENS.get(key)
    if cached and cached[1] - time.time() > 300:
        return cached[0]

    slot = (asyncio.get_running_loop(), key)
    task = _TOKEN_REFRESH.get(slot)
    if task is None:

        async def refresh() -> str:
            token, expiry = await asyncio.to_thread(
                _refresh_token_sync, auth_file, proxy or get_proxy()
            )
            _TOKENS[key] = (token, expiry)
            logger.info("chat_bot: Vertex token 已刷新")
            return token

        task = asyncio.create_task(refresh())
        _TOKEN_REFRESH[slot] = task

        def completed(done: asyncio.Task) -> None:
            if _TOKEN_REFRESH.get(slot) is done:
                _TOKEN_REFRESH.pop(slot, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
    return await asyncio.shield(task)


def _to_vertex_schema(schema: dict) -> dict:
    """OpenAI JSON Schema 转换为 Vertex responseSchema（类型名大写，剔除 additionalProperties）。"""
    if not isinstance(schema, dict):
        return schema
    out: dict = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, str):
            out[key] = value.upper()
        elif key == "properties" and isinstance(value, dict):
            out[key] = {k: _to_vertex_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[key] = _to_vertex_schema(value)
        elif key == "additionalProperties":
            continue
        else:
            out[key] = value
    return out


def _role(role: str) -> str:
    return "model" if role == "assistant" else "user"


def supports_explicit_cache(model: str) -> bool:
    """Gemini 4 系列仅支持隐式前缀缓存，禁止调用 cachedContents。"""
    return not model.rsplit("/", 1)[-1].lower().startswith("gemini-4")


def _vertex_host(location: str) -> str:
    return (
        "aiplatform.googleapis.com"
        if not location or location == "global"
        else f"{location}-aiplatform.googleapis.com"
    )


def _build_contents(
    messages: list[dict], system_prompt: str, images: list[bytes] | None
) -> list[dict]:
    """将消息列表组装为严格 user/model 交替的 Vertex contents，保证前缀稳定对齐。"""
    contents: list[dict] = []

    head = (system_prompt or "").strip()
    if head:
        contents.append({"role": "user", "parts": [{"text": head}]})
        contents.append({"role": "model", "parts": [{"text": "嗯。"}]})

    for idx, msg in enumerate(messages):
        role = _role(str(msg.get("role", "user")))
        text = str(msg.get("content", "") or "")
        is_last = idx == len(messages) - 1
        parts: list[dict] = []
        if text:
            parts.append({"text": text})
        if is_last and images:
            for raw in images:
                try:
                    from .vision import compress_image

                    payload, mime = compress_image(raw)
                except Exception:  # noqa: BLE001
                    payload, mime = raw, "image/jpeg"
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": mime,
                            "data": base64.b64encode(payload).decode("ascii"),
                        }
                    }
                )
        if not parts:
            parts.append({"text": "（空）"})

        # 合并意外相邻的同角色纯文本轮次，防止破坏缓存前缀对齐或触发 Vertex 400
        if (
            contents
            and contents[-1]["role"] == role
            and not (is_last and images)
            and len(contents[-1]["parts"]) == 1
            and "text" in contents[-1]["parts"][0]
            and len(parts) == 1
            and "text" in parts[0]
        ):
            contents[-1]["parts"][0]["text"] += "\n\n" + parts[0]["text"]
        else:
            contents.append({"role": role, "parts": parts})

    if contents and contents[-1]["role"] != "user":
        contents.append({"role": "user", "parts": [{"text": "继续。"}]})
    return contents


def _implicit_contents(
    messages: list[dict],
    persona: str,
    images: list[bytes] | None,
    min_chars: int = 0,
    *,
    include_refs: bool = True,
) -> list[dict]:
    """构造隐式缓存友好的固定前缀：人设 → 角色立绘（仅角色对话开启）→ 历史 → 当前消息尾部状态。"""
    contents = _build_contents(messages, persona, images)
    if include_refs:
        refs = []
        for i, (raw, mime, _fingerprint) in enumerate(_load_ref_images()):
            refs.extend(
                [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": mime,
                                    "data": base64.b64encode(raw).decode("ascii"),
                                }
                            },
                            {
                                "text": (
                                    "这是你本来的样子，记住这张脸和这身装扮。"
                                    if i == 0
                                    else "这张也是你。"
                                )
                            },
                        ],
                    },
                    {"role": "model", "parts": [{"text": "……嗯。"}]},
                ]
            )
        offset = 2 if persona.strip() else 0
        contents[offset:offset] = refs

    if not contents or contents[-1]["role"] != "user":
        contents.append({"role": "user", "parts": [{"text": "继续。"}]})
    if min_chars > 0 and contents:
        contents[-1]["parts"].append(
            {
                "text": f"【本轮输出要求】reply 约 {min_chars} 字，说清楚就停。"
                + (
                    "只说一两句，说完就停。"
                    if min_chars <= 40
                    else (
                        "先回答，再补必要的解释。"
                        if min_chars <= 150
                        else "按对方需要的详细度展开。"
                    )
                )
            }
        )
    return contents


# ====================================================================== 连接池与超时预算
CONNECT_TIMEOUT = 5.0
PER_PROXY_TIMEOUT = 10.0
SINGLE_PROXY_ATTEMPTS = 2
VERTEX_TOTAL_BUDGET = 20.0

_CLIENTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _request_timeout(seconds: float) -> httpx.Timeout:
    return httpx.Timeout(connect=CONNECT_TIMEOUT, read=max(0.01, seconds), write=15.0, pool=5.0)


def _get_client(proxy: str, timeout: float) -> "httpx.AsyncClient":
    proxy = proxy or get_proxy()
    clients = _CLIENTS.setdefault(asyncio.get_running_loop(), {})
    existing = clients.get(proxy)
    if existing is not None and not existing.is_closed:
        return existing

    kwargs: dict = {
        "timeout": _request_timeout(30.0),
        "trust_env": False,
        "limits": httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
            keepalive_expiry=15.0,
        ),
        "http2": False,
    }
    if proxy:
        kwargs["proxy"] = proxy

    client = clients[proxy] = httpx.AsyncClient(**kwargs)
    logger.info("vertex: 固定代理连接池已建立")
    return client


async def close_clients() -> None:
    """取消当前事件循环下的后台缓存/刷新任务并关闭连接池。"""
    loop = asyncio.get_running_loop()
    tasks = [t for t in list(_BACKGROUND_TASKS) if t.get_loop() is loop]
    tasks += [t for (owner, _), t in list(_TOKEN_REFRESH.items()) if owner is loop]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    clients = _CLIENTS.pop(loop, {})
    if clients:
        await asyncio.gather(*(c.aclose() for c in clients.values()), return_exceptions=True)


# ====================================================================== 显式缓存（人设级 + 会话级）
CACHE_TTL_SECONDS = 3600.0
CACHE_REFRESH_MARGIN = 300.0
_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "state" / "vertex_cache.json"
_CACHE_REGISTRY: dict[str, dict] = {}
_CACHE_LOADED = False
_CACHE_SIGNATURE = None
_CACHE_BASELINE: dict = {}
_CACHE_LOCK: "asyncio.Lock | None" = None


def _cache_lock() -> "asyncio.Lock":
    global _CACHE_LOCK
    if _CACHE_LOCK is None:
        _CACHE_LOCK = asyncio.Lock()
    return _CACHE_LOCK


def _load_cache_registry() -> None:
    global _CACHE_LOADED, _CACHE_SIGNATURE, _CACHE_BASELINE
    try:
        info = _CACHE_FILE.stat()
        signature = (info.st_mtime_ns, info.st_size)
    except OSError:
        _CACHE_LOADED = True
        return
    if _CACHE_LOADED and signature == _CACHE_SIGNATURE:
        return
    try:
        latest = registry_io.load(_CACHE_FILE)
        _CACHE_REGISTRY.clear()
        _CACHE_REGISTRY.update(latest)
        _CACHE_BASELINE = json.loads(json.dumps(latest))
        _CACHE_SIGNATURE = signature
        _CACHE_LOADED = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"vertex: 读取缓存登记失败：{type(exc).__name__}")


def _save_cache_registry() -> None:
    global _CACHE_SIGNATURE, _CACHE_BASELINE
    try:
        latest = registry_io.save(_CACHE_FILE, _CACHE_REGISTRY, _CACHE_BASELINE)
        _CACHE_REGISTRY.clear()
        _CACHE_REGISTRY.update(latest)
        _CACHE_BASELINE = json.loads(json.dumps(latest))
        info = _CACHE_FILE.stat()
        _CACHE_SIGNATURE = (info.st_mtime_ns, info.st_size)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"vertex: 写缓存登记失败：{type(exc).__name__}")


def _persona_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


_REF_IMAGE_DIR = Path(__file__).resolve().parent.parent.parent / "assets"
_REFS_CACHE: list | None = None


def _shrink_ref(raw: bytes) -> tuple[bytes, str]:
    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((512, 512), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=78, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001
        return raw, "image/png"


def _load_ref_images() -> list[tuple[bytes, str, str]]:
    global _REFS_CACHE
    if _REFS_CACHE is not None:
        return _REFS_CACHE

    out: list[tuple[bytes, str, str]] = []
    try:
        files = sorted(_REF_IMAGE_DIR.glob("ronova_*.png"))
    except Exception:  # noqa: BLE001
        return out

    for p in files:
        try:
            raw = p.read_bytes()
            data, mime = _shrink_ref(raw)
            digest = hashlib.md5(data).hexdigest()[:8]
            out.append((data, mime, digest))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"vertex: 读立绘 {p.name} 失败 {exc!r}")
    _REFS_CACHE = out
    return out


_PERSONA_BUILDING: set[str] = set()
_PERSONA_COOLDOWN: dict[str, float] = {}
CACHE_START_DELAY = 8.0


def _persona_cache_key(persona: str, model: str) -> str:
    return _persona_hash(persona + "|" + model + "|" + "|".join(r[2] for r in _load_ref_images()))


def _lookup_persona_cache(persona: str, model: str) -> str:
    _load_cache_registry()
    entry = _CACHE_REGISTRY.get(_persona_cache_key(persona, model))
    if entry and float(entry.get("expire", 0)) - time.time() > 30.0:
        return str(entry.get("name") or "")
    return ""


def _schedule_persona_cache(**kwargs) -> None:
    persona, model = str(kwargs.get("persona") or ""), str(kwargs.get("model") or "")
    if not supports_explicit_cache(model) or len(persona) < 500:
        return
    key = _persona_cache_key(persona, model)
    _load_cache_registry()
    if float(_CACHE_REGISTRY.get(key, {}).get("expire", 0)) - time.time() > CACHE_REFRESH_MARGIN:
        return
    if key in _PERSONA_BUILDING or _PERSONA_COOLDOWN.get(key, 0) > time.monotonic():
        return
    loop = asyncio.get_running_loop()
    _PERSONA_BUILDING.add(key)

    async def build() -> None:
        try:
            await asyncio.sleep(CACHE_START_DELAY)
            if not await _get_or_create_cache(**kwargs):
                _PERSONA_COOLDOWN[key] = time.monotonic() + 900.0
        except Exception as exc:  # noqa: BLE001
            _PERSONA_COOLDOWN[key] = time.monotonic() + 900.0
            logger.warning(f"vertex: 后台创建人设缓存失败（冷却 15 分钟）：{type(exc).__name__}")
        finally:
            _PERSONA_BUILDING.discard(key)

    task = loop.create_task(build())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _get_or_create_cache(
    *,
    project: str,
    location: str,
    model: str,
    persona: str,
    token: str,
    proxy: str,
) -> str:
    _load_cache_registry()
    if not supports_explicit_cache(model) or not persona or len(persona) < 500:
        return ""

    refs = _load_ref_images()
    key = _persona_hash(persona + "|" + model + "|" + "|".join(r[2] for r in refs))
    now = time.time()
    entry = _CACHE_REGISTRY.get(key)
    if entry and float(entry.get("expire", 0)) - now > CACHE_REFRESH_MARGIN:
        return str(entry.get("name") or "")

    async with _cache_lock():
        async with shared_cache_lock(_CACHE_FILE, "persona:" + key):
            _load_cache_registry()
            entry = _CACHE_REGISTRY.get(key)
            if entry and float(entry.get("expire", 0)) - time.time() > CACHE_REFRESH_MARGIN:
                return str(entry.get("name") or "")

            host = _vertex_host(location)
            url = f"https://{host}/v1beta1/projects/{project}/locations/{location}/cachedContents"

            contents: list[dict] = []
            for i, (raw, mime, _h) in enumerate(refs):
                b64 = base64.b64encode(raw).decode("ascii")
                label = (
                    "这是你本来的样子，记住这张脸和这身装扮。"
                    if i == 0
                    else "这张也是你。"
                )
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {"inlineData": {"mimeType": mime, "data": b64}},
                            {"text": label},
                        ],
                    }
                )
                contents.append({"role": "model", "parts": [{"text": "……嗯。"}]})

            contents.append(
                {"role": "user", "parts": [{"text": "请严格遵照角色人设与规则交互。"}]}
            )
            contents.append({"role": "model", "parts": [{"text": "明白，随时开始。"}]})

            payload = {
                "model": f"projects/{project}/locations/{location}/publishers/google/models/{model}",
                "systemInstruction": {"role": "system", "parts": [{"text": persona}]},
                "contents": contents,
                "ttl": f"{int(CACHE_TTL_SECONDS)}s",
            }

            try:
                client = _get_client(proxy, timeout=180.0)
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=_request_timeout(180.0),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    name = str(data.get("name") or "")
                    if name:
                        tokens = int((data.get("usageMetadata") or {}).get("totalTokenCount") or 0)
                        _CACHE_REGISTRY[key] = {
                            "name": name,
                            "expire": now + CACHE_TTL_SECONDS,
                        }
                        _save_cache_registry()
                        logger.success(
                            f"vertex: 已建显式缓存 {name.split('/')[-1]}（{tokens} tokens，含 {len(refs)} 张立绘）"
                        )
                        return name
                logger.warning(f"vertex: 建缓存失败 HTTP {resp.status_code} {resp.text[:150]}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"vertex: 建缓存异常 {type(exc).__name__}: {str(exc)[:120]}")

    return ""


SESSION_BOUNDARIES = (8, 20, 40, 60)
SESSION_TTL_SECONDS = 3300.0
SESSION_REFRESH_MARGIN = 300.0
SESSION_MIN_HISTORY = 8


def _cache_proxy() -> str:
    return get_proxy()


def _session_boundary(total: int) -> int:
    if total < SESSION_MIN_HISTORY:
        return 0
    best = 0
    for b in SESSION_BOUNDARIES:
        if b <= total:
            best = b
    return best


def _prefix_contents(prefix: list[dict]) -> list[dict]:
    raw: list[dict] = []
    for msg in prefix:
        role = _role(str(msg.get("role", "user")))
        text = str(msg.get("content", "") or "").strip() or "（空）"
        raw.append({"role": role, "parts": [{"text": text}]})

    merged: list[dict] = []
    for item in raw:
        if merged and merged[-1]["role"] == item["role"]:
            merged[-1]["parts"][0]["text"] += "\n\n" + item["parts"][0]["text"]
        else:
            merged.append(item)
    if merged and merged[-1]["role"] != "model":
        merged.append({"role": "model", "parts": [{"text": "（已读）"}]})
    return merged


async def _delete_cache(name: str, token: str, proxy: str, location: str = "global") -> None:
    if not name:
        return
    try:
        host = _vertex_host(location)
        client = _get_client(proxy, timeout=30.0)
        resp = await client.delete(
            f"https://{host}/v1beta1/{name}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_request_timeout(30.0),
        )
        if resp.status_code not in (200, 204, 404):
            logger.info(f"vertex: 删旧会话缓存返回 {resp.status_code}")
    except Exception:  # noqa: BLE001
        pass


def _session_key(
    scope: str, prefix: list[dict], persona: str, refs: list, model: str = ""
) -> str:
    tail_sig = hashlib.md5(
        json.dumps(
            [(m.get("role", "user"), m.get("content", "")) for m in prefix],
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()[:12]
    return (
        f"sess:{scope}:{len(prefix)}:{tail_sig}:"
        f"{_persona_hash(persona + '|' + model + '|' + '|'.join(r[2] for r in refs))}"
    )


def _lookup_session_cache(
    scope: str, prefix: list[dict], persona: str, model: str = ""
) -> str:
    _load_cache_registry()
    if not scope or len(prefix) < SESSION_MIN_HISTORY:
        return ""
    key = _session_key(scope, prefix, persona, _load_ref_images(), model)
    entry = _CACHE_REGISTRY.get(key)
    if entry and float(entry.get("expire", 0)) - time.time() > 30.0:
        return str(entry.get("name") or "")
    return ""


_building: set[str] = set()
_session_cooldown: dict[str, float] = {}


def _schedule_session_cache(**kwargs) -> None:
    if not supports_explicit_cache(str(kwargs.get("model") or "")):
        return
    scope = str(kwargs.get("scope") or "")
    _load_cache_registry()
    key = _session_key(
        scope,
        kwargs.get("prefix", []),
        str(kwargs.get("persona") or ""),
        _load_ref_images(),
        str(kwargs.get("model") or ""),
    )
    if float(_CACHE_REGISTRY.get(key, {}).get("expire", 0)) - time.time() > SESSION_REFRESH_MARGIN:
        return
    if not scope or scope in _building:
        return
    if _session_cooldown.get(scope, 0) > time.time():
        return
    _building.add(scope)

    async def _run() -> None:
        try:
            await asyncio.sleep(CACHE_START_DELAY)
            name = await _get_or_create_session_cache(**kwargs)
            if not name:
                _session_cooldown[scope] = time.time() + 900
        except Exception as exc:  # noqa: BLE001
            _session_cooldown[scope] = time.time() + 900
            logger.warning(f"vertex: 后台建会话缓存失败（冷却 15 分钟）{exc!r}")
        finally:
            _building.discard(scope)

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_run())
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
    except RuntimeError:
        _building.discard(scope)


async def _get_or_create_session_cache(
    *,
    scope: str,
    prefix: list[dict],
    persona: str,
    project: str,
    location: str,
    model: str,
    token: str,
    proxy: str,
) -> str:
    _load_cache_registry()
    if not supports_explicit_cache(model) or not scope or not persona or len(persona) < 500:
        return ""
    boundary = len(prefix)
    if boundary < SESSION_MIN_HISTORY:
        return ""

    refs = _load_ref_images()
    key = _session_key(scope, prefix, persona, refs, model)
    now = time.time()
    entry = _CACHE_REGISTRY.get(key)
    if entry and float(entry.get("expire", 0)) - now > SESSION_REFRESH_MARGIN:
        return str(entry.get("name") or "")

    async with _cache_lock():
        async with shared_cache_lock(_CACHE_FILE, "session:" + key):
            _load_cache_registry()
            entry = _CACHE_REGISTRY.get(key)
            if entry and float(entry.get("expire", 0)) - time.time() > SESSION_REFRESH_MARGIN:
                return str(entry.get("name") or "")

            host = _vertex_host(location)
            url = f"https://{host}/v1beta1/projects/{project}/locations/{location}/cachedContents"

            contents: list[dict] = []
            for i, (raw, mime, _h) in enumerate(refs):
                b64 = base64.b64encode(raw).decode("ascii")
                label = (
                    "这是你本来的样子，记住这张脸和这身装扮。"
                    if i == 0
                    else "这张也是你。"
                )
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {"inlineData": {"mimeType": mime, "data": b64}},
                            {"text": label},
                        ],
                    }
                )
                contents.append({"role": "model", "parts": [{"text": "……嗯。"}]})
            contents.append(
                {"role": "user", "parts": [{"text": "请严格遵照角色人设与规则交互。"}]}
            )
            contents.append({"role": "model", "parts": [{"text": "明白，随时开始。"}]})
            contents.extend(_prefix_contents(prefix))

            payload = {
                "model": f"projects/{project}/locations/{location}/publishers/google/models/{model}",
                "systemInstruction": {"role": "system", "parts": [{"text": persona}]},
                "contents": contents,
                "ttl": f"{int(SESSION_TTL_SECONDS)}s",
            }

            try:
                client = _get_client(proxy, timeout=300.0)
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=_request_timeout(300.0),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    name = str(data.get("name") or "")
                    if name:
                        tokens = int((data.get("usageMetadata") or {}).get("totalTokenCount") or 0)
                        old = [
                            str(v.get("name"))
                            for v in _CACHE_REGISTRY.values()
                            if v.get("kind") == "session"
                            and v.get("scope") == scope
                            and v.get("name") != name
                        ]
                        _CACHE_REGISTRY[key] = {
                            "name": name,
                            "expire": now + SESSION_TTL_SECONDS,
                            "kind": "session",
                            "scope": scope,
                            "boundary": boundary,
                        }
                        _save_cache_registry()
                        logger.success(
                            f"vertex: 会话缓存已建（{scope}，覆盖 {boundary} 条历史，{tokens} tokens）"
                        )
                        for old_key, old_entry in list(_CACHE_REGISTRY.items()):
                            if old_entry.get("name") in old:
                                _CACHE_REGISTRY.pop(old_key, None)
                        _save_cache_registry()

                        async def retire() -> None:
                            await asyncio.sleep(35.0)
                            for old_name in old:
                                await _delete_cache(old_name, token, proxy, location=location)

                        task = asyncio.create_task(retire())
                        _BACKGROUND_TASKS.add(task)
                        task.add_done_callback(_BACKGROUND_TASKS.discard)
                        return name
                logger.warning(f"vertex: 建会话缓存失败 HTTP {resp.status_code} {resp.text[:150]}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"vertex: 建会话缓存异常 {type(exc).__name__}: {str(exc)[:120]}")

    return ""


def _normalize_proxies(proxy) -> list[str]:
    if not proxy:
        raw: list[str] = []
    elif isinstance(proxy, str):
        raw = [x.strip() for x in proxy.replace(";", ",").split(",")]
    else:
        raw = [str(x).strip() for x in proxy]

    out: list[str] = []
    for x in raw:
        if x and x not in out:
            out.append(x)
    if not out:
        out.append(get_proxy())
    return out[:1]


def _is_network_error(error: str) -> bool:
    if not error or error.startswith("HTTP "):
        return False
    keys = (
        "ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout",
        "PoolTimeout", "RemoteProtocolError", "ReadError", "WriteError",
        "ProxyError", "NetworkError", "ConnectionResetError", "TimeoutError",
    )
    return any(k in error for k in keys)


async def vertex_chat(
    messages: list[dict],
    system_prompt: str,
    *,
    auth_file: str,
    model: str,
    proxy="",
    timeout: float = 90.0,
    min_chars: int = 0,
    images: list[bytes] | None = None,
    thinking_level: str = "low",
    cache_scope: str = "",
    plain_text: bool = False,
    temperature: float | None = None,
    total_budget: float | None = None,
    per_try: float | None = None,
    max_output: int | None = None,
    include_refs: bool = True,
) -> dict:
    """带总时限预算与单路由网络重试的 Vertex 对话入口。"""
    candidates = _normalize_proxies(proxy)
    route = candidates[0] if candidates else get_proxy()
    started = time.monotonic()
    budget = max(0.0, float(total_budget if total_budget is not None else VERTEX_TOTAL_BUDGET))
    per = max(0.01, float(per_try if per_try is not None else PER_PROXY_TIMEOUT))
    deadline = started + budget
    result = {"ok": False, "error": "TimeoutError: Vertex 总预算已用完", "elapsed": 0.0}
    for attempt in range(SINGLE_PROXY_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        limit = min(remaining, per)
        try:
            req_kwargs = dict(
                auth_file=auth_file,
                model=model,
                proxy=route,
                timeout=limit,
                min_chars=min_chars,
                images=images,
                thinking_level=thinking_level,
                cache_scope=cache_scope,
                plain_text=plain_text,
                temperature=temperature,
                max_output=max_output,
            )
            if not include_refs:
                req_kwargs["include_refs"] = False
            result = await asyncio.wait_for(
                _vertex_request(messages, system_prompt, **req_kwargs),
                timeout=limit,
            )
        except asyncio.TimeoutError:
            result = {
                "ok": False,
                "error": "请求失败：TimeoutError（本次请求预算已用完）",
                "elapsed": time.monotonic() - started,
            }
        if result.get("ok") or not _is_network_error(str(result.get("error", ""))):
            return result
        if attempt + 1 < SINGLE_PROXY_ATTEMPTS and deadline > time.monotonic():
            logger.warning("vertex: 固定出口网络请求失败，重试同一路由")
    result["elapsed"] = time.monotonic() - started
    return result


_SAFETY_CATEGORIES = (
    "HARM_CATEGORY_HARASSMENT",
    "HARM_CATEGORY_HATE_SPEECH",
    "HARM_CATEGORY_SEXUALLY_EXPLICIT",
    "HARM_CATEGORY_DANGEROUS_CONTENT",
)


def _safety_settings() -> list[dict]:
    mode = (os.environ.get("CHAT_VERTEX_SAFETY") or "off").strip().lower()
    if mode in ("default", "on", "platform"):
        return []
    return [{"category": c, "threshold": "BLOCK_NONE"} for c in _SAFETY_CATEGORIES]


async def _vertex_request(
    messages: list[dict],
    system_prompt: str,
    *,
    auth_file: str,
    model: str,
    proxy: str = "",
    timeout: float = 90.0,
    min_chars: int = 0,
    images: list[bytes] | None = None,
    thinking_level: str = "low",
    cache_scope: str = "",
    plain_text: bool = False,
    temperature: float | None = None,
    max_output: int | None = None,
    include_refs: bool = True,
) -> dict:
    started = time.time()

    if not auth_file or not Path(auth_file).is_file():
        return {"ok": False, "error": f"找不到凭据 {auth_file}", "elapsed": 0.0}

    try:
        _sa, project, location = _load_service_account(auth_file)
        token = await _get_token(auth_file, proxy)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"换 token 失败：{type(exc).__name__}: {exc}",
            "elapsed": time.time() - started,
        }

    host = _vertex_host(location)
    url = (
        f"https://{host}/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{model}:generateContent"
    )

    from .llm import build_reply_schema

    explicit_cache = supports_explicit_cache(model)
    cfg: dict = {
        "maxOutputTokens": int(max_output) if max_output else 4096,
    }
    if not plain_text:
        cfg["responseMimeType"] = "application/json"
        # Schema 始终保持固定不变，彻底避免因字数档位切换使隐式/显式前缀失效
        cfg["responseSchema"] = _to_vertex_schema(build_reply_schema(0)["schema"])
    if temperature is not None and explicit_cache:
        cfg["temperature"] = max(0.0, min(2.0, float(temperature)))
    if thinking_level:
        cfg["thinkingConfig"] = {"thinkingLevel": thinking_level}

    safety = _safety_settings()
    history_len = max(0, len(messages) - 1)
    boundary = _session_boundary(history_len) if (cache_scope and explicit_cache and include_refs) else 0
    cache_name = ""

    if boundary:
        used_boundary = 0
        for candidate in reversed(SESSION_BOUNDARIES):
            if candidate <= history_len:
                cache_name = _lookup_session_cache(
                    cache_scope, messages[:candidate], system_prompt, model
                )
                if cache_name:
                    used_boundary = candidate
                    break
        _schedule_session_cache(
            scope=cache_scope,
            prefix=messages[:boundary],
            persona=system_prompt,
            project=project,
            location=location,
            model=model,
            token=token,
            proxy=_cache_proxy(),
        )
        if cache_name:
            body = {
                "cachedContent": cache_name,
                "contents": _build_contents(messages[used_boundary:], "", images),
                "generationConfig": cfg,
            }
            if safety:
                body["safetySettings"] = safety

    if not explicit_cache:
        body = {
            "contents": _implicit_contents(
                messages, system_prompt, images, min_chars, include_refs=include_refs
            ),
            "generationConfig": cfg,
        }
        if safety:
            body["safetySettings"] = safety
    elif not cache_name:
        if include_refs:
            cache_name = _lookup_persona_cache(system_prompt, model)
            _schedule_persona_cache(
                project=project,
                location=location,
                model=model,
                persona=system_prompt,
                token=token,
                proxy=_cache_proxy(),
            )
        if cache_name:
            body = {
                "cachedContent": cache_name,
                "contents": _build_contents(messages, "", images),
                "generationConfig": cfg,
            }
        else:
            body = {
                "contents": _build_contents(messages, system_prompt, images),
                "generationConfig": cfg,
            }
        if safety:
            body["safetySettings"] = safety

    use_proxy = proxy or get_proxy()
    try:
        client = _get_client(use_proxy, timeout)
        resp = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=_request_timeout(timeout),
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"请求失败：{type(exc).__name__}: {str(exc)[:200]}",
            "elapsed": time.time() - started,
        }

    elapsed = time.time() - started
    cache_invalid = resp.status_code in (403, 404) or (
        resp.status_code == 400 and "Invalid resource state" in resp.text
    )
    if cache_invalid and cache_name:
        for k, v in list(_CACHE_REGISTRY.items()):
            if str(v.get("name")) == cache_name:
                _CACHE_REGISTRY.pop(k, None)
        _save_cache_registry()
        logger.warning("vertex: 缓存可能失效，已清除记录，改用人设缓存重试")
        persona_cache = _lookup_persona_cache(system_prompt, model) if include_refs else ""
        if include_refs and not persona_cache:
            _schedule_persona_cache(
                project=project,
                location=location,
                model=model,
                persona=system_prompt,
                token=token,
                proxy=_cache_proxy(),
            )
        body = {
            "contents": _build_contents(messages, "" if persona_cache else system_prompt, images),
            "generationConfig": cfg,
        }
        if persona_cache:
            body["cachedContent"] = persona_cache
        if safety:
            body["safetySettings"] = safety
        try:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
                timeout=_request_timeout(timeout),
            )
            elapsed = time.time() - started
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "error": f"请求失败：{type(exc).__name__}",
                "elapsed": time.time() - started,
            }

    if resp.status_code == 400 and safety and "safety" in (resp.text or "").lower():
        logger.warning("vertex: safetySettings 被拒，去掉安全设置后重试一次")
        body.pop("safetySettings", None)
        try:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
                timeout=_request_timeout(timeout),
            )
            elapsed = time.time() - started
        except Exception:  # noqa: BLE001
            pass

    if resp.status_code != 200:
        return {
            "ok": False,
            "error": f"HTTP {resp.status_code}: {resp.text[:250]}",
            "elapsed": elapsed,
        }

    try:
        data = resp.json()
        cands = data.get("candidates") or []
        if not cands:
            fb = data.get("promptFeedback") or {}
            reason = str(fb.get("blockReason") or "")
            detail = str(fb.get("blockReasonMessage") or "")
            if reason:
                return {
                    "ok": False,
                    "error": f"被安全策略拦截：{reason} {detail}"[:220],
                    "elapsed": elapsed,
                }
            return {"ok": False, "error": "没有 candidate（可能被安全策略拦了）", "elapsed": elapsed}
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(
            p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought")
        )
        if not text:
            finish = cands[0].get("finishReason") or "?"
            return {"ok": False, "error": f"返回内容为空（finish={finish}）", "elapsed": elapsed}
        usage_raw = data.get("usageMetadata") or {}
        usage = {
            "prompt_tokens": int(usage_raw.get("promptTokenCount") or 0),
            "completion_tokens": int(usage_raw.get("candidatesTokenCount") or 0),
            "_prompt_tokens": int(usage_raw.get("promptTokenCount") or 0),
            "_cached_tokens": int(usage_raw.get("cachedContentTokenCount") or 0),
        }
        finish = str(cands[0].get("finishReason") or "")
        return {
            "ok": True,
            "error": "",
            "text": text,
            "usage": usage,
            "elapsed": elapsed,
            "finish": finish,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"解析响应失败：{type(exc).__name__}: {exc}",
            "elapsed": elapsed,
        }


async def warmup(auth_file: str, model: str, persona: str) -> None:
    """启动预热：提前完成 OAuth2 token 换取，支持显式缓存的模型在后台建人设缓存。"""
    if not auth_file or not model or not Path(auth_file).is_file():
        return
    try:
        _, project, location = _load_service_account(auth_file)
        token = await _get_token(auth_file, get_proxy())
        if supports_explicit_cache(model):
            _schedule_persona_cache(
                project=project,
                location=location,
                model=model,
                persona=persona,
                token=token,
                proxy=get_proxy(),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"vertex: 预热暂不可用，聊天时重试：{type(exc).__name__}")
