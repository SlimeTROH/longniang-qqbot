"""Structured browser views over the same transactional game engine as QQ."""
from __future__ import annotations

import hmac
import importlib
import json
import re
import secrets
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import TypedDict

import elevator_web_actions as web_actions


def _engine():
    path = Path(__file__).resolve().parent / "plugins/endless_elevator"
    # Import the rules without registering NoneBot handlers in the web process.
    package = ModuleType("_elevator_web_rules")
    package.__path__ = [str(path)]
    sys.modules[package.__name__] = package
    return importlib.import_module(package.__name__ + ".game")


rules = _engine()
expedition = rules.expedition
ACTIVE_READS = frozenset({"battle_log", "bag", "field_guide", "atlas", "rank", "all_rank",
                         "help", "lookup", "quests", "gallery", "shop", "warehouse",
                         "black_market", "equipment"})
ACTIONS = ACTIVE_READS | {
    "menu", "start", "simulate", "down", "rush", "rush_settings", "fight", "flee", "extract", "save_box",
    "swap_box", "use", "event", "rob", "rescue", "leave", "flare", "support", "buy", "sell",
    "wear", "autoequip", "reroll", "craft", "decode", "blessing", "rename", "field", "field_use", "field_pick",
}
MAP_DESCRIPTIONS = {
    "mall": "停业后的购物中心，适合熟悉探索路线。",
    "subway": "末班车已离站，向无人站台深处探索。",
    "abyss": "封锁的实验设施，首批战利品附一件保底装备。",
    "auction": "错过午夜开槌的地下拍卖场，重金珍藏与撤离风险并存。",
}


class Action(TypedDict):
    label: str
    action: str
    argument: str


def action(label: str, key: str, argument: str = "", *, params=None, command=None, **metadata) -> Action:
    value = {"label": label, "action": key, "argument": argument, **metadata}
    if params is not None:
        value["control"] = {"command": command or key, "params": params}
    return value


def _control(label: str, command: str, params=None, **metadata) -> dict:
    return {"label": label, "command": command, "params": params or {}, **metadata}


def _actions(choices) -> list[Action]:
    return [action(c.label, c.action, c.argument) for c in choices if c.action in ACTIONS]


def _reply(reply) -> dict:
    return {"text": reply.text, "choices": _actions(reply.choices), "card": reply.card,
            "lines": list(reply.card_lines)}


def _group_context(turn) -> tuple[list[dict], dict | None, int | None]:
    db = turn.db
    signals, own = [], None
    rows = db.execute(
        "SELECT s.owner_id,s.expires_at,s.helpers,p.nickname FROM signals s "
        "JOIN players p ON p.user_id=s.owner_id WHERE s.group_id=? AND s.expires_at>? "
        "ORDER BY s.expires_at DESC,s.owner_id", (turn.gid, turn.at),
    )
    for row in rows:
        if row["owner_id"] == turn.uid:
            own = {"expires_at": row["expires_at"], "helpers": len(json.loads(row["helpers"]))}
        elif turn.uid not in json.loads(row["helpers"]):
            signals.append({"nickname": row["nickname"], "expires_at": row["expires_at"],
                            "action": action("支援 " + row["nickname"], "support", row["owner_id"],
                                             params={"owner_id": row["owner_id"]})})
    active_buff = db.execute(
        "SELECT expires_at FROM group_buffs WHERE group_id=? AND expires_at>?", (turn.gid, turn.at),
    ).fetchone()
    return signals, own, active_buff["expires_at"] if active_buff else None


def _records(turn):
    db, uid = turn.db, turn.uid
    row = db.execute(
        "SELECT payload,updated_at,group_id FROM active_runs WHERE user_id=?", (uid,)
    ).fetchone()
    gear = turn._equipment_options()
    return row, turn._stock(), gear, _group_context(turn)


