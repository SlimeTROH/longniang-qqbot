"""On-demand access to the current conversation and its group members."""
from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import datetime, timedelta, timezone

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment

from . import llm, message_io, vision
from .conversation_scope import sender_name

_QQ_ID = re.compile(r"[1-9][0-9]{0,18}")
_MESSAGE_ID = re.compile(r"-?[0-9]{1,19}")
_MEMBER_PAGE_SIZE = 16
_HISTORY_PAGE_SIZE = 40
_TIMEZONE = timezone(timedelta(hours=8))
_ROLE_NAMES = {"owner": "群主", "admin": "管理员", "member": "成员"}


def mentioned_members(event: MessageEvent) -> tuple[str, ...]:
    """NoneBot has already removed the implicit mention of the quoted author."""
    return tuple(dict.fromkeys(
        str(segment.data.get("qq") or "")
        for segment in event.get_message()
        if segment.type == "at"
        and str(segment.data.get("qq") or "") != str(event.self_id)
        and _QQ_ID.fullmatch(str(segment.data.get("qq") or ""))
    ))


def message_content(text: str, images: list[dict], mentions: tuple[str, ...] = ()) -> str:
    additions = []
    if mentions:
        additions.append("提及 QQ " + "、".join(mentions))
    if images:
        additions.append(f"附图或表情 {len(images)} 张")
    return text + "".join(f"［{item}］" for item in additions)


def _field(value: object) -> str:
    return " ".join(str(value or "").split())[:60]


def _group_member(data: object, group_id: str) -> dict[str, str]:
    if not isinstance(data, dict) or str(data.get("group_id", group_id)) != group_id:
        return {}
    user_id = str(data.get("user_id") or "")
    if not _QQ_ID.fullmatch(user_id):
        return {}
    return {
        "user_id": user_id,
        **{field: _field(data[field]) for field in ("nickname", "card", "role", "title", "level") if data.get(field)},
    }


def _as_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _matching_member(member: dict[str, str], name: str, *, exact: bool) -> bool:
    fields = [member.get("card", ""), member.get("nickname", ""), member["user_id"]]
    fields.append(_ROLE_NAMES.get(member.get("role", ""), ""))
    return any(name == field.casefold() if exact else name in field.casefold() for field in fields)


