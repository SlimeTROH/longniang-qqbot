"""Weekly fixed-seed high-difficulty trial: 「来自群星的寓言集」.

Every player in the same ISO week (Beijing time) shares the exact same:
- Fixed 4-slot equipment loadout (weapon, armor, pendant, core)
- Fixed shining blessing cards (count & pieces)
- Fixed rainbow passive loadout (count & pieces)
- Fixed starting tactical supplies
- Fixed floor-by-floor encounters, monsters, events, loot, and field items

Rules:
- Entry requires full collection of all Gold and Red collectibles in the player's atlas.
- Entry is completely free (0 tickets, 0 gold, 0 personal supplies).
- Maximum floor: 2000, with steep deep-floor scaling so reaching F2000 is extremely hard.
- Zero takeout: never awards coins, items, equipment, atlas discoveries, shining, or rainbow.
- Weekly leaderboard settles automatically when a week ends, broadcasting results to groups.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from random import Random

from . import expedition, heirlooms
from .storage import encode

TITLE = "来自群星的寓言集"
MAX_FLOOR = 2000
BEIJING = timezone(timedelta(hours=8))
EXIT_ITEMS = frozenset({"smoke"})

_OMEN_PREFIXES = (
    "星蚀纪", "白矮星潮", "天穹逆转", "脉冲星回响", "银心折跃",
    "双子轨道", "星尘寂灭", "红巨星余烬", "黑洞视界", "彗尾长夜",
)
_OMEN_TITLES = (
    "群星的低语", "断轨的星图", "寂静星海", "无尽观测站", "星穹审判",
    "倒悬天体", "星环孤岛", "天狼星誓约", "末日星历", "光年尽头",
)
_OMEN_LINES = (
    "万千星光落入同一座井道，唯有同样锋刃与同样命运之下，方可见真章。",
    "星轨已锁死本周的每一扇电梯门——同样的怪物、同样的奇遇、同样的孤勇。",
    "群星不赐予带出井道的战利品，只铭记你能抵达的光年深度。",
    "两千层尽头悬着熄灭的星核，越近深渊，引力越沉重。",
    "卸下过往家当，接过群星降下的唯一构筑，向深空试炼。",
)

_HIGH_MAPS = ("abyss", "auction", "tide", "zenith")
_SLOTS = ("武器", "护甲", "挂件", "核心")

# One socketing choice per cache. The scale is the asymptotic stat bonus at this depth.
# Rank n grants n/(n+4) of that scale; progress in a single stat has diminishing returns.
STAR_RELICS = {
    "lens": ("裂星棱镜", "atk", "攻击", (30, .70, .00084)),
    "anchor": ("轨道星锚", "def", "防御", (18, .27, 0)),
    "heart": ("余晖心匣", "hp", "最大生命", (65, 1.10, 0)),
}
STAR_RULE = ("本周星匣的位置和两件候选对所有人相同。每匣可嵌入一件：裂星棱镜加攻击，轨道星锚加防御，"
             "余晖心匣提高生命上限。星器随楼层成长，同款升阶的边际收益递减。离层即错过，离局清空。")


def week_key(at: int) -> str:
    iso = datetime.fromtimestamp(at, BEIJING).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def previous_week_key(current_week: str) -> str:
    year_s, week_s = current_week.split("-W")
    monday = date.fromisocalendar(int(year_s), int(week_s), 1)
    prev = (monday - timedelta(days=7)).isocalendar()
    return f"{prev.year}-W{prev.week:02d}"


def week_bounds(week: str) -> tuple[int, int]:
    year_s, week_s = week.split("-W")
    monday = date.fromisocalendar(int(year_s), int(week_s), 1)
    start = datetime(monday.year, monday.month, monday.day, tzinfo=BEIJING)
    end = start + timedelta(days=7)
    return int(start.timestamp()), int(end.timestamp())


def required_collections(catalog) -> set[str]:
    """All Gold and Red collectibles required to unlock Starlight Prophecy."""
    return {
        key for key, item in catalog.collections.items()
        if item.get("rarity") in ("gold", "red") and not item.get("boss_only")
    }


def unlock_status(catalog, archives: dict[str, int]) -> dict:
    required = required_collections(catalog)
    owned = required & set(archives.keys())
    missing_keys = sorted(required - owned)
    missing_names = [catalog.items[k]["name"] for k in missing_keys]
    return {
        "unlocked": len(owned) >= len(required),
        "owned": len(owned),
        "total": len(required),
        "missing_count": len(missing_keys),
        "missing_preview": missing_names[:6],
    }


def difficulty_multiplier(floor: int) -> float:
    """Multiplies the normal floor-dependent monster stats, including boss rank scaling."""
    depth = max(0, min(MAX_FLOOR, int(floor)) - 1)
    return round(2.60 + .75 * depth / (150 + depth) + 15.0 * (depth / (MAX_FLOOR - 1)) ** 3, 5)


def boss_number(floor: int) -> int:
    if floor == MAX_FLOOR:
        return expedition.boss_number(expedition.next_boss(floor))
    return expedition.boss_number(floor)


def make_monster(catalog, base_map: str, floor: int, rng: Random, *, elite: bool = False, boss: bool = False) -> dict:
    from . import combat
    enemy = combat.monster(prophecy_map_data(catalog, base_map, floor), floor, rng, elite=elite,
                           boss=boss, boss_rank=boss_number(floor) if boss else None)
    # Deep prophecy floors gradually lift enemy agility up to 75 so speed/hit/dodge matter for AI strategy.
    agi_bonus = min(20, max(0, floor - 10) // 30)
    enemy["agi"] = min(75, enemy["agi"] + agi_bonus)
    return enemy


def world_rng(week: str, floor: int, tag: str) -> Random:
    return Random(f"prophecy:world:{week}:F{floor}:{tag}")


@lru_cache(maxsize=32)
def _star_schedule(week: str) -> dict[int, tuple[str, str]]:
    rng = world_rng(week, 0, "star-caches:v1")
    windows = [(1, 1), (4, 7), (12, 17)]
    windows.extend((start, min(start + 39, MAX_FLOOR - 1)) for start in range(21, MAX_FLOOR, 40))
    schedule = {}
    pairs = []
    for low, high in windows:
        if not pairs:
            pairs = [("lens", "anchor"), ("lens", "heart"), ("anchor", "heart")]
            rng.shuffle(pairs)
        schedule[rng.randint(low, high)] = pairs.pop()
    return schedule


def star_offer(run: dict) -> tuple[str, ...]:
    if not run.get("prophecy") or run.get("star_claimed_floor", 0) >= run["floor"]:
        return ()
    return _star_schedule(run["prophecy_week"]).get(run["floor"], ())


def star_bonus(key: str, rank: int, floor: int) -> int:
    base, linear, quadratic = STAR_RELICS[key][3]
    depth = max(0, min(MAX_FLOOR, floor) - 1)
    return round((base + linear * depth + quadratic * depth ** 2) * rank / (rank + 4))


def refresh_stars(run: dict) -> None:
    """Apply only the change since the last refresh. Growing max HP does not heal."""
    applied = run.setdefault("star_bonus", {})
    for key, rank in run["star_ranks"].items():
        stat = STAR_RELICS[key][1]
        bonus = star_bonus(key, rank, run["floor"])
        run["stats"][stat] += bonus - applied.get(stat, 0)
        applied[stat] = bonus


def migrate_stars(run: dict, catalog) -> None:
    """Old active trials get a balanced choice from each cache below their saved floor."""
    ranks = run["star_ranks"] = {}
    for floor, options in _star_schedule(run["prophecy_week"]).items():
        if floor >= run["floor"]:
            break
        key = min(options, key=lambda item: ranks.get(item, 0))
        ranks[key] = ranks.get(key, 0) + 1
        run["star_claimed_floor"] = floor
    refresh_stars(run)
    for pocket in ("bag", "safe"):
        run[pocket] = [item for item in run[pocket] if item["id"] not in EXIT_ITEMS]
    event = run["event"]
    if event.get("offer", {}).get("id") in EXIT_ITEMS:
        offer = catalog.ordinary_item("shield", world_rng(run["prophecy_week"], run["floor"], "vendor-replacement"))
        event.update(offer=offer, price=max(1, int(offer["value"] * .70)))
    enemy = run["event"].get("enemy")
    if enemy and enemy.get("max_hp", 0) > 0:
        is_boss = bool(enemy.get("boss"))
        rng = world_rng(run["prophecy_week"], run["floor"], "boss" if is_boss else "monster")
        fresh = make_monster(catalog, run["map"], run["floor"], rng, elite=bool(enemy["elite"]), boss=is_boss)
        enemy["hp"] = math.ceil(fresh["hp"] * enemy["hp"] / enemy["max_hp"])
        enemy.update({k: fresh[k] for k in ("max_hp", "atk", "def", "agi")})


def pick_star(run: dict, floor: int, key: str) -> str:
    if floor != run["floor"] or key not in star_offer(run):
        raise ValueError("这件星器已失效，请以当前楼层显示的星匣为准。")
    ranks = run["star_ranks"]
    ranks[key] = ranks.get(key, 0) + 1
    run["star_claimed_floor"] = floor
    refresh_stars(run)
    name, stat, label, _ = STAR_RELICS[key]
    return f"✦ 已嵌入{name} · {ranks[key]} 阶，当前{label}加成 +{run['star_bonus'][stat]}。"


def star_rows(run: dict) -> list[dict]:
    ranks = run.get("star_ranks", {})
    options = star_offer(run)
    rows = []
    for key, (name, stat, label, _) in STAR_RELICS.items():
        rank = ranks.get(key, 0)
        bonus = star_bonus(key, rank, run["floor"])
        rows.append({"id": key, "name": name, "stat": stat, "label": label, "rank": rank, "bonus": bonus,
                     "offered": key in options, "gain": star_bonus(key, rank + 1, run["floor"]) - bonus})
    return rows


def _roll_fixed_gear(catalog, rng: Random, base_id: str, rarity: str) -> dict:
    factor = catalog.progression["grade_factors"][rarity]
    stats = {
        attr: round(value * (factor if value > 0 else 1) * rng.uniform(0.95, 1.08))
        for attr, value in catalog.equipment[base_id]["stats"].items()
    }
    affixes = catalog.roll_affixes(rarity, rng)
    return {
        "base_id": base_id,
        "name": catalog.equipment[base_id]["name"],
        "slot": catalog.equipment[base_id]["slot"],
        "rarity": rarity,
        "stats": stats,
        "affixes": affixes,
        "set_name": catalog.gear_sets[catalog.equipment[base_id]["gear_set"]]["name"]
        if catalog.equipment[base_id].get("gear_set") else "",
    }


def _pick_weekly_gear(catalog, rng: Random) -> list[dict]:
    # Deterministically pick a coherent high-tier build: 2–4 red gear pieces + gold fillers across all 4 slots.
    set_codes = sorted(catalog.gear_sets.keys())
    chosen_set = rng.choice(set_codes) if set_codes else ""
    red_count = rng.choices([2, 3, 4], weights=[30, 35, 35], k=1)[0]
    red_slots = set(rng.sample(_SLOTS, red_count))
    gear = []
    for slot in _SLOTS:
        if slot in red_slots and chosen_set:
            candidates = [
                k for k, v in catalog.equipment.items()
                if v["slot"] == slot and v.get("gear_set") == chosen_set
            ]
            if not candidates:
                candidates = [k for k in catalog.red_bases if catalog.equipment[k]["slot"] == slot]
            if candidates:
                gear.append(_roll_fixed_gear(catalog, rng, rng.choice(candidates), "red"))
                continue
        candidates = [k for k in catalog.standard_bases if catalog.equipment[k]["slot"] == slot]
        gear.append(_roll_fixed_gear(catalog, rng, rng.choice(candidates), "gold"))
    return gear


def _pick_weekly_blessings(rng: Random) -> list[str]:
    set_codes = sorted(heirlooms.SETS.keys())
    pattern = rng.choices(["full4", "pair22", "three", "two"], weights=[45, 30, 15, 10], k=1)[0]
    if pattern == "full4":
        suit = rng.choice(set_codes)
        return [f"{suit}-{i}" for i in range(1, 5)]
    if pattern == "pair22":
        s1, s2 = rng.sample(set_codes, 2)
        p1 = sorted(rng.sample([f"{s1}-{i}" for i in range(1, 5)], 2))
        p2 = sorted(rng.sample([f"{s2}-{i}" for i in range(1, 5)], 2))
        return p1 + p2
    if pattern == "three":
        s1, s2 = rng.sample(set_codes, 2)
        p1 = sorted(rng.sample([f"{s1}-{i}" for i in range(1, 5)], 2))
        p2 = [rng.choice([f"{s2}-{i}" for i in range(1, 5)])]
        return p1 + p2
    suit = rng.choice(set_codes)
    return sorted(rng.sample([f"{suit}-{i}" for i in range(1, 5)], 2))


def _pick_weekly_rainbows(catalog, rng: Random) -> list[str]:
    all_rainbows = sorted(catalog.rainbow.keys())
    count = rng.choices([1, 2, 3, 4], weights=[15, 30, 35, 20], k=1)[0]
    return sorted(rng.sample(all_rainbows, min(count, len(all_rainbows))))


def _pick_weekly_supplies(catalog, rng: Random) -> list[tuple[str, int]]:
    heals = [k for k in ("field_surgery", "critical_serum", "barrier_serum", "triage_vial", "slow_serum") if k in catalog.items]
    tactical = [k for k in ("shield", "grenade", "adrenaline", "coffee") if k in catalog.items]
    chosen = []
    if heals:
        h1 = rng.choice(heals)
        chosen.append((h1, 2))
        other_heals = [h for h in heals if h != h1]
        if other_heals:
            chosen.append((rng.choice(other_heals), 1))
    if tactical:
        for key in rng.sample(tactical, min(2, len(tactical))):
            chosen.append((key, 1 if key != "coffee" else 2))
    return chosen


def weekly_config(week: str, catalog) -> dict:
    """Deterministically generate and fix the entire week's Prophecy loadout and environment."""
    rng = Random(f"prophecy:config:{week}:v1")
    prefix = rng.choice(_OMEN_PREFIXES)
    subtitle = rng.choice(_OMEN_TITLES)
    omen = rng.choice(_OMEN_LINES)
    available_maps = [m for m in _HIGH_MAPS if m in catalog.maps] or list(catalog.maps)
    base_map = rng.choice(available_maps)
    gear = _pick_weekly_gear(catalog, rng)
    blessings = _pick_weekly_blessings(rng)
    rainbows = _pick_weekly_rainbows(catalog, rng)
    supplies = _pick_weekly_supplies(catalog, rng)

    # Compute identical baseline stats/perks for every entrant.
    stats = dict(catalog.base)
    perks: dict[str, float] = {}
    sources = {k: [{"source": "角色基础", "op": "base", "value": v, "result": v}] for k, v in stats.items()}
    # Standardize veteran & gold/red archive bonus so every player starts with identical attributes.
    expedition.add_stats(stats, {"hp": 100, "atk": 25, "def": 12}, sources, "群星统一体格（满资深基准）")
    for item in gear:
        expedition.add_stats(stats, item["stats"], sources, f"{item['name']}（本周固定）")
        for perk in item["affixes"]:
            power = catalog.affixes[perk]["power"]
            perks[perk] = max(power, perks.get(perk, 0))
    catalog.apply_gear_sets(stats, perks, (item["base_id"] for item in gear), sources=sources)
    heirlooms.apply(stats, perks, blessings, sources=sources)
    # Apply only the fixed gold/red collection baseline + this week's fixed rainbows!
    fixed_owned = set(required_collections(catalog)) | set(rainbows)
    expedition.apply_archive(stats, perks, fixed_owned, catalog, sources=sources)

    start_ts, end_ts = week_bounds(week)
    return {
        "week": week,
        "name": f"{prefix} · {subtitle}",
        "omen": omen,
        "base_map": base_map,
        "base_map_name": catalog.maps[base_map]["name"],
        "max_floor": MAX_FLOOR,
        "start_at": start_ts,
        "end_at": end_ts,
        "gear": gear,
        "blessings": blessings,
        "blessing_names": [
            f"{heirlooms.SETS[heirlooms.PIECES[p]['set']]['name']}·{heirlooms.PIECES[p]['name']}"
            for p in blessings if p in heirlooms.PIECES
        ],
        "rainbows": rainbows,
        "rainbow_names": [catalog.rainbow[k]["name"] for k in rainbows if k in catalog.rainbow],
        "supplies": [{"id": k, "name": catalog.items[k]["name"], "quantity": q} for k, q in supplies],
        "stats": stats,
        "perks": perks,
        "stat_sources": sources,
    }


