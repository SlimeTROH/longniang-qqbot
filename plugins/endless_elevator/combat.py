"""Turn-based combat for the fictional elevator expedition."""
from __future__ import annotations

from dataclasses import dataclass, field
from random import Random

from . import expedition


def clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def agility(run: dict) -> int:
    tea = 15 if run["floor"] <= run["tea_until"] else 0
    return max(0, run["stats"]["agi"] + tea + run.get("field_mods", {}).get("agi", 0))


def defence(run: dict) -> float:
    return max(0, run["stats"]["def"] * (1 + run.get("field_mods", {}).get("defense", 0)))


def attack_power(run: dict) -> float:
    multiplier = 1 + run.get("atk_boost", 0) + run.get("field_mods", {}).get("atk", 0)
    return run["stats"]["atk"] * max(.25, multiplier)


def flee_rate(run: dict) -> float:
    rate = .45 + (agility(run) - run["floor"] * 1.5) * .015
    normal = clamp(clamp(rate,.15,.90) + run["perks"].get("slippery",0) + run.get("field_mods",{}).get("flee",0), .05, .90)
    return min(1.0, normal + run["perks"].get("prism_flee",0))


def dodge_rate(agi: float) -> float:
    return min(.45, .03 + agi * .004)


def crit_rate(luck: float, multiplier: float = 1.0) -> float:
    return min(.60, multiplier * (.05 + luck * .005))


def attack_crit_rate(run, blood=False):
    if blood:
        return 0
    desperate = run["hp"] < run["stats"]["hp"] * run["perks"].get("wager",0)
    return clamp(crit_rate(run["stats"]["luk"], 2 if desperate else 1) + run.get("field_mods",{}).get("crit",0),0,.65)


def player_dodge(run):
    return clamp(dodge_rate(agility(run)) + run.get("field_mods",{}).get("dodge",0),0,.52)


def combat_rates(run):
    perks, mods = run["perks"], run.get("field_mods",{})
    return {"pierce":min(.75, perks.get("pierce",0)+perks.get("prism_pierce",0)+mods.get("pierce",0)),
            "combo":min(.70, perks.get("prism_combo",0)+mods.get("combo",0)),
            "leech":min(.42, perks.get("prism_leech",0)+mods.get("leech",0)),
            "boss_bonus":min(1.2, perks.get("prism_boss",0)+mods.get("boss",0))}


def secondary_stats(run, *, in_run=True, escape_blocked=False):
    enemy = (run.get("event") or {}).get("enemy")
    burst = run.get("burst") or {}
    hit = (1.0 if burst.get("sure") else 1-dodge_rate(enemy["agi"])) if enemy else None
    escapable = in_run and enemy and not (escape_blocked or enemy.get("boss") or enemy.get("ambush"))
    return {**combat_rates(run), "hit":hit,"dodge":player_dodge(run),"crit":attack_crit_rate(run,burst.get("blood")),
            "crit_multiplier":run["perks"].get("high_roller",1.6),"flee":flee_rate(run) if escapable else None,
            "enemy":enemy["name"] if enemy else None,"ring_shield_limit":expedition.ring_shield_limit(run)}


