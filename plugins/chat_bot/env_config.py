"""环境变量与运行时配置读取（支持 UTF-8 / GB18030 自动识别与按 mtime+size 热重读）。"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from nonebot import get_driver, logger
from shared_history import atomic_json

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = ROOT_DIR / ".env"
CHAT_MODEL_PATH = ROOT_DIR / "state" / "chat_model.json"
SOLVE_MODEL_PATH = ROOT_DIR / "state" / "solve_model.json"

# https://developers.openai.com/api/docs/guides/reasoning#reasoning-effort
_REASONING_LEVELS = ("low", "medium", "high", "xhigh", "max")
CHAT_MODEL_EFFORTS = {
    "gpt-6.2-astra": _REASONING_LEVELS,
    "gpt-6.2-sol": _REASONING_LEVELS,
    "gpt-6.1-sol": _REASONING_LEVELS,
    "gpt-6.1-terra": ("none", *_REASONING_LEVELS),
    "gpt-6-astra": _REASONING_LEVELS,
    "gpt-6-sol": ("none", *_REASONING_LEVELS),
    "gpt-6-luna": ("none", *_REASONING_LEVELS),
}

_ENV_FILE_CACHE: dict = {"sig": None, "data": {}}


def parse_env_file(path: Path) -> dict[str, str]:
    """解析 .env 文件（兼容 UTF-8-SIG / GB18030 / Latin-1，支持行尾 ` #` 注释）。"""
    data: dict[str, str] = {}
    try:
        raw = path.read_bytes()
    except OSError:
        return data

    text = ""
    for enc in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        for marker in (" #", "\t#"):
            if marker in value:
                value = value.split(marker, 1)[0].strip()
                break
        data[key.strip()] = value.strip('"').strip("'")
    return data


def read_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    """读取 .env（仅在 mtime_ns 或文件大小变化时重新解析）。"""
    try:
        stat = path.stat()
        sig = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        return {}
    if sig != _ENV_FILE_CACHE["sig"]:
        _ENV_FILE_CACHE["data"] = parse_env_file(path)
        _ENV_FILE_CACHE["sig"] = sig
    return _ENV_FILE_CACHE["data"]


def get_env(name: str, default: str = "") -> str:
    """优先读取 .env 热配置，回退 NoneBot Driver 配置与系统环境变量。"""
    file_env = read_env_file()
    if name in file_env:
        return file_env[name] or default
    try:
        raw = getattr(get_driver().config, name.lower(), None)
    except Exception:  # noqa: BLE001
        raw = None
    if raw is None or raw == "":
        raw = os.environ.get(name)
    return (str(raw).strip() if raw is not None else "") or default


def chat_model_family(model: str) -> str:
    return re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)


def chat_model_efforts(model: str) -> tuple[str, ...]:
    return CHAT_MODEL_EFFORTS.get(chat_model_family(model), ())


def _load_saved_model(path: Path) -> tuple[str, str] | None:
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            model, effort = saved.get("model"), saved.get("effort")
            if isinstance(model, str) and effort in chat_model_efforts(model):
                return model, effort
        logger.warning("chat_bot: 已保存的模型设置无效，使用环境配置")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning(f"chat_bot: 读取模型设置失败，使用环境配置（{type(exc).__name__}）")
    return None


def load_chat_model(default_model: str, default_effort: str) -> tuple[str, str]:
    return _load_saved_model(CHAT_MODEL_PATH) or (
        get_env("CHAT_OPENAI_CHAT_MODEL", default_model),
        get_env("CHAT_OPENAI_CHAT_EFFORT", default_effort).lower(),
    )


def save_chat_model(model: str, effort: str) -> None:
    atomic_json(CHAT_MODEL_PATH, {"model": model, "effort": effort})


def load_solve_model(default_model: str, default_effort: str) -> tuple[str, str]:
    return _load_saved_model(SOLVE_MODEL_PATH) or (default_model, default_effort)


def save_solve_model(model: str, effort: str) -> None:
    atomic_json(SOLVE_MODEL_PATH, {"model": model, "effort": effort})


def env_bool(name: str, default: bool | str = "1") -> bool:
    if isinstance(default, bool):
        def_str = "1" if default else "0"
    else:
        def_str = str(default)
    return get_env(name, def_str).strip().lower() not in ("0", "false", "no", "")


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    lo: int | None = None,
    hi: int | None = None,
) -> int:
    low = minimum if minimum is not None else lo
    high = maximum if maximum is not None else hi
    try:
        val = int(get_env(name, str(default)) or str(default))
    except ValueError:
        val = default
    if low is not None:
        val = max(low, val)
    if high is not None:
        val = min(high, val)
    return val


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    lo: float | None = None,
    hi: float | None = None,
) -> float:
    low = minimum if minimum is not None else lo
    high = maximum if maximum is not None else hi
    try:
        val = float(get_env(name, str(default)) or str(default))
    except ValueError:
        val = default
    if low is not None:
        val = max(low, val)
    if high is not None:
        val = min(high, val)
    return val


def admin_set() -> set[str]:
    """管理员 QQ 集合（沿用 CHAT_ADMIN_LIST 或 DSH_BRIDGE_ALLOWLIST）。"""
    raw = get_env("CHAT_ADMIN_LIST") or get_env("DSH_BRIDGE_ALLOWLIST")
    return {x.strip() for x in raw.split(",") if x.strip()}


def is_admin(user_id: object) -> bool:
    admins = admin_set()
    if not admins:
        return True
    return str(user_id) in admins
