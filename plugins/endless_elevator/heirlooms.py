"""Wen Cen's shining keepsakes. The focus is frozen on entry; cards are fitted in the lobby."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

_book = json.loads(Path(__file__).with_name("heirlooms.json").read_text(encoding="utf-8"))
SETS = _book["sets"]
PIECES = _book["pieces"]
STAT_TEXT = _book["stats"]
SLOT_COUNT = 4
BONUSES = {
    "B01": ("hit", .14), "B02": ("crit", .10), "B03": ("mitigation", .10),
    "B04": ("dodge", .08), "B05": ("crit_multiplier", .35), "B06": ("pierce", .12),
    "B07": ("combo", .12), "B08": ("leech", .08),
}
ECHO_PERCENT = 50
REDRAW_DAMAGE_FACTOR = 1.
BALLAST_THRESHOLD, BALLAST_EXCESS, BALLAST_CAP = 10, 70, 14
LETTER_MULTIPLIER = 1.45
CRACK_PERCENT = 12
TIMING_DAMAGE_FACTOR = .65
REEL_PERCENT, REEL_CAP = 40, 12
SNOW_HEALTH_PERCENT = 20

DEPTH_START = 50
DEPTH_HALFWAY = 85
DEPTH_FULL_CAP = .60
DEPTH_PAIR_CAP = .44
_SET_KEYS = {alias.casefold(): k for k,s in SETS.items() for alias in (k,str(int(k[1:])),s["name"])}
_PIECE_KEYS = {alias.casefold(): k for k,p in PIECES.items() for alias in (k,p["name"],p["alias"])}


def set_key(name):
    return _SET_KEYS.get(str(name).strip().casefold())


def piece_key(name):
    return _PIECE_KEYS.get(str(name).strip().casefold())


def probability(floor):
    depth = max(0, floor - 8)
    return min(.10, .03 + .07 * depth / (depth + 40)) if floor >= 8 else 0.


def roll(floor, focus, owned, rng):
    if len(focus) != 2 or len(set(focus)) != 2 or not all(k in SETS for k in focus):
        raise ValueError("请先选择两套不同的追寻祝福。")
    if rng.random() >= probability(floor):
        return None
    suit = rng.choice(focus)
    pieces = [k for k,p in PIECES.items() if p["set"] == suit]
    return rng.choices(pieces, weights=[1 if owned.get(k) else 2 for k in pieces], k=1)[0]


def load(db, uid):
    row = db.execute("SELECT focus,slots FROM heirloom_loadouts WHERE user_id=?", (uid,)).fetchone()
    focus, slots = (json.loads(row[0]),json.loads(row[1])) if row else ([],[None]*SLOT_COUNT)
    owned = dict(db.execute("SELECT piece,quantity FROM heirloom_inventory WHERE user_id=?", (uid,)))
    return focus, slots, owned


def apply(stats, perks, slots, *, sources=None):
    counts = Counter()
    for key in dict.fromkeys(slots):
        if key not in PIECES:
            continue
        item = PIECES[key]
        counts[item["set"]] += 1
        attr, amount = item["stats"]
        stats[attr] += amount
        if sources is not None:
            sources[attr].append({"source":f"闪耀 · {item['name']}","op":"add","value":amount,"result":stats[attr]})
    for code, n in counts.items():
        if n >= 2:
            attr, amount = BONUSES[code]
            perks["heirloom_" + attr] = amount
        if n == 4:
            perks["heirloom_full"] = code
    if "heirloom_full" in perks:
        perks["heirloom_depth_cap"] = DEPTH_FULL_CAP
    elif sum(n == 2 for n in counts.values()) == 2:
        perks["heirloom_depth_cap"] = DEPTH_PAIR_CAP


def depth_shred(floor, perks):
    depth = max(0, floor - DEPTH_START)
    return perks.get("heirloom_depth_cap", 0) * depth / (depth + DEPTH_HALFWAY)


def trigger(code, **values):
    return SETS[code]["trigger"].format_map(values)
