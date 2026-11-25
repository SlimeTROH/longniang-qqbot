"""OneBot V11 消息解析、引用与图片提取（直连国内 CDN）、回复拼装与续写去重工具。"""
from __future__ import annotations

import asyncio
import difflib
import re
import urllib.request
import weakref
from pathlib import Path
from typing import Optional

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply
from nonebot.compat import model_dump
from nonebot.rule import Rule

from .conversation_scope import quote_allowed as _quote_allowed, sender_name, speaker_label

FACE_TYPES = ("mface", "face")
_CQ_REPLY_RE = re.compile(r"\[(?:CQ:)?reply[^\]]*?id=([^,\]]+)")
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")
_MESSAGE_ID = re.compile(r"-?[0-9]{1,20}")
_MARKDOWN_IMAGE_RE = re.compile(
    r'''(?<!\\)!\[(?:\\.|[^\[\]\\\r\n])*\]\([ \t]*(?:<([^<>\s]+)>|([^<>()\s]+))'''
    r'''(?:[ \t]+(?:"[^"\r\n]*"|'[^'\r\n]*'))?[ \t]*\)'''
)
_QQ_MARKDOWN_IMAGE_HOSTS = frozenset({
    "qqbot.ugcimg.cn", "multimedia.nt.qq.com.cn", "gchat.qpic.cn", "c2cpicdw.qpic.cn",
})
_MAX_MARKDOWN_IMAGE_BYTES = 20 * 1024 * 1024

# 图片下载复用直连连接池（trust_env=False，避免国内 QQ 图床绕行海外代理）
_IMAGE_CLIENTS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _image_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _IMAGE_CLIENTS.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=25.0, write=10.0, pool=5.0),
            trust_env=False,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5, keepalive_expiry=15.0),
        )
        _IMAGE_CLIENTS[loop] = client
    return client


async def close_image_clients() -> None:
    client = _IMAGE_CLIENTS.pop(asyncio.get_running_loop(), None)
    if client is not None and not client.is_closed:
        await client.aclose()


def is_private_event(event: MessageEvent) -> bool:
    return getattr(event, "message_type", "") == "private"


