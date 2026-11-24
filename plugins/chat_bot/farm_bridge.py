"""农场桥接层（向后兼容接口，底层委托统一操控引擎 game_controller）。"""
from __future__ import annotations

from .game_controller import (
    SOIL_ISSUES,
    SOIL_LEVEL_NAMES,
    CropInfo,
    FarmSnapshot,
    SoilInfo,
    build_farm_block,
    ensure_farm_ready as _ensure_ready,
    format_farm_for_prompt as format_for_prompt,
    format_market,
    load_crop_market,
    read_farm,
)

__all__ = [
    "SOIL_LEVEL_NAMES",
    "SOIL_ISSUES",
    "SoilInfo",
    "FarmSnapshot",
    "CropInfo",
    "read_farm",
    "load_crop_market",
    "format_market",
    "format_for_prompt",
    "build_farm_block",
]
