"""Floor, atlas and run-only item rules shared by the game and its calculator."""
from __future__ import annotations

import math
from random import Random

RAINBOW_CHANCE = .005
RARE_CAP = .68
FIELD_CHANCE = .52
FIELD_HEAL_WEIGHT = 1.5
FIELD_EQUIPPED_WEIGHT = .4
FIELD_WEAR_SLOTS = 2
ARCHIVE_VERSION = 2
FIELD_TIMED_SLOTS = 3
JUDGEMENT_DEATH_CHANCE = .5
BOSS_EXCHANGE_ROUNDS = 4
BOSS_HEAVY = 1.26
TICKET_DAILY_REFILL = 20
TICKET_DAILY_PURCHASE_LIMIT = 20
TICKET_PAID_CAP = 20
TICKET_BASE_CAPACITY = TICKET_DAILY_REFILL + TICKET_PAID_CAP
WEB_URL = "https://elevator.slime.icu/elevator/"
WEB_ENTRY = f"🌐 网页版（绑定 QQ 后共用存档）\n{WEB_URL}"
STAT_LABELS = {"hp":"生命","atk":"攻击","def":"防御","agi":"敏捷","luk":"幸运"}
MOD_LABELS = {"atk":"攻击","defense":"防御","agi":"敏捷","crit":"暴击率","dodge":"闪避率",
              "flee":"逃跑率","leech":"命中吸血","combo":"追击率","mitigation":"减伤","pierce":"穿透",
              "first":"首回合攻击","boss":"对首领伤害","thorns":"反伤","regen":"进层回血"}
MOD_CAPS = {"atk":(-.5,1.2),"defense":(-.55,1.4),"agi":(-30,55),"crit":(-.25,.4),
            "dodge":(-.25,.3),"flee":(-.25,.4),"leech":(0,.42),"combo":(0,.70),
            "mitigation":(-.50,.55),"pierce":(0,.55),"first":(0,.85),"boss":(0,.85),
            "thorns":(0,.65),"regen":(0,.12)}


def ticket_capacity(completed_sets, sets):
    return TICKET_BASE_CAPACITY + sum(sets[name].get("ticket_cap", 0) for name in completed_sets)


def ticket_status(player, day, capacity):
    """Project the shared daily ticket ledger without writing to the save."""
    tickets = player["tickets"]
    paid = player["paid_tickets"] if "paid_tickets" in player.keys() else -1
    if paid < 0:
        # Older saves mixed the two balances. Preserve today's purchases and any
        # carried balance above the former free refill as paid, up to the amount held.
        paid = min(tickets, TICKET_PAID_CAP, max(player["bought_tickets"], tickets - 10)) if player["ticket_day"] else 0
    free = tickets - paid
    if player["ticket_day"] != day:
        return min(capacity, max(TICKET_DAILY_REFILL, free) + paid), 0, paid
    prior_refill = player["daily_ticket_refill"] if "daily_ticket_refill" in player.keys() else 10
    supplement = max(0, TICKET_DAILY_REFILL - prior_refill)
    return min(capacity, tickets + supplement), player["bought_tickets"], paid


def boss_number(floor: int) -> int:
    if floor == 8:
        return 1
    if floor == 18:
        return 2
    if floor >= 30 and (floor - 30) % 20 == 0:
        return 3 + (floor - 30) // 20
    return 0


