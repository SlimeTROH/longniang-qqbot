"""Send QQ login alerts to a private ntfy subscription."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
import io
import json
import logging
import os
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit

import httpx
import qrcode
from dotenv import dotenv_values

from napcat_webui import WebUI
from onebot_probe import OneBotProbeError, check_onebot

POLL_SECONDS = 10
OFFLINE_SECONDS = 25
REFRESH_COOLDOWN = 15
ALERT_TOPIC = "qq-login"
REQUEST_TOPIC = "qq-login-refresh"
LOG = logging.getLogger("qq_watch")


def qr_url(status: dict) -> str:
    if status.get("loginPhase") != "waiting_qrcode" or status.get("qrLoginAccepted"):
        return ""
    url = str(status.get("qrcodeurl") or "")
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme == "https" and (host == "qq.com" or host.endswith(".qq.com")):
        return url
    return ""


def qr_png(url: str) -> bytes:
    out = io.BytesIO()
    qrcode.make(url, box_size=8, border=4).save(out, format="PNG")
    return out.getvalue()


class Ntfy:
    def __init__(
        self, client: httpx.AsyncClient, url: str, token: str, button_token: str,
        *, internal_url: str = "", qr_refresh: bool = True,
        refresh_body: str = "refresh", label: str = "QQ", sequence: str = "qq-login-state",
    ):
        base = urlsplit(url)
        if base.scheme != "https" or not base.netloc or base.username or base.query or base.fragment:
            raise ValueError("NTFY_URL must be a private HTTPS server URL")
        if not token or (qr_refresh and (not button_token or token == button_token)):
            raise ValueError("Set separate ntfy tokens for the service and refresh button")
        origin = urlunsplit(base).rstrip("/")
        api_origin = origin
        if internal_url:
            local = urlsplit(internal_url)
            if (local.scheme != "http" or local.hostname not in {"127.0.0.1", "::1", "localhost"}
                    or local.username is not None or local.query or local.fragment):
                raise ValueError("NTFY_INTERNAL_URL must be a loopback HTTP endpoint")
            api_origin = urlunsplit(local).rstrip("/")
        self.client = client
        self.alert_url = f"{api_origin}/{ALERT_TOPIC}"
        self.request_url = f"{api_origin}/{REQUEST_TOPIC}"
        self.auth = {"Authorization": f"Bearer {token}"}
        self.refresh_body = refresh_body
        self.label = label
        self.sequence = sequence
        self.action = json.dumps([{
            "action": "http",
            "label": "刷新二维码",
            "url": f"{origin}/{REQUEST_TOPIC}",
            "method": "POST",
            "headers": {"Authorization": f"Bearer {button_token}"},
            "body": refresh_body,
        }], ensure_ascii=True) if qr_refresh else ""
        self.request_time = int(time.time())
        self.request_ids: set[str] = set()

    async def requested(self) -> bool:
        if not self.action:
            return False
        response = await self.client.get(
            f"{self.request_url}/json",
            params={"poll": 1, "since": self.request_time},
            headers=self.auth,
        )
        response.raise_for_status()
        refresh = False
        for line in response.text.splitlines():
            event = json.loads(line)
            if event["event"] != "message" or event.get("message") != self.refresh_body:
                continue
            event_time, event_id = event["time"], event["id"]
            if event_time < self.request_time:
                continue
            if event_time > self.request_time:
                self.request_time = event_time
                self.request_ids.clear()
            if event_id not in self.request_ids:
                self.request_ids.add(event_id)
                refresh = refresh or time.time() - event_time < 300
        return refresh

    async def send(self, message: str, *, code: str = "", online: bool = False) -> None:
        headers = {
            **self.auth,
            "X-Title": (self.label + (" 已在线" if online else " 掉线")).encode("utf-8"),
            "X-Sequence-ID": self.sequence,
        }
        body = message.encode("utf-8")
        if not online:
            if self.action:
                headers["X-Actions"] = self.action
            headers["X-Priority"] = "4"
        if code:
            headers["X-Filename"] = "qq-login.png"
            headers["X-Message"] = body
            headers["Content-Type"] = "image/png"
            body = qr_png(code)
        response = await self.client.put(self.alert_url, content=body, headers=headers)
        response.raise_for_status()


class Watch:
    def __init__(
        self, web: WebUI | None, ntfy: Ntfy, *,
        onebot_check: Callable[[], Awaitable[None]] | None = None,
    ):
        self.web = web
        self.ntfy = ntfy
        self.onebot_check = onebot_check
        self.offline_since: float | None = None
        self.announced = False
        self.sent_code = False
        self.refresh_pending = False
        self.next_refresh = 0.0

    async def status(self) -> dict:
        if self.onebot_check is not None:
            try:
                await self.onebot_check()
            except OneBotProbeError as exc:
                LOG.warning("OneBot status unavailable: %s", exc)
                return {}
            return {"isLogin": True, "coreReady": True}
        assert self.web is not None
        try:
            return await self.web.call("CheckLoginStatus") or {}
        except (httpx.HTTPError, PermissionError, RuntimeError, ValueError) as exc:
            self.web.credential = ""
            LOG.warning("%s status unavailable (%s)", self.ntfy.label, type(exc).__name__)
            return {}

    async def refresh(self, status: dict) -> str:
        if self.web is None:
            return ""
        can_refresh = status.get("canRefreshQRcode", status.get("loginPhase") == "waiting_qrcode")
        if not can_refresh or status.get("qrLoginAccepted"):
            return ""
        previous = qr_url(status)
        try:
            await self.web.call("RefreshQRcode")
        except (httpx.HTTPError, PermissionError, RuntimeError, ValueError) as exc:
            LOG.warning("%s refresh failed (%s)", self.ntfy.label, type(exc).__name__)
            return ""
        for _ in range(5):
            latest = await self.status()
            code = qr_url(latest)
            if code and code != previous:
                return code
            if latest.get("isLogin") or latest.get("qrLoginAccepted"):
                return ""
            await asyncio.sleep(1)
        return ""

    async def tick(self) -> None:
        self.refresh_pending |= await self.ntfy.requested()
        status = await self.status()
        now = time.monotonic()
        if status.get("isLogin") and status.get("coreReady") and not status.get("isOffline"):
            if self.announced or self.refresh_pending:
                message = "QQ 已连上，扫码提醒已结束。" if self.web is not None else "QQ 已连上。"
                await self.ntfy.send(message, online=True)
            self.offline_since = None
            self.announced = self.sent_code = self.refresh_pending = False
            self.next_refresh = 0.0
            return

        if self.offline_since is None:
            self.offline_since = now
        code = qr_url(status)
        if self.refresh_pending and now >= self.next_refresh and status.get("canRefreshQRcode") is not False:
            self.next_refresh = now + REFRESH_COOLDOWN
            code = await self.refresh(status)
            if code:
                await self.ntfy.send("这是刚刷新的登录二维码，请用原 QQ 号扫码。", code=code)
                self.sent_code = True
            else:
                await self.ntfy.send(f"{self.ntfy.label} 暂时没有生成新二维码，可以稍后再点刷新。")
            self.announced = True
            self.refresh_pending = False
        elif now - self.offline_since >= OFFLINE_SECONDS:
            if code and not self.sent_code:
                await self.ntfy.send("QQ 需要重新登录。二维码过期后，点下方按钮刷新。", code=code)
                self.announced = self.sent_code = True
            elif not self.announced:
                message = (f"{self.ntfy.label} 连接异常，暂时没有登录二维码。" if self.web is not None
                           else "QQ 连接异常，请在 SnowLuma 所连接的 QQ 客户端查看登录。")
                await self.ntfy.send(message)
                self.announced = True


async def main() -> None:
    onebot_url = os.environ.get("QQ_WATCH_ONEBOT_URL", "")
    web = None
    onebot_check = None
    if onebot_url:
        config = dotenv_values(Path(__file__).resolve().parent / ".env")
        onebot_check = partial(
            check_onebot, onebot_url, config.get("ONEBOT_ACCESS_TOKEN") or "",
            config.get("QQBOT_ACCOUNT") or "",
        )
    else:
        path = Path(os.environ["NAPCAT_WEBUI_CONFIG"])
        web = WebUI(json.loads(path.read_text(encoding="utf-8-sig")))
    try:
        if web is not None and not web.token:
            raise ValueError("NapCat WebUI token is missing")
        async with httpx.AsyncClient(timeout=6, trust_env=False) as client:
            ntfy = Ntfy(
                client, os.environ["NTFY_URL"], os.environ["NTFY_WATCH_TOKEN"],
                os.environ.get("NTFY_REFRESH_BUTTON_TOKEN", ""),
                internal_url=os.environ.get("NTFY_INTERNAL_URL", ""),
                qr_refresh=web is not None,
            )
            watch = Watch(web, ntfy, onebot_check=onebot_check)
            LOG.info("QQ login watch started")
            while True:
                try:
                    await watch.tick()
                except (httpx.HTTPError, ValueError) as exc:
                    LOG.warning("Phone notification unavailable (%s)", type(exc).__name__)
                await asyncio.sleep(POLL_SECONDS)
    finally:
        if web is not None:
            await web.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