def face_to_text(data: dict) -> str:
    summary = str(data.get("summary") or "").strip()
    if summary:
        return summary
    for key in ("text", "name", "face_text"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return "[表情]"


def face_url(data: dict) -> str:
    for key in ("url", "file"):
        value = str(data.get(key) or "").strip()
        if value.startswith(("https://", "http://")):
            return value
    return ""


def _qq_markdown_image_url(value: str) -> str:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        return ""
    if (url.scheme == "https" and url.host in _QQ_MARKDOWN_IMAGE_HOSTS
            and not url.userinfo and url.port in (None, 443)):
        return str(url)
    return ""


def _collect_part(kind: str, data: dict, texts: list[str], images: list[dict]) -> None:
    if kind == "text":
        texts.append(str(data.get("text") or ""))
    elif kind == "image":
        images.append(dict(data))
    elif kind == "markdown":
        content = data.get("content")
        if not isinstance(content, str):
            return
        seen = {part.get("url") for part in images if isinstance(part.get("url"), str)}
        for match in _MARKDOWN_IMAGE_RE.finditer(content):
            url = _qq_markdown_image_url(match[1] or match[2])
            if url and url not in seen:
                images.append({"url": url, "_markdown_image": True})
                seen.add(url)
    elif kind in FACE_TYPES:
        texts.append(f" {face_to_text(data)} ")
        url = face_url(data)
        if url or data.get("file") or data.get("file_id"):
            images.append({**data, "url": url, "_is_sticker": True})


def extract_message(event: MessageEvent) -> tuple[str, list[dict]]:
    """把消息拆成 (纯文本, 图片段列表)。"""
    texts: list[str] = []
    images: list[dict] = []
    for seg in event.get_message():
        _collect_part(seg.type, seg.data, texts, images)
    return "".join(texts).strip(), images


def addressed_to_bot(event: MessageEvent) -> bool:
    self_id = str(event.self_id)
    segments = getattr(event, "original_message", None) or event.get_message()
    sender = getattr(getattr(event, "reply", None), "sender", None)
    quotes_bot = not is_private_event(event) and str(getattr(sender, "user_id", "")) == self_id
    implicit_at = -1
    if quotes_bot:
        # NoneBot treats a mention of the quoted author immediately after the reply as part of that reply.
        reply_index = next((index for index, seg in enumerate(segments) if seg.type == "reply"), -1)
        following = reply_index + 1
        if reply_index >= 0 and following < len(segments):
            seg = segments[following]
            if seg.type == "at" and str(seg.data.get("qq")) == self_id:
                implicit_at = following
    mentions = {
        str(seg.data.get("qq") or "") for index, seg in enumerate(segments)
        if seg.type == "at" and index != implicit_at
    }
    if self_id in mentions:
        return True
    if quotes_bot and mentions - {"", "all"}:
        return False
    return event.is_tome()


def clean_trigger_text(text: str, trigger_names: list[str]) -> str:
    out = text
    for name in trigger_names:
        if name and out.startswith(name):
            out = out[len(name) :].lstrip("，,、:： ")
    return out.strip()


def quote_id(event: MessageEvent) -> Optional[str]:
    try:
        for seg in getattr(event, "original_message", None) or event.get_message():
            if seg.type == "reply":
                mid = str(seg.data.get("id") or "").strip()
                if mid:
                    return mid
    except Exception:  # noqa: BLE001
        pass
    raw = str(getattr(event, "raw_message", "") or "")
    if raw:
        m = _CQ_REPLY_RE.search(raw)
        if m:
            return m.group(1).strip()
    return None


def _unescape_cq(value: str) -> str:
    return (
        value.replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&amp;", "&")
    )


def parse_cq(text: str) -> tuple[str, list[dict]]:
    texts: list[str] = []
    images: list[dict] = []
    pos = 0
    for match in _CQ_RE.finditer(text):
        if match.start() > pos:
            texts.append(text[pos : match.start()])
        cq_type = match.group(1)
        params = match.group(2) or ""
        data: dict = {}
        for pair in params.split(","):
            pair = pair.strip()
            if "=" in pair:
                key, _, value = pair.partition("=")
                data[key.strip()] = _unescape_cq(value.strip())
        _collect_part(cq_type, data, texts, images)
        pos = match.end()
    if pos < len(text):
        texts.append(text[pos:])
    return "".join(texts).strip(), images


def segments_to_parts(segments: list) -> tuple[str, list[dict]]:
    texts: list[str] = []
    images: list[dict] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        sdata = seg.get("data") or {}
        if isinstance(sdata, dict):
            _collect_part(seg.get("type"), sdata, texts, images)
    return "".join(texts).strip(), images


async def fetch_quote(bot: Bot, event: MessageEvent) -> tuple[str, list[dict], str]:
    mid = quote_id(event)
    if not mid:
        return "", [], ""
    logger.info(f"chat_bot: 检测到引用消息 id={mid}")
    try:
        resp = await bot.call_api("get_msg", message_id=mid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"chat_bot: 获取引用消息失败 {exc!r}")
        return "", [], ""

    data = resp if isinstance(resp, dict) else {}
    if not _quote_allowed(event, data):
        logger.warning("chat_bot: 已忽略不属于当前群的引用消息")
        return "", [], ""

    body, images = _message_parts(data)
    sender = data.get("sender") or {}
    uid = data.get("user_id") or (sender.get("user_id") if isinstance(sender, dict) else None)
    author = speaker_label(str(uid), sender_name(sender)) if str(uid).isdigit() else "发言人未知"
    return body, images, author


def _message_parts(data: dict) -> tuple[str, list[dict]]:
    segments = data.get("message")
    if isinstance(segments, list):
        body, images = segments_to_parts(segments)
        if body or images:
            return body, images
    for key in ("raw_message", "message"):
        raw = data.get(key)
        if isinstance(raw, str) and raw:
            return parse_cq(raw)
    return "", []


def _parent_message_id(data: dict) -> str:
    message = data.get("message")
    if isinstance(message, list) and message:
        for part in message:
            if isinstance(part, dict) and part.get("type") == "reply":
                fields = part.get("data")
                return str(fields.get("id") or "").strip() if isinstance(fields, dict) else ""
        return ""
    raw = data.get("raw_message") or message
    if isinstance(raw, str):
        for match in _CQ_RE.finditer(raw):
            if match[1] == "reply":
                for item in match[2].split(","):
                    key, _, value = item.partition("=")
                    if key.strip() == "id":
                        return value.strip()
    return ""


async def fetch_quoted_reply_images(bot: Bot, event: MessageEvent) -> list[dict]:
    """Recover images behind at most two of this bot's replies in the current group."""
    mid = quote_id(event) or ""
    if (
        not getattr(event, "group_id", None) or is_private_event(event)
        or str(bot.self_id) != str(event.self_id) or not _MESSAGE_ID.fullmatch(mid)
    ):
        return []

    reply = getattr(event, "reply", None)
    data = model_dump(reply) if isinstance(reply, Reply) and str(reply.message_id) == mid else None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3.0
    seen = {mid, str(getattr(event, "message_id", ""))}
    for hop in range(3):
        if data is None:
            try:
                data = await asyncio.wait_for(bot.call_api("get_msg", message_id=mid), deadline - loop.time())
            except Exception as exc:  # noqa: BLE001 - backend errors end this optional lookup
                logger.debug(f"chat_bot: 历史引用补图未完成（{type(exc).__name__}）")
                return []
        if not isinstance(data, dict) or not _quote_allowed(event, data):
            return []
        _, images = _message_parts(data)
        if images:
            return images if hop else []
        if hop == 2:
            return []
        sender = data.get("sender")
        if not isinstance(sender, dict) or str(sender.get("user_id", "")) != str(bot.self_id):
            return []
        mid = _parent_message_id(data)
        if mid in seen or not _MESSAGE_ID.fullmatch(mid):
            return []
        seen.add(mid)
        data = None
    return []


async def _fetch_markdown_image(value: str) -> Optional[bytes]:
    url = _qq_markdown_image_url(value)
    if not url:
        return None
    try:
        async with _image_client().stream("GET", url, follow_redirects=False) as response:
            content_type = response.headers.get("content-type", "").lower()
            if response.status_code != 200 or not content_type.startswith("image/"):
                logger.warning(f"chat_bot: QQ Markdown 图片不可读 HTTP {response.status_code}")
                return None
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > _MAX_MARKDOWN_IMAGE_BYTES:
                    logger.warning("chat_bot: QQ Markdown 图片超过下载上限")
                    return None
                content.extend(chunk)
            return bytes(content) or None
    except httpx.HTTPError as exc:
        logger.warning(f"chat_bot: QQ Markdown 图片下载失败（{type(exc).__name__}）")
        return None


async def fetch_image(seg: dict, bot: Bot | None = None) -> Optional[bytes]:
    """获取图片字节（直连 HTTP → 本地文件 → OneBot get_image）。"""
    if seg.get("_markdown_image"):
        return await _fetch_markdown_image(str(seg.get("url") or ""))

    file_id = str(seg.get("file") or seg.get("file_id") or "").strip()
    url = face_url(seg)
    if file_id == url:
        file_id = str(seg.get("file_id") or "").strip()

    if url.startswith("http"):
        try:
            resp = await _image_client().get(url)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"chat_bot: 下载图片失败 {exc!r}")

    if file_id:
        candidate = file_id[7:] if file_id.startswith("file://") else file_id
        try:
            p = Path(urllib.request.url2pathname(candidate))
            if p.is_file():
                return p.read_bytes()
        except Exception:  # noqa: BLE001
            pass

    if bot is not None and file_id:
        resp = None
        try:
            resp = await bot.call_api("get_image", file=file_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"chat_bot: get_image 调用失败 {exc!r}")
        if isinstance(resp, dict):
            local = str(resp.get("file") or "").strip()
            if local:
                try:
                    p = Path(local)
                    if p.is_file():
                        return p.read_bytes()
                except Exception:  # noqa: BLE001
                    pass
            url2 = str(resp.get("url") or "").strip()
            if url2.startswith("http"):
                try:
                    r2 = await _image_client().get(url2)
                    if r2.status_code == 200 and r2.content:
                        return r2.content
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"chat_bot: get_image 后下载失败 {exc!r}")

    logger.warning(f"chat_bot: 取不到图片内容 file={file_id!r} url={url[:60]!r}")
    return None


