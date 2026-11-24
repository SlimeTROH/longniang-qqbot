"""Bot-local proxy entry; the running client chooses the VPS node."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PROXY = "http://127.0.0.1:10808"
_ENV_FILE = Path(__file__).resolve().parent / ".env"
_cache_signature: tuple[int, int] | None = None
_cache_proxy: str | None = None


def get_proxy() -> str:
    """Read only QQBOT_PROXY, preserving the rest of the private .env contents."""
    global _cache_signature, _cache_proxy
    try:
        stat = _ENV_FILE.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature != _cache_signature:
            raw = _ENV_FILE.read_bytes()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("gb18030")
            configured = None
            for line in text.splitlines():
                key, separator, value = line.partition("=")
                if separator and key.strip() == "QQBOT_PROXY":
                    value = value.strip()
                    if value.startswith(('"', "'")):
                        quote = value[0]
                        end = value.find(quote, 1)
                        if end < 0:
                            raise ValueError("QQBOT_PROXY has an unmatched quote")
                        configured = value[1:end]
                    else:
                        configured = value.split(" #", 1)[0].strip()
            _cache_proxy, _cache_signature = configured, signature
        proxy = _cache_proxy or os.environ.get("QQBOT_PROXY") or DEFAULT_PROXY
    except FileNotFoundError:
        proxy = os.environ.get("QQBOT_PROXY") or DEFAULT_PROXY
    parsed = urlsplit(proxy.strip())
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname or not parsed.port:
        raise ValueError("QQBOT_PROXY must be one complete proxy URL with a port")
    if "," in proxy or ";" in proxy:
        raise ValueError("QQBOT_PROXY accepts a single fixed proxy, not a proxy list")
    return proxy.strip()


def proxy_status() -> dict:
    parsed = urlsplit(get_proxy())
    return {"host": parsed.hostname, "port": parsed.port,
            "label": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}


def proxy_for_url(url: str) -> str | None:
    """本机中转直连，外部服务使用 QQBOT_PROXY。"""
    return None if urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"} else get_proxy()


def configure_process_proxy() -> None:
    """Apply only to this bot process. Loopback OneBot/relay traffic stays local."""
    proxy = get_proxy()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ[name] = proxy
        os.environ[name.lower()] = proxy
    bypass = []
    for value in (os.environ.get("NO_PROXY", ""), os.environ.get("no_proxy", ""), "localhost,127.0.0.1,::1"):
        for host in value.split(","):
            host = host.strip()
            if host and host not in bypass:
                bypass.append(host)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ",".join(bypass)


if __name__ == "__main__":
    print(json.dumps(proxy_status(), ensure_ascii=False))
