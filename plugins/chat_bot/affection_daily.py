"""好感度的按天汇总（给 /好感度 展示用）。

为什么要单独记：
    好感度状态里只有「今日已增加/已减少额度」（只统计 +2/-3），
    日常对话 ±0.5 的微调、以及 +2/-3 的触发次数都没有留痕。
    这里按天累计，用来展示「今天总共涨/跌了多少、夸赞冒犯各触发几次」。

存储：state/affection_daily.json
    {user_id: {"2026-10-20": {"daily": 0.35, "praise": 1, "offense": 0,
                               "total": 2.35, "turns": 12, "updated": 1792...}}}
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

FILE = Path(__file__).resolve().parents[2] / "state" / "affection_daily.json"


def _today(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        raw = json.loads(FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(FILE.parent), prefix=".affdaily-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, FILE)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except Exception:  # noqa: BLE001
            pass


def record(
    user_id: str,
    *,
    daily_delta: float = 0.0,
    praise: int = 0,
    offense: int = 0,
    total_delta: float = 0.0,
    now: float | None = None,
) -> dict:
    """记一笔。返回当天的汇总。"""
    uid = str(user_id)
    date = _today(now)
    data = _load()
    user = data.setdefault(uid, {})
    item = user.setdefault(
        date, {"daily": 0.0, "praise": 0, "offense": 0, "total": 0.0, "turns": 0}
    )
    item["daily"] = round(float(item.get("daily", 0.0)) + float(daily_delta), 3)
    item["praise"] = int(item.get("praise", 0)) + int(praise)
    item["offense"] = int(item.get("offense", 0)) + int(offense)
    item["total"] = round(float(item.get("total", 0.0)) + float(total_delta), 3)
    item["turns"] = int(item.get("turns", 0)) + 1
    item["updated"] = time.time()
    _save(data)
    return item


def day(user_id: str, date: str | None = None, now: float | None = None) -> dict:
    """某天的汇总（默认今天）；没有记录就返回全 0。"""
    uid = str(user_id)
    date = date or _today(now)
    item = (_load().get(uid) or {}).get(date) or {}
    return {
        "date": date,
        "daily": float(item.get("daily", 0.0)),
        "praise": int(item.get("praise", 0)),
        "offense": int(item.get("offense", 0)),
        "total": float(item.get("total", 0.0)),
        "turns": int(item.get("turns", 0)),
    }


def recent(user_id: str, days: int = 7, now: float | None = None) -> list[dict]:
    """最近 N 天里有记录的日子，新的在前。"""
    uid = str(user_id)
    user = _load().get(uid) or {}
    today = datetime.fromtimestamp(time.time() if now is None else now).date()
    out: list[dict] = []
    for i in range(max(1, days)):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        if d not in user:
            continue
        item = user[d]
        out.append(
            {
                "date": d,
                "daily": float(item.get("daily", 0.0)),
                "praise": int(item.get("praise", 0)),
                "offense": int(item.get("offense", 0)),
                "total": float(item.get("total", 0.0)),
                "turns": int(item.get("turns", 0)),
            }
        )
    return out