def _revision(turn, records, key: bytes) -> str:
    row, stock, gear, group = records
    # Fingerprint actual persisted payload: legacy runs hydrate field seeds on read.
    snapshot = [turn.gid, turn.day, turn.player, list(row) if row else None, stock,
                [entry.item for entry in gear], turn.archives, group, turn.rush_floors, turn.rush_skip_valuable,
                turn.rush_event_mode, turn.rush_auto_elite]
    data = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hmac.digest(key, data.encode("utf-8"), "sha256").hex()


def _affix_details(cat, ids) -> list[dict]:
    return [{"id": k, "name": cat.affixes[k]["name"], "description": cat.affixes[k]["description"]} for k in ids]


def _stock_item(turn, item, item_actions=None) -> dict:
    cat = turn.catalog
    key = item["id"]
    spec = cat.definition(key)
    row = {
        "id": key, "reference": f"#{item['stock_id']}" if "stock_id" in item else "",
        "stock_id": item.get("stock_id"),
        "name": cat.name(item, symbol=False), "rarity": cat.rarity(item),
        "kind": spec.get("kind", "equipment"), "quantity": item.get("quantity", 1),
        "value": item["value"], "description": turn._item_brief(key) or spec.get("description", ""),
        "actions": item_actions or [],
    }
    if key in cat.equipment:
        row.update(slot=spec["slot"], stats=item.get("stats", {}), affixes=_affix_details(cat, item.get("affixes", ())))
    return row


def _warehouse(turn, stock: list[dict]) -> list[dict]:
    rows = []
    for item in stock:
        reference = f"#{item['stock_id']}"
        kind = turn.catalog.definition(item["id"])["kind"]
        choices = [action("查看详情", "lookup", item["id"])]
        if turn.run is None:
            if kind == "crate":
                choices.append(action("开一个", "decode", reference + " 1", params={"stock_id": item["stock_id"]}))
            if item["id"] not in turn.catalog.rainbow:
                choices.append(action("出售", "sell", reference, params={"stock_id": item["stock_id"]}))
        rows.append(_stock_item(turn, item, choices))
    return rows


def _equipment(turn, rows) -> list[dict]:
    equipped = set(json.loads(turn.player["loadout"]).values())
    items = []
    for row in rows:
        item = row.item
        cat = turn.catalog
        key, grade = item["base_id"], item["rarity"]
        spec = cat.equipment[key]
        worn = item["equip_id"] in equipped
        choices = [action("查看详情", "equipment", row.reference)]
        if turn.run is None:
            if not worn:
                choices.extend((action("穿戴", "wear", row.reference, params={"equip_id": item["equip_id"]}),
                                action("出售", "sell", f"装备 {row.reference}", params={"equip_id": item["equip_id"]})))
            if grade != "green":
                choices.append(action("重铸", "reroll", row.reference, params={"equip_id": item["equip_id"]}))
        items.append({
            "id": key, "reference": row.reference, "equip_id": item["equip_id"],
            "name": spec["name"], "slot": spec["slot"], "rarity": grade, "kind": "equipment",
            "quantity": 1, "value": cat.gear_sell.get(grade, 260), "description": turn._item_brief(key),
            "set_name": cat.gear_sets[spec["gear_set"]]["name"] if spec.get("gear_set") else "",
            "stats": json.loads(item["stats"]), "equipped": worn, "actions": choices,
            "affixes": _affix_details(cat, json.loads(item["affixes"])),
        })
    return items


def _shop(turn) -> list[dict]:
    items = []
    for option in turn._shop_options():
        key = option.item["id"]
        spec = turn.catalog.items.get(key, {}) or turn.catalog.progression["gear_boxes"].get(key, {})
        options = [] if turn.run else [action("购买", "buy", option.reference, params={"id": key})]
        item = {
            "id": key, "name": option.label.partition(" · ")[0], "rarity": spec.get("rarity", "white"),
            "kind": spec.get("kind", "purchase"), "category": turn.catalog.shop_category(key),
            "quantity": 1, "value": option.item["price"],
            "price": option.item["price"], "description": turn._item_brief(key), "actions": options,
        }
        if key == "ticket":
            item["max_quantity"] = turn.remaining_ticket_purchases()
        items.append(item)
    return items


