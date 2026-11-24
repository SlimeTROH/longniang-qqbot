"""Root-owned socket handler for two fixed actions on the SnowLuma desktop."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from urllib.parse import urlsplit

CONTAINER = "qqbot-snowluma"
SNAPSHOT = "/tmp/qq-login-watch.png"


def docker(*args: str, detached: bool = False) -> subprocess.CompletedProcess:
    command = ["/usr/bin/docker", "exec"]
    if detached:
        command.append("-d")
    return subprocess.run(
        [*command, CONTAINER, *args], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, timeout=8,
    )


def capture() -> str:
    expected_host = os.environ["SNOWLUMA_LOGIN_QR_HOST"]
    expected_path = os.environ["SNOWLUMA_LOGIN_QR_PATH"]
    frame = docker(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "x11grab",
        "-framerate", "1", "-video_size", "1280x800", "-i", ":95",
        "-frames:v", "1", "-y", SNAPSHOT,
    )
    if frame.returncode:
        return ""
    try:
        for raw in docker("zbarimg", "--quiet", "--raw", SNAPSHOT).stdout.splitlines():
            url = raw.strip()
            parts = urlsplit(url)
            host = parts.hostname or ""
            if parts.scheme == "https" and host == expected_host and parts.path == expected_path:
                return url
        return ""
    finally:
        docker("rm", "-f", SNAPSHOT)


def refresh() -> None:
    docker("pkill", "-TERM", "-x", "qq")
    for _ in range(12):
        if docker("pgrep", "-x", "qq").returncode:
            break
        time.sleep(.2)
    else:
        raise RuntimeError("QQ login window didn't close")
    result = docker(
        "/bin/sh", "-c",
        "umask 077; exec dbus-run-session -- /usr/bin/qq --no-sandbox --disable-gpu >/qq-home/qq-desktop.log 2>&1",
        detached=True,
    )
    if result.returncode:
        raise RuntimeError("Could not reopen the QQ login window")


def main() -> None:
    action = sys.stdin.readline(24).strip()
    try:
        if action == "capture":
            reply = {"ok": True, "code": capture()}
        elif action == "refresh":
            refresh()
            reply = {"ok": True}
        else:
            reply = {"ok": False}
    except (subprocess.TimeoutExpired, OSError, RuntimeError, ValueError):
        reply = {"ok": False}
    print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
