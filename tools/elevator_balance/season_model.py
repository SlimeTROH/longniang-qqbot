"""Boss-season calculator. Load the game itself without starting QQ or touching player saves."""
from __future__ import annotations

import copy
import importlib
import json
import math
import sqlite3
import sys
import types
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from random import Random
from types import SimpleNamespace

import numpy as np
import voyages
from model import finite, seeded

ROOT = Path(__file__).resolve().parent
source = ROOT / "season_rules"
if not source.is_dir():
    source = ROOT.parents[1] / "plugins/endless_elevator"
package = types.ModuleType("elevator_season_engine")
package.__path__ = [str(source)]
sys.modules[package.__name__] = package
game = importlib.import_module(package.__name__ + ".game")
schema = importlib.import_module(package.__name__ + ".storage").SCHEMA
expedition, combat = game.expedition, game.combat

PRESETS = {
    "new": ("无装新人", "", 0, 0),
    "green": ("绿装起步", "green", 3, 2),
    "blue": ("蓝装探索", "blue", 10, 8),
    "purple": ("紫装成型", "purple", 18, 16),
    "gold": ("金装老手", "gold", 25, 26),
    "red": ("日冕四件红装", "red", 25, 26),
    "rainbow": ("满图鉴四彩", "gold", 25, 44),
}
STANDARD_GEAR = ["crowbar", "vest", "belt", "dual_core"]
RED_GEAR = ["red_corona_" + suffix for suffix in ("weapon","armor","charm","core")]
AFFIX_PAIRS = [("heavy_strike","berserk"), ("shield_start","last_stand"),
               ("pierce","vampire"), ("high_roller","thorns")]
PREPARED = ["field_drive", "field_siphon", "field_ratchet"]


def apply_overrides(cat, info, changes):
    """Change an in-memory copy of game data for one experiment."""
    allowed = {"curve","equipment","map","box_prices","gear_sell"}
    if not isinstance(changes,dict) or set(changes)-allowed:
        raise ValueError("参数方案仅支持 curve、equipment、map、box_prices、gear_sell")
    curve = {}
    for kind, attributes in changes.get("curve",{}).items():
        if kind not in expedition.DIFFICULTY: raise ValueError("未知敌人类型")
        curve[kind] = {}
        for stat, values in attributes.items():
            if stat not in expedition.DIFFICULTY[kind] or len(values)!=3:
                raise ValueError("敌人属性需要三个非负系数")
            curve[kind][stat] = [finite(v,"曲线系数",0,10000) for v in values]
    info["difficulty"] = curve
    for item, stats in changes.get("equipment",{}).items():
        if not set(stats)<=cat.base.keys(): raise ValueError("未知装备属性")
        for stat,value in stats.items():
            cat.equipment[item]["stats"][stat] = finite(value,"装备底材",-100,1000,True)
    limits = {"entry":(0,1_000_000), "rare_rate":(0,1), "value_multiplier":(.05,100), "ambush_rate":(0,1)}
    for key,value in changes.get("map",{}).items():
        info[key] = finite(value,key,*limits[key],integer=key=="entry")
    for key,value in changes.get("box_prices",{}).items():
        if key not in cat.gear_boxes: raise ValueError("未知装备箱")
        cat.shop[key] = finite(value,"箱价",1,1_000_000,True)
    for key,value in changes.get("gear_sell",{}).items():
        if key not in cat.gear_sell: raise ValueError("未知品质")
        cat.gear_sell[key] = finite(value,"回收价",0,1_000_000,True)