def _market(turn) -> list[dict]:
    rows = []
    for recipe in turn.catalog.weekly_recipes(turn.week):
        key = recipe["output"]
        spec = turn.catalog.items[key]
        enough = all(sum(i["quantity"] for i in turn._matching_stock(item)) >= count
                     for item, count in recipe["requires"].items())
        rows.append({
            "id": key, "name": spec["name"], "rarity": spec["rarity"], "kind": spec["kind"],
            "quantity": 1, "value": spec.get("value", 0), "description": turn._item_brief(key),
            "requirements": turn._recipe_requirements(recipe, with_progress=True), "featured": recipe["featured"],
            "ready": enough, "actions": [action("兑换", "craft", key, params={"id": key})]
            if not turn.run and enough else [],
        })
    return rows


def _atlas(turn) -> dict:
    cat = turn.catalog
    known = len(turn.archives.keys() & cat.collections.keys())
    total = len(cat.collections)
    stages = []
    for row in cat.progression["atlas"]:
        required = expedition.atlas_required(row,total)
        stages.append({**row, "required": required, "unlocked": known >= required})
    rainbow = [{
        "id": k, "name": v["name"], "description": v["description"], "lore": v.get("lore", ""),
        "set": "虹彩共鸣", "source": "正式首领独立 0.5% 掉落，四款等概率。掉落即入仓，首次点亮永久技能，可重复收藏。",
        "unlocked": k in turn.archives,
    } for k, v in cat.rainbow.items()]
    got_rainbow = sum(1 for r in rainbow if r["unlocked"])
    res_spec = cat.progression.get("resonance", {})
    resonance = {
        "id": "prism_resonance",
        "name": "虹彩共鸣",
        "description": res_spec.get("description") or cat.sets.get("虹彩共鸣", {}).get("description", ""),
        "lore": res_spec.get("lore") or cat.sets.get("虹彩共鸣", {}).get("lore", ""),
        "set": "虹彩共鸣",
        "source": "集齐四件首领专属虹彩背包技能后自动永久觉醒",
        "owned": got_rainbow,
        "total": len(rainbow),
        "unlocked": len(rainbow) > 0 and got_rainbow == len(rainbow),
    }
    items = []
    for k, v in cat.collections.items():
        source = ""
        if v.get("map") and v["map"] in cat.maps:
            mname = cat.maps[v["map"]]["name"]
            floor_hint = f"第 {v['min_floor']} 层起" if v.get("min_floor") else ""
            source = f"{mname}{floor_hint}限定掉落"
        items.append({
            "id": k, "name": v["name"], "rarity": v["rarity"], "description": v["description"],
            "lore": v.get("lore", ""), "unlocked": k in turn.archives, "set": v["set"],
            **({"source": source} if source else {}),
        })
    return {"known": known, "total": total, "stages": stages, "rainbow": rainbow, "resonance": resonance, "items": items}


def _earned_loot(catalog, items):
    return [{"id": item["id"], "name": catalog.name(item, symbol=False), "rarity": catalog.rarity(item),
             "value": item["value"], "quantity": 1, **({"banked": True} if item.get("banked") else {})} for item in items]


def _settlement(catalog, record):
    return {**record, "loot": _earned_loot(catalog, record.get("loot", []))} if record else None


