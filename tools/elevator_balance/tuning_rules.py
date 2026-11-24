"""Numeric monster tuning shared by the workbench and its reviewable export."""
from __future__ import annotations


def raw_stats(map_data, floor, elite=False, scale=1.0):
    """Mirror the production order of rounding; apply bounded growth before rounding."""
    tune = map_data.get("difficulty", {})
    m = map_data["multiplier"] * scale

    def coefficients(f):
        return (38 * (1 + .18*f + .008*f*f), 11.5 * (1 + .13*f),
                3 * (1 + .10*f), min(42, 7 + f))

    if tune.get("mode") == "bounded":
        start = coefficients(1)
        end = coefficients(tune["cap_floor"])
        progress = 1 - 2 ** (-(floor - 1) / tune["half"])
        h, a, d, speed = (lo + (hi-lo)*progress for lo, hi in zip(start, end))
    else:
        # These expressions intentionally retain the multiplication order of combat.py.
        h = 38 * m * (1 + .18*floor + .008*floor**2)
        a = 11.5 * m * (1 + .13*floor)
        d = 3.0 * m * (1 + .10*floor)
        speed = min(42, 7 + floor)
    if tune.get("mode") == "bounded":
        h, a, d = h*m, a*m, d*m
    hp = max(1, int(h * tune.get("hp_scale", 1)))
    atk = max(1, int(a * tune.get("atk_scale", 1)))
    if elite:
        hp = max(1, int(hp * tune.get("elite_hp", 1.75)))
        atk = max(1, int(atk * tune.get("elite_atk", 1.30)))
    return {"hp": hp, "atk": atk, "def": max(0, int(d)), "agi": int(speed)}


def apply_monster_settings(map_data, floor, enemy, rng, *, elite=False):
    tuning = map_data.get("difficulty")
    if not tuning:
        return enemy
    mix = tuning.get("mixtures", {}).get("elite" if elite else "normal")
    if mix:
        stats = mix["weak"]
        if rng.random() < mix_weight(mix,floor):
            if mix.get("tail_weight",0) and rng.random()<mix["tail_weight"]:
                stats=mix["tail"]
                enemy["name"]="深层精英 · "+enemy["name"]
            else:
                stats = mix["strong"]
                if "hard_near" in mix and rng.random() >= mix["hard_weight"]:
                    stats = mix["hard_near"]
    else:
        stats = raw_stats(map_data, floor, elite)
    enemy.update(stats)
    enemy["max_hp"] = enemy["hp"]
    return enemy


def mix_weight(mix, floor):
    return mix["end_weight"] + (mix["start_weight"]-mix["end_weight"])*2**(-(floor-1)/mix["half"])