def formula_guide():
    """Player-facing formulas shared by the game UI and the chatbot."""
    return [
        {"title":"基础属性与装备","lines":[
            "面板从角色基础开始，加上资深、已穿装备、装备与收藏套装、图鉴档位和虹彩。资深取撤离次数与 25 的较小值，每级生命 +4、攻击 +1，每两级防御 +1。",
            "装备每项正属性按品质倍率和 90%–110% 浮动后取整。绿 ×1.25、蓝 ×1.75、紫 ×2.45、金 ×3.35、红 ×4.25。负属性只参与浮动。",
            "熔阳的攻击 ×1.50、星环的最大生命 ×1.30 单独乘算。星环防御 +30%。四彩共鸣额外使生命、攻击、防御 ×1.25。各次属性乘算按面板规则取整。",
            "局内攻击 = 永久攻击 × max(0.25，1 + 本局攻击加成 + 临装攻击修正)。局内防御 = max(0，永久防御 × (1 + 临装防御修正))。敏捷加上有效的红茶与临装修正，最低 0。"]},
        {"title":"命中、闪避与暴击","lines":[
            "基础闪避率 = min(45%，3% + 敏捷 × 0.4个百分点)。玩家闪避再加临装修正，限制在 0%–52%。主攻命中率 = 100% − 当前怪物的基础闪避率，必中蓄力的主攻为 100%。",
            "基础暴击率 = min(60%，(5% + 幸运 × 0.5个百分点) × 低血系数)。触发孤注一掷时系数为 2，其余为 1。加临装暴击修正后限制在 0%–65%。",
            "暴击伤害默认 ×1.60，豪赌之眼为 ×1.90。旧版锁血蓄力无法暴击。敏捷相同由玩家先手。"]},
        {"title":"伤害、追击与恢复","lines":[
            "命中伤害 = 攻击 × 100 ÷ (100 + 2.5 × 有效防御) × 0.9–1.1 浮动，向下取整，最低 1。有效敌防 = 敌防 × (1 − 穿透率)，穿透最高 75%。",
            "第 t 回合越战越勇倍率 = 1 + min(75%，(t−1) × 词条值)。孤注一掷触发时攻击再 ×1.75。首回合主攻再乘 (1 + 重击 + 临装首击加成)。蓄力倍率由道具决定。",
            "首击、越战越勇、低血强化、蓄力和首领增伤按实际触发条件结算。心灯对首领 +30%，与同类加成合计最高 +120%。追击概率最高 70%，追击攻击系数 60%。",
            "暴击后再结算双向伤害临装倍率。怪物攻击先算防御、减伤与格挡，再算双向倍率，最后由护盾吸收，溢出扣生命。",
            "心灯命中吸血 20%。当回合吸血 = 主攻与追击实际造成的总伤害 × 吸血率，向下取整。吸血率最高 42%，单回合回复最多为最大生命的 10%（至少 1 点的上限），实际不能超过缺失生命。",
            "穹顶星环进入新层时，把独立护盾补到 40 + ⌊(当前层数 − 1) ÷ 2⌋。受伤先扣星环盾，普通护盾余额继续保留。同层交战期间星环盾不会恢复。"]},
        {"title":"逃跑与首领","lines":[
            "基础逃跑率 = 45% + (自身敏捷 − 1.5 × 楼层) × 1.5个百分点，先限制在 15%–90%。再加词条及临装，限制在 5%–90%。风隙羽标在此后加 20 个百分点，最终最高 100%。首领与撤离守门怪不可普通逃跑。",
            "首领位于 F8、F18、F30，之后每 20 层一只。每四回合重击 ×1.26 并进入整备窗口，群友单次对首领支援最多削去其最大生命的 22%。",
            "支援期望伤害以支援者攻击和穿透过防，再乘 (3 + 重击)、(1 + 基础暴击率 × 0.6)、(1 − 敌方闪避率) 及求援者增幅。反伤按怪物本次实际打掉的生命与护盾之和计算，反伤率最高 70%。"]},
        {"title":"掉落、估值与图鉴","lines":[
            "金红基础率 = 地图基础率 × (1 + 0.12 × (楼层 − 1)) × (1 + 0.012 × 非负幸运) × 各爆率加成，最高 68%。宝箱加成只用于宝箱，金雨另乘 1.2。",
            "金红藏品的地图估值倍率 = 1 + min(0.60，max(0，(普通物资倍率 − 1) × 0.60 / 3.30))。再乘人物估值加成。首领藏品另乘战损系数 0.75 − 0.27 × 2^(-(首领序号−1)/3)。",
            "装备箱的平均回收 = 各品质概率 × 对应固定回收价之和。三档售价为 400、1,600、5,500 金。正式首领独立判定虹彩 0.5%，另一次红装判定最高 3%。",
            "藏品成功入库才点亮，同名重复只算一个图鉴。永久档位按 2、8、16、26、34、40、44、48 件累计。"]},
        {"title":"怪物成长与首领红装","lines":[
            "令 x = 楼层 − 1，z = 6 × ln(1 + x/6)。怪物各项基础值 = a + b×x + c×z。生命、攻击、防御再乘地图强度并取整，怪物敏捷最高 55。",
            *[label+"的 (a,b,c) · "+"，".join(expedition.STAT_LABELS[k]+str(tuple(v)) for k,v in expedition.DIFFICULTY[kind].items())
              for kind,label in (("normal","普通怪"),("elite","精英"),("boss","首领"))],
            "第 n 个首领生命再乘 1 + 0.055 × log₂(n)，攻击再乘 1 + 0.033 × log₂(n)。",
            "首领红装率 = 0.4% + 2.3% × (1 − 2^(-(n−1)/4)) + 地图区间加成。地图项为 (地图强度−1)/3.45 × 0.3%，限制在 0–0.3%，合计最高 3%。"]},
    ]