def _run(turn, last_action, own_signal) -> dict:
    run, cat = turn.run, turn.catalog
    pending = run["event"]
    kind = pending["kind"]
    event = {"kind": kind}
    if kind == "combat":
        # Expose only values a player can already inspect before choosing.
        event["enemy"] = {
            k: v for k, v in pending["enemy"].items()
            if k in {"name", "hp", "max_hp", "atk", "def", "agi", "boss", "elite", "ambush", "rounds_fought"}
        }
        if (rank := pending["enemy"].get("boss")):
            event["boss_rules"] = {"exchange":expedition.BOSS_EXCHANGE_ROUNDS, "heavy":expedition.BOSS_HEAVY,
                                   "red_gear_rate":expedition.boss_red_gear_rate(rank,cat.maps[run["map"]]["multiplier"])}
    elif kind == "choice":
        spec = cat.events[pending["id"]]
        event.update(name=spec["name"], text=spec["text"], choices=[
            action(option["label"], "event", str(i), params={"option": i},
                   description=turn._describe_event_option(option, pending))
            for i, option in enumerate(spec["choices"], 1)
        ])
        event["controls"] = [
            _control(choice["label"], **choice["control"], description=choice["description"]) for choice in event["choices"]
        ]
        if "offer" in pending:
            event["offer"] = _stock_item(turn, pending["offer"])
            event["price"] = pending["price"]
    elif kind == "relic":
        box = turn._box()
        if box:
            event.update(name="遗物盒", text=f"{box['owner_name']} 遗落的物资，估值 {box['value']:,} 金。")
    bag = []
    safe_capacity = turn._capacity()
    for index, item in enumerate(run["bag"], 1):
        ref = str(index)
        choices = []
        if cat.definition(item["id"]).get("kind") == "supply":
            choices.append(action("使用", "use", ref, params={"index": index}))
        can_protect = turn._can_protect(item)
        if can_protect and len(run["safe"]) < safe_capacity:
            choices.append(action("锁入安全箱", "save_box", ref, params={"index": index}))
        bag.append({**_stock_item(turn, item, choices), "reference": ref, "index": index,
                    "can_protect": can_protect, "healing": item["id"] in {"coffee", "ice_tea"} or bool(cat.items.get(item["id"], {}).get("medicine"))})
    safe = [{**_stock_item(turn, item, [action("取回背包", "take_box", params={"safe_index": i})]),
             "reference": str(i), "index": i} for i, item in enumerate(run["safe"], 1)]
    def field_item(key):
        spec = cat.field_items[key]
        return {"id": key, "name": spec["name"], "kind": "field", "rarity": "field",
                "description": expedition.field_description(spec), "healing": bool(spec.get("heal"))}

    def active_charge(charge):
        key = charge["id"]
        name, description = expedition.field_charge_info(cat.field_items[key], charge)
        return {**field_item(key), "name": name, "description": description, "left": charge["left"]}

    field = []
    for key, quantity in run.get("field_items", {}).items():
        if cat.field_items[key]["mode"] == "wear":
            choices = [action(choice.label, "field_use", choice.argument,
                              params={"id": key, "slot": int(choice.argument.rsplit(" ", 1)[1])})
                       for choice in turn.field_equip_options(key)]
        else:
            label = "发动公正裁决" if cat.field_items[key].get("judgement") else "使用"
            choices = [action(label, "field_use", key, params={"id": key})]
        field.append({**field_item(key), "quantity": quantity, "value": 0, "actions": choices})
    slots = [{**field_item(key),
              "action": action("卸下", "field", f"解除 {key} {slot}", command="field_remove",
                               params={"id": key, "slot": slot})}
             if key else None for slot, key in enumerate(expedition.field_slots(run), 1)]
    field_offer = None
    if (offer := run.get("field_offer")):
        key, floor = offer["id"], offer["floor"]
        field_offer = {
            **field_item(key), "floor": floor,
            "choices": [action(choice.label, "field_pick", choice.argument,
                               params={"id": key, "floor": floor, "slot": int(choice.argument.rsplit(":", 1)[1])})
                        for choice in turn._field_offer_choices()],
        }
    buffs = [{**field_item(key), "end_floor": expires,
              "action": action("解除", "field", f"解除 {key}", command="field_remove", params={"id": key})}
             for key, expires in run.get("field_buffs", {}).items()]
    charges = [active_charge(charge)
               for mode in ("guard", "burst") if (charge := run.get(mode))]
    floor_result = None
    recorded = run.get("floor_result")
    if recorded and recorded["floor"] == run["floor"]:
        floor_result = {
            "floor": recorded["floor"], "title": recorded["title"],
            "loot": _earned_loot(cat, recorded["loot"]),
            "field": [{"id": key, "name": cat.field_items[key]["name"], "rarity": "field", "quantity": quantity}
                      for key, quantity in recorded["field"].items()],
            "battle": _settlement(cat, recorded.get("battle")),
        }
    return {
        "map": run["map"], "map_name": cat.maps[run["map"]]["name"], "floor": run["floor"],
        "hp": run["hp"], "max_hp": run["stats"]["hp"], "shield": run.get("shield", 0), "stats": run["stats"],
        "effective_stats": {**run["stats"], "atk": round(rules.combat.attack_power(run), 2),
                            "def": round(rules.combat.defence(run), 2), "agi": rules.combat.agility(run)},
        "damage_multiplier": run.get("field_damage_multiplier", 1),
        "radar_scan": turn.radar_scan(),
        "recovery": run.get("med_regen"),
        "simulation": bool(run.get("sim")), "event": event, "floor_result": floor_result,
        "adventure": _settlement(cat, run.get("adventure")),
        "bag": bag, "safe": safe, "safe_capacity": safe_capacity,
        "field": field, "field_slots": slots, "field_offer": field_offer,
        "field_worn": next((item for item in slots if item), None), "field_buffs": buffs, "field_charges": charges,
        "expires_at": last_action + rules.RUN_TIMEOUT, "value": turn._value(run["bag"] + run["safe"]),
        "signal": own_signal, "next_floor": turn._next_floor_summary(),
        "next_boss": expedition.next_boss(run["floor"]),
    }


