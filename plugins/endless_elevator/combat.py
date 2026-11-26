"""Turn-based combat for the fictional elevator expedition."""
from __future__ import annotations

from dataclasses import dataclass, field
from random import Random

from . import expedition, heirlooms


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
    return _secondary_value(run, "crit")


def player_dodge(run):
    return _secondary_value(run, "dodge")


def combat_rates(run):
    return {k: _secondary_value(run,k) for k in ("pierce","combo","leech","boss_bonus")}


def effective_enemy_defence(enemy, pierce, depth_shred=0.):
    cracks = enemy.get("heirloom_state", {}).get("cracks", 0)
    return enemy["def"] * (1 - cracks * heirlooms.CRACK_PERCENT / 100) * (1 - depth_shred) * (1 - pierce)


def _secondary_value(run, key, *, enemy=None, sources=None, origins=None):
    """Apply ordinary caps before shining bonuses. Combat and the panel use this calculation."""
    perks, mods = run["perks"], run.get("field_mods",{})
    value = 0.
    def step(name, new):
        nonlocal value
        if sources is not None:
            sources.append({"name":name,"before":value,"change":new-value,"after":new})
        value = new
    def add_perk(name, fallback):
        amount = perks.get(name,0)
        if sources is not None and origins and name in origins:
            parts = origins[name]
            if abs(sum(n for _,n in parts)-amount) < 1e-7:
                for label,n in parts:
                    step(label,value+n)
                return
        if amount:
            step(fallback,value+amount)
    high = {"hit":1., "crit":.65, "dodge":.52, "crit_multiplier":2.2, "leech":.42,
            "combo":.70, "pierce":.75, "boss_bonus":1.2, "mitigation":.55}[key]
    if key == "hit":
        if enemy is None:
            if sources is not None:
                sources.append({"name":"遇敌后根据敌方敏捷结算"})
                if perks.get("heirloom_hit"):
                    sources.append({"name":heirlooms.SETS["B01"]["name"]+" · 两件套破限",
                                    "change":perks["heirloom_hit"]})
            return None
        step(f"敌方敏捷 {enemy['agi']} · 通常命中",1-dodge_rate(enemy["agi"]))
    elif key == "crit":
        desperate = run["hp"] < run["stats"]["hp"]*perks.get("wager",0)
        step(f"幸运 {run['stats']['luk']} · 基础暴击",.05+.005*run["stats"]["luk"])
        if desperate:
            step("孤注一掷 · 低血翻倍",value*2)
        step("基础暴击上限 60%", min(.60,value))
    elif key == "crit_multiplier":
        step("基础暴击倍率",1.6)
        if "high_roller" in perks:
            step((origins or {}).get("high_roller",[("豪赌之眼 · 同款取最高",0)])[0][0],
                 perks["high_roller"])
    elif key == "dodge":
        step(f"有效敏捷 {agility(run)} · 换算",.03+.004*agility(run))
        step("基础闪避上限 45%", min(.45,value))
    else:
        step("基础",0.)
        for name,label in {"pierce":[("pierce","装备穿透"),("prism_pierce","熔阳刻痕")],
                           "combo":[("prism_combo","追击加成")],"leech":[("prism_leech","吸血加成")],
                           "boss_bonus":[("prism_boss","首领伤害加成")]}.get(key,()):
            add_perk(name,label)
    field = "boss" if key == "boss_bonus" else key
    if mods.get(field) or (origins and "field:"+field in origins):
        parts = (origins or {}).get("field:"+field,())
        for label,n in parts:
            step(label,value+n)
        adjustment = mods.get(field,0)-sum(n for _,n in parts)
        if adjustment:
            step("临装分项上限" if parts else "局内临装",value+adjustment)
    ordinary = clamp(value,-.50 if key=="mitigation" else 0,high)
    if ordinary != value:
        step("常规上限" if value>high else "常规下限",ordinary)
    bonus = perks.get("heirloom_"+key,0)
    if bonus:
        label = next(heirlooms.SETS[k]["name"] for k,(stat,_) in heirlooms.BONUSES.items() if stat==key)
        step(label+" · 两件套破限",value+bonus)
    if key == "hit" and value>1:
        step("命中最高 100%",1.)
    return value