def build(spec):
    cat = game.Catalog()
    key = spec.get("map", "mall")
    info = cat.maps[key]
    apply_overrides(cat, info, spec.get("overrides",{}))
    info["multiplier"] *= finite(spec.get("map_scale", 1), "地图强度倍率", .25, 4)
    _, grade, veteran, ordinary = PRESETS[spec.get("preset", "green")]
    veteran = finite(spec.get("extracts", veteran), "撤离数", 0, 1000, True)
    ordinary = finite(spec.get("known", ordinary), "普通藏品", 0, len(cat.collections)-len(cat.rainbow), True)
    buckets = defaultdict(list)
    for item, row in cat.collections.items():
        if item not in cat.rainbow:
            buckets[row["set"]].append(item)
    ordered = [group[i] for i in range(max(map(len,buckets.values()))) for group in buckets.values() if i<len(group)]
    rainbow = spec.get("rainbow", list(cat.rainbow) if spec.get("preset")=="rainbow" else [])
    if not set(rainbow) <= cat.rainbow.keys():
        raise ValueError("未知虹彩技能")
    owned = dict.fromkeys(ordered[:ordinary] + list(rainbow), 1)
    grade = spec.get("grade", grade or "green")
    default_gear = RED_GEAR if grade == "red" else STANDARD_GEAR
    slots = spec.get("gear", default_gear if spec.get("preset","green") != "new" else [""]*4)
    if len(slots)!=4 or grade not in cat.progression["grade_factors"]:
        raise ValueError("需要四个槽位及有效品质")
    factor = cat.progression["grade_factors"][grade]
    roll = finite(spec.get("roll", 1), "装备浮动", .9, 1.1)
    affix_count = {"green":0, "blue":1, "purple":2, "gold":2, "red":3}[grade]
    gear = []
    for slot, item, affixes in zip(game.SLOTS, slots, AFFIX_PAIRS):
        if not item: continue
        base = cat.equipment[item]
        if base["slot"] != slot: raise ValueError("装备槽位不匹配")
        if bool(base.get("gear_set")) != (grade == "red"):
            raise ValueError("红装使用红色套装底材，其他品质使用普通底材。")
        stats = {k:round(v*(factor if v>0 else 1)*roll) for k,v in base["stats"].items()}
        affixes = (*affixes, "slippery")
        gear.append({"base_id":item,"stats":json.dumps(stats), "affixes":json.dumps(affixes[:affix_count])})
    completed = [name for name in cat.sets if {k for k,v in cat.collections.items() if v["set"]==name} <= owned.keys()]
    panel = object.__new__(game.Turn)
    panel.catalog, panel.archives, panel.completed_sets = cat, owned, completed
    panel.player = {"extracts":veteran}
    stats, perks = panel._gear_panel(gear)
    hero = {"stats":stats, "perks":perks, "sets":completed, "archives":owned, "gear":gear, "extracts":veteran}
    return cat, key, hero


def bootstrap():
    cat = game.Catalog()
    return {"maps":{k:v["name"] for k,v in cat.maps.items()}, "presets":PRESETS, "standard":STANDARD_GEAR,"red_standard":RED_GEAR,
            "red_bases":list(cat.red_bases), "medicines":{k:v["name"] for k,v in cat.items.items() if v.get("medicine")},
            "gear":{s:{k:v["name"] for k,v in cat.equipment.items() if v["slot"]==s} for s in game.SLOTS},
            "rainbow":{k:v["name"] for k,v in cat.rainbow.items()},
            "field":{k:{"map":v["map"],"name":v["name"],"effect":expedition.field_description(v)}
                     for k,v in cat.field_items.items()}}


