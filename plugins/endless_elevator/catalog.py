"""Config-backed maps, collectibles, supplies and item generation."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from random import Random

from . import expedition, heirlooms

RARITIES = ("white", "green", "blue", "purple", "gold", "red")
SLOTS = ("武器", "护甲", "挂件", "核心")
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
        self.progression = data["expedition"]
        self.field_items = self.progression["field_items"]
        self.gear_sell = self.progression["gear_sell"]
        self.gear_sets = self.progression.get("gear_sets", {})
        self.standard_bases = tuple(key for key, item in self.equipment.items() if not item.get("gear_set"))
        self.red_bases = tuple(key for key, item in self.equipment.items() if item.get("gear_set"))
        self.gear_boxes = {k:(v["name"],tuple(v["grades"]),tuple(v["weights"]))
                           for k,v in self.progression["gear_boxes"].items()}
        self.rainbow = self.progression["rainbow"]
        for name in SLOTS:
            if not any(item["slot"] == name for item in self.equipment.values()):
                raise ValueError(f"缺少{name}装备配置")
        for recipe in self.recipes:
            if recipe["output"] not in self.items or any(
                ingredient not in ("any_blue", "any_green") and ingredient not in self.items for ingredient in recipe["requires"]
            ):
                raise ValueError("黑市配方含未知物品")
        self.collections = {key: item for key, item in self.items.items() if item["kind"] == "collection"}
        if len(self.rainbow) != 4 or any(not self.collections.get(k, {}).get("boss_only") for k in self.rainbow):
            raise ValueError("虹彩背包技能配置不完整")

    def definition(self, key: str) -> dict:
        if key in heirlooms.PIECES:
            return heirlooms.PIECES[key]
        return self.items[key] if key in self.items else self.equipment[key]

    def name(self, loot: dict, *, symbol: bool = True) -> str:
        definition = self.definition(loot["id"])
        quality, _, variant = loot.get("quality", "").partition(":")
        prefixes = self.qualities.get(quality, {}).get("prefixes", [""])
        prefix = prefixes[int(variant or 0) % len(prefixes)]
        label = definition["name"] if not prefix else f"【{prefix}】{definition['name']}"
        return self.rarities[self.rarity(loot)]["symbol"] + label if symbol else label

    def rarity(self, loot: dict) -> str:
        return loot.get("rarity") or ("shining" if loot["id"] in heirlooms.PIECES else self.items[loot["id"]]["rarity"])

    def value(self, loot: dict) -> int:
        return loot["value"]

    def ordinary_item(self, key: str, rng: Random, *, multiplier: float = 1.0, roll_quality: bool = False) -> dict:
        item = self.items[key]
        if item.get("boss_only"):
            raise ValueError("虹彩背包技能仅由首领胜利登记")
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
        return rng.sample(list(self.affixes), {"blue": 1, "purple": 2, "gold": 2, "red": 3}.get(grade, 0))

    def roll_gear(self, rng: Random, grade: str, multiplier: float = 1.0) -> dict:
        key = rng.choice(self.red_bases if grade == "red" else self.standard_bases)
        factor = self.progression["grade_factors"][grade]
        stats = {
            attr: round(value * (factor if value > 0 else 1) * rng.uniform(0.9, 1.1))
            for attr, value in self.equipment[key]["stats"].items()
        }
        value = self.gear_sell["red"] if grade == "red" else max(1, round(rng.randint(*self.rarities[grade]["range"]) * multiplier))
        return {
            "id": key, "rarity": grade, "value": value, "stats": stats,
            "affixes": self.roll_affixes(grade, rng), "quality": "", "rules_version": 2,
        }

    def migrated_stats(self, key, grade, rolled):
        """Transfer the old roll's approximate percentile, including negative trade-offs."""
        previous = self.progression["old_equipment_stats"].get(key)
        if previous is None or key not in self.equipment or grade not in self.progression["old_grade_factors"]:
            return dict(rolled)
        old_factor = self.progression["old_grade_factors"][grade]
        factor = self.progression["grade_factors"][grade]
        result = {}
        for stat, value in self.equipment[key]["stats"].items():
            old_value = previous.get(stat, 0)
            old_scale = old_value * (old_factor if old_value > 0 else 1)
            roll = max(.9, min(1.1, rolled[stat] / old_scale)) if old_scale and stat in rolled else 1
            result[stat] = round(value * (factor if value > 0 else 1) * roll)
        return result

    def migrate_loose_gear(self, item):
        if item.get("id") in self.equipment and item.get("rules_version", 1) < 2:
            item["stats"] = self.migrated_stats(item["id"], item["rarity"], item["stats"])
            item["rules_version"] = 2

    def gear_set_counts(self, base_ids):
        return Counter(self.equipment[key]["gear_set"] for key in base_ids
                       if self.equipment.get(key, {}).get("gear_set"))

    def apply_gear_sets(self, stats, perks, base_ids, *, sources=None):
        for code, count in self.gear_set_counts(base_ids).items():
            for size, bonus in self.gear_sets[code]["bonuses"].items():
                if count < int(size):
                    continue
                expedition.add_stats(stats, bonus.get("stats",{}), sources, f"{self.gear_sets[code]['name']} · {size}件")
                for key, value in bonus.get("perks", {}).items():
                    perks[key] = perks.get(key, 0) + value

    def gear_set_description(self, code):
        spec = self.gear_sets[code]
        lines = [spec["name"] + "套装"]
        for size, bonus in spec["bonuses"].items():
            effects = [f"{STAT_NAMES[k]}{n:+g}" for k,n in bonus.get("stats", {}).items()]
            for key, value in bonus.get("perks", {}).items():
                label = {"prism_combo":"追击率","prism_leech":"命中吸血","prism_boss":"对首领伤害"}.get(key)
                if label is None:
                    label = self.affixes[key]["name"]
                amount = f"{value:+g}" if key == "shield_start" else f"{value:+.1%}".replace(".0%","%")
                effects.append(label + amount)
            lines.append(f"{size}件 · " + "、".join(effects))
        return "\n".join(lines)

    def shop_category(self, key):
        if key in self.gear_boxes:
            return "装备箱"
        if key in ("coffee", "ice_tea") or self.items.get(key, {}).get("medicine"):
            return "回复药剂"
        if key in ("ticket", "safebox"):
            return "通行与扩容"
        return "战术补给"

    def box_description(self, key):
        return expedition.box_description(self.progression, key)

    def boss_rewards(self, run, rng, number):
        gold, red = expedition.boss_rates(number)
        result = []
        map_info = self.maps[run["map"]]
        map_scale = map_info["value_multiplier"]
        collection_scale = expedition.collection_value_multiplier(map_info)
        bonus = 1 + run["perks"].get("greed",0) + run.get("value_bonus",0)
        pricing = self.progression["trophy_value"]
        condition = pricing["limit"] - (pricing["limit"]-pricing["first"]) * 2**(-(number-1)/pricing["half_bosses"])
        for _ in range(2):
            roll = rng.random()
            if roll < red + gold:
                grade = "red" if roll < red else "gold"
                pool = [k for k,item in self.collections.items() if item["rarity"] == grade
                        and item.get("map",run["map"]) == run["map"] and item.get("min_floor",1) <= run["floor"]]
                result.append(self.ordinary_item(rng.choice(pool), rng, multiplier=collection_scale*bonus*condition))
            else:
                result.append(self.roll_gear(rng, rng.choice(("blue","purple")), map_scale*bonus))
        if not run.get("sim") and rng.random() < expedition.boss_red_gear_rate(number, map_info["multiplier"]):
            result.append(self.roll_gear(rng, "red", map_scale*bonus))
        return result

    def loot(
        self, run: dict, rng: Random, *, chest: bool, blessing: bool = False,
        boost: int = 0, minimum: str = "white",
    ) -> dict:
        map_data, perks = self.maps[run["map"]], run["perks"]
        floor = run["floor"]
        rate = expedition.rate(map_data, floor, run["stats"]["luk"], run["rare_bonus"],
                               perks.get("treasure", 0) if chest else 0, blessing)
        if rng.random() < rate:
            rarity = rng.choices(RARITIES[-2:], [self.rarities[grade]["weight"] for grade in RARITIES[-2:]])[0]
        else:
            rarity = rng.choices(RARITIES[:4], [self.rarities[grade]["weight"] for grade in RARITIES[:4]])[0]
        rank = min(len(RARITIES) - 1, max(RARITIES.index(minimum), RARITIES.index(rarity) + boost))
        rarity = RARITIES[rank]
        bonus = 1 + perks.get("greed", 0) + run.get("value_bonus", 0)
        multiplier = map_data["value_multiplier"] * bonus
        if rarity in RARITIES[-2:]:
            if chest and rarity == "gold" and rng.random() < 0.06:
                return self.ordinary_item(rng.choice(("crate_gold", "vip_card")), rng, multiplier=multiplier)
            options = [
                key for key, definition in self.collections.items()
                if definition["rarity"] == rarity and definition.get("map", run["map"]) == run["map"]
                and floor >= definition.get("min_floor", 1)
            ]
            return self.ordinary_item(rng.choice(options), rng, multiplier=expedition.collection_value_multiplier(map_data)*bonus)
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