def reply_to(event: MessageEvent, text: str) -> Message | str:
    """私聊回纯文本，群聊引用原消息回复。"""
    body = str(text or "").lstrip("\n").lstrip()
    if is_private_event(event):
        return body
    if not body:
        body = "……"
    try:
        mid = getattr(event, "message_id", None)
        if mid is not None:
            return MessageSegment.reply(int(mid)) + MessageSegment.text(body)
    except Exception:  # noqa: BLE001
        pass
    return body


async def send_as_forward(
    bot: Bot, event: MessageEvent, chunks: list[list[str]], title: str, char_name: str = "若娜瓦"
) -> bool:
    nodes = [
        {
            "type": "node",
            "data": {
                "name": f"{char_name} · {title}",
                "uin": str(bot.self_id),
                "content": [{"type": "text", "data": {"text": "\n".join(chunk)}}],
            },
        }
        for chunk in (chunks or [[""]])
    ]
    payload: dict = {"messages": nodes}
    if is_private_event(event):
        payload["user_id"] = event.user_id
    else:
        payload["group_id"] = event.group_id
    try:
        await bot.call_api("send_forward_msg", **payload)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"chat_bot: 合并转发失败（{exc!r}），退回普通消息")
        return False


def cmd_boundary_match(text: str, names: tuple[str, ...]) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    for name in names:
        if name and t.startswith(name):
            rest = t[len(name) :]
            if not rest or rest[0].isspace():
                return True
    return False