def battle(cat, key, hero, floor, kind, fields, n, seed, health=1.0, medicines=()):
    if kind not in ("normal","elite","boss"): raise ValueError("未知敌人种类")
    if any(cat.field_items[k]["map"] not in ("all",key) for k in fields):
        raise ValueError("这张地图无法拾取所选道具")
    if sum(cat.field_items[k]["mode"] == "wear" for k in fields) > getattr(expedition, "FIELD_WEAR_SLOTS", 1):
        raise ValueError("所选临装超过佩戴槽位")
    perks, stats = hero["perks"], hero["stats"]
    template = {"floor":floor, "stats":stats, "perks":perks, "hp":max(1,round(stats["hp"]*health)),
                "tea_until":0, "shield":perks.get("shield_start",0)+perks.get("prism_shield",0),
                "last_stand":False, "field_items":dict(Counter(fields))}
    judgement = any(cat.field_items[k].get("judgement") for k in fields)
    for k in fields:
        if not cat.field_items[k].get("judgement"):
            expedition.activate(template,k,cat.field_items)
    enemy = combat.monster(cat.maps[key],floor,Random(0),elite=kind=="elite",boss=kind=="boss")
    if any(not cat.items.get(k,{}).get("medicine") for k in medicines):
        raise ValueError("未知回复药剂")
    wins = ties = first_wins = rounds = after_hit_points = meds_used = 0
    for index in range(n):
        rng = Random(seed + index)
        run = copy.deepcopy(template)
        dose_ids = list(medicines)
        opponent = enemy.copy()
        run["event"] = {"kind":"combat","enemy":opponent}
        if judgement:
            if rng.random() < expedition.JUDGEMENT_DEATH_CHANCE:
                run["hp"] = 0
            else:
                opponent["hp"] = 0
                if opponent["boss"]:
                    run["hp"] = stats["hp"]
                elif perks.get("vampire") and not run.get("burst", {}).get("blood"):
                    run["hp"] = min(stats["hp"], run["hp"] + max(1, round(stats["hp"] * perks["vampire"])))
        else:
            rounds += combat.fight(run,opponent,rng).rounds
        first_wins += not opponent["hp"]
        while kind=="boss" and run["hp"] and opponent["hp"]:
            if dose_ids and run["hp"] <= .72 * stats["hp"]:
                for dose_index, dose in enumerate(dose_ids):
                    try:
                        expedition.use_medicine(run, cat.items[dose]["medicine"])
                    except ValueError:
                        continue
                    dose_ids.pop(dose_index)
                    meds_used += 1
                    break
            rounds += combat.fight(run,opponent,rng).rounds
        wins += not opponent["hp"]
        ties += bool(run["hp"] and opponent["hp"])
        after_hit_points += run["hp"]
    lo,hi = voyages.wilson(wins,n)
    return {"floor":floor,"kind":kind,"n":n,"win":wins/n,"ci":[lo,hi],"draw":ties/n,
            "first_win":first_wins/n,"rounds":rounds/n,"hp_after_combat":after_hit_points/n,"meds_used":meds_used/n,
            "enemy":{k:enemy[k] for k in ("hp","atk","def","agi")}}


class SeasonTrial(game.Turn):
    """Native transitions, deterministic solo decisions and isolated liquidation accounting."""
    protect = voyages.Trial.protect
    has_item = voyages.Trial.has_item
    use_one = voyages.Trial.use_one
    _take_loadout = voyages.Trial._take_loadout
    _eligible_relics = voyages.Trial._eligible_relics
    _advance_quest = voyages.Trial._advance_quest
    _record_battle_log = voyages.Trial._record_battle_log
    branch_exit = voyages.Trial.branch_exit
    observation = voyages.Trial.observation

    def reply(self,text,*args,**kwargs):
        return game.Reply(text)

    def _stats(self):
        # Scenario unlocks and veteran strength can be controlled independently.
        veteran = self.player["extracts"]
        self.player["extracts"] = self.hero["extracts"]
        try: return self._gear_panel(self.hero["gear"])
        finally: self.player["extracts"] = veteran

    def deposit(self,item,**kwargs):
        count = kwargs.get("quantity",1)
        self.appraisal += count * item["value"]
        sale = self.catalog.gear_sell[item["rarity"]] if item["id"] in self.catalog.equipment else item["value"]
        self.sale += count*sale

    def finish_run(self,extracted,reason="",*,forfeit_safe=False):
        self.terminal = (bool(extracted),self.run["floor"])
        if forfeit_safe:
            return super().finish_run(extracted,reason,forfeit_safe=True)
        return super().finish_run(extracted,reason)

    def heal(self):
        run = self.run
        pool = self.catalog.field_items
        if self.policy["field"]:
            for k in list(run["field_items"]):
                item = pool[k]
                if item["mode"]=="instant" and item.get("heal") and run["hp"] < .72*run["stats"]["hp"]:
                    expedition.activate(run,k,pool)
        voyages.Trial.heal(self)

    def handle_floor(self):
        run = self.run
        if offer := run.get("field_offer"):
            empty = [slot for slot, item in enumerate(expedition.field_slots(run), 1) if item is None]
            slot = empty[0] if empty and self.policy["field"] else 0
            expedition.choose_field(run, offer["id"], offer["floor"], slot, self.catalog.field_items)
        if self.policy["field"] and run["event"]["kind"]=="combat":
            pool = self.catalog.field_items
            self.heal()
            tough = run["event"]["enemy"]["elite"]
            for k in list(run["field_items"]):
                item = pool[k]
                mode = item["mode"]
                if mode=="wear" and not hasattr(expedition, "field_slots") and not run.get("field_worn"):
                    expedition.activate(run,k,pool)
                elif tough and mode=="timed" and len(run["field_buffs"])<3 and k not in run["field_buffs"]:
                    expedition.activate(run,k,pool)
                elif tough and mode=="guard" and not run.get("guard"):
                    expedition.activate(run,k,pool)
                elif tough and mode=="burst" and not item.get("blood") and not run.get("burst"):
                    expedition.activate(run,k,pool)
                elif tough and mode=="instant" and item.get("shield") and not item.get("heal"):
                    expedition.activate(run,k,pool)
        voyages.Trial.handle_floor(self)


