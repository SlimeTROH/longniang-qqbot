"""Persistent daily image output quotas and generation cooldowns per QQ account."""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path

from .image_settings import ImageSettings, fit_image


SHANGHAI = timezone(timedelta(hours=8))


def _now() -> datetime:
    return datetime.now(SHANGHAI)


ZERO_AFFECTION_LIMIT = 2_000
MIN_DAILY_LIMIT = 8_000
MAX_DAILY_LIMIT = 30_000
DEFAULT_COOLDOWN_SECONDS = 180
UNAVAILABLE_TEXT = "好感度达到 0 分即可生图。"


def daily_limit(score: float, tier_min: float, last_tier_min: float) -> int:
    if score < 0:
        return 0
    if score == 0:
        return ZERO_AFFECTION_LIMIT
    if last_tier_min <= 0:
        return MIN_DAILY_LIMIT
    progress = max(0.0, min(1.0, tier_min / last_tier_min))
    increment = (MAX_DAILY_LIMIT - MIN_DAILY_LIMIT) * progress
    return MIN_DAILY_LIMIT + int(increment / 100 + 0.5) * 100


@dataclass(frozen=True)
class Balance:
    day: str
    limit: int
    used: int
    reserved: int
    lifetime: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used - self.reserved)


@dataclass(frozen=True)
class Reservation:
    settings: ImageSettings | None
    balance: Balance
    duplicate: bool = False
    retry_after: int = 0


class ImageQuota:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS image_usage (
                    request_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    month TEXT NOT NULL,
                    estimated_tokens INTEGER NOT NULL,
                    output_tokens INTEGER,
                    created_at REAL
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS image_usage_by_user ON image_usage (user_id, month)")
            if not any(row[1] == "created_at" for row in db.execute("PRAGMA table_info(image_usage)")):
                db.execute("BEGIN IMMEDIATE")
                if not any(row[1] == "created_at" for row in db.execute("PRAGMA table_info(image_usage)")):
                    # Old records only have a month and still count toward lifetime use.
                    db.execute("ALTER TABLE image_usage ADD COLUMN created_at REAL")
                db.commit()
            db.execute("CREATE INDEX IF NOT EXISTS image_usage_by_user_time ON image_usage (user_id, created_at)")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS image_quota_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cooldown_seconds INTEGER NOT NULL CHECK (cooldown_seconds >= 0)
                )
                """
            )
            yield db

    @staticmethod
    def _balance(db: sqlite3.Connection, user_id: str, now: datetime, limit: int) -> Balance:
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = midnight.timestamp(), (midnight + timedelta(days=1)).timestamp()
        used, reserved, lifetime = db.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN created_at >= ? AND created_at < ? THEN output_tokens END), 0),
                   COALESCE(SUM(CASE WHEN created_at >= ? AND created_at < ? AND output_tokens IS NULL
                                     THEN estimated_tokens END), 0),
                   COALESCE(SUM(output_tokens), 0)
              FROM image_usage
             WHERE user_id = ?
            """,
            (start, end, start, end, user_id),
        ).fetchone()
        return Balance(now.date().isoformat(), limit, used, reserved, lifetime)

    def balance(self, user_id: str, limit: int) -> Balance:
        with self._database() as db:
            return self._balance(db, user_id, _now(), limit)

    @staticmethod
    def _read_cooldown(db: sqlite3.Connection) -> int:
        saved = db.execute("SELECT cooldown_seconds FROM image_quota_settings WHERE id = 1").fetchone()
        return saved[0] if saved else DEFAULT_COOLDOWN_SECONDS

    def cooldown_seconds(self) -> int:
        with self._database() as db:
            return self._read_cooldown(db)

    def set_cooldown_seconds(self, seconds: int) -> None:
        if type(seconds) is not int or not 0 <= seconds < 2**31:
            raise ValueError("Invalid image cooldown")
        with self._database() as db:
            db.execute(
                """
                INSERT INTO image_quota_settings (id, cooldown_seconds) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET cooldown_seconds = excluded.cooldown_seconds
                """,
                (seconds,),
            )

    def reserve(self, request_id: str, user_id: str, limit: int, requested: ImageSettings) -> Reservation:
        with self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            now = _now()
            timestamp = now.timestamp()
            balance = self._balance(db, user_id, now, limit)
            if db.execute("SELECT 1 FROM image_usage WHERE request_id = ?", (request_id,)).fetchone():
                return Reservation(None, balance, duplicate=True)
            remaining = balance.remaining
            if remaining <= 0:
                return Reservation(None, balance)
            cooldown = self._read_cooldown(db)
            if cooldown:
                latest, = db.execute(
                    "SELECT MAX(created_at) FROM image_usage WHERE user_id = ? AND created_at > ?",
                    (user_id, timestamp - cooldown),
                ).fetchone()
                if latest is not None:
                    return Reservation(None, balance, retry_after=ceil(latest + cooldown - timestamp))
            settings = requested
            if settings.reservation_tokens > remaining + limit // 10:
                settings = fit_image(requested, remaining)
            if settings is not None:
                db.execute(
                    """
                    INSERT INTO image_usage (request_id, user_id, month, estimated_tokens, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (request_id, user_id, balance.day[:7], settings.reservation_tokens, timestamp),
                )
            return Reservation(settings, balance)

    def settle(self, request_id: str, output_tokens: int, *, cancelled: bool = False) -> None:
        if type(output_tokens) is not int or output_tokens < 0 or (cancelled and output_tokens):
            raise ValueError("Invalid image output token count")
        with self._database() as db:
            db.execute(
                """
                UPDATE image_usage
                   SET output_tokens = ?, created_at = CASE WHEN ? THEN NULL ELSE created_at END
                 WHERE request_id = ? AND output_tokens IS NULL
                """,
                (output_tokens, cancelled, request_id),
            )