def _lobby_actions(turn):
    if not turn.player["name_confirmed"]:
        return [action("沿用 QQ 群昵称", "rename", "__keep_lobby__")]
    return [
        action("今日悬赏", "quests"), action("本群排行榜", "rank"),
        action("全服排行榜", "all_rank"), action("图鉴", "atlas"),
        action("道具手册", "field_guide"), action("玩法说明", "help", "攻略"),
    ]


def _expedition_controls(turn, run) -> list[dict]:
    if run is None:
        return []
    if run["field_offer"]:
        return []
    event = run["event"]
    kind = event["kind"]
    escape = _control("撤离", "extract")
    if kind == "ready":
        return [_control("深入一层", "down"), _control(f"连战 {turn.rush_floors} 层", "rush"), escape]
    if kind == "choice":
        return [*event["controls"], _control("跳过", "leave"), escape]
    if kind == "relic":
        return [_control("拾取遗物", "rob"), _control("归还换赏金", "rescue"), _control("跳过", "leave"), escape]
    if kind != "combat":
        return [escape]
    enemy = event["enemy"]
    boss, ambush = enemy.get("boss"), enemy.get("ambush")
    controls = []
    if not run["signal"]:
        controls.append(_control("挑战首领" if boss else "攻击", "fight"))
        if not (boss or ambush):
            odds = rules.combat.flee_rate(turn.run)
            controls.append(_control(f"脱战 · {odds:.0%}", "flee"))
        if (enemy.get("elite") or ambush) and not turn.run.get("sos_used") and not run["simulation"]:
            controls.append(_control("呼叫支援", "flare"))
    if not ambush:
        controls.append(escape)
    return controls


