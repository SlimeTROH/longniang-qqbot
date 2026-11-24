"""单人滑动窗口限流与连续冒犯冷却（原子持久化）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from nonebot import logger
from shared_history import atomic_json

COOLDOWN_NOTICE = (
    "【系统提示】\n"
    "已连续 3 次判定为不友好发言，对话功能限制 24 小时。\n"
    "限制期间不再响应你的消息，请冷静后再试。"
)


class OffenseCooldownStore:
    """连续冒犯冷却状态管理。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        if self._cache is None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._cache = raw if isinstance(raw, dict) else {}
            except Exception:  # noqa: BLE001
                self._cache = {}
        return self._cache

    def _save(self) -> None:
        try:
            atomic_json(self.path, self._load())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"chat_bot: 写冷却记录失败 {exc!r}")

    def in_cooldown(self, user_id: str, *, is_admin: bool = False) -> bool:
        if is_admin:
            return False
        rec = self._load().get(str(user_id)) or {}
        return float(rec.get("until") or 0) > time.time()

    def bump_offense(self, user_id: str, *, limit: int = 3, cooldown_seconds: int = 86400) -> None:
        data = self._load()
        uid = str(user_id)
        rec = dict(data.get(uid) or {"count": 0, "until": 0})
        rec["count"] = int(rec.get("count") or 0) + 1
        if rec["count"] >= limit:
            rec["until"] = time.time() + cooldown_seconds
            rec["count"] = 0
            rec["notified"] = 0
            logger.warning(
                f"chat_bot: user={uid} 连续冒犯 {limit} 次，已冷却 {cooldown_seconds} 秒"
            )
        data[uid] = rec
        self._save()

    def reset_offense(self, user_id: str) -> None:
        data = self._load()
        rec = data.get(str(user_id))
        if rec and int(rec.get("count") or 0):
            rec["count"] = 0
            self._save()

    def take_notice(self, user_id: str) -> str:
        data = self._load()
        rec = data.get(str(user_id))
        if not rec:
            return ""
        if float(rec.get("until") or 0) <= time.time():
            return ""
        if float(rec.get("notified") or 0) > 0:
            return ""
        rec["notified"] = time.time()
        self._save()
        return COOLDOWN_NOTICE


class SlidingRateLimiter:
    """按用户滑动时间窗计数限流。"""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}
        self._notice_at: dict[str, float] = {}

    def take(self, uid: str, *, max_hits: int, window_sec: float) -> bool:
        now = time.time()
        hits = [t for t in self._hits.get(uid, []) if now - t < window_sec]
        if len(hits) >= max_hits:
            self._hits[uid] = hits
            return False
        hits.append(now)
        self._hits[uid] = hits
        return True

    def should_notify(self, uid: str, cooldown: float = 30.0) -> bool:
        now = time.time()
        if now - self._notice_at.get(uid, 0.0) >= cooldown:
            self._notice_at[uid] = now
            return True
        return False
