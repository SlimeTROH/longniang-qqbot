"""Atomic, scope-aware history storage shared by QQ and the local web app."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from weakref import WeakValueDictionary


_scope_queues = WeakValueDictionary()


def _key(value):
    value = str(value)
    return value if value.startswith(("private:", "group:")) else "private:" + value


def _retry_file_access(operation, *args):
    """A concurrent open or replace can temporarily deny access on Windows."""
    for delay in (.01, .02, .04, .08):
        try:
            return operation(*args)
        except PermissionError:
            time.sleep(delay)
    return operation(*args)


def _read_raw(path: Path) -> dict:
    try:
        raw = json.loads(_retry_file_access(path.read_bytes))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("History must be an object")
    return raw


def _normalize_message(message: dict) -> dict:
    row = {"role": message["role"], "content": str(message["content"])}
    for key in ("speaker_id", "speaker_name"):
        value = str(message.get(key) or "").strip()
        if value:
            row[key] = value[:64]
    at = message.get("at")
    if isinstance(at, (int, float)) and 0 < at < float("inf"):
        row["at"] = at
    return row


def _compaction(row: dict) -> dict | None:
    state = row.get("compaction")
    if not isinstance(state, dict):
        return None
    items, tokens, model = state.get("items"), state.get("tokens"), state.get("model")
    if (not isinstance(items, list) or not items or not all(isinstance(item, dict) for item in items)
            or type(tokens) is not int or tokens <= 0 or not isinstance(model, str) or not model):
        return None
    return {"items": items, "tokens": tokens, "model": model}


def _normalize_rows(items) -> dict:
    result = {}
    for name, row in items:
        if isinstance(row, list):
            row = {"recent": row}
        if not isinstance(row, dict):
            continue
        key = _key(name)
        value = {
            "summary": str(row.get("summary") or ""),
            "sections": [str(s) for s in (row.get("sections") or []) if str(s).strip()],
            "recent": [
                _normalize_message(msg)
                for msg in (row.get("recent") or [])
                if isinstance(msg, dict) and msg.get("role") in ("user", "assistant")
                and msg.get("content")
            ],
        }
        if compacted := _compaction(row):
            value["compaction"] = compacted
        previous = result.get(key)
        if previous is not None and previous != value:
            if previous.get("compaction"):
                value.setdefault("compaction", previous["compaction"])
            sections = previous["sections"] or ([previous["summary"]] if previous["summary"] else [])
            extra = value["sections"] or ([value["summary"]] if value["summary"] else [])
            value["sections"] = sections + [section for section in extra if section not in sections]
            value["summary"] = "\n\n".join(value["sections"])
            prefix = 0
            for left, right in zip(previous["recent"], value["recent"]):
                if left != right:
                    break
                prefix += 1
            value["recent"] = previous["recent"] + value["recent"][prefix:]
        result[key] = value
    return result


def read_sessions(path: Path, *, migrate_groups: bool = False) -> dict:
    raw = _read_raw(path)
    if not migrate_groups or not any(key.startswith("group:") and key.count(":") == 2 for key in raw):
        return _normalize_rows(raw.items())
    with _file_lock(path.with_suffix(".write.lock")):
        rows = _normalize_rows(_read_raw(path).items())
        legacy = [key for key in rows if key.startswith("group:") and key.count(":") == 2]
        for key in legacy:
            group, uid = key.rsplit(":", 1)
            old = rows.pop(key)
            current = rows.setdefault(group, {"summary": "", "sections": [], "recent": []})
            archive = old["sections"] or ([old["summary"]] if old["summary"] else [])
            if archive:
                if current["summary"] and not current["sections"]:
                    current["sections"].append(current["summary"])
                current["sections"].append(f"【QQ {uid} 的旧群聊摘要】\n" + "\n\n".join(archive))
            for message in old["recent"]:
                message.setdefault("speaker_id", uid)
                current["recent"].append(message)
        for group in {key.rsplit(":", 1)[0] for key in legacy}:
            rows[group]["recent"].sort(key=lambda message: message.get("at", 0))
        if legacy:
            atomic_json(path, rows, compact=True)
        return rows


def read_session(path: Path, key: str) -> dict | None:
    """Read one scope. A legacy user key belongs only to that user's private scope."""
    raw = _read_raw(path)
    key = _key(key)
    names = [key]
    if key.startswith("private:"):
        legacy = key[len("private:"):]
        if _key(legacy) == key and legacy in raw:
            # On collision the original file order determines the order of both histories.
            names = [name for name in raw if name in (key, legacy)] if key in raw else [legacy]
    return _normalize_rows((name, raw[name]) for name in names if name in raw).get(key)


def _handle(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a+b", buffering=0)


def _try_lock(f):
    f.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        return False
    # Windows can lock byte zero past EOF. Initialize the marker only after acquiring it.
    if os.fstat(f.fileno()).st_size == 0:
        f.write(b"0")
    return True


def _unlock(f):
    f.seek(0)
    if os.name=='nt':
        import msvcrt
        msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
    else:
        import fcntl
        fcntl.flock(f.fileno(),fcntl.LOCK_UN)


@contextmanager
def _file_lock(path):
    f=_handle(path);held=False;deadline=time.monotonic()+5
    try:
        while not _try_lock(f):
            if time.monotonic()>deadline:raise TimeoutError('History file is busy')
            time.sleep(.01)
        held=True;yield
    finally:
        if held:_unlock(f)
        f.close()


@asynccontextmanager
async def scope_lock(path: Path, key: str):
    digest = hashlib.sha256(key.encode()).hexdigest()[:24]
    lock_path = path.parent.resolve() / ".conversation-locks" / (digest + ".lock")
    queue_key = (asyncio.get_running_loop(), lock_path)
    local_lock = _scope_queues.get(queue_key)
    if local_lock is None:
        local_lock = _scope_queues[queue_key] = asyncio.Lock()
    # Let requests in this process queue in arrival order while keeping the cross-process lock.
    async with local_lock:
        file = _handle(lock_path)
        held = False
        deadline = time.monotonic() + 180
        try:
            while not _try_lock(file):
                if time.monotonic() > deadline:
                    raise TimeoutError("Conversation is busy")
                await asyncio.sleep(.04)
            held = True
            yield
        finally:
            if held:
                _unlock(file)
            file.close()


def atomic_json(path: Path, data: dict, *, compact: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix="." + path.stem + "-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            if compact:
                out.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            else:
                json.dump(data, out, ensure_ascii=False, indent=2)
        _retry_file_access(os.replace, name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def merge_sessions(path: Path, current: dict, baseline: dict) -> dict:
    """Merge the supplied scopes with the latest file and return a detached baseline."""
    changed = {key: row for key, row in current.items() if baseline.get(key) != row}
    removed = baseline.keys() - current.keys()
    with _file_lock(path.with_suffix(".write.lock")):
        latest = _read_raw(path)
        migrate = any(_key(name) != name for name in latest)
        if migrate:
            latest = _normalize_rows(latest.items())
        if migrate or changed or not removed.isdisjoint(latest):
            latest.update(changed)
            for key in removed:
                latest.pop(key, None)
            atomic_json(path, latest, compact=True)
    return deepcopy(current)


def apply_affection(store, *args, **kwargs):
    """Refresh and settle one score atomically across QQ and web processes."""
    with _file_lock(Path(store._path).with_suffix('.write.lock')):
        store.load()
        return store.apply(*args, **kwargs)