def secondary_stats(run, *, in_run=True, escape_blocked=False, details=False, origins=None):
    enemy = (run.get("event") or {}).get("enemy")
    burst = run.get("burst") or {}
    rows = {}
    values = {key:_secondary_value(run,key,enemy=enemy,sources=rows.setdefault(key,[]) if details else None,
                                    origins=origins) for key in ("hit","dodge","crit","crit_multiplier","leech",
                                                                 "combo","pierce","boss_bonus","mitigation")}
    if enemy and burst.get("sure"):
        if details: rows["hit"].append({"name":"必中蓄力","before":values["hit"],"change":1-values["hit"],"after":1})
        values["hit"]=1.
    if burst.get("blood"):
        if details: rows["crit"].append({"name":"锁血蓄力 · 本击禁止暴击","before":values["crit"],"change":-values["crit"],"after":0})
        values["crit"]=0.
    escapable = in_run and enemy and not (escape_blocked or enemy.get("boss") or enemy.get("ambush"))
    values.update(flee=flee_rate(run) if escapable else None, enemy=enemy["name"] if enemy else None,
                  ring_shield_limit=expedition.ring_shield_limit(run))
    if details:
        values["sources"]=rows
        base=.45+(agility(run)-run["floor"]*1.5)*.015
        ordinary=clamp(base,.15,.90)
        final=flee_rate(run)
        slip=run["perks"].get("slippery",0); field=run.get("field_mods",{}).get("flee",0)
        capped=clamp(ordinary+slip+field,.05,.9)
        rows["flee"]=[{"name":label,"after":v} for label,v in (("楼层与有效敏捷",base),("基础范围 15–90%",ordinary),
            ("装备逃脱",ordinary+slip),("局内逃脱",ordinary+slip+field),("常规上限 90%",capped),
            ("风隙羽标后 · 最高 100%",final))]
        ring=values["ring_shield_limit"]
        rows["ring_shield_limit"]=[{"name":"星环基础", "after":40 if ring else 0},
            {"name":f"F{run['floor']} · 增量 48×深度/(96+深度)，最高 88","after":ring}]
        values["shield_split"]=[run.get("ring_shield",0),run.get("shield",0)-run.get("ring_shield",0)]
        depth_shred=heirlooms.depth_shred(run["floor"],run["perks"])
        values["condition"]={"blood":bool(burst.get("blood")), "guard":(run.get("guard") or {}).get("reduction",0),
            "damage_multiplier":run.get("field_damage_multiplier",1),"boss":bool(enemy and enemy.get("boss")),
            "full":run["perks"].get("heirloom_full"),"state":(enemy or {}).get("heirloom_state",{}),
            "round":(enemy or {}).get("rounds_fought",0)+1,"snow_used":bool(run.get("snow_used")),
            "hp":run["hp"],"max_hp":run["stats"]["hp"],"enemy_def":(enemy or {}).get("def"),
            "depth_shred":depth_shred,"depth_cap":run["perks"].get("heirloom_depth_cap",0),
            "effective_enemy_def":effective_enemy_defence(enemy,values["pierce"],depth_shred) if enemy else None}
    return values


