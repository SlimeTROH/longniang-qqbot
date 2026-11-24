"""Read-only checks for a local OneBot v11 WebSocket endpoint."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException


class OneBotProbeError(RuntimeError):
    pass


def local_endpoint(url: str) -> str:
    endpoint = urlsplit(url)
    if (
        endpoint.scheme != "ws" or endpoint.hostname not in {"127.0.0.1", "::1", "localhost"}
        or not endpoint.port or endpoint.username is not None or endpoint.query or endpoint.fragment
        or any(char.isspace() for char in url)
    ):
        raise ValueError("OneBot 地址必须是带端口的本机 ws 地址")
    return url


async def check_onebot(
    url: str, token: str, account: str, *, implementation: str = "", timeout: float = 6.0
) -> None:
    """Confirm the configured account is online before using the endpoint."""
    try:
        local_endpoint(url)
    except ValueError as exc:
        raise OneBotProbeError("OneBot 地址无效") from exc
    if not account or not token:
        raise OneBotProbeError("需要配置机器人的 QQ 号和 OneBot 访问令牌")

    async def probe() -> None:
        async with connect(
            url, additional_headers={"Authorization": f"Bearer {token}"}, proxy=None,
            open_timeout=timeout, close_timeout=1, max_size=256 * 1024,
        ) as socket:
            async def read(action: str) -> dict:
                echo = uuid4().hex
                await socket.send(json.dumps({"action": action, "params": {}, "echo": echo}))
                while True:
                    frame = json.loads(await socket.recv())
                    if not isinstance(frame, dict) or frame.get("echo") != echo:
                        continue
                    if frame.get("retcode") != 0 or not isinstance(frame.get("data"), dict):
                        raise OneBotProbeError(f"OneBot 未完成 {action}")
                    return frame["data"]

            if implementation:
                version = await read("get_version_info")
                if implementation.casefold() not in str(version.get("app_name", "")).casefold():
                    raise OneBotProbeError("目标端口未报告预期的协议端")
                if str(version.get("protocol_version")).casefold() not in {"v11", "11"}:
                    raise OneBotProbeError("目标端口未报告 OneBot v11")
            status = await read("get_status")
            if status.get("online") is not True or status.get("good") is False:
                raise OneBotProbeError("目标 QQ 还没登录")
            profile = await read("get_login_info")
            if str(profile.get("user_id")) != account:
                raise OneBotProbeError("目标 QQ 号与机器人配置不一致")

    try:
        await asyncio.wait_for(probe(), timeout=timeout)
    except (asyncio.TimeoutError, OSError, ValueError, WebSocketException) as exc:
        raise OneBotProbeError(f"读取 OneBot 状态失败（{type(exc).__name__}）") from exc