def _snapshot(turn, records, revision: str) -> dict:
    record, stock, gear, group = records
    signals, own_signal, blessing_until = group
    p = turn.player
    sources = {}
    stats, perks = turn._stats(sources=sources)
    run = _run(turn, record["updated_at"] if record else turn.at, own_signal) if turn.run else None
    sheet = turn.run or {"stats":stats,"hp":stats["hp"],"perks":perks,"floor":1,"tea_until":0}
    if run:
        for key, permanent in sheet["stats"].items():
            if stats[key] != permanent:
                sources[key].append({"source":"本局入场面板","op":"set","value":permanent,"result":permanent})
        active_names = [item["name"] for item in run["field_slots"] if item]
        active_names.extend(item["name"] for item in run["field_buffs"])
        mods = sheet.get("field_mods",{})
        temp_label = "、".join(active_names) or "临装"
        atk_boost, field_atk = sheet.get("atk_boost",0), mods.get("atk",0)
        if atk_boost or field_atk:
            label = f"本局加攻 {atk_boost:+.0%}，{temp_label} {field_atk:+.0%}"
            sources["atk"].append({"source":label,"op":"multiply","value":max(.25,1+atk_boost+field_atk),
                                   "result":run["effective_stats"]["atk"]})
        if mods.get("defense"):
            sources["def"].append({"source":temp_label,"op":"multiply","value":1+mods["defense"],
                                   "result":run["effective_stats"]["def"]})
        tea = 15 if sheet["floor"] <= sheet["tea_until"] else 0
        if tea:
            sources["agi"].append({"source":"红茶","op":"add","value":tea,"result":sheet["stats"]["agi"]+tea})
        if mods.get("agi"):
            raw = sheet["stats"]["agi"] + tea + mods["agi"]
            sources["agi"].append({"source":temp_label,"op":"add","value":mods["agi"],"result":raw})
        if sources["agi"][-1]["result"] != run["effective_stats"]["agi"]:
            sources["agi"].append({"source":"敏捷下限","op":"set","value":0,"result":run["effective_stats"]["agi"]})
    warehouse = _warehouse(turn, stock)
    maps = [
        {"id": key, "name": info["name"], "entry": info["entry"], "extracts": info["extracts"],
         "unlocked": p["extracts"] >= info["extracts"], "description": info.get("description") or MAP_DESCRIPTIONS.get(key, ""),
         "multiplier": info["multiplier"], "rare_rate": info["rare_rate"],
         "action": action("开始探索", "start", key, params={"map_id": key}),
         "simulate": action("模拟探索", "simulate", key, params={"map_id": key})}
        for key, info in turn.catalog.maps.items()
    ]
    moves = _actions(turn.actions()) if run else _lobby_actions(turn)
    if run and run["event"]["kind"] == "choice":
        details = {choice["argument"]:choice for choice in run["event"]["choices"]}
        moves = [details[a["argument"]] if a["action"]=="event" else a for a in moves]
    return {
        "revision": revision, "player": {
            **{k: p[k] for k in ("nickname", "gold", "tickets", "paid_tickets", "safebox", "runs", "extracts")},
            "name_confirmed": bool(p["name_confirmed"]), "ticket_cap": turn.ticket_cap,
            "paid_ticket_cap": expedition.TICKET_PAID_CAP,
            "rush_floors": turn.rush_floors, "rush_skip_valuable": turn.rush_skip_valuable,
            "rush_event_mode": turn.rush_event_mode, "rush_auto_elite": turn.rush_auto_elite,
            "loadout": json.loads(p["loadout"]), "stats": stats, "stat_sources":sources,
            "secondary":rules.combat.secondary_stats(sheet, in_run=bool(run),
                                                    escape_blocked=bool(own_signal or sheet.get("field_offer"))),
        },
        "formulas": rules.combat.formula_guide(),
        "run": run, "maps": maps, "actions": moves, "controls": _expedition_controls(turn, run), "warehouse": warehouse,
        "supplies": [i for i in warehouse if i["kind"] == "supply"],
        "equipment": _equipment(turn, gear), "shop": _shop(turn), "market": _market(turn),
        "atlas": _atlas(turn), "signals": signals, "group_blessing_until": blessing_until,
        "tabs": [{"id": "game", "label": "电梯"}, {"id": "warehouse", "label": "仓库"},
                 {"id": "equipment", "label": "装备"}, {"id": "shop", "label": "商店"},
                 {"id": "atlas", "label": "图鉴"}],
        "notices": [{"id": r["notice_id"], "text": r["body"]} for r in turn.db.execute(
            "SELECT notice_id,body FROM notices WHERE user_id=? ORDER BY notice_id DESC LIMIT 6", (turn.uid,)
        )],
    }


