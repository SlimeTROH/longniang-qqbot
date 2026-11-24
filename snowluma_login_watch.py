"""Deliver the separate SnowLuma QQ desktop's login QR to the existing ntfy phone."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import time

from dotenv import dotenv_values
import httpx

from onebot_probe import check_onebot, OneBotProbeError
from qq_watch import Ntfy, OFFLINE_SECONDS, Watch

DESKTOP_SOCKET = "/run/qq-snowluma-desktop.sock"
ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("snowluma_login_watch")


async def desktop_call(action: str) -> dict:
    async def exchange() -> dict:
        reader, writer = await asyncio.open_unix_connection(DESKTOP_SOCKET)
        try:
            writer.write((action + "\n").encode("ascii"))
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()

    try:
        result = await asyncio.wait_for(exchange(), timeout=18)
    except (asyncio.TimeoutError, OSError, ValueError) as exc:
        raise RuntimeError("SnowLuma desktop helper unavailable") from exc
    if not result.get("ok"):
        raise RuntimeError("SnowLuma desktop action failed")
    return result


class DesktopLogin:
    credential = ""

    def __init__(self, account: str, token: str):
        self.account = account
        self.token = token
        self.offline_since: float | None = None

    async def online(self) -> bool:
        try:
            await check_onebot("ws://127.0.0.1:3002", self.token, self.account, implementation="SnowLuma", timeout=3)
        except OneBotProbeError:
            return False
        return True

    async def qr(self) -> str:
        return str((await desktop_call("capture")).get("code") or "")

    async def call(self, action: str) -> dict:
        if await self.online():
            self.offline_since = None
            return {"isLogin": True, "coreReady": True}
        if self.offline_since is None:
            self.offline_since = time.monotonic()
        if action == "CheckLoginStatus":
            code = await self.qr()
            can_refresh = bool(code) or time.monotonic() - self.offline_since >= OFFLINE_SECONDS
            return {
                "isLogin": False,
                "loginPhase": "waiting_qrcode" if code else "offline",
                "qrcodeurl": code,
                "canRefreshQRcode": can_refresh,
            }
        if action != "RefreshQRcode":
            raise ValueError("Unsupported desktop login operation")

        # A fresh desktop login window generates a new code. This targets only the
        # separate SnowLuma QQ process, after the account's online check above.
        await desktop_call("refresh")
        await asyncio.sleep(2)
        return {}


async def main(refresh_now: bool) -> None:
    runtime = os.environ if os.environ.get("NTFY_URL") else dotenv_values("/etc/qq-watch.env")
    bot = dotenv_values(ROOT / ".env")
    desktop = DesktopLogin(bot.get("QQBOT_ACCOUNT") or "", bot.get("ONEBOT_ACCESS_TOKEN") or "")
    if not desktop.account or not desktop.token:
        raise ValueError("Missing robot account or local OneBot token")
    async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
        notify = Ntfy(
            client, runtime["NTFY_URL"], runtime["NTFY_WATCH_TOKEN"], runtime["NTFY_REFRESH_BUTTON_TOKEN"],
            internal_url=runtime.get("NTFY_INTERNAL_URL") or "", refresh_body="refresh:snowluma",
            label="SnowLuma QQ", sequence="qq-snowluma-login-state",
        )
        watch = Watch(desktop, notify)
        if refresh_now:
            watch.refresh_pending = True
            desktop.offline_since = watch.offline_since = time.monotonic() - OFFLINE_SECONDS
        while True:
            try:
                had_code = watch.sent_code
                await watch.tick()
                if watch.sent_code and not had_code:
                    LOG.info("SnowLuma login QR delivered with refresh action")
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                LOG.warning("SnowLuma ntfy watch unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(5)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--refresh-now", action="store_true")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(p.parse_args().refresh_now))
