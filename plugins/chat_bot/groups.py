"""QQ 群指令的增删设置，优先于 .env 的启用群。"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from shared_history import atomic_json

DATA_FILE = Path(__file__).resolve().parents[2] / "state" / "chat_groups.json"


@dataclass
class GroupSettings:
    added: set[str] = field(default_factory=set)
    disabled: set[str] = field(default_factory=set)
    restricted: bool = False

    def enabled(self, configured: set[str]) -> set[str] | None:
        if not (configured or self.added or self.restricted):
            return None  # 默认允许所有群，但仍应用单独排除的群。
        return (configured | self.added) - self.disabled

    def allows(self, group_id: str, configured: set[str]) -> bool:
        enabled = self.enabled(configured)
        return group_id not in self.disabled if enabled is None else group_id in enabled

    def describe(self, configured: set[str]) -> str:
        enabled = self.enabled(configured)
        if enabled is None:
            return "全部群" if not self.disabled else "全部群（排除 " + "、".join(sorted(self.disabled)) + "）"
        return "、".join(sorted(enabled)) or "无启用群"


def _ids(values: object) -> set[str]:
    if not isinstance(values, list):
        raise ValueError("群名单格式无效")
    return {str(value).strip() for value in values if str(value).strip()}


def load() -> GroupSettings:
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return GroupSettings()
    if isinstance(raw, list):
        raw = {"groups": raw}
    if not isinstance(raw, dict):
        raise ValueError("群名单格式无效")
    added = _ids(raw.get("groups", []))
    return GroupSettings(added, _ids(raw.get("disabled", [])), bool(added) or raw.get("restricted") is True)


def _save(settings: GroupSettings) -> None:
    atomic_json(DATA_FILE, {
        "groups": sorted(settings.added),
        "disabled": sorted(settings.disabled),
        "restricted": settings.restricted,
    })


def add(group_id: str, configured: set[str]) -> bool:
    settings = load()
    if group_id not in settings.disabled and group_id in (configured | settings.added):
        return False
    # 恢复默认开放模式下排除的群时，保留其余群现有权限。
    if group_id not in configured and (group_id not in settings.disabled or settings.enabled(configured) is not None):
        settings.added.add(group_id)
        settings.restricted = True
    settings.disabled.discard(group_id)
    _save(settings)
    return True


def remove(group_id: str, configured: set[str]) -> bool:
    settings = load()
    if not settings.allows(group_id, configured):
        return False
    settings.added.discard(group_id)
    settings.disabled.add(group_id)
    _save(settings)
    return True