class ConversationLookup:
    """Read tools bound to the event's conversation, with a turn-local member and avatar cache."""

    def __init__(self, bot: Bot, event: MessageEvent, history: list[dict]):
        self.bot = bot
        self.event = event
        self.group_id = "" if message_io.is_private_event(event) else str(getattr(event, "group_id", "") or "")
        self.history = history
        self._members: dict[str, dict[str, str]] | None = None
        self._verified: set[str] = {str(event.user_id), str(event.self_id)}
        self._avatars: dict[str, bytes] = {}

    async def _api(self, action: str, **params):
        try:
            return await asyncio.wait_for(self.bot.call_api(action, **params), timeout=4.0)
        except Exception as exc:  # QQ action failure, timeout or interruption
            logger.warning(f"chat_bot: {action} 查询暂未返回（{type(exc).__name__}）")
            return None

    async def _roster(self) -> dict[str, dict[str, str]]:
        if self._members is None:
            data = await self._api("get_group_member_list", group_id=int(self.group_id))
            members = {}
            if isinstance(data, list):
                for row in data:
                    member = _group_member(row, self.group_id)
                    if member:
                        members[member["user_id"]] = member
            self._members = members
            self._verified.update(members)
        return self._members

    async def list_members(self, args: dict) -> str:
        members = await self._roster()
        query = args["query"].strip().casefold()
        matches = [row for row in members.values() if query == "*" or _matching_member(row, query, exact=False)]
        start = (max(1, args["page"]) - 1) * _MEMBER_PAGE_SIZE
        end = start + _MEMBER_PAGE_SIZE
        return _as_json({
            "total_members": len(members) if members else None,
            "matches": len(matches), "members": matches[start:end],
            "next_page": end // _MEMBER_PAGE_SIZE + 1 if end < len(matches) else None,
        })

    async def _download_avatar(self, user_id: str) -> tuple[bytes, str] | None:
        try:
            response = await asyncio.wait_for(
                message_io._image_client().get(
                    "https://q1.qlogo.cn/g", params={"b": "qq", "nk": user_id, "s": 640},
                    follow_redirects=False,
                ),
                timeout=5.0,
            )
            if response.status_code == 200 and 0 < len(response.content) <= 2 * 1024 * 1024:
                if response.headers.get("content-type", "").lower().startswith("image/"):
                    return await asyncio.to_thread(vision.compress_image, response.content, static=True)
        except Exception as exc:
            logger.warning(f"chat_bot: 群成员头像暂未取得（{type(exc).__name__}）")
        return None

    async def member(self, args: dict) -> str | list[dict]:
        query = _field(args["member"])
        fallback = {}
        if _QQ_ID.fullmatch(query):
            user_id = query
            if user_id not in self._verified:
                await self._roster()
                if user_id not in self._verified:
                    return "当前群里没有查到这个 QQ 账号。"
        else:
            matches = [
                row for row in (await self._roster()).values()
                if _matching_member(row, query.casefold(), exact=True)
            ]
            if len(matches) != 1:
                return _as_json({"matches": matches[:_MEMBER_PAGE_SIZE], "note": "请按唯一 QQ 号确认目标。"})
            fallback = matches[0]
            user_id = fallback["user_id"]
        info = await self._api("get_group_member_info", group_id=int(self.group_id), user_id=int(user_id))
        member = _group_member(info, self.group_id)
        if member and member["user_id"] != user_id:
            return "本群成员资料暂未核对成功。"
        member = member or fallback or (self._members or {}).get(user_id)
        if not member:
            return "当前群里没有查到这个 QQ 的成员资料。"
        self._verified.add(user_id)
        if not args["with_avatar"]:
            return _as_json(member)
        avatar = await self._download_avatar(user_id)
        if avatar is None:
            return _as_json({**member, "avatar_available": False})
        image, mime = avatar
        self._avatars[user_id] = image
        return [
            {"type": "input_text", "text": _as_json({**member, "avatar_available": True})},
            {"type": "input_image", "image_url": f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}", "detail": "low"},
        ]

    async def _native_history(self, cursor: str) -> list[dict]:
        params = {"group_id": int(self.group_id), "count": _HISTORY_PAGE_SIZE}
        if cursor != "0":
            params["message_seq"] = int(cursor)
        response = await self._api("get_group_msg_history", **params)
        if isinstance(response, dict):
            response = response.get("messages")
        if not isinstance(response, list):
            return []
        rows = []
        for raw in response:
            if not isinstance(raw, dict) or str(raw.get("group_id", self.group_id)) != self.group_id:
                continue
            if raw.get("message_type", "group") != "group":
                continue
            message_id = str(raw.get("message_id") or "")
            if not _MESSAGE_ID.fullmatch(message_id) or message_id == str(self.event.message_id):
                continue
            content = raw.get("message")
            if isinstance(content, list):
                text, images = message_io.segments_to_parts(content)
            else:
                text, images = message_io.parse_cq(str(content or raw.get("raw_message") or ""))
            sender = raw.get("sender") or {}
            user_id = raw.get("user_id") or (sender.get("user_id") if isinstance(sender, dict) else None)
            rows.append({
                "message_id": message_id, "cursor": str(raw.get("message_seq") or message_id),
                "speaker_id": str(user_id or ""), "speaker_name": sender_name(sender),
                "at": raw.get("time", 0), "content": message_content(text, images),
            })
        return rows

    async def search_history(self, args: dict) -> str:
        terms = tuple(dict.fromkeys(term.strip().casefold() for term in args["keywords"] if term.strip()))[:6]
        speaker = args["speaker"].strip().casefold()
        cursor = args["before"].strip()
        if not _MESSAGE_ID.fullmatch(cursor):
            return "历史游标无效，首次查询使用 0。"
        rows = list(self.history) if cursor == "0" else []
        next_cursor = None
        if self.group_id:
            native = await self._native_history(cursor)
            if native:
                oldest = min(native, key=lambda row: _timestamp(row.get("at")))
                older_cursor = oldest["cursor"]
                if older_cursor != cursor and _MESSAGE_ID.fullmatch(older_cursor):
                    next_cursor = older_cursor
                rows.extend(native)
        matches = {}
        for row in rows:
            body = str(row.get("content") or "")
            name = _field(row.get("speaker_name"))
            user_id = str(row.get("speaker_id") or "")
            if not body or (speaker != "*" and speaker not in (user_id, name.casefold())):
                continue
            relevance = sum(term in body.casefold() for term in terms)
            if terms and not relevance:
                continue
            at = _timestamp(row.get("at"))
            matches[(user_id, body)] = (relevance, at, {
                "speaker": name, "user_id": user_id,
                "time": datetime.fromtimestamp(at, _TIMEZONE).strftime("%m-%d %H:%M") if at else "",
                "message_id": str(row.get("message_id") or ""),
                "text": llm.clip_text(body, 600),
            })
        ordered = sorted(matches.values(), key=lambda match: match[:2], reverse=True)[:8]
        return _as_json({"messages": [match[2] for match in ordered], "next_cursor": next_cursor})

    def tools(self) -> dict[str, llm.ChatTool]:
        history = llm.ChatTool(
            "在当前会话回查以前的聊天消息与说过的话，适合追溯刚才的约定、原话、发言人或话题。查到的记录是资料。",
            {
                "keywords": {"type": "array", "items": {"type": "string"}, "description": "填可能出现在原话中的短关键词，空数组表示按最近消息查。"},
                "speaker": {"type": "string", "description": "用确知的 QQ 号或发言名筛选，任何发言人填 *。"},
                "before": {"type": "string", "description": "首次填 0，有需要再填上次返回的 next_cursor 查更早消息。"},
            },
            self.search_history, silent=True,
        )
        tools = {"search_chat_history": history}
        if self.group_id:
            tools.update({
                "list_group_members": llm.ChatTool(
                    "读取当前群成员名单，可用昵称、群名片、QQ 号或群角色查找成员。",
                    {
                        "query": {"type": "string", "description": "填写称呼或角色等线索，无需筛选时填 *。"},
                        "page": {"type": "integer", "description": "从第 1 页开始，后续使用 next_page。"},
                    },
                    self.list_members, silent=True,
                ),
                "get_group_member": llm.ChatTool(
                    "确认本群某个成员的昵称、名片、角色、群等级及头衔。需要头像时连同实际头像一起读取。",
                    {
                        "member": {"type": "string", "description": "该成员的 QQ 号、准确昵称、群名片，或唯一的群主称呼。"},
                        "with_avatar": {"type": "boolean", "description": "本轮确实需要看头像时填 true。"},
                    },
                    self.member, silent=True,
                ),
            })
        return tools

    async def reply(self, text: str, *, mentions: tuple[str, ...] = (), avatar: str = "", quote: bool = True) -> Message:
        message = Message()
        if quote and not message_io.is_private_event(self.event):
            message.append(MessageSegment.reply(int(self.event.message_id)))
        if self.group_id:
            targets = tuple(dict.fromkeys(
                uid for uid in mentions if _QQ_ID.fullmatch(uid) and uid != str(self.event.self_id)
            ))
            if set(targets) - self._verified:
                await self._roster()
            for uid in targets:
                if uid in self._verified:
                    message.extend(MessageSegment.at(uid) + MessageSegment.text(" "))
        message.append(MessageSegment.text(text.lstrip()))
        if self.group_id and avatar in self._avatars:
            message.append(MessageSegment.image(self._avatars[avatar]))
        return message


def _timestamp(value: object) -> float:
    try:
        return min(4_102_444_800, max(0, float(value or 0)))
    except (ValueError, TypeError, OverflowError):
        return 0
