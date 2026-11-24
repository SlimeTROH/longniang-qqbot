"""按月免费额度计数器 —— 保证外部识图服务只花免费额度。

为什么需要它：
    Google Cloud Vision 的免费层级是「每月前 1,000 次」，超出即按次计费。
    API 本身不会帮你拦，请求一多就悄悄扣钱。这里在**发请求之前**做本地计数，
    到上限直接短路（返回失败、不联网），宁可少用，也绝不越界。

设计：
    · 每个 provider 一条记录，按自然月（本机时区）归零；
    · 文件原子写入，进程重启/机器人重启都不丢；
    · consume() 是同步函数、在 await 之前完成，天然不会被并发打穿；
    · 计数发生在「即将发送」时，所以网络失败也会占 1 次——宁可保守。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "vision_quota.json"


def current_month() -> str:
    """本机时区的自然月，例如 2026-10。"""
    return time.strftime("%Y-%m")


def _load(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # 计数写不进去也不能阻塞业务；下次仍可尝试


def peek(provider: str, cap: int, path: Path | None = None) -> dict:
    """只看不占：返回 {month, used, cap, remaining}。"""
    p = path or STATE_PATH
    data = _load(p)
    item = data.get(provider) or {}
    month = current_month()
    used = int(item.get("used") or 0) if item.get("month") == month else 0
    remaining = max(0, cap - used) if cap > 0 else 0
    return {"month": month, "used": used, "cap": cap, "remaining": remaining}


def consume(provider: str, cap: int, path: Path | None = None) -> tuple[bool, dict]:
    """尝试占用 1 次额度。

    返回 (allowed, state)：
      allowed=False 表示本月已到上限，调用方必须**直接放弃、不联网**。
      cap <= 0 视为禁用（永远不允许）。
    """
    p = path or STATE_PATH
    data = _load(p)
    month = current_month()
    item = data.get(provider) or {}
    used = int(item.get("used") or 0) if item.get("month") == month else 0

    if cap <= 0 or used >= cap:
        return False, {
            "month": month,
            "used": used,
            "cap": cap,
            "remaining": max(0, cap - used),
        }

    used += 1
    data[provider] = {"month": month, "used": used, "updated_at": time.time()}
    _save(p, data)
    return True, {
        "month": month,
        "used": used,
        "cap": cap,
        "remaining": max(0, cap - used),
    }
