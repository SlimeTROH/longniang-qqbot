#!/usr/bin/env python3
"""Check a running SnowLuma gateway and switch only the QQ bot and its watcher."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from onebot_probe import OneBotProbeError, check_onebot, local_endpoint

BOT_ENV = ROOT / ".env"
WATCH_ENV = Path("/etc/qq-watch.env")


def env_line(data: bytes, name: str, value: str) -> bytes:
    assignment = (name + "=" + value).encode("utf-8")
    pattern = rb"(?m)^[ \t]*(?:export[ \t]+)?" + name.encode("ascii") + rb"[ \t]*=[^\r\n]*"
    updated, count = re.subn(pattern, lambda _: assignment, data)
    if count:
        return updated
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    return data + (b"" if not data or data.endswith(b"\n") else newline) + assignment + newline


def replace(path: Path, data: bytes) -> None:
    permissions = path.stat()
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            os.fchown(file.fileno(), permissions.st_uid, permissions.st_gid)
            os.fchmod(file.fileno(), stat.S_IMODE(permissions.st_mode))
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=True, stdout=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ws", default="ws://127.0.0.1:3002")
    parser.add_argument("--apply", action="store_true", help="switch the bot after a successful check")
    parser.add_argument(
        "--ntfy-status-only", action="store_true",
        help="keep offline/online notifications after switching, with QR handled in QQ",
    )
    args = parser.parse_args()
    if args.apply and not args.ntfy_status_only:
        parser.error("当前 SnowLuma 对接只有 ntfy 掉线/恢复通知，需明确选择 --ntfy-status-only 才切换")

    try:
        url = local_endpoint(args.ws)
        config = dotenv_values(BOT_ENV, interpolate=False)
        token = config.get("ONEBOT_ACCESS_TOKEN") or ""
        account = config.get("QQBOT_ACCOUNT") or ""

        def verify() -> None:
            asyncio.run(check_onebot(url, token, account, implementation="SnowLuma"))

        verify()
        print("SnowLuma WebSocket 已认证，机器人 QQ 号匹配且在线。", flush=True)
        if not args.apply:
            return 0
        if os.geteuid() != 0:
            raise PermissionError("--apply 需要 sudo 来重启 qqbot 和 qq-watch")

        originals = {path: path.read_bytes() for path in (BOT_ENV, WATCH_ENV)}
        updates = {
            BOT_ENV: env_line(originals[BOT_ENV], "ONEBOT_WS_URLS", json.dumps([url])),
            WATCH_ENV: env_line(originals[WATCH_ENV], "QQ_WATCH_ONEBOT_URL", json.dumps(url)),
        }
        backup = ROOT / ".secrets" / ("onebot-switch-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        backup.mkdir(mode=0o700, parents=True)
        for name, data in (("bot.env", originals[BOT_ENV]), ("qq-watch.env", originals[WATCH_ENV])):
            with os.fdopen(os.open(backup / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as file:
                file.write(data)
        try:
            for path, data in updates.items():
                replace(path, data)
            systemctl("restart", "qqbot", "qq-watch")
            verify()
            systemctl("is-active", "qqbot")
            systemctl("is-active", "qq-watch")
        except (OSError, subprocess.SubprocessError, OneBotProbeError):
            for path, data in originals.items():
                replace(path, data)
            systemctl("restart", "qqbot", "qq-watch")
            print("切换失败，原配置已恢复。", file=sys.stderr)
            raise
        print(f"已切换机器人与掉线提醒到 {url}。原配置保存在 {backup}。")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, OneBotProbeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
