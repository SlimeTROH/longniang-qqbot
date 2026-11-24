"""好感度显著事件（夸赞/冒犯）JSONL 历史记录读写与原子清理。"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from nonebot import logger


def record_affection_event(
    path: Path,
    *,
    user_id: str,
    before: float,
    after: float,
    delta: float,
    event: str = "",
    reason: str = "",
    user_text: str = "",
    reply: str = "",
) -> None:
    """追加一条显著好感变化记录（JSONL）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": time.time(),
        "uid": str(user_id),
        "before": round(before, 2),
        "after": round(after, 2),
        "delta": round(delta, 3),
        "user": (user_text or "")[:300],
        "reply": (reply or "")[:300],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_affection_events(path: Path, user_id: str, limit: int = 15) -> list[dict]:
    """读取某个用户最近的好感变化记录（最新在前）。"""
    if not path.is_file():
        return []
    uid = str(user_id)
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if str(item.get("uid")) == uid:
                    rows.append(item)
    except OSError:
        return []
    return rows[-limit:][::-1]


def clear_affection_history(path: Path, user_id: str) -> int:
    """原子删除指定用户的全部好感历史记录，返回删除条数。"""
    if not path.is_file():
        return 0
    uid = str(user_id)
    kept: list[str] = []
    removed = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            t = line.strip()
            if not t:
                continue
            try:
                item = json.loads(t)
            except Exception:  # noqa: BLE001
                kept.append(t)
                continue
            if str(item.get("uid")) == uid:
                removed += 1
            else:
                kept.append(t)
    except OSError:
        return 0

    if removed == 0:
        return 0

    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".affhist-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for t in kept:
                fh.write(t + "\n")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"chat_bot: 清理好感度历史失败 {exc!r}")
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return removed
