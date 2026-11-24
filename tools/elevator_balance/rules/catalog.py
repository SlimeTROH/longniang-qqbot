"""Config-backed maps, collectibles, supplies and item generation."""
from __future__ import annotations

import json
from pathlib import Path
from random import Random

from .combat import clamp

RARITIES = ("white", "green", "blue", "purple", "gold", "red")
SLOTS = ("武器", "护甲", "挂件")
STAT_NAMES = {"hp": "生命", "atk": "攻击", "def": "防御", "agi": "敏捷", "luk": "幸运"}


class Catalog:
    def __init__(self, path: Path | None = None):
        path = path or Path(__file__).with_name("content.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.base = data["stats"]
        self.maps = data["maps"]
        self.weights = data["event_weights"]
        self.rarities = data["rarities"]
        self.qualities = data["qualities"]
        self.items = data["items"]
        self.equipment = data["equipment"]
        self.affixes = data["affixes"]
        self.sets = data["sets"]
        self.events = {event["id"]: event for event in data["events"]}
        self.recipes = data["recipes"]
        self.shop = data["shop"]
        self.safebox_prices = data["safebox_prices"]
        self.reforge_price = data["reforge_price"]
        self.group_buff_price = data["group_buff_price"]
        for name in SLOTS:
            if not any(item["slot"] == name for item in self.equipment.values()):
                raise ValueError(f"缺少{name}装备配置")
        for recipe in self.recipes:
            if recipe["output"] not in self.items or any(
                ingredient not in ("any_blue", "any_green") and ingredient not in self.items for ingredient in recipe["requires"]
            ):
                raise ValueError("黑市配方含未知物品")
        self.collections = {key: item for key, item in self.items.items() if item["kind"] == "collection"}

    def definition(self, key: str) -> dict:
        return self.items[key] if key in self.items else self.equipment[key]

    def name(self, loot: dict, *, symbol: bool = True) -> str:
        definition = self.definition(loot["id"])
        quality, _, variant = loot.get("quality", "").partition(":")
        prefixes = self.qualities.get(quality, {}).get("prefixes", [""])
        prefix = prefixes[int(variant or 0) % len(prefixes)]
        label = definition["name"] if not prefix else f"【{prefix}】{definition['name']}"
        return self.rarities[self.rarity(loot)]["symbol"] + label if symbol else label

    def rarity(self, loot: dict) -> str:
        return loot.get("rarity") or self.items[loot["id"]]["rarity"]

    def value(self, loot: dict) -> int:
        return loot["value"]

    def ordinary_item(self, key: str, rng: Random, *, multiplier: float = 1.0, roll_quality: bool = False) -> dict:
        item = self.items[key]
        base = item["value"] if "value" in item else rng.randint(*self.rarities[item["rarity"]]["range"])
        quality = ""
        if roll_quality and item["kind"] == "material" and item["rarity"] in RARITIES[:4]:
            tags = list(self.qualities)
            tag = rng.choices(tags, [self.qualities[tag]["weight"] for tag in tags])[0]
            definition = self.qualities[tag]
            quality = f"{tag}:{rng.randrange(len(definition['prefixes']))}"
            multiplier *= definition["multiplier"]
        return {"id": key, "value": max(1, round(base * multiplier)), "quality": quality}

    def roll_affixes(self, grade: str, rng: Random) -> list[str]:
        return rng.sample(list(self.affixes), {"blue": 1, "purple": 2, "gold": 2}.get(grade, 0))

    def roll_gear(self, rng: Random, grade: str, multiplier: float = 1.0) -> dict:
        key = rng.choice(list(self.equipment))
        factor = {"green": 1.25, "blue": 1.85, "purple": 2.75, "gold": 4.0}[grade]
        stats = {
            attr: round(value * (factor if value > 0 else 1) * rng.uniform(0.9, 1.1))
            for attr, value in self.equipment[key]["stats"].items()
        }
        value = max(1, round(rng.randint(*self.rarities[grade]["range"]) * multiplier))
        return {
            "id": key, "rarity": grade, "value": value, "stats": stats,
            "affixes": self.roll_affixes(grade, rng), "quality": "",
        }

    def loot(
        self, run: dict, rng: Random, *, chest: bool, blessing: bool = False,
        boost: int = 0, minimum: str = "white",
    ) -> dict:
        map_data, perks = self.maps[run["map"]], run["perks"]
        floor = run["floor"]
        rate = map_data["rare_rate"] * (1 + (floor - 1) * 0.12)
        rate *= 1 + max(0, run["stats"]["luk"]) * 0.012
        rate *= 1 + run["rare_bonus"]
        if chest:
            rate *= 1 + perks.get("treasure", 0)
        if blessing:
            rate *= 1.20
        rate = clamp(rate, 0, 0.75)
        if rng.random() < rate:
            rarity = rng.choices(RARITIES[-2:], [self.rarities[grade]["weight"] for grade in RARITIES[-2:]])[0]
        else:
            rarity = rng.choices(RARITIES[:4], [self.rarities[grade]["weight"] for grade in RARITIES[:4]])[0]
        rank = min(len(RARITIES) - 1, max(RARITIES.index(minimum), RARITIES.index(rarity) + boost))
        rarity = RARITIES[rank]
        multiplier = map_data["value_multiplier"] * (1 + perks.get("greed", 0) + run.get("value_bonus", 0))
        if rarity in RARITIES[-2:]:
            if chest and rarity == "gold" and rng.random() < 0.06:
                return self.ordinary_item(rng.choice(("crate_gold", "vip_card")), rng, multiplier=multiplier)
            options = [
                key for key, definition in self.collections.items()
                if definition["rarity"] == rarity and definition.get("map", run["map"]) == run["map"]
                and floor >= definition.get("min_floor", 1)
            ]
            return self.ordinary_item(rng.choice(options), rng, multiplier=multiplier)
        extra = rng.random()
        if rank and extra < 0.16:
            return self.roll_gear(rng, rarity, multiplier)
        if rarity in ("blue", "purple") and extra < 0.22:
            return self.ordinary_item("crate_" + rarity, rng, multiplier=multiplier)
        if extra < 0.35:
            supplies = [
                key for key, definition in self.items.items()
                if definition["kind"] == "supply" and definition["rarity"] == rarity and key != "doll"
            ]
            if supplies:
                return self.ordinary_item(rng.choice(supplies), rng, multiplier=multiplier)
        if rarity == "purple" and extra > 0.995:
            return self.ordinary_item("doll", rng, multiplier=multiplier)
        candidates = [
            key for key, definition in self.items.items()
            if definition["kind"] == "material" and definition.get("map") == run["map"]
            and definition["rarity"] == rarity
        ]
        return self.ordinary_item(rng.choice(candidates), rng, multiplier=multiplier, roll_quality=True)

    def weekly_recipes(self, week: str) -> list[dict]:
        featured = set(Random(week).sample(range(len(self.recipes)), min(3, len(self.recipes))))
        recipes = []
        for index, recipe in enumerate(self.recipes):
            requirements = dict(recipe["requires"])
            if index in featured:
                first = next(iter(requirements))
                if requirements[first] > 1:
                    requirements[first] -= 1
                elif len(requirements) > 1:
                    del requirements[first]
            recipes.append({**recipe, "requires": requirements, "featured": index in featured})
        return recipes
