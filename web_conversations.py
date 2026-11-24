from __future__ import annotations

from collections.abc import Mapping
import json
import math
from pathlib import Path
import threading
import time
from typing import Any
import uuid

from shared_history import atomic_json

SCHEMA_VERSION = 1
DEFAULT_LEGACY_ID = "legacy"
DEFAULT_LEGACY_TITLE = "原有对话"
DEFAULT_NEW_TITLE = "新对话"
MAX_TITLE_LEN = 80
MAX_PROMPT_LEN = 12000
AFF_MIN = -50.0
AFF_MAX = 100.0
TEMP_MIN = 0.0
TEMP_MAX = 1.5
DEFAULT_TEMP = 0.9

class InvalidConversationSetting(ValueError):
    pass


def _clamp_float(value: Any, field_name: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidConversationSetting(f"{field_name} 需要填写有限数值") from exc
    if not math.isfinite(number):
        raise InvalidConversationSetting(f"{field_name} 需要填写有限数值")
    return max(low, min(high, number))


def _clean_title(value: Any) -> str:
    return str(value or DEFAULT_NEW_TITLE)[:MAX_TITLE_LEN]


def _clean_prompt(value: Any) -> str:
    return str(value or "")[:MAX_PROMPT_LEN]


class Conversations:
    """Manage and persist version-1 web conversation mappings."""

    def __init__(
        self,
        path: str | Path,
        legacy: Mapping[str, Any],
        legacy_uid: Any,
        initial_aff: Any,
    ) -> None:
        """Load an existing store or initialize a missing one via atomic_json."""
        self.path = Path(path)
        self._lock = threading.RLock()
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            row = {**legacy, "id": DEFAULT_LEGACY_ID, "uid": legacy_uid,
                   "title": DEFAULT_LEGACY_TITLE, "aff": initial_aff,
                   "pre_prompt": str(legacy.get("pre_prompt") or ""), "created": time.time()}
            self.data = {"version": SCHEMA_VERSION, "conversations": {DEFAULT_LEGACY_ID: row}}
            self.save()
        rows = self.data.get("conversations") if isinstance(self.data, dict) else None
        if (not isinstance(rows, dict) or DEFAULT_LEGACY_ID not in rows
                or any(not isinstance(row, dict) for row in rows.values())):
            raise ValueError("Invalid conversation collection")

    def _write_row(self, key, row):
        # Publish the live row only after the candidate reaches disk.
        candidates = dict(self.data["conversations"])
        candidates[key] = row
        atomic_json(self.path, {**self.data, "conversations": candidates})

    def get(self, identifier: str = DEFAULT_LEGACY_ID) -> dict[str, Any]:
        """Return the live conversation row for identifier (blank defaults to 'legacy')."""
        key = identifier or DEFAULT_LEGACY_ID
        with self._lock:
            return self.data["conversations"][key]

    def create(
        self,
        title: str = DEFAULT_NEW_TITLE,
        aff: float = 0.0,
        prompt: str = "",
    ) -> dict[str, Any]:
        """Validate, persist, and publish a new conversation row."""
        clean_title = _clean_title(title)
        clean_aff = _clamp_float(aff, "aff", AFF_MIN, AFF_MAX)
        clean_prompt = _clean_prompt(prompt)

        with self._lock:
            row_id = uuid.uuid4().hex
            new_row: dict[str, Any] = {
                "id": row_id,
                "uid": f"web:{row_id}",
                "title": clean_title,
                "aff": clean_aff,
                "pre_prompt": clean_prompt,
                "temp": DEFAULT_TEMP,
                "history": [],
                "sections": [],
                "n": 0,
                "created": time.time(),
            }
            self._write_row(row_id, new_row)
            self.data["conversations"][row_id] = new_row
            return new_row

    def configure(
        self,
        identifier: str,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate supported fields, persist a candidate, and update the live row in place."""
        if not isinstance(values, Mapping):
            raise InvalidConversationSetting("会话设置需要是一个对象")

        updates: dict[str, Any] = {}
        if "title" in values:
            updates["title"] = _clean_title(values["title"])
        if "pre_prompt" in values:
            updates["pre_prompt"] = _clean_prompt(values["pre_prompt"])
        if "aff" in values:
            updates["aff"] = _clamp_float(values["aff"], "aff", AFF_MIN, AFF_MAX)
        if "temp" in values:
            updates["temp"] = _clamp_float(values["temp"], "temp", TEMP_MIN, TEMP_MAX)
        updates["stage_sent"] = 0

        key = identifier or DEFAULT_LEGACY_ID
        with self._lock:
            live_row = self.data["conversations"][key]

            # Shallow-copy only the affected mappings for candidate serialization.
            candidate_row = dict(live_row)
            candidate_row.update(updates)
            self._write_row(key, candidate_row)
            live_row.update(updates)
            return live_row

    @staticmethod
    def summary(row: Mapping[str, Any]) -> dict[str, Any]:
        """Extract summary fields from a conversation row using row.get."""
        fields = ("id", "title", "aff", "pre_prompt", "temp", "n", "created")
        return {key: row.get(key) for key in fields}

    def listing(self) -> list[dict[str, Any]]:
        """Return conversation summaries in insertion order."""
        with self._lock:
            return [self.summary(row) for row in self.data["conversations"].values()]

    def save(self) -> None:
        """Persist current live conversation rows (including external nested edits)."""
        with self._lock:
            atomic_json(self.path, self.data)