class WebGame:
    """Check browser revisions and apply the same gameplay under a single database lock."""

    def __init__(self, path: Path, *, game=None, on_action=None) -> None:
        self.game = game if game is not None else rules.ElevatorGame(Path(path))
        self.on_action = on_action
        self._revision_key = secrets.token_bytes(32)

    def _turn(self, db, uid, group, nickname, at):
        turn = rules.Turn(self.game, db, uid, rules._nickname(nickname), group, at)
        if turn.run and at - turn.last_action >= rules.RUN_TIMEOUT:
            # A passive page refresh never extends the game timeout.
            turn.expire()
        return turn

    def state(self, uid: str, group: str, nickname: str = "乘客", *, now=None) -> dict:
        at = int(time.time() if now is None else now)
        with self.game.store.transaction() as db:
            turn = self._turn(db, uid, group, nickname, at)
            records = _records(turn)
            return _snapshot(turn, records, _revision(turn, records, self._revision_key))

    def act(self, uid: str, group: str, action: str, argument: str, revision: str,
            nickname: str = "乘客", *, now=None) -> dict:
        if not isinstance(action, str) or action not in ACTIONS or not isinstance(argument, str) or len(argument) > 240:
            raise ValueError("无效的游戏操作。")
        return self._apply(uid, group, revision, nickname, lambda _: (action, argument), now=now)

    def command(self, uid: str, group: str, command: str, params: dict, revision: str,
                nickname: str = "乘客", *, now=None) -> dict:
        web_actions.check_command(command, params)
        return self._apply(
            uid, group, revision, nickname, lambda turn: web_actions.prepare(turn, command, params, rules.GameError),
            now=now, command=command,
        )

    def _apply(self, uid, group, revision, nickname, prepare, *, now, command=None):
        if not isinstance(revision, str) or re.fullmatch(r"[a-f0-9]{64}", revision) is None:
            raise ValueError("缺少游戏状态版本，请先刷新。")
        at = int(time.time() if now is None else now)
        with self.game.store.transaction() as db:
            rules._settle_months(db, rules._clock(at).strftime("%Y-%m"))
            turn = self._turn(db, uid, group, nickname, at)
            records = _records(turn)
            current = _revision(turn, records, self._revision_key)
            if not hmac.compare_digest(revision,current):
                return {"state": _snapshot(turn, records, current), "reply": None,
                        "error": "状态有更新，请核对后再操作。", "code": "stale", "result": None}
            try:
                action, argument = prepare(turn)
            except rules.GameError as error:
                return {"state": _snapshot(turn, records, current), "reply": None,
                        "error": str(error), "code": "invalid", "result": None}
            if action == "menu":
                # On QQ this entry point can start a run for a returning newcomer.
                return {"state": _snapshot(turn, records, current), "reply": _reply(turn._lobby()),
                        "error": None, "code": None}
            touches = (action not in ACTIVE_READS and action != "rush_settings") or (action == "equipment" and argument.startswith(("穿", "一键")))
            if action == "field" and not argument.startswith("解除 "):
                touches = False
            old_floor = turn.run["floor"] if turn.run else None
            old_hp = turn.run["hp"] if turn.run else None
            old_gold = turn.player["gold"]
            pending = self.on_action.before(db, uid, group, action) if self.on_action else None
            turn, reply, code = self.game.apply_turn(
                turn, action, argument, touch=touches,
                cooldown=action not in {"rank", "all_rank"} and command not in web_actions.QUICK_COMMANDS,
            )
            if not code and self.on_action:
                self.on_action.after(db, uid, group, action, pending)
            updated = self._turn(db, uid, group, nickname, at)
            records = _records(updated)
            error = {"cooldown": "操作太快，请稍等再试。", "invalid": reply.text}.get(code)
            state = _snapshot(updated, records, _revision(updated, records, self._revision_key))
            response = {"state": state, "reply": _reply(reply), "error": error, "code": code}
            if command is not None:
                response["result"] = None if code else _command_result(command, state, turn, old_floor, old_hp, old_gold)
            return response


