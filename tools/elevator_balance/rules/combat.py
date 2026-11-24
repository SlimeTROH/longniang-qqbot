"""Grouped turn resolution for the fictional expedition encounters."""
from __future__ import annotations

from dataclasses import dataclass, field
from random import Random


def clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def agility(run: dict) -> int:
    return run["stats"]["agi"] + (15 if run["floor"] <= run["tea_until"] else 0)


def flee_rate(run: dict) -> float:
    rate = 0.45 + (agility(run) - run["floor"] * 1.5) * 0.015
    return min(0.90, clamp(rate, 0.15, 0.90) + run["perks"].get("slippery", 0))


def dodge_rate(agi: float) -> float:
    return min(0.45, 0.03 + agi * 0.004)


def crit_rate(luck: float, multiplier: float = 1.0) -> float:
    return min(0.60, multiplier * (0.05 + luck * 0.005))


def damage(attack: float, defence: float, roll: float = 1.0) -> int:
    return max(1, int(attack * 100 / (100 + defence * 2.5) * roll))


def hurt(run: dict, amount: int) -> int:
    if run["perks"].get("greed"):
        amount = max(1, int(amount * 1.25))
    shield = int(run.get("shield", 0))
    if shield > 0:
        absorbed = min(shield, amount)
        run["shield"] = shield - absorbed
        amount -= absorbed
        if amount <= 0:
            return 0
    remaining = max(0, run["hp"] - amount)
    if not remaining and run["perks"].get("last_stand") and not run["last_stand"]:
        run["last_stand"] = True
        remaining = 1
    lost = run["hp"] - remaining
    run["hp"] = remaining
    return lost


def monster(
    map_data: dict,
    floor: int,
    rng: Random,
    *,
    elite: bool = False,
    ambush: bool = False,
    exclude_name: str = "",
) -> dict:
    multiplier = map_data["multiplier"]
    base_hp = int(38 * multiplier * (1 + 0.18 * floor + 0.008 * floor ** 2))
    base_atk = int(11.5 * multiplier * (1 + 0.13 * floor))
    hp = int(base_hp * 1.75) if elite else base_hp
    pool = map_data["elites" if elite else "monsters"]
    if exclude_name and len(pool) > 1:
        filtered = [n for n in pool if n != exclude_name]
        if filtered:
            pool = filtered
    name = rng.choice(pool)
    return {
        "name": f"电梯守门怪 · {name}" if ambush else name,
        "hp": hp, "max_hp": hp, "atk": int(base_atk * 1.30) if elite else base_atk,
        "def": int(3.0 * multiplier * (1 + 0.10 * floor)),
        "agi": min(42, 7 + floor), "elite": elite, "ambush": ambush,
    }


@dataclass
class FightResult:
    rounds: int = 0
    peak: int = 0
    crits: int = 0
    dodges: int = 0
    player_first: bool = True
    player_start_hp: int = 0
    enemy_start_hp: int = 0
    total_dealt: int = 0
    total_taken: int = 0
    log: list[str] = field(default_factory=list)


def fight(run: dict, enemy: dict, rng: Random) -> FightResult:
    stats, perks = run["stats"], run["perks"]
    player_first = agility(run) >= enemy["agi"]
    result = FightResult(
        player_first=player_first,
        player_start_hp=run["hp"],
        enemy_start_hp=enemy["hp"],
    )
    for turn in range(1, 16):
        result.rounds = turn
        steps: list[str] = []
        for is_player in (player_first, not player_first):
            if is_player:
                if rng.random() < dodge_rate(enemy["agi"]):
                    steps.append("你出手被怪闪开")
                else:
                    desperate = run["hp"] < stats["hp"] * perks.get("wager", 0)
                    heavy = turn == 1 and bool(perks.get("heavy_strike", 0))
                    berserk_mult = 1 + (turn - 1) * perks.get("berserk", 0)
                    attack = stats["atk"] * (1 + run.get("atk_boost", 0)) * berserk_mult * (2 if desperate else 1)
                    if turn == 1:
                        attack *= 1 + perks.get("heavy_strike", 0)
                    defence = enemy["def"] * (1 - perks.get("pierce", 0))
                    strike = damage(attack, defence, rng.uniform(0.9, 1.1))
                    is_crit = rng.random() < crit_rate(stats["luk"], 2 if desperate else 1)
                    if is_crit:
                        crit_mult = perks.get("high_roller", 1.6)
                        strike = int(strike * crit_mult)
                        result.crits += 1
                    result.peak = max(result.peak, strike)
                    result.total_dealt += strike
                    enemy["hp"] = max(0, enemy["hp"] - strike)
                    tags: list[str] = []
                    if desperate:
                        tags.append("孤注一掷")
                    if heavy:
                        tags.append("重击")
                    if is_crit:
                        tags.append("豪赌暴击" if perks.get("high_roller") else "暴击")
                    tag_str = f"【{'·'.join(tags)}】" if tags else ""
                    tail = "怪倒地！" if not enemy["hp"] else f"怪剩{enemy['hp']}"
                    steps.append(f"你{tag_str}打 {strike}（{tail}）")
            elif rng.random() < dodge_rate(agility(run)):
                result.dodges += 1
                steps.append("怪出手被你闪开")
            else:
                had_last_stand = bool(run["last_stand"])
                shield_before = int(run.get("shield", 0))
                lost = hurt(run, damage(enemy["atk"], stats["def"], rng.uniform(0.9, 1.1)))
                shield_used = shield_before - int(run.get("shield", 0))
                result.total_taken += lost
                saved = (not had_last_stand) and bool(run["last_stand"])
                shield_note = f"【护盾挡{shield_used}】" if shield_used else ""
                thorns_note = ""
                total_hit = lost + shield_used
                if perks.get("thorns") and total_hit > 0 and run["hp"] > 0:
                    reflected = max(1, int(total_hit * perks["thorns"]))
                    enemy["hp"] = max(0, enemy["hp"] - reflected)
                    result.total_dealt += reflected
                    thorns_note = f"【反伤{reflected}{'·怪倒地!' if not enemy['hp'] else ''}】"
                if saved:
                    steps.append(f"怪打 {lost}{shield_note}【不屈锁1血！】{thorns_note}（你剩1）")
                elif not run["hp"]:
                    steps.append(f"怪打 {lost}{shield_note}（你倒地！）")
                elif lost == 0 and shield_used > 0:
                    steps.append(f"怪打 0{shield_note}{thorns_note}（盾剩{run['shield']}）")
                else:
                    steps.append(f"怪打 {lost}{shield_note}{thorns_note}（你剩{run['hp']}）")
            if not enemy["hp"] or not run["hp"]:
                result.log.append(f"R{turn}：{' ｜ '.join(steps)}")
                return result
        result.log.append(f"R{turn}：{' ｜ '.join(steps)}")
    return result


def support_damage(stats: dict, perks: dict, enemy: dict, owner_perks: dict) -> int:
    defence = enemy["def"] * (1 - perks.get("pierce", 0))
    hits = 3 + perks.get("heavy_strike", 0)
    expected = damage(stats["atk"], defence) * hits * (1 + crit_rate(stats["luk"]) * 0.6)
    expected *= (1 - dodge_rate(enemy["agi"])) * (1 + owner_perks.get("loud", 0))
    return max(1, int(expected))