def next_boss(floor: int) -> int:
    if floor < 8: return 8
    if floor < 18: return 18
    if floor < 30: return 30
    return 50 + 20 * ((floor - 30) // 20)


def landing(floor: int, distance: int) -> int:
    return min(floor + distance, next_boss(floor))


DIFFICULTY = {
    # Each row is (floor-one base, persistent growth, diminishing growth).
    "normal": {"hp": (40, 1.15, 4.5), "atk": (10.8, .13, .88),
               "def": (2.7, .045, .16), "agi": (7, .09, .58)},
    "elite": {"hp": (74, 1.9, 11.4), "atk": (15.2, .16, 1.16),
              "def": (3.1, .07, .24), "agi": (8, .11, .74)},
    "boss": {"hp": (198, 2.60, 7.1), "atk": (30.5, .21, .58),
             "def": (4.4, .095, .33), "agi": (9, .12, .8)},
}


def enemy_stats(multiplier: float, floor: int, *, elite: bool, boss: int = 0, curve=None) -> dict:
    x = max(0, floor - 1)
    z = 6 * math.log1p(x / 6)
    kind = "boss" if boss else "elite" if elite else "normal"
    coefficients = {**DIFFICULTY[kind], **(curve or {}).get(kind, {})}
    result = {key: base + slope*x + taper*z for key, (base, slope, taper) in coefficients.items()}
    if boss:
        rank = math.log2(boss)
        result["hp"] *= 1 + .055*rank
        result["atk"] *= 1 + .033*rank
    return {"hp": max(1, int(result["hp"]*multiplier)), "atk": max(1, int(result["atk"]*multiplier)),
            "def": max(0, int(result["def"]*multiplier)), "agi": int(min(55, result["agi"]))}


def boss_rates(number: int) -> tuple[float, float]:
    """Per trophy roll. The separate rainbow draw is always once per victory."""
    return .60, .15 + min(.05, .01 * max(0,number-1))


def boss_red_gear_rate(number: int, map_multiplier: float) -> float:
    depth = .023 * (1 - 2 ** (-max(0, number - 1) / 4))
    region = min(.003, max(0, map_multiplier - 1) * (.003 / 3.45))
    return min(.03, .004 + depth + region)


def rate(map_info, floor, luck, rare_bonus=0, treasure=0, blessing=False):
    p = map_info["rare_rate"] * (1 + .12*(floor-1)) * (1 + .012*max(0,luck))
    return min(RARE_CAP,max(0,p*(1+rare_bonus)*(1+treasure)*(1.2 if blessing else 1)))


def weights(rows, floor):
    first, last = rows[0]["weights"], rows[-1]["weights"]
    u = 1 - 2 ** (-max(0,floor-1) / 12)
    return {k:v+(last[k]-v)*u for k,v in first.items()}


def atlas_required(stage, total):
    return stage["required"] if "required" in stage else math.ceil(stage["percent"]*total/100)


def milestones(rules, known, total):
    return [row for row in rules["atlas"] if total and known >= atlas_required(row,total)]


def stat_line(stats):
    return "、".join(f"{STAT_LABELS.get(k,k)}{v:+g}" for k,v in stats.items())


def box_odds(rules,key):
    box=rules["gear_boxes"][key]
    names={"green":"绿","blue":"蓝","purple":"紫","gold":"金","red":"红"}
    return " / ".join(f"{names[k]} {p:g}%" for k,p in zip(box["grades"],box["weights"]))


def box_description(rules, key):
    box = rules["gear_boxes"][key]
    prices = rules["gear_sell"]
    expected = sum(prices[g]*p for g,p in zip(box["grades"], box["weights"])) / sum(box["weights"])
    return f"{box['tier']} · 每箱 1 件。{box_odds(rules,key)}。平均回收 {expected:,.1f} 金。"


def collection_value_multiplier(map_info):
    """Compress the ordinary-loot multiplier range of 1–4.3 into 1–1.6 for collectibles."""
    ordinary_bonus = map_info["value_multiplier"] - 1.0
    return 1.0 + min(0.60, max(0.0, ordinary_bonus * (0.60 / 3.30)))


def health_limit_note(max_hp: int) -> str:
    return f"本次治疗达到生命上限 {max_hp} HP，溢出的回复不会保留。"


def ring_shield_limit(run):
    base = run["perks"].get("prism_floor_shield", 0)
    return base + (run["floor"] - 1) // 2 if base else 0


def refill_ring_shield(run):
    limit = ring_shield_limit(run)
    if not limit or run.get("ring_shield_floor") == run["floor"]:
        return
    amount = 0 if run.get("burst", {}).get("blood") else limit
    run["shield"] = run.get("shield", 0) - run.get("ring_shield", 0) + amount
    run["ring_shield"] = amount
    run["ring_shield_floor"] = run["floor"]


def repair_legacy_ring_shield(run):
    """Old saves mixed round grants and ordinary shield in one balance."""
    perks = run["perks"]
    old_base = perks.pop("prism_round_shield", None)
    if old_base is None:
        return False
    perks.pop("prism_shield_growth", None)
    perks["prism_floor_shield"] = old_base
    # Keep the remaining shield up to the known ordinary opening grants.
    opening = int(perks.get("shield_start", 0) + perks.get("prism_shield", 0))
    run["shield"] = min(run.get("shield", 0), opening)
    refill_ring_shield(run)
    return True


def use_medicine(run, spec):
    if run.get("burst", {}).get("blood"):
        raise ValueError("锁血蓄力期间无法治疗。")
    if spec.get("combat_only") and run["event"]["kind"] != "combat":
        raise ValueError("这种药剂只能在交战中使用。")
    if spec.get("visits") and run.get("med_regen"):
        raise ValueError("上一次缓释治疗还在生效。")
    maximum = run["stats"]["hp"]
    fraction = spec["heal"]
    if "critical_at" in spec and run["hp"] <= maximum * spec["critical_at"]:
        fraction = spec["critical_heal"]
    healed = min(maximum-run["hp"], round(maximum*fraction) + spec.get("flat_heal",0))
    ordinary_shield = run.get("shield", 0) - run.get("ring_shield", 0)
    shield = max(0, round(maximum*spec.get("shield_floor",0))-ordinary_shield)
    if not (healed or shield or spec.get("visits")):
        raise ValueError("当前生命与本药可补的护盾均已满。")
    run["hp"] += healed
    run["shield"] = run.get("shield",0) + shield
    if spec.get("visits"):
        run["med_regen"] = {"left":spec["visits"], "fraction":spec["regen"], "last_floor":run["floor"]}
    return healed, shield


def boss_guide():
    return ("👑 井道守门人档案（首领规则）"
            "\n• 坐镇层数：F8、F18、F30，之后每 20 层一尊（50、70、90……）。连进、跃迁卡和跳层奇遇都会在首领门前急刹车，绝不让你一头撞进去。"
            f"\n• 每 {BOSS_EXCHANGE_ROUNDS} 回合一次整备窗口，第 4、8、12……回合重击 ×{BOSS_HEAVY:g}。双方血量与回合数延续，可以用药、上临装或请求支援。"
            "\n• 推倒赏格：击败当场回满血，连抽 2 份战利品（大金 60%，传世红 15% 起步、每多推一只 +1% 封顶 20%，其余给蓝/紫装）。金红藏品的地图倍率最高 ×1.60，再计首领战损（首场 48%，越深越逼近 75%）和角色估值加成，图鉴照常点亮！"
            "\n• 🌈 正式击败首领有独立 0.5% 概率掉一件虹彩，四款等概率。掉落即入仓，首次获得当场点亮技能，同款可重复收藏。集齐四种解锁「虹彩共鸣」。"
            "\n• 正式局每只首领额外判定一次红装，从商场首领的 0.40% 起，按地图难度与首领层序递增，上限 3%。红装进入背包，撤离后入库。"
            "\n• 战术逃生：打不过可撤回大厅或扔烟雾弹溜走；脉冲雷对单只首领限炸 2 枚（每枚削 12% 当前血，最低留 1 HP）。")


def atlas_lines(rules,owned,total):
    count=len(owned)
    lines=[f"📖 井道收藏图鉴 · {count}/{total}（{count/total:.1%}）"]
    for row in rules["atlas"]:
        target=atlas_required(row,total)
        lines.append(f"{'●' if count>=target else '○'} {row['percent']}% · {target} 件 · {stat_line(row['stats'])}")
    rainbow=rules["rainbow"]
    got_rainbow=sum(k in owned for k in rainbow)
    lines.append(f"🌈 虹彩背包技能（{got_rainbow}/{len(rainbow)} · 正式首领独立 0.5% 邂逅）")
    for key,skill in rainbow.items():
        lines.append(f"{'●' if key in owned else '○'} 【{skill['name']}】{skill['description']}")
        if skill.get("lore"):
            lines.append(f"   📜 {skill['lore']}")
    res=rules.get("resonance",{})
    res_mark="● 已共鸣" if got_rainbow==len(rainbow) else f"○ {got_rainbow}/{len(rainbow)}"
    lines.append(f"✨ 四彩共鸣（{res_mark}）：{res.get('description','生命、攻击、防御再 ×1.25，敏捷 +8，追击率 +10 个百分点，开局护盾再 +45。')}")
    if res.get("lore"):
        lines.append(f"   📜 {res['lore']}")
    return lines


def probability_guide(catalog):
    lines=["📊 无尽电梯 · 爆率与规则手记",
           "• 楼层事件：非首领层权重随深度平滑过渡（每 12 层走完一半差值）；若附近没群友翻车遗物，遗物权重全送进宝箱。",
           "• 金红爆率：基础率叠加楼层、幸运、套装、宝箱词条与金雨，封顶 68%。出货后 82.5% 大金 / 17.5% 传世红；撬锁与斩精英升档另算。",
           "• 新获得的金红藏品按地图加成估值，地图倍率最高 ×1.60。角色估值加成与首领战损另计。"]
    for m in catalog.maps.values():
        lines.append(f"• {m['name']}：基础金红 {m['rare_rate']:.2%} ｜ 普通物资估值 ×{m['value_multiplier']:g} "
                     f"｜ 金红藏品估值 ×{collection_value_multiplier(m):.2f}")
    for key in catalog.gear_boxes:
        lines.append(f"• {catalog.gear_boxes[key][0]}（{catalog.shop[key]:,} 金）：{box_odds(catalog.progression,key)}")
    lines.extend(("• 加密盲盒：珍藏率 蓝 6% / 紫 15% / 金 29%，另有 6% 替身娃娃，其余出对应品质装备（虹彩只认首领，箱子里开不出来）。",
                  "• 战斗公式：单次命中 = 攻×100/(100+2.5×防)×(0.9~1.1) 取整；敏捷高者先手，同敏你先手。",
                  boss_guide()))
    return "\n".join(lines)


def add_stats(stats, changes, sources=None, label=""):
    for key,value in changes.items():
        stats[key] += value
        if sources is not None and value:
            sources[key].append({"source":label, "op":"add", "value":value, "result":stats[key]})


def scale_stats(stats, factors, sources=None, label=""):
    for key,factor in factors.items():
        stats[key] = round(stats[key]*factor)
        if sources is not None and factor != 1:
            sources[key].append({"source":label, "op":"multiply", "value":factor, "result":stats[key]})


def apply_archive(stats, perks, owned, catalog, *, sources=None):
    """Apply permanent effects once per unique registered ID, irrespective of copy count."""
    known = set(owned).intersection(catalog.collections)
    for row in milestones(catalog.progression,len(known),len(catalog.collections)):
        add_stats(stats, row["stats"], sources, f"图鉴 {atlas_required(row,len(catalog.collections))} 件")
    rainbow = catalog.progression["rainbow"]
    effects = [rainbow[k] for k in rainbow if k in known]
    if len(effects) == len(rainbow):
        effects.append(catalog.progression["resonance"])
    multipliers = []
    for effect in effects:
        label = effect.get("name","四件虹彩共鸣")
        add_stats(stats, effect.get("stats",{}), sources, label)
        scale_stats(stats, {k:1+v for k,v in effect.get("percent",{}).items()}, sources, label)
        for k,v in effect.get("perks",{}).items(): perks[k] = perks.get(k,0)+v
        if effect.get("multipliers"):
            multipliers.append((label,effect["multipliers"]))
    for label, factors in multipliers:
        scale_stats(stats, factors, sources, label+"独立倍率")
    for key in ("hp","atk"):
        if stats[key] < 1:
            stats[key] = 1
            if sources is not None:
                sources[key].append({"source":"基础属性下限","op":"set","value":1,"result":1})


def field_description(item):
    if item.get("judgement"):
        return (
            f"限与本层怪物交战时使用，消耗 1 件 · {JUDGEMENT_DEATH_CHANCE:.0%} 当场阵亡，"
            f"背包和安全箱均无法带出，替身娃娃和新手保险无法救场 · "
            f"{1 - JUDGEMENT_DEATH_CHANCE:.0%} 直接击杀当前怪物，首领也适用，战利品照常结算"
        )
    form = item["mode"]
    if form == "wear": detail = [f"本局佩戴，占 {FIELD_WEAR_SLOTS} 个临装位之一"]
    elif form == "timed": detail = [f"持续 {item['floors']} 层（含使用层）"]
    elif form == "guard": detail = [f"接下来 {item['hits']} 次受击减伤 {item['reduction']:.0%}"]
    elif form == "burst":
        detail = [f"下 {item['hits']} 次主攻击伤害 ×{item['mult']:g}"]
        if item.get("sure"): detail.append("主攻击必中")
        if item.get("blood"): detail.append("生命降至 1 并清盾，蓄力中禁疗禁盾；本次不叠暴击、重击或低血增幅，每局限用一次")
    else: detail = ["立即使用"]
    if "heal" in item: detail.append(f"回复 {item['heal']:.0%} 最大生命")
    if "shield" in item: detail.append(f"获得 {item['shield']} 护盾")
    if "cost_hp" in item: detail.append(f"消耗 {item['cost_hp']:.0%} 当前生命，至少留 1 点")
    if "damage_multiplier" in item:
        scale = item["damage_multiplier"]
        detail.append(f"对怪物造成伤害 ×{scale:g} · 承受怪物伤害 ×{scale:g}")
    for k,v in item.get("mods",{}).items():
        amount = f"{v:+g}" if k=="agi" else f"{v:+.0%}"
        detail.append(MOD_LABELS[k]+amount)
    return " · ".join(detail)


def field_charge_info(item: dict, charge: dict) -> tuple[str, str]:
    if item.get("judgement") and charge.get("blood"):
        return ("旧版奇点蓄力",
                f"主攻击必中，伤害 ×{charge['mult']:g} · 打出前禁疗禁盾")
    return item["name"], field_description(item)


def field_slots(run) -> list[str | None]:
    """Migrate the old single worn item once and keep physical slot positions."""
    stored = run.get("field_slots")
    legacy = run.pop("field_worn", None)
    if not isinstance(stored, list):
        stored = [legacy]
    slots: list[str | None] = []
    for index in range(FIELD_WEAR_SLOTS):
        key = stored[index] if index < len(stored) else None
        slots.append(key if isinstance(key, str) and key else None)
    run["field_slots"] = slots
    return slots


def field_choices(run, key) -> list[int]:
    slots = field_slots(run)
    if None in slots:
        return [slots.index(None) + 1]
    return list(range(1, FIELD_WEAR_SLOTS + 1))


def _equip_field(run, key, slot, collection, note_limit=None) -> str:
    choices = field_choices(run, key)
    if slot is None and len(choices) == 1:
        slot = choices[0]
    if type(slot) is not int or slot not in choices:
        allowed = "、".join(str(number) for number in choices)
        raise ValueError(f"请选择临装位 {allowed}。")
    slots = run["field_slots"]
    previous = slots[slot - 1]
    slots[slot - 1] = key
    text = f"🎽 已在临装位 {slot} 穿上【{collection[key]['name']}】。"
    if previous:
        text += f"旧临装【{collection.get(previous, {}).get('name', previous)}】已丢弃。"
    elif note_limit is not None and all(slots):
        note_limit(f"{FIELD_WEAR_SLOTS} 个临装槽已占满，再拾到临装需要替换一件或放弃。")
    return text


def choose_field(run, key, floor, slot, collection, *, note_limit=None) -> str:
    offer = run.get("field_offer")
    if offer != {"id": key, "floor": floor} or floor != run["floor"]:
        raise ValueError("这件临装的拾取选项已失效。")
    item = collection.get(key)
    if not item or item["mode"] != "wear":
        raise ValueError("找不到这件临装。")
    if type(slot) is not int:
        raise ValueError("请选临装位，或选择放弃。")
    if slot == 0:
        run.pop("field_offer")
        return f"已放弃【{item['name']}】。"
    before = _field_mod_totals(run, collection) if note_limit is not None else {}
    text = _equip_field(run, key, slot, collection, note_limit)
    run.pop("field_offer")
    refresh(run, collection, before=before, note_limit=note_limit)
    return f"{text}\n效果：{field_description(item)}"


def remove_field(run, key, collection, *, slot=None, note_limit=None) -> str:
    before = _field_mod_totals(run, collection) if note_limit is not None else {}
    slots = field_slots(run)
    buffs = run.get("field_buffs", {})
    name = collection.get(key, {}).get("name", key)
    matches = [index for index, equipped in enumerate(slots) if equipped and equipped == key]
    if slot is not None:
        if type(slot) is not int or slot - 1 not in matches:
            raise ValueError("指定的临装位没有这件道具。")
        matches = [slot - 1]
    if len(matches) > 1:
        raise ValueError("这件临装占用了两个位置，请指定要卸下的临装位。")
    if matches:
        index = matches[0]
        slots[index] = None
        text = f"已卸下临装位 {index + 1} 的【{name}】，物品已丢弃。"
    elif key in buffs:
        del buffs[key]
        text = f"已解除【{name}】的限层增益。"
    else:
        raise ValueError("找不到正在佩戴或生效的这件道具。")
    refresh(run, collection, before=before, note_limit=note_limit)
    return text


def _field_mod_totals(run, collection):
    keys = [key for key, end in run.get("field_buffs", {}).items() if key in collection and end >= run["floor"]]
    keys.extend(key for key in field_slots(run) if key in collection)
    mods={}
    for key in keys:
        for k,v in collection[key].get("mods",{}).items(): mods[k]=mods.get(k,0)+v
    return mods


def refresh(run, collection, *, before=None, note_limit=None):
    floor=run["floor"]
    buffs=run.setdefault("field_buffs",{})
    for key in list(buffs):
        if key not in collection or buffs[key]<floor: del buffs[key]
    mods = _field_mod_totals(run, collection)
    run["field_mods"]={k:min(MOD_CAPS[k][1],max(MOD_CAPS[k][0],v)) for k,v in mods.items()}
    run["field_damage_multiplier"] = math.prod(
        collection[key].get("damage_multiplier", 1) for key in run["field_slots"] if key in collection
    )
    if note_limit is not None:
        before = before or {}
        for key, value in mods.items():
            previous = before.get(key, 0)
            if math.isclose(value, previous, rel_tol=0, abs_tol=1e-9):
                continue
            lower, upper = MOD_CAPS[key]
            if value > previous and (value >= upper or math.isclose(value, upper, rel_tol=0, abs_tol=1e-9)):
                bound, label = upper, "上限"
            elif value < previous and (value <= lower or math.isclose(value, lower, rel_tol=0, abs_tol=1e-9)):
                bound, label = lower, "下限"
            else:
                continue
            amount = f"{bound:+g}" if key == "agi" else f"{bound:+.0%}"
            note_limit(f"局内道具的{MOD_LABELS[key]}修正已达{label} {amount}，超出部分不生效。")


def enter_field(run, items):
    """Use a distinct deterministic stream; field pickups never draw from the loot RNG."""
    floor=run["floor"]
    if floor<=run.get("field_seen",0) or run.get("field_offer"): return ""
    run["field_seen"]=floor
    refresh(run,items)
    regen=run["field_mods"].get("regen",0)
    if regen and not run.get("burst",{}).get("blood"):
        run["hp"]=min(run["stats"]["hp"],run["hp"]+max(1,round(run["stats"]["hp"]*regen)))
    rng=Random(f"{run['field_seed']}:{floor}")
    if floor!=1 and not boss_number(floor) and not boss_number(floor+1) and rng.random()>=FIELD_CHANCE:
        return ""
    worn = set(field_slots(run))
    pool, weights = [], []
    for key, item in items.items():
        if item["map"] not in ("all", run["map"]):
            continue
        weight = item["weight"]
        mods = item.get("mods", {})
        if item.get("heal", 0) > 0 or mods.get("regen", 0) > 0 or mods.get("leech", 0) > 0:
            weight *= FIELD_HEAL_WEIGHT
        if item["mode"] == "wear" and key in worn:
            weight *= FIELD_EQUIPPED_WEIGHT
        pool.append(key)
        weights.append(weight)
    key = rng.choices(pool, weights)[0]
    item = items[key]
    if item["mode"] == "wear":
        run["field_offer"] = {"id": key, "floor": floor}
    else:
        stash=run.setdefault("field_items",{})
        stash[key]=stash.get(key,0)+1
    leads = (
        "顺手在门缝角落拾到局内道具",
        "掀开墙边应急箱，拾到局内道具",
        "脚下踢到个亮晶晶的玩意——拾到局内道具",
        "在走廊检修口摸出一件宝贝，拾到局内道具",
    )
    lead = leads[rng.randrange(len(leads))]
    if item["mode"] == "wear":
        return f"🎒 {lead}【{item['name']}】！请选择佩戴或放弃。"
    return f"🎒 {lead}【{item['name']}】！（发「局内道具」查看使用）"


def activate(run, key, collection, *, slot=None, note_limit=None):
    item=collection[key]
    if item.get("judgement"):
        raise ValueError("请对本层正在交战的怪物发动公正裁决。")
    stash=run.setdefault("field_items",{})
    if stash.get(key,0)<1: raise ValueError("本局临时兜里还没摸到这件道具。")
    if item.get("blood") and run.get("blood_used"): raise ValueError("奇点放电管这局已经梭哈过一次啦，电芯彻底熔穿了。")
    if run.get("burst",{}).get("blood") and (item.get("heal") or item.get("shield") or item["mode"]=="guard"):
        raise ValueError("奇点正在嗡鸣锁血蓄力，把这一发惊天重炮打出去再回血架盾！")
    mode=item["mode"]
    if mode=="burst" and run.get("burst"): raise ValueError("手里还攥着没打出去的蓄力，先糊对面一巴掌再说。")
    if mode=="guard" and run.get("guard"): raise ValueError("身上还顶着防护次数呢，别叠浪费了。")
    if mode=="instant" and item.get("heal") and not item.get("shield") and run["hp"]>=run["stats"]["hp"]:
        raise ValueError("现在满血活蹦乱跳的，这口药留给危急关头吧。")
    before = _field_mod_totals(run, collection) if note_limit is not None else {}
    if mode=="timed":
        buffs=run.setdefault("field_buffs",{})
        added = key not in buffs
        if added and len(buffs)>=FIELD_TIMED_SLOTS: raise ValueError("身上已挂满三种限层增益！发「局内道具 解除 名称」卸掉一个腾位。")
        until=run["floor"]+item["floors"]-1
        if buffs.get(key,0)>=until: raise ValueError("这层已经拍过同款增益啦，效果不会白叠。")
        buffs[key]=until
        if note_limit is not None and added and len(buffs) == FIELD_TIMED_SLOTS:
            note_limit(f"{FIELD_TIMED_SLOTS} 个限层增益位已占满，等效果到期或解除一个后再用新道具。")
    wear_text = _equip_field(run, key, slot, collection, note_limit) if mode == "wear" else ""
    if item.get("cost_hp"):
        run["hp"]=max(1,run["hp"]-max(1,int(run["hp"]*item["cost_hp"])))
    if mode=="guard": run["guard"]={"id":key,"left":item["hits"],"reduction":item["reduction"]}
    if mode=="burst":
        run["burst"]={"id":key,"left":item["hits"],"mult":item["mult"],"sure":item.get("sure",False),"blood":item.get("blood",False)}
        if item.get("blood"):
            run["hp"],run["shield"],run["blood_used"]=1,0,True
            run.pop("ring_shield", None)
            run.pop("guard",None)
            if note_limit is not None:
                note_limit(f"【{item['name']}】本局限用 1 次，已用完。")
    if "heal" in item:
        max_hp = run["stats"]["hp"]
        run["hp"] = min(max_hp, run["hp"] + max(1, round(max_hp * item["heal"])))
        if note_limit is not None and run["hp"] == max_hp:
            note_limit(health_limit_note(max_hp))
    if "shield" in item: run["shield"]=run.get("shield",0)+item["shield"]
    stash[key]-=1
    if not stash[key]: del stash[key]
    refresh(run,collection,before=before,note_limit=note_limit)
    if item.get("blood"):
        lead = f"⚡ 滋啦——【{item['name']}】过载锁血至 1 HP！下一次主攻击将倾泻毁天灭地的 6 倍奇点放电！"
    elif mode == "burst":
        lead = f"🔥 蓄力完成！【{item['name']}】已上膛，下一击准备看烟花！"
    elif mode == "guard":
        lead = f"🛡️ 咔哒撑开【{item['name']}】，接下来几下硬挨也不慌了。"
    elif mode == "wear":
        lead = wear_text
    elif mode == "timed":
        lead = f"✨ 拍下【{item['name']}】，状态一路续到 F{run['field_buffs'][key]}（含）！"
    else:
        lead = f"💊 麻利拆开【{item['name']}】当场见效！"
    return f"{lead}\n效果：{field_description(item)}\n当前状态：HP {run['hp']}/{run['stats']['hp']} · 盾 {run.get('shield',0)}"