def cmd_boundary(*names: str) -> Rule:
    name_set = tuple(n for n in names if n)

    async def _checker(event: MessageEvent) -> bool:
        return cmd_boundary_match(event.get_plaintext(), name_set)

    return Rule(_checker)


def _map_non_space_to_index(text: str, non_space_count: int) -> int:
    count = 0
    for idx, ch in enumerate(text):
        if ch.isspace():
            continue
        count += 1
        if count >= non_space_count:
            return idx + 1
    return len(text)


def strip_duplicate(base: str, extra: str) -> str:
    base = (base or "").strip()
    extra = (extra or "").strip()
    if not extra:
        return ""
    if not base:
        return extra

    base_c = re.sub(r"\s+", "", base)
    extra_c = re.sub(r"\s+", "", extra)
    if not extra_c or extra_c in base_c:
        return ""
    if not base_c:
        return extra
    if base_c in extra_c:
        return extra[_map_non_space_to_index(extra, len(base_c)) :].strip()

    max_len = min(len(base_c), len(extra_c))
    overlap = 0
    for size in range(max_len, 5, -1):
        if base_c.endswith(extra_c[:size]):
            overlap = size
            break
    if overlap <= 0:
        return extra
    return extra[_map_non_space_to_index(extra, overlap) :].strip()


def is_near_duplicate(base: str, extra: str, threshold: float = 0.65) -> bool:
    base = (base or "").strip()
    extra = (extra or "").strip()
    if not base or not extra:
        return False

    def split_sents(t: str) -> list[str]:
        return [s.strip() for s in re.split(r"[。！？…；\n]+", t) if s.strip()]

    base_sents = split_sents(base)
    extra_sents = split_sents(extra)
    if not base_sents or not extra_sents:
        return False

    hit = 0
    for es in extra_sents:
        best = max(
            (difflib.SequenceMatcher(None, es, bs).ratio() for bs in base_sents),
            default=0.0,
        )
        if best >= threshold:
            hit += 1
    return hit / len(extra_sents) >= 0.6