def prophecy_map_data(catalog, base_map: str, floor: int) -> dict:
    """Build a virtual map descriptor for combat.monster() at the given floor."""
    source = catalog.maps[base_map]
    return {
        **source,
        "name": TITLE,
        "multiplier": difficulty_multiplier(floor),
        "ambush_rate": 0.0,
        "ambush_floor": MAX_FLOOR + 1,
        "guaranteed_equipment": "",
    }


def filter_field_items(catalog) -> dict:
    """Exclude instant-kill wish and coin-flip judgement so Prophecy remains a pure skill trial."""
    return {
        k: v for k, v in catalog.field_items.items()
        if not v.get("wish") and not v.get("judgement")
    }


def trial_events(events: dict) -> dict:
    result = {}
    for key, event in events.items():
        choices = []
        for option in event["choices"]:
            allowed = dict(option)
            for field in ("random_supply", "items"):
                if field in allowed:
                    allowed[field] = [item for item in allowed[field] if item not in EXIT_ITEMS]
            choices.append(allowed)
        result[key] = {**event, "choices": choices, "text": event["text"].replace("烟雾弹、", "")}
    return result


def record_score(
    db: sqlite3.Connection, week: str, uid: str, nickname: str, *,
    max_floor: int, cleared_floor: int, value: int, extracted: bool, now: int,
) -> bool:
    """Record a Prophecy attempt. Returns True if it improved the player's weekly personal best."""
    existing = db.execute(
        "SELECT max_floor,cleared_floor,best_value,extracted,attempts FROM prophecy_scores WHERE week=? AND user_id=?",
        (week, uid),
    ).fetchone()
    ext_int = 1 if extracted else 0
    candidate = (cleared_floor, max_floor, ext_int, value)
    if existing is None:
        db.execute(
            """INSERT INTO prophecy_scores(
                week,user_id,nickname,max_floor,cleared_floor,best_value,extracted,attempts,updated_at
            ) VALUES(?,?,?,?,?,?,?,1,?)""",
            (week, uid, nickname, max_floor, cleared_floor, value, ext_int, now),
        )
        return True
    prev = (existing["cleared_floor"], existing["max_floor"], existing["extracted"], existing["best_value"])
    if candidate > prev:
        db.execute(
            """UPDATE prophecy_scores
               SET nickname=?,max_floor=?,cleared_floor=?,best_value=?,extracted=?,attempts=attempts+1,updated_at=?
               WHERE week=? AND user_id=?""",
            (nickname, max_floor, cleared_floor, value, ext_int, now, week, uid),
        )
        return True
    db.execute(
        "UPDATE prophecy_scores SET nickname=?,attempts=attempts+1 WHERE week=? AND user_id=?",
        (nickname, week, uid),
    )
    return False


