"""角色人设与好感度分档加载器（persona.toml）。"""
from __future__ import annotations

import time
import tomllib
from pathlib import Path

from nonebot import logger

DEFAULT_PERSONA_FILE = Path(__file__).resolve().parent / "persona.toml"


class Persona:
    def __init__(self, path: Path = DEFAULT_PERSONA_FILE) -> None:
        self.path: Path = path
        self.name: str = "GPT龙娘"
        self.base: str = ""
        self.tiers: list[dict] = []
        self.loaded_at: float = 0.0
        self.error: str = ""

    def load(self) -> None:
        self.error = ""
        if not self.path.is_file():
            self.error = f"找不到 {self.path}"
            logger.error(f"chat_bot: {self.error}")
            return
        try:
            with open(self.path, "rb") as fh:
                conf = tomllib.load(fh)
        except Exception as exc:  # noqa: BLE001
            self.error = f"解析 persona.toml 失败：{exc}"
            logger.error(f"chat_bot: {self.error}")
            return

        persona_sec = conf.get("persona") or {}
        self.name = str(persona_sec.get("name") or "GPT龙娘").strip()
        self.base = str(persona_sec.get("base") or "").strip()
        raw_tiers = conf.get("tiers") or []
        tiers: list[dict] = []
        for item in raw_tiers:
            try:
                tiers.append(
                    {
                        "name": str(item.get("name", "?")),
                        "min": float(item.get("min", 0)),
                        "max": float(item.get("max", 0)),
                        "desc": str(item.get("desc", "")).strip(),
                    }
                )
            except (TypeError, ValueError):
                continue
        tiers.sort(key=lambda x: x["min"])
        self.tiers = tiers
        self.loaded_at = time.time()

        if not self.base:
            self.error = "persona.base 为空"
        if not tiers:
            self.error = (self.error + "; " if self.error else "") + "没有配置任何 tiers"
        if self.error:
            logger.warning(f"chat_bot: 人设有问题 —— {self.error}")
        else:
            names = ", ".join(t["name"] for t in tiers)
            logger.success(f"chat_bot: 人设已加载（{self.name}，{len(tiers)} 档：{names}）")