_COMMAND_MESSAGES = {
    "buy": "购买完成", "sell": "出售完成", "craft": "兑换完成",
    "decode": "盲盒已打开", "decode_all": "盲盒已打开", "wear": "装备已穿戴",
    "autoequip": "配装已更新", "reroll": "词条已重铸", "rename": "昵称已更新",
    "save_box": "已存入安全箱", "swap_box": "安全箱已更新", "take_box": "已取回背包", "use": "补给已使用",
    "field_use": "局内道具已使用", "field_remove": "效果已解除", "flare": "已发出求援信号",
    "rush_settings": "连战设置已保存",
    "support": "支援完成", "blessing": "已开启金雨祈福", "rob": "遗物已拾取",
    "rescue": "遗物已归还", "leave": "已跳过当前遭遇", "event": "奇遇已处理",
}


def _command_result(command, state, turn, old_floor, old_hp, old_gold) -> dict:
    run = state["run"]
    departure = getattr(turn, "last_departure", None)
    battle = _settlement(turn.catalog, getattr(turn, "last_battle", None))
    adventure = _settlement(turn.catalog, getattr(turn, "last_adventure", None))
    floor = run["floor"] if run else departure["floor"] if departure else None
    encounter = run["event"]["kind"] if run else None
    pickups = run["floor_result"] if run else None
    scan = getattr(turn,"last_radar_scan",None)
    rush = getattr(turn,"last_rush",None)
    if rush and battle and run and battle["floor"] != floor:
        battle = None
    if departure:
        if departure["simulation"] and departure["extracted"]:
            outcome, title = "simulation_complete", "演练结束"
        elif departure["extracted"]:
            outcome, title = "extracted", "安全撤离"
        elif departure.get("protected_by_doll"):
            outcome, title = "failed", "战败，替身保全"
        else:
            outcome, title = "failed", "探索失败"
        reward = f"估值 {departure['value']:,} 金" if departure["simulation"] else f"保住 {departure['value']:,} 金物资"
        summary = f"{title} · 第 {floor} 层 · {reward}"
    else:
        outcome = "ongoing" if run else "idle"
        if scan:
            summary = f"📡 热成像雷达 · F{scan['floor']} 侦测到「{scan['room']}」"
        elif rush and run:
            summary = f"连战至 F{floor} · {rush['reason']}"
        elif command in ("start", "simulate", "down", "rush") and run:
            summary = f"抵达第 {floor} 层"
        elif command == "field_pick":
            summary = "临装选择已完成"
        elif battle:
            summary = {"defeat": "战斗失败", "victory": "击败敌人", "draw": "战斗结束"}[battle["outcome"]]
        elif command == "extract" and encounter == "combat":
            summary = "撤离途中遭遇伏击"
        else:
            summary = _COMMAND_MESSAGES.get(command, "操作完成")
    return {
        "kind": command, "floor": floor, "previous_floor": old_floor, "encounter": encounter,
        "pickups": pickups, "hp_delta": run["hp"] - old_hp if run and old_hp is not None else None,
        "gold_delta": state["player"]["gold"] - old_gold, "outcome": outcome, "summary": summary,
        "limit_notices": turn.limit_notices,
        "radar_scan": scan,
        "rush": {"reason":rush["reason"], "floors":[
            {"floor":row["floor"], "title":row["title"], "loot":_earned_loot(turn.catalog,row["loot"]),
             "field":[turn.catalog.field_items[k]["name"] for k in row.get("field",{})],
             "battle":_settlement(turn.catalog,row.get("battle")),
             "adventure":_settlement(turn.catalog,row.get("adventure"))} for row in rush["floors"]
        ]} if rush else None,
        "battle": battle, "adventure": adventure, "departure": departure,
    }