def formula_guide():
    """Player-facing formulas shared by the game UI and the chatbot."""
    return [
        {"title":"基础属性与装备","lines":[
            "面板从角色基础开始，加上资深、已穿装备、装备与收藏套装、图鉴档位和虹彩。资深取撤离次数与 25 的较小值，每级生命 +4、攻击 +1，每两级防御 +1。",
            "装备每项正属性按品质倍率和 90%–110% 浮动后取整。绿 ×1.25、蓝 ×1.75、紫 ×2.45、金 ×3.35、红 ×4.25。负属性只参与浮动。",
            "熔阳攻击 ×1.30，星环最大生命 ×1.20、防御 +18%。点亮 2/3/4 种时再独立乘以 1.03/1.07/1.12，仅取当前档。每步就近取整。",
            "局内攻击 = 永久攻击 × max(0.25，1 + 本局攻击加成 + 临装攻击修正)。局内防御 = max(0，永久防御 × (1 + 临装防御修正))。敏捷加上有效的红茶与临装修正，最低 0。"]},
        {"title":"命中、闪避与暴击","lines":[
            "基础闪避率 = min(45%，3% + 敏捷 × 0.4 个百分点)。常规闪避计入临装后最高 52%，退件邮路再加 8 点。通常命中 = 100% − 敌方基础闪避，再加测绘簿 14 点后最高 100%。必中蓄力直接取 100%。",
            "基础暴击率 = min(60%，(5% + 幸运 × 0.5 个百分点) × 低血系数)。孤注一掷触发时系数为 2，其余为 1。加临装后常规最高 65%，末班灯列再加 10 点。",
            "暴击伤害默认 ×1.60，豪赌之眼为 ×1.90，余温窑账在常规结算后加 0.35。锁血蓄力本击无法暴击。敏捷相同由玩家先手。"]},
        {"title":"伤害、追击与恢复","lines":[
            "命中伤害 = 攻击 × 100 ÷ (100 + 2.5 × 有效防御) × 0.9–1.1 浮动，向下取整，最低 1。有效敌防 = 敌防 × 裂纹系数 × 深层系数 × (1 − 穿透率)。常规穿透最高 75%，缺刻值夜表再加 12 点。",
            "完整四件套或两套各两件，从 F51 起对主攻、追击生效深层破甲。令 x=楼层−50，削防比例为上限×x/(85+x)。四件套上限 60%，两套各两件上限 44%。",
            "第 t 回合越战越勇倍率 = 1 + min(75%，(t−1) × 词条值)。孤注一掷触发时攻击再 ×1.75。首回合主攻再乘 (1 + 重击 + 临装首击加成)。蓄力倍率由道具决定。",
            "心灯对首领 +20%。常规首领增伤最高 +120%。常规追击最高 70%，逆风放线人再加 12 点。追击攻击系数 60%。",
            "暴击后再结算双向伤害临装倍率。怪物攻击先算防御、减伤与格挡，再算双向倍率，最后由护盾吸收，溢出扣生命。",
            "心灯命中吸血 14%。常规吸血率上限 42%，雪线留声筒再加 8 点。按本回合主攻与追击实际削去的生命取整，常规回复最多为最大生命的 10%，并受缺失生命限制。",
            "穹顶星环进新层时补足独立盾。设 x=楼层−1，上限为 40＋四舍五入(48x/(96+x))，最多 88。受伤先扣星环盾，普通盾独立。"]},
        {"title":"逃跑与首领","lines":[
            "基础逃跑率 = 45% + (自身敏捷 − 1.5 × 楼层) × 1.5个百分点，先限制在 15%–90%。再加词条及临装，限制在 5%–90%。风隙羽标在此后加 20 个百分点，最终最高 100%。首领与撤离守门怪不可普通逃跑。",
            "首领位于 F8、F18、F30，之后每 20 层一只。每四回合重击 ×1.26 并进入整备窗口，群友单次对首领支援最多削去其最大生命的 22%。精英第 8 回合整备，16 回合未分胜负则退场。",
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


def hurt(run: dict, amount: int, *, strike: bool = False, enemy=None, turn=0, log=None) -> int:
    enemy = enemy if enemy is not None else (run.get("event") or {}).get("enemy")
    full = run["perks"].get("heirloom_full")
    if run["perks"].get("greed"):
        amount = max(1,int(amount * 1.20))
    mitigation = _secondary_value(run,"mitigation") if strike else run.get("field_mods",{}).get("mitigation",0)
    amount = max(1,int(amount * (1 - mitigation)))
    guard = run.get("guard")
    if strike and guard:
        amount = max(1,int(amount * (1 - guard["reduction"])))
        guard["left"] -= 1
        if guard["left"] == 0:
            run.pop("guard")
    if strike:
        if full=="B06" and turn and turn%4==0:
            amount=max(1,int(amount*heirlooms.TIMING_DAMAGE_FACTOR))
        amount = scale_dealt_damage(run, amount)
        if full=="B03" and enemy:
            max_hp = run["stats"]["hp"]
            excess = amount * 100 - max_hp * heirlooms.BALLAST_THRESHOLD
            state=enemy.setdefault("heirloom_state",{})
            if excess > 0 and not state.get("ballast"):
                saved=min(excess*heirlooms.BALLAST_EXCESS//10000,max_hp*heirlooms.BALLAST_CAP//100)
                amount-=saved
                state["ballast"]=True
                if log is not None: log.append(heirlooms.trigger("B03",减免伤害=saved))
    absorbed = min(int(run.get("shield",0)), amount)
    run["shield"] = int(run.get("shield",0)) - absorbed
    if "ring_shield" in run:
        run["ring_shield"] = max(0, run["ring_shield"] - absorbed)
    amount -= absorbed
    remaining = max(0,run["hp"] - amount)
    if not remaining and run["perks"].get("last_stand") and not run["last_stand"]:
        run["last_stand"] = True
        remaining = 1
    if (not remaining and strike and full=="B08" and not run.get("snow_used")
            and not run.get("burst",{}).get("blood")):
        remaining = max(1,run["stats"]["hp"]*heirlooms.SNOW_HEALTH_PERCENT//100)
        run["snow_used"] = True
        if log is not None: log.append(heirlooms.trigger("B08",保留生命=remaining))
    lost = max(0,run["hp"] - remaining)
    run["hp"] = remaining
    return lost


def monster(map_data: dict, floor: int, rng: Random, *, elite: bool=False,
            ambush: bool=False, exclude_name: str="", boss: bool=False, boss_rank: int | None=None) -> dict:
    rank = (boss_rank if boss_rank is not None else expedition.boss_number(floor)) if boss else 0
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
    full=perks.get("heirloom_full")
    state=enemy.setdefault("heirloom_state",{}) if full else {}
    notes=[]
    corrected=False
    if not (burst and burst["sure"]) and rng.random() < 1-_secondary_value(run,"hit",enemy=enemy):
        if not extra and full=="B01" and not state.get("redraw"):
            state["redraw"]=corrected=True
        else:
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
    if corrected:
        attack *= heirlooms.REDRAW_DAMAGE_FACTOR
    if not extra and full=="B04" and state.pop("letter",False):
        attack *= heirlooms.LETTER_MULTIPLIER
        notes.append(heirlooms.trigger("B04"))
    penetration = rates["pierce"]
    target_defence=effective_enemy_defence(enemy,penetration,heirlooms.depth_shred(run.get("floor",1),perks))
    hit = damage(attack,target_defence,rng.uniform(.9,1.1))
    crit = not blood and rng.random() < attack_crit_rate(run)
    if crit:
        hit = int(hit * _secondary_value(run,"crit_multiplier"))
        report.crits += 1
    hit = scale_dealt_damage(run, hit)
    effective = min(hit,enemy["hp"])
    enemy["hp"] -= effective
    report.total_dealt += effective
    report.peak = max(report.peak,hit)
    if corrected: notes.append(heirlooms.trigger("B01",伤害=effective))
    if not extra and full=="B02":
        state["hits"]=state.get("hits",0)+1
        if state["hits"]%3==0 and enemy["hp"]:
            echo=min(enemy["hp"],effective*heirlooms.ECHO_PERCENT//100)
            enemy["hp"]-=echo
            report.total_dealt+=echo
            if echo:
                notes.append(heirlooms.trigger("B02",余响伤害=echo))
    if not extra and full=="B05" and crit and state.get("cracks",0)<2:
        state["cracks"]=state.get("cracks",0)+1
        notes.append(heirlooms.trigger("B05",敌人=enemy["name"],降防比例=f"{state['cracks']*heirlooms.CRACK_PERCENT}%",裂纹层数=state["cracks"]))
    if extra and full=="B07" and not state.get("reeled"):
        state["reeled"]=True
        healed=0 if run.get("burst",{}).get("blood") else min(stats["hp"]-run["hp"],
            effective*heirlooms.REEL_PERCENT//100,stats["hp"]*heirlooms.REEL_CAP//100)
        run["hp"]+=healed
        notes.append(heirlooms.trigger("B07",回复生命=healed))
    tag = "追击" if extra else "蓄力" if burst else "你"
    return " ｜ ".join([f"{tag}{'暴击' if crit else ''}打 {hit}（怪剩{enemy['hp']}）",*notes]), effective


def fight(run: dict, enemy: dict, rng: Random) -> FightResult:
    stats, perks, mods = run["stats"], run["perks"], run.get("field_mods",{})
    first = agility(run) >= enemy["agi"]
    rates = combat_rates(run)
    result = FightResult(player_first=first,player_start_hp=run["hp"],enemy_start_hp=enemy["hp"])
    offset = enemy.get("rounds_fought",0)
    length = expedition.BOSS_EXCHANGE_ROUNDS if enemy.get("boss") else 15
    if enemy.get("elite") and not enemy.get("boss"):
        next_break = expedition.ELITE_INTERVAL if offset < expedition.ELITE_INTERVAL else expedition.ELITE_LIMIT
        length = max(0, next_break-offset)
    for round_no in range(1,length+1):
        turn = offset + round_no
        result.rounds = round_no
        enemy["rounds_fought"] = turn
        steps = []
        takes_first=first
        if perks.get("heirloom_full")=="B06" and turn%4==0:
            steps.append(heirlooms.trigger("B06",回合=turn))
            takes_first=True
        for player in (takes_first,not takes_first):
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
                if perks.get("heirloom_full")=="B04":
                    enemy.setdefault("heirloom_state",{})["letter"]=True
            else:
                shield = run.get("shield",0)
                charged = bool(enemy.get("boss") and turn%4==0)
                power = enemy["atk"] * (expedition.BOSS_HEAVY if charged else 1)
                lost = hurt(run,damage(power,defence(run),rng.uniform(.9,1.1)),strike=True,enemy=enemy,turn=turn,log=steps)
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
    view = {"stats":stats,"hp":stats["hp"],"perks":perks}
    pierce = _secondary_value(view,"pierce")
    resistance = effective_enemy_defence(enemy,pierce)
    hits = 3 + perks.get("heavy_strike",0)
    crit = _secondary_value(view,"crit")
    # Support retains its ordinary damage proxy; shining contributes its own usable bonuses.
    expected = damage(stats["atk"],resistance)*hits*(1+crit*(.6+perks.get("heirloom_crit_multiplier",0)))
    expected *= _secondary_value(view,"hit",enemy=enemy)*(1+owner_perks.get("loud",0))
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