def damage(attack: float, defence: float, roll: float = 1.0) -> int:
    return max(1, int(attack * 100 / (100 + defence * 2.5) * roll))


def scale_dealt_damage(run: dict, amount: int) -> int:
    """Scale a combat hit without creating damage from an empty hit."""
    if amount <= 0:
        return 0
    return max(1, int(amount * run.get("field_damage_multiplier", 1)))


def hurt(run: dict, amount: int, *, strike: bool = False) -> int:
    if run["perks"].get("greed"):
        amount = max(1,int(amount * 1.20))
    amount = max(1,int(amount * (1 - run.get("field_mods",{}).get("mitigation",0))))
    guard = run.get("guard")
    if strike and guard:
        amount = max(1,int(amount * (1 - guard["reduction"])))
        guard["left"] -= 1
        if guard["left"] == 0:
            run.pop("guard")
    if strike:
        amount = scale_dealt_damage(run, amount)
    absorbed = min(int(run.get("shield",0)), amount)
    run["shield"] = int(run.get("shield",0)) - absorbed
    if "ring_shield" in run:
        run["ring_shield"] = max(0, run["ring_shield"] - absorbed)
    amount -= absorbed
    remaining = max(0,run["hp"] - amount)
    if not remaining and run["perks"].get("last_stand") and not run["last_stand"]:
        run["last_stand"] = True
        remaining = 1
    lost = run["hp"] - remaining
    run["hp"] = remaining
    return lost


def monster(map_data: dict, floor: int, rng: Random, *, elite: bool=False,
            ambush: bool=False, exclude_name: str="", boss: bool=False) -> dict:
    rank = expedition.boss_number(floor) if boss else 0
    if boss and not rank:
        raise ValueError("非首领层")
    stats = expedition.enemy_stats(map_data["multiplier"],floor,elite=elite,boss=rank,curve=map_data.get("difficulty"))
    if rank:
        names = map_data["bosses"]
        name = f"首领 · {names[(rank-1) % len(names)]}"
    else:
        pool = map_data["elites" if elite else "monsters"]
        name = rng.choice([n for n in pool if n != exclude_name] or pool)
        if ambush: name = f"电梯守门怪 · {name}"
    return {**stats,"name":name,"max_hp":stats["hp"],"elite":elite or bool(rank),
            "ambush":ambush,"boss":rank,"rounds_fought":0}


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


def _player_attack(run, enemy, rng, report, turn, *, extra=False, rates=None):
    stats, perks = run["stats"], run["perks"]
    mods = run.get("field_mods",{})
    rates = combat_rates(run) if rates is None else rates
    burst = run.get("burst") if not extra else None
    if burst:
        burst["left"] -= 1
        if burst["left"] == 0: run.pop("burst")
    if not (burst and burst["sure"]) and rng.random() < dodge_rate(enemy["agi"]):
        return ("追击落空" if extra else "你出手被怪闪开"), 0
    blood = bool(burst and burst["blood"])
    desperate = not blood and run["hp"] < stats["hp"] * perks.get("wager",0)
    attack = attack_power(run)
    attack *= 1 + min(.75,(turn-1) * perks.get("berserk",0))
    if desperate: attack *= 1.75
    if turn == 1 and not extra and not blood:
        attack *= 1 + perks.get("heavy_strike",0) + mods.get("first",0)
    if enemy.get("boss"):
        attack *= 1 + rates["boss_bonus"]
    if extra: attack *= .60
    if burst: attack *= burst["mult"]
    penetration = rates["pierce"]
    hit = damage(attack,enemy["def"]*(1-penetration),rng.uniform(.9,1.1))
    crit = not blood and rng.random() < attack_crit_rate(run)
    if crit:
        hit = int(hit * perks.get("high_roller",1.6))
        report.crits += 1
    hit = scale_dealt_damage(run, hit)
    effective = min(hit,enemy["hp"])
    enemy["hp"] -= effective
    report.total_dealt += effective
    report.peak = max(report.peak,hit)
    tag = "追击" if extra else "蓄力" if burst else "你"
    return f"{tag}{'暴击' if crit else ''}打 {hit}（怪剩{enemy['hp']}）", effective


