"""Keep this bot on its configured QQ account and ignore replayed messages."""
from __future__ import annotations

import asyncio
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from nonebot.adapters.onebot.v11 import Adapter as OneBotAdapter
from nonebot.adapters.onebot.v11 import ActionFailed, Bot, Event, Message, MessageEvent, MessageSegment
from nonebot.drivers import Driver, Request, Response, WebSocket
from nonebot.log import logger

from qq_commands import is_affection_history, is_image_quota_query


class Adapter(OneBotAdapter):
    _replay_window = 120.0
    _replay_capacity = 4096
    # Match the OneBot V11 spec's default interval for rate-limited calls.
    _send_interval = 0.5
    _send_apis = frozenset((
        "send_msg", "send_private_msg", "send_group_msg",
        "send_forward_msg", "send_private_forward_msg", "send_group_forward_msg",
        "upload_private_file", "upload_group_file",
        "send_poke", "friend_poke", "group_poke",
    ))
    _private_send_apis = frozenset((
        "send_private_msg", "send_private_forward_msg", "upload_private_file", "friend_poke",
    ))

    def __init__(self, driver: Driver, *, private_admins: Callable[[], set[str]] | None = None, **kwargs: Any):
        self._account = str(
            os.environ.get("QQBOT_ACCOUNT")
            or getattr(driver.config, "qqbot_account", "")
            or ""
        ).strip()
        self._warned_wrong_account = False
        self._private_admins = private_admins
        self._messages: OrderedDict[tuple[int, str, int, int], float] = OrderedDict()
        self._send_lock = asyncio.Lock()
        self._next_send_time = 0.0
        super().__init__(driver, **kwargs)

    def _accepts_account(self, account: object) -> bool:
        return not self._account or str(account) == self._account

    def _allows_private_peer(self, user_id: object) -> bool:
        return self._private_admins is None or str(user_id) in self._private_admins()

    def _check_inbound_account(self, account: object) -> bool:
        if self._accepts_account(account):
            return True
        if not self._warned_wrong_account:
            logger.warning("OneBot 登录账号与 QQBOT_ACCOUNT 不符，已忽略该账号的事件")
            self._warned_wrong_account = True
        return False

    async def _handle_http(self, request: Request) -> Response:
        self_id = request.headers.get("x-self-id")
        if self_id and not self._check_inbound_account(self_id):
            return Response(403, content="OneBot account mismatch")
        return await super()._handle_http(request)

    async def _handle_ws(self, websocket: WebSocket) -> None:
        self_id = websocket.request.headers.get("x-self-id")
        if self_id and not self._check_inbound_account(self_id):
            await websocket.close(1008, "OneBot account mismatch")
            return
        await super()._handle_ws(websocket)

    async def _call_api(self, bot: Bot, api: str, **data: Any) -> Any:
        if not self._accepts_account(bot.self_id):
            raise ActionFailed(message="OneBot account mismatch")
        action = api.removesuffix("_async").removesuffix("_rate_limited")
        is_private = action in self._private_send_apis or (
            action in ("send_msg", "send_forward_msg", "send_poke")
            and (data.get("message_type") == "private" or not data.get("group_id"))
        )
        if is_private and not self._allows_private_peer(data.get("user_id")):
            raise ActionFailed(message="Private messages are restricted to administrators")
        return await self._call_paced_api(bot, api, action, **data)

    async def send_affection_history(self, bot: Bot, event: MessageEvent, messages: list[str]) -> None:
        """Only send history to the user who invoked that command."""
        if (
            not self._accepts_account(bot.self_id)
            or str(event.self_id) != str(bot.self_id)
            or not is_affection_history(event.get_plaintext())
        ):
            raise ActionFailed(message="Affection history requires a matching request")
        group_id = getattr(event, "group_id", None)
        if group_id and event.message_type == "group":
            friends = await self._call_api(bot, "get_friend_list")
            if any(str(friend.get("user_id")) == str(event.user_id) for friend in friends):
                group_id = None
        data: dict[str, Any] = {"user_id": event.user_id}
        if group_id:
            data["group_id"] = group_id
        for text in messages:
            message = Message(MessageSegment.text(text))
            try:
                await self._call_paced_api(bot, "send_private_msg", "send_private_msg", message=message, **data)
            except ActionFailed as exc:
                # SnowLuma rejects this before sending. Try the regular private route once.
                if "group_id" not in data or "no such temp session" not in str(exc):
                    raise
                data.pop("group_id")
                await self._call_paced_api(bot, "send_private_msg", "send_private_msg", message=message, **data)

    async def send_image_quota(self, bot: Bot, event: MessageEvent, text: str) -> None:
        if (
            not self._accepts_account(bot.self_id)
            or str(event.self_id) != str(bot.self_id)
            or event.message_type != "private"
            or not is_image_quota_query(event.get_plaintext())
        ):
            raise ActionFailed(message="Image quota requires a matching private request")
        await self._call_paced_api(
            bot, "send_private_msg", "send_private_msg",
            message=Message(MessageSegment.text(text)), user_id=event.user_id,
        )

    async def _call_paced_api(self, bot: Bot, api: str, action: str, /, **data: Any) -> Any:
        if action in self._send_apis:
            async with self._send_lock:
                delay = self._next_send_time - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._next_send_time = time.monotonic() + self._send_interval
        return await super()._call_api(bot, api, **data)

    def json_to_event(self, json_data: Any) -> Event | None:
        # The base parser also delivers API responses to the waiting caller.
        event = super().json_to_event(json_data)
        if event is None or not self._check_inbound_account(event.self_id):
            return None
        is_private = getattr(event, "message_type", None) == "private" or (
            event.post_type == "notice" and hasattr(event, "user_id")
            and not getattr(event, "group_id", None)
        )
        if is_private and not self._allows_private_peer(getattr(event, "user_id", None)):
            if not isinstance(event, MessageEvent):
                return None
            text = event.get_plaintext()
            if not (is_affection_history(text) or is_image_quota_query(text)):
                return None
        if isinstance(event, MessageEvent) and event.message_id and self._is_replay(event):
            return None
        return event

    def _is_replay(self, event: MessageEvent) -> bool:
        key = (event.self_id, event.get_session_id(), event.message_id, event.time)
        now = time.monotonic()
        while self._messages and now - next(iter(self._messages.values())) >= self._replay_window:
            self._messages.popitem(last=False)
        if key in self._messages:
            return True
        self._messages[key] = now
        if len(self._messages) > self._replay_capacity:
            self._messages.popitem(last=False)
        return False