def trips(cat,key,hero,floors,n,seed,use_field=True,coffee=2,smoke=1):
    paths = np.empty((n,len(floors),6))
    rules = {**voyages.DEFAULT_POLICY,"auto_use":True,"coffee":coffee,"smoke":smoke,"field":use_field}
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.executescript(schema)
        for i in range(n):
            db.execute("SAVEPOINT run")
            rng = Random(seeded(seed,"run",i))
            t = SeasonTrial(SimpleNamespace(catalog=cat,rng=rng), db, "calculator", "测算", "solo", 1795430000+i)
            t.hero, t.policy = hero, rules
            t.archives, t.completed_sets = dict(hero["archives"]), list(hero["sets"])
            t.fights = t.rounds = t.sale = t.appraisal = 0
            t.player.update(gold=voyages.WALLET,tickets=12,name_confirmed=1,safebox=1,runs=5,
                            extracts=max(hero["extracts"],cat.maps[key]["extracts"]))
            t.start(key,"")
            done = 0
            while t.run and done<len(floors):
                floor = t.run["floor"]
                t.protect()
                t.handle_floor()
                if not t.run: break
                if t.run["event"]["kind"]=="combat": continue
                if floor == floors[done]:
                    paths[i,done] = t.branch_exit(seeded(seed,"exit",i,floor))
                    done += 1
                if done < len(floors): t.down("","")
            if done < len(floors): paths[i,done:] = t.observation()
            db.execute("ROLLBACK TO run")
            db.execute("RELEASE run")
    return voyages.summarize(paths,floors)


def economy(cat):
    boxes = {}
    for k,(_,grades,weights) in cat.gear_boxes.items():
        ev = sum(p*cat.gear_sell[g]/100 for p,g in zip(weights,grades))
        boxes[k] = {"price":cat.shop[k], "sale_ev":ev, "net":ev-cat.shop[k]}
    p = expedition.RAINBOW_CHANCE
    def complete(k):
        return 1-sum(math.comb(k,i)*p**i*(1-p)**(k-i) for i in range(min(3,k)+1))
    return {"boxes":boxes, "bosses_first_mean":1/p, "bosses_set_mean":4/p,
            "set_percentiles":{str(q):next(k for k in range(1,4000) if complete(k)>=q) for q in (.5,.9,.95)}}


def calculate(req):
    cat,key,hero = build(req)
    n = finite(req.get("samples",500),"样本数",10,10000,True)
    seed = finite(req.get("seed",261123),"种子",0,4294967295,True)
    floors = sorted({finite(f,"楼层",1,300,True) for f in req.get("floors",[1,4,8,12,18,30])})
    if not 1<=len(floors)<=15: raise ValueError("请选择 1 至 15 个楼层")
    output = {"stats":hero["stats"], "perks":hero["perks"], "power":round(combat.loadout_score(hero["stats"],hero["perks"])),
              "economy":economy(cat)}
    if req.get("mode")=="trip":
        output["trips"] = trips(cat,key,hero,floors,min(3000,n),seed,req.get("use_field",True),
                                finite(req.get("coffee",2),"咖啡",0,8,True),finite(req.get("smoke",1),"烟雾弹",0,4,True))
    else:
        fields = req.get("field",[])
        health = finite(req.get("health",1),"开战生命比例",.01,1)
        output["fights"] = [battle(cat,key,hero,f,k,fields,n,seed+f*10009,health,req.get("medicines",())) for f in floors
                            for k in (("boss",) if expedition.boss_number(f) else ("normal","elite"))]
    return output
