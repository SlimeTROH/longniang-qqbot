"""QQ 用户 ↔ DSH 会话绑定的持久化存储。

机器人重启后，ACP 进程里所有会话都变成「未激活」，但 DSH 侧持有持久化的
会话记录。这里把每个用户的「当前会话 + 工作区」落盘，重启后自动读回，
用户无须重新 /dsh new 或 /dsh use。

存储文件是普通 JSON，可人工查看/编辑；写入采用「临时文件 + 替换」，
避免进程被中断时留下半个文件。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

STATE_VERSION = 1


def first_writable(candidates: list[Path]) -> Path:
    """返回第一个真正可写的候选路径。

    受限环境（例如外层还套了一层沙箱）里，某些目录允许写入、某些不允许；
    这里逐个建父目录并写一个探测文件实测，全部失败时回退到首个候选。
    """
    for path in candidates:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            probe = path.parent / ".dsh-state-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return path
        except OSError:
            continue
    return candidates[0]


def load_state(path: Path) -> dict[str, dict]:
    """读回用户绑定；文件缺失或损坏时返回空表（不抛异常）。"""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    users = raw.get("users")
    if not isinstance(users, dict):
        return {}
    result: dict[str, dict] = {}
    for key, value in users.items():
        if not isinstance(value, dict):
            continue
        session_id = value.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        cwd = value.get("cwd")
        result[str(key)] = {
            "session_id": session_id,
            "cwd": cwd if isinstance(cwd, str) else None,
            "updated_at": value.get("updated_at"),
        }
    return result


def save_state(path: Path, users: dict[str, dict]) -> None:
    """原子写入用户绑定表。"""
    payload = {"version": STATE_VERSION, "saved_at": time.time(), "users": users}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