def leaderboard(db: sqlite3.Connection, week: str, limit: int = 10) -> list[dict]:
    rows = db.execute(
        """SELECT s.user_id, coalesce(p.nickname, s.nickname) AS nickname,
                  s.max_floor, s.cleared_floor, s.best_value, s.extracted, s.attempts, s.updated_at
           FROM prophecy_scores s LEFT JOIN players p USING(user_id)
           WHERE s.week=?
           ORDER BY s.cleared_floor DESC, s.max_floor DESC, s.extracted DESC, s.best_value DESC, s.updated_at ASC
           LIMIT ?""",
        (week, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def player_record(db: sqlite3.Connection, week: str, uid: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM prophecy_scores WHERE week=? AND user_id=?", (week, uid),
    ).fetchone()
    if row is None:
        return None
    rank = db.execute(
        """SELECT count(*)+1 FROM prophecy_scores
           WHERE week=? AND (
             cleared_floor>? OR
             (cleared_floor=? AND max_floor>?) OR
             (cleared_floor=? AND max_floor=? AND extracted>?) OR
             (cleared_floor=? AND max_floor=? AND extracted=? AND best_value>?) OR
             (cleared_floor=? AND max_floor=? AND extracted=? AND best_value=? AND updated_at<?)
           )""",
        (
            week,
            row["cleared_floor"],
            row["cleared_floor"], row["max_floor"],
            row["cleared_floor"], row["max_floor"], row["extracted"],
            row["cleared_floor"], row["max_floor"], row["extracted"], row["best_value"],
            row["cleared_floor"], row["max_floor"], row["extracted"], row["best_value"], row["updated_at"],
        ),
    ).fetchone()[0]
    return {**dict(row), "rank": int(rank)}


def format_board_text(week: str, cfg: dict, rows: list[dict], *, finished: bool = False) -> str:
    tag = "最终结算榜" if finished else "实时周榜"
    lines = [
        f"✦ 【{TITLE}】{week} · {tag}",
        f"🌌 本期星兆：{cfg['name']}（上限 F{MAX_FLOOR} · 零带出试炼）",
        f"⚔️ 固定构筑：{' / '.join(g['name'] for g in cfg['gear'])}",
        f"✨ 固定祝福（{len(cfg['blessings'])}槽）：{'、'.join(cfg['blessing_names']) or '无'}",
        f"🌈 固定虹彩（{len(cfg['rainbows'])}件）：{'、'.join(cfg['rainbow_names']) or '无'}",
    ]
    if not rows:
        lines.append("（本期暂无人踏入星兆留名）")
        return "\n".join(lines)
    medals = ("🥇", "🥈", "🥉")
    for idx, row in enumerate(rows, 1):
        prefix = medals[idx - 1] if idx <= 3 else f"{idx}."
        badge = "👑 登顶F2000" if row["cleared_floor"] >= MAX_FLOOR else ("安全撤离" if row["extracted"] else "力竭止步")
        lines.append(
            f"{prefix} {row['nickname']} · 推进 F{row['max_floor']}（通过 F{row['cleared_floor']} · {badge} · 估值 {row['best_value']:,} · 挑战 {row['attempts']} 次）"
        )
    return "\n".join(lines)


def _enqueue_group_announcement(db: sqlite3.Connection, group_id: str, body: str, now: int) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS elevator_web_announcements(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            next_at INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            signal_owner TEXT,
            signal_expires_at INTEGER
        )
    """)
    db.execute(
        "INSERT INTO elevator_web_announcements(group_id,body,created_at,next_at,signal_owner,signal_expires_at) "
        "VALUES(?,?,?,?,NULL,NULL)",
        (group_id, body, now, now),
    )


def settle_completed_weeks(db: sqlite3.Connection, current_week: str, catalog, now: int) -> list[str]:
    """Settle any finished weeks, notify winners, and queue QQ group announcements."""
    unsettled = [
        r["week"] for r in db.execute(
            "SELECT DISTINCT week FROM prophecy_scores WHERE week < ? "
            "AND week NOT IN (SELECT week FROM prophecy_settled) ORDER BY week",
            (current_week,),
        ).fetchall()
    ]
    if not unsettled:
        return []
    new_cfg = weekly_config(current_week, catalog)
    # Active groups (exclude private: scopes)
    groups = [
        r["group_id"] for r in db.execute(
            "SELECT DISTINCT group_id FROM group_active_users WHERE group_id NOT LIKE 'private:%' AND group_id<>''"
        ).fetchall()
    ]
    settled_weeks = []
    for old_week in unsettled:
        old_cfg = weekly_config(old_week, catalog)
        rows = leaderboard(db, old_week, limit=10)
        summary = format_board_text(old_week, old_cfg, rows, finished=True)
        inserted = db.execute(
            "INSERT OR IGNORE INTO prophecy_settled(week,settled_at,summary) VALUES(?,?,?)",
            (old_week, now, summary),
        ).rowcount
        if not inserted:
            continue
        settled_weeks.append(old_week)
        for rank_idx, row in enumerate(rows[:3], 1):
            db.execute(
                "INSERT INTO notices(user_id,body) VALUES(?,?)",
                (
                    row["user_id"],
                    f"✦ 【{TITLE}】{old_week} 已结算，你以通过 F{row['cleared_floor']}（推进 F{row['max_floor']}）位列全服第 {rank_idx} 名！",
                ),
            )
        announcement = (
            f"{summary}\n\n"
            f"🌠 新一期星兆已轮换开启（{current_week} · {new_cfg['name']}）！\n"
            f"⚔️ 新周配装：{' / '.join(g['name'] for g in new_cfg['gear'])}\n"
            f"✨ 新周祝福：{'、'.join(new_cfg['blessing_names'])}\n"
            f"🌈 新周虹彩：{'、'.join(new_cfg['rainbow_names'])}\n"
            "发「寓言集」或在网页版点击星兆入口即可免费挑战！"
        )
        for gid in groups:
            _enqueue_group_announcement(db, gid, announcement, now)
    return settled_weeks