def fight(run: dict, enemy: dict, rng: Random) -> FightResult:
    stats, perks, mods = run["stats"], run["perks"], run.get("field_mods",{})
    first = agility(run) >= enemy["agi"]
    rates = combat_rates(run)
    result = FightResult(player_first=first,player_start_hp=run["hp"],enemy_start_hp=enemy["hp"])
    offset = enemy.get("rounds_fought",0)
    for round_no in range(1,(expedition.BOSS_EXCHANGE_ROUNDS if enemy.get("boss") else 15)+1):
        turn = offset + round_no
        result.rounds = round_no
        enemy["rounds_fought"] = turn
        steps = []
        for player in (first,not first):
            if player:
                note, dealt = _player_attack(run,enemy,rng,result,turn,rates=rates)
                steps.append(note)
                chance = rates["combo"]
                if enemy["hp"] and chance and rng.random()<chance:
                    note, follow = _player_attack(run,enemy,rng,result,turn,extra=True,rates=rates)
                    steps.append(note)
                    dealt += follow
                drain = rates["leech"]
                healed = min(stats["hp"]-run["hp"],int(dealt*drain),max(1,int(stats["hp"]*.10)))
                if healed and not run.get("burst",{}).get("blood"):
                    run["hp"] += healed
                    steps.append(f"吸血 +{healed}")
            elif rng.random() < player_dodge(run):
                result.dodges += 1
                steps.append("怪出手被你闪开")
            else:
                shield = run.get("shield",0)
                charged = bool(enemy.get("boss") and turn%4==0)
                power = enemy["atk"] * (expedition.BOSS_HEAVY if charged else 1)
                lost = hurt(run,damage(power,defence(run),rng.uniform(.9,1.1)),strike=True)
                blocked = shield-run.get("shield",0)
                result.total_taken += lost
                steps.append(f"{'首领重击' if charged else '怪打'} {lost}（你剩{run['hp']}，盾挡{blocked}）")
                thorns = min(.70,perks.get("thorns",0) + mods.get("thorns",0))
                if thorns and run["hp"] and lost+blocked:
                    hit = scale_dealt_damage(run, max(1, int((lost + blocked) * thorns)))
                    reflected = min(enemy["hp"], hit)
                    enemy["hp"] -= reflected
                    result.total_dealt += reflected
                    steps.append(f"反伤 {reflected}")
            if not enemy["hp"] or not run["hp"]: break
        result.log.append(f"R{turn}：" + " ｜ ".join(steps))
        if not enemy["hp"] or not run["hp"]: break
    return result


def support_damage(stats: dict, perks: dict, enemy: dict, owner_perks: dict) -> int:
    resistance = enemy["def"] * (1 - min(.75,perks.get("pierce",0)+perks.get("prism_pierce",0)))
    hits = 3 + perks.get("heavy_strike",0)
    expected = damage(stats["atk"],resistance)*hits*(1+crit_rate(stats["luk"])*.6)
    expected *= (1-dodge_rate(enemy["agi"]))*(1+owner_perks.get("loud",0))
    if enemy.get("boss"): expected = min(expected,enemy["max_hp"]*.22)
    return max(1,int(expected))


def loadout_score(stats,perks):
    """A sustained-combat proxy evaluated on the combined set, including unique affixes."""
    pierce=min(.75,perks.get("pierce",0)+perks.get("prism_pierce",0))
    attack=stats["atk"]*100/(100+25*(1-pierce))
    attack*=1+crit_rate(stats["luk"])*(perks.get("high_roller",1.6)-1)
    attack*=1+perks.get("heavy_strike",0)/6+2.5*perks.get("berserk",0)+.60*perks.get("prism_combo",0)
    health=stats["hp"]+perks.get("shield_start",0)+perks.get("prism_shield",0)+perks.get("prism_floor_shield",0)
    endurance=health*(1+.025*stats["def"])/(1-dodge_rate(stats["agi"]))
    endurance*=1+perks.get("vampire",0)+perks.get("prism_leech",0)+.10*bool(perks.get("last_stand"))
    if perks.get("greed"): endurance/=1.20
    return attack*endurance*(1+.002*min(stats["agi"],55)+.08*perks.get("thorns",0)+.10*perks.get("wager",0))

