"""按用户和可见范围保存长期记忆。"""
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from shared_history import _file_lock, atomic_json

# 好感度高于这个值才开始记录 / 读取记忆
MEMORY_THRESHOLD = 50.0
# 每个用户最多保留多少条
MAX_MEMORIES = 30


@dataclass
class Memory:
    """一条长期记忆。"""

    key: str = ""          # 类别：名字 / 喜好 / 经历 / 约定 …
    value: str = ""        # 内容
    at: float = 0.0        # 记录时间戳
    affection: float = 0.0 # 记录时的好感度
    scope: str = "private"   # 来源：group（群里说的）/ private（私聊说的）
    group_id: str = ""


class MemoryStore:
    """长期记忆存储（JSON 持久化，原子写）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, list[Memory]] = {}
        self.load()

    # ---------------------------------------------------------- 持久化
    def load(self) -> bool:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError):
            return False
        if not isinstance(raw, dict):
            return False
        restored: dict[str, list[Memory]] = {}
        for uid, items in raw.items():
            if not isinstance(items, list):
                continue
            bucket: list[Memory] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("value") or "").strip()
                if not value:
                    continue
                bucket.append(
                    Memory(
                        key=str(item.get("key") or "其他").strip(),
                        value=value,
                        at=float(item.get("at") or 0.0),
                        affection=float(item.get("affection") or 0.0),
                        scope=str(item.get("scope") or "private").strip() or "private",
                        group_id=str(item.get("group_id") or ""),
                    )
                )
            if bucket:
                restored[str(uid)] = bucket
        self._data = restored
        return True

    def save(self) -> None:
        payload = {
            uid: [asdict(m) for m in items] for uid, items in self._data.items()
        }
        atomic_json(self._path, payload, compact=True)

    # ---------------------------------------------------------- 读写
    def list(self, user_id: str, scope: Optional[str] = None, *, group_id: str | None = None) -> list[Memory]:
        """列出记忆。scope 给值时只返回该来源（group / private），None 返回全部。"""
        items = self._data.get(str(user_id), [])
        if scope is None:
            return list(items)
        return [m for m in items if m.scope == scope and (group_id is None or m.group_id == str(group_id))]

    def count(self, user_id: str) -> int:
        return len(self._data.get(str(user_id), []))

    def add(
        self,
        user_id: str,
        key: str,
        value: str,
        affection: float,
        now: Optional[float] = None,
        scope: str = "private",
        *,
        group_id: str = "",
    ) -> Optional[Memory]:
        return self._remember(user_id, key, value, affection, now, scope, group_id, replace=False)

    def set(
        self,
        user_id: str,
        key: str,
        value: str,
        affection: float,
        now: Optional[float] = None,
        scope: str = "private",
        *,
        group_id: str = "",
    ) -> Optional[Memory]:
        return self._remember(user_id, key, value, affection, now, scope, group_id, replace=True)

    def _remember(self, user_id, key, value, affection, now, scope, group_id, *, replace):
        if affection <= MEMORY_THRESHOLD:
            return None
        key = (key or "其他").strip()[:12]
        value = (value or "").strip()
        if not value or len(value) > 120:
            return None

        uid = str(user_id)
        origin = str(group_id or "") if scope == "group" else ""
        item = Memory(key, value, now if now is not None else time.time(), affection, scope, origin)
        with _file_lock(self._path.with_suffix(".write.lock")):
            if not self.load():
                return None
            bucket = self._data.get(uid, [])
            duplicates = [
                m for m in bucket if (m.key, m.scope, m.group_id) == (key, scope, origin)
                and (replace or value in m.value or m.value in value)
            ]
            if replace and any(m.at > item.at for m in duplicates):
                return None
            if not replace and any(len(m.value) >= len(value) for m in duplicates):
                return None
            updated = [m for m in bucket if m not in duplicates] + [item]
            self._data[uid] = sorted(updated, key=lambda m: m.at)[-MAX_MEMORIES:]
            try:
                self.save()
            except OSError:
                self.load()
                return None
        return item

    def clear(self, user_id: str) -> int:
        uid = str(user_id)
        with _file_lock(self._path.with_suffix(".write.lock")):
            if not self.load():
                return 0
            removed = self._data.pop(uid, [])
            if removed:
                try:
                    self.save()
                except OSError:
                    self._data[uid] = removed
                    return 0
            return len(removed)

    # ---------------------------------------------------------- 渲染
    def render(
        self,
        user_id: str,
        affection: float,
        scope: Optional[str] = None,
        *,
        group_id: str | None = None,
    ) -> str:
        """把记忆拼成提示词片段。好感度不够时返回空串。

        scope：
          "group"   —— 正在群里说话，**只注入群里得来的记忆**。
                        私聊里说过的事不能拿到群里讲，哪怕是"事实"。
          None      —— 私聊，全部都能用（群聊的记忆当然也能，那是同一个人的事）。
        """
        if affection <= MEMORY_THRESHOLD:
            return ""
        items = self.list(user_id, scope, group_id=group_id)
        if not items:
            return ""

        # 按类别归组，让模型读起来更顺
        grouped: dict[str, list[str]] = {}
        for m in items:
            grouped.setdefault(m.key, []).append(m.value)

        lines = ["【当前发言人的长期记忆】"]
        for key, values in grouped.items():
            joined = "；".join(values)
            lines.append(f"- {key}：{joined}")
        return "\n".join(lines)
