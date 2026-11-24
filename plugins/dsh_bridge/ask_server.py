"""极简 HTTP 服务：接收 dsh-qq-bridge 插件转发的 ask_user_question 请求。

只处理 ``POST /ask``，协议：
    请求  {"questions": [...], "sessionId": "..."}
    响应  {"answers": [{"id": "...", "selected": ["..."], "custom": "..."}]}

用 asyncio 原生实现，避免引入框架；也不会干扰 OneBot 的 WS 连接。
"""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional

# (user_key, questions) -> answers
AskHandler = Callable[[str, list], Awaitable[list]]
# sessionId -> user_key
UserResolver = Callable[[Optional[str]], Optional[str]]

_REASONS = {
    200: "OK",
    404: "Not Found",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class AskHttpServer:
    def __init__(self, host: str, port: int, handler: AskHandler) -> None:
        self._host = host
        self._port = port
        self._handler = handler
        self._server: Optional[asyncio.AbstractServer] = None
        self._resolver: UserResolver = lambda _session_id: None

    def set_user_resolver(self, resolver: UserResolver) -> None:
        self._resolver = resolver

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle, self._host, self._port)

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------ 请求处理
    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin1").split()
            method = parts[0] if parts else ""
            path = parts[1] if len(parts) > 1 else ""

            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin1").partition(":")
                headers[key.strip().lower()] = value.strip()

            length = int(headers.get("content-length", "0") or 0)
            body = await reader.readexactly(length) if length > 0 else b""

            if method != "POST" or not path.startswith("/ask"):
                await self._respond(writer, 404, {"error": "not found"})
                return

            payload = json.loads(body.decode("utf-8")) if body else {}
            questions = payload.get("questions") or []
            session_id = payload.get("sessionId")
            user_key = self._resolver(session_id)
            if user_key is None:
                await self._respond(
                    writer, 503, {"error": "no active QQ user bound to this DSH session"}
                )
                return
            answers = await self._handler(user_key, questions)
            await self._respond(writer, 200, {"answers": answers})
        except Exception as exc:  # noqa: BLE001
            try:
                await self._respond(writer, 500, {"error": str(exc)})
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _respond(
        self, writer: asyncio.StreamWriter, status: int, payload: dict
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        reason = _REASONS.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(head + body)
        await writer.drain()
