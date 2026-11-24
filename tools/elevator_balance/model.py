"""Combat first-passage probabilities, strength calibration, and editable game data."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import types
from functools import lru_cache
from pathlib import Path
from random import Random

import numpy as np

from rules import catalog as catalogue
from rules import combat
from rules import game
from tuning_rules import apply_monster_settings, mix_weight, raw_stats

ROOT = Path(__file__).resolve().parent
BASE = json.loads((ROOT / "rules/content.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((ROOT / "rules/manifest.json").read_text(encoding="utf-8"))
STATS = ("hp", "atk", "def", "agi", "luk")
GRADES = {"green": 1.25, "blue": 1.85, "purple": 2.75, "gold": 4.0}
GRADE_LABELS = {"green": "绿", "blue": "蓝", "purple": "紫", "gold": "金"}
DEFAULT_TUNING = {"mode": "live", "cap_floor": 20, "half": 6, "hp_scale": 1,
                  "atk_scale": 1, "elite_hp": 1.75, "elite_atk": 1.30}
MAP_ORDER = list(BASE["maps"])
DEFAULT_SEED = 261123


def seeded(seed, *parts):
    key = json.dumps([seed, *parts], ensure_ascii=False, separators=(",", ":")).encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:8], "little")


def finite(value, name, low, high, integer=False):
    if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} 应在 {low:g} 到 {high:g} 之间")
    if integer and int(value) != value:
        raise ValueError(f"{name} 需要整数")
    return int(value) if integer else float(value)


def preset(extracts, grade=None, perks=(), items=("crowbar", "vest", "belt")):
    gear = [{"id": key, "grade": grade, "roll": 1} for key in items] if grade else []
    return {"extracts": extracts, "gear": gear, "perks": list(perks), "sets": [],
            "bonus": dict.fromkeys(STATS, 0), "hp_fraction": 1, "shield": 0, "atk_boost": 0}


PRESETS = {
    "new": ("无装新人", preset(0)),
    "veteran": ("25 撤无装", preset(25)),
    "green": ("3 撤绿装", preset(3, "green")),
    "blue": ("10 撤蓝装", preset(10, "blue", ("pierce", "heavy_strike", "treasure"))),
    "purple": ("18 撤紫装", preset(18, "purple", ("pierce", "heavy_strike", "shield_start", "high_roller", "treasure", "slippery"))),
    "gold": ("25 撤金装", preset(25, "gold", ("pierce", "heavy_strike", "shield_start", "high_roller", "vampire", "treasure"))),
}


def recommended():
    """Designer proposal. Its reference equipment and sampled outcomes are documented."""
    edits = {
        "crowbar": {"atk":10,"def":2}, "laser":{"atk":11,"agi":6},
        "shotgun":{"atk":16,"agi":-5}, "keyboard":{"atk":9,"def":4},
        "pan":{"atk":8,"luk":8}, "wrench":{"atk":12,"def":4,"agi":-3},
        "cyber_rod":{"atk":9,"agi":3,"luk":5},
        "vest":{"def":10,"hp":18}, "cloak":{"def":5,"agi":10},
        "frame":{"hp":36,"def":8,"agi":-3}, "camouflage":{"def":7,"luk":7},
        "coat":{"hp":29,"def":6}, "delivery_jacket":{"hp":16,"def":5,"agi":7},
        "padded_coat":{"hp":40,"def":8,"agi":-5},
        "necklace":{"luk":14}, "skates":{"agi":12}, "belt":{"hp":32},
        "drum":{"atk":4,"luk":9}, "binoculars":{"agi":6,"luk":7},
        "hairband":{"def":6,"luk":6}, "gold_abacus":{"atk":6,"luk":6},
    }
    curves = {}
    for key,cap,value,rare in zip(MAP_ORDER,(6.6,8.2,8.4,9.2),(1.0,1.8,2.8,3.6),(.02,.04,.065,.085)):
        curves[key] = {"value_multiplier":value,"rare_rate":rare,
                       "difficulty":{**DEFAULT_TUNING,"mode":"bounded","cap_floor":cap,"half":12}}
    return {"maps": curves, "gear_factors":{"green":1.25,"blue":1.75,"purple":2.45,"gold":3.35},
            "gear_sell":{"green":160,"blue":400,"purple":1500,"gold":5700},
            "equipment": edits}


def clean_mix(row):
    values={}
    for key in ("weak","strong",*(k for k in ("hard_near","tail") if k in row)):
        values[key]={s:finite(row[key][s],s,1 if s in ("hp","atk") else 0,
                              2000000 if s=="hp" else 10000,True) for s in ("hp","atk","def","agi")}
    for key in ("start_weight","end_weight"):
        values[key]=finite(row[key],key,0,1)
    if "hard_near" in row:
        values["hard_weight"]=finite(row["hard_weight"],"强档相邻面板混合权重",0,1)
    if "tail" in row:
        values["tail_weight"]=finite(row["tail_weight"],"深层精英占比",0,.5)
    values["half"]=finite(row["half"],"半程层数",.25,80)
    return values


def scenario(overrides=None):
    """Apply a JSON scenario only to a fresh local copy of the source configuration."""
    overrides = overrides or {}
    if not isinstance(overrides,dict) or set(overrides)-{"stats","gear_sell","shop","gear_factors","equipment","maps","event_weights"}:
        raise ValueError("需要工作台导出的方案或实验 JSON")
    data = copy.deepcopy(BASE)
    data["gear_factors"] = dict(GRADES)
    data["gear_sell"] = dict(game.EQUIP_SELL_PRICES)
    for grade,value in overrides.get("gear_sell",{}).items():
        if grade not in GRADES: raise ValueError("未知装备品质")
        data["gear_sell"][grade]=finite(value,"装备回收价",0,100000,True)
    for key,value in overrides.get("shop",{}).items():
        if key not in game.GEAR_BOX_POOLS: raise ValueError("未知装备箱")
        data["shop"][key]=finite(value,"装备箱价格",1,100000,True)
    for field,value in overrides.get("stats",{}).items():
        if field not in STATS: raise ValueError("未知面板属性")
        data["stats"][field] = finite(value, field, 1 if field in ("hp","atk") else 0, 1000, True)
    for grade,value in overrides.get("gear_factors",{}).items():
        if grade not in GRADES: raise ValueError("未知装备品质")
        data["gear_factors"][grade]=finite(value, grade, .1, 10)
    for key,values in overrides.get("equipment",{}).items():
        if key not in data["equipment"] or not isinstance(values,dict) or not set(values)<=set(STATS):
            raise ValueError("装备底材或属性无效")
        data["equipment"][key]["stats"]={s:finite(v,s,-120,400,True) for s,v in values.items() if v}
    for key, changes in overrides.get("maps",{}).items():
        if key not in data["maps"]: raise ValueError("未知地图")
        m = data["maps"][key]
        for f,lo,hi in (("multiplier",.1,12),("value_multiplier",.1,25),("rare_rate",0,1),
                        ("entry",0,1000000),("ambush_floor",0,100),("ambush_rate",0,1)):
            if f in changes:
                m[f]=finite(changes[f],f,lo,hi,f in ("entry","ambush_floor"))
        if "difficulty" in changes:
            t = {**DEFAULT_TUNING,**changes["difficulty"]}
            if t["mode"] not in ("live","bounded"): raise ValueError("未知曲线")
            for f,lo,hi in (("cap_floor",1,100),("half",.25,80),("hp_scale",.1,4),
                            ("atk_scale",.1,4),("elite_hp",1,4),("elite_atk",1,4)):
                t[f]=finite(t[f],f,lo,hi)
            if t.get("mixtures"):
                t["mixtures"]={k:clean_mix(v) for k,v in t["mixtures"].items() if k in ("normal","elite")}
            m["difficulty"]=t
    if "event_weights" in overrides:
        rows=overrides["event_weights"]
        if not isinstance(rows,list) or not 1<=len(rows)<=20: raise ValueError("事件分层数量无效")
        ranks=[]
        for row in rows:
            w={k:finite(row["weights"][k],k,0,1000) for k in ("loot","normal","elite","choice","relic")}
            if sum(w.values())<=0: raise ValueError("每段事件权重总和必须为正")
            ranks.append({"from":finite(row["from"],"起始层",1,100,True),"weights":w})
        if ranks[0]["from"]!=1 or any(b["from"]<=a["from"] for a,b in zip(ranks,ranks[1:])):
            raise ValueError("事件分层须从 1 开始并严格递增")
        data["event_weights"]=ranks
    return data


class TunedCatalog(catalogue.Catalog):
    def roll_gear(self,rng,grade,multiplier=1.0):
        key=rng.choice(list(self.equipment))
        factor=self.gear_factors[grade]
        stats={k:round(v*(factor if v>0 else 1)*rng.uniform(.9,1.1))
               for k,v in self.equipment[key]["stats"].items()}
        amount=max(1,round(rng.randint(*self.rarities[grade]["range"])*multiplier))
        return {"id":key,"rarity":grade,"value":amount,"stats":stats,
                "affixes":self.roll_affixes(grade,rng),"quality":""}


def make_catalog(data):
    cat = TunedCatalog(ROOT/"rules/content.json")
    cat.base,cat.maps,cat.weights = data["stats"],data["maps"],data["event_weights"]
    cat.equipment = data["equipment"]
    cat.gear_factors = data["gear_factors"]
    cat.equip_sell,cat.shop = data["gear_sell"],data["shop"]
    return cat


def panel(data, spec):
    stats = dict(data["stats"])
    level = finite(spec.get("extracts",0),"累计撤离",0,1000000,True)
    lv=min(25,level)
    stats["hp"]+=lv*4
    stats["atk"]+=lv
    stats["def"]+=lv//2
    sets=list(dict.fromkeys(spec.get("sets",[])))
    for name in sets:
        for key,value in data["sets"][name].get("stats",{}).items(): stats[key]+=value
    slots=set()
    for piece in spec.get("gear",[]):
        definition=data["equipment"][piece["id"]]
        if definition["slot"] in slots: raise ValueError("同一装备栏只能穿一件")
        slots.add(definition["slot"])
        grade=data["gear_factors"][piece["grade"]]
        roll=finite(piece.get("roll",1),"装备浮动",.9,1.1)
        for key,value in definition["stats"].items():
            stats[key]+=round(value*(grade if value>0 else 1)*roll)
    for key in STATS: stats[key]+=finite(spec.get("bonus",{}).get(key,0),key,-1000,1500,True)
    if min(stats.values())<0 or stats["hp"]<1 or stats["atk"]<1:
        raise ValueError("最终生命与攻击需为正，其余面板属性至少为零")
    perks={key:data["affixes"][key]["power"] for key in dict.fromkeys(spec.get("perks",[]))}
    return {"stats":stats,"perks":perks,"sets":sets,"extracts":level,
            "hp_fraction":finite(spec.get("hp_fraction",1),"开战血量比例",.01,1),
            "shield":finite(spec.get("shield",0),"额外初始护盾",0,1000,True),
            "atk_boost":finite(spec.get("atk_boost",0),"攻击增幅",0,.6)}


def initial_state(hero, floor):
    return {"stats":hero["stats"],"perks":hero["perks"],"floor":floor,"tea_until":0,
            "hp":hero.get("current_hp",max(1,round(hero["stats"]["hp"]*hero["hp_fraction"]))),
            "shield":hero.get("remaining_shield",hero["shield"]+int(hero["perks"].get("shield_start",0))),
            "last_stand":hero.get("spent_last_stand",False),"atk_boost":hero["atk_boost"]}


def monster(map_data, floor, rng, **args):
    enemy = combat.monster(map_data, floor, rng, **args)
    return apply_monster_settings(map_data, floor, enemy, rng, elite=args.get("elite", False))


# The copy of Turn packaged with this workbench gets a scenario-aware spawn function.
# The live plugin and its rule files are never imported from the user's installation.
game.combat=types.SimpleNamespace(**{**vars(combat),"monster":monster})


@lru_cache(maxsize=8192)
def hit_pmf(atk, defence, miss, crit=0.0, crit_mult=1.6, incoming_mult=1.0):
    base=atk*100/(100+2.5*defence)
    lo,hi=.9*base,1.1*base
    masses={0:miss}
    for raw in range(math.floor(lo),math.floor(hi)+1):
        prob=max(0,min(hi,raw+1)-max(lo,raw))/(hi-lo)
        if not prob: continue
        basic=max(1,raw)
        for multiplier,weight in ((1,1-crit),(crit_mult,crit)):
            if not weight: continue
            damage=max(1,int(int(basic*multiplier)*incoming_mult))
            masses[damage]=masses.get(damage,0)+(1-miss)*weight*prob
    p=np.zeros(max(masses)+1)
    for amount,mass in masses.items(): p[amount]=mass
    return p/p.sum()


def passage(health, shield, pmfs, last_stand=False):
    """Track damage below lethal threshold and first lethal hits, one round at a time.

    All output arrays include round zero. The separate 'used' mass represents one
    remaining HP after last stand. A miss preserves it, and a subsequent hit kills.
    """
    threshold=health+shield
    active=np.array([1.0])
    used=0.0
    survival=[1.0]
    deaths=[0.0]
    hp_mass=[float(health)]
    hp_by_damage=np.minimum(health,threshold-np.arange(min(threshold,1+sum(len(p)-1 for p in pmfs))))
    for pmf in pmfs:
        after=np.convolve(active,pmf)
        crossing=float(after[threshold:].sum())
        active=after[:threshold]
        deaths.append(used*(1-pmf[0]) if last_stand else crossing)
        used=used*pmf[0]+crossing if last_stand else 0.0
        survival.append(float(active.sum()+used))
        hp_mass.append(float(active@hp_by_damage[:len(active)]+used))
    return survival,deaths,hp_mass


def can_solve_exact(hero):
    return not (hero["perks"].get("wager") or hero["perks"].get("thorns"))


def exact_battle(hero, enemy, floor):
    """Combine the two independent first-passage times in actual initiative order."""
    if not can_solve_exact(hero): raise ValueError("孤注一掷与反伤须联合模拟")
    run=initial_state(hero,floor)
    s,p=hero["stats"],hero["perks"]
    first=s["agi"]>=enemy["agi"]
    attack=s["atk"]*(1+run["atk_boost"])
    attacks=[]
    for t in range(15):
        strength=attack*(1+t*p.get("berserk",0))*(1+p.get("heavy_strike",0) if t==0 else 1)
        attacks.append(hit_pmf(strength,enemy["def"]*(1-p.get("pierce",0)),combat.dodge_rate(enemy["agi"]),
                               combat.crit_rate(s["luk"]),p.get("high_roller",1.6)))
    incoming=hit_pmf(enemy["atk"],s["def"],combat.dodge_rate(s["agi"]),incoming_mult=1.25 if p.get("greed") else 1)
    enemy_survival,enemy_death,_=passage(enemy["hp"],0,attacks)
    player_survival,player_death,hp_mass=passage(
        run["hp"],run["shield"],[incoming]*15,bool(p.get("last_stand")) and not run["last_stand"])
    win=sum(enemy_death[t]*player_survival[t-int(first)] for t in range(1,16))
    lose=sum(player_death[t]*enemy_survival[t-int(not first)] for t in range(1,16))
    draw=enemy_survival[-1]*player_survival[-1]
    end_hp=sum(enemy_death[t]*hp_mass[t-int(first)] for t in range(1,16))+enemy_survival[-1]*hp_mass[-1]
    return {"win":float(win),"loss":float(lose),"draw":float(draw),
            "rounds":float(sum(a*b for a,b in zip(enemy_survival[:-1],player_survival[:-1]))),
            "hp_loss":float(max(0,run["hp"]-end_hp)),"se":0.0,"method":"DP"}


def sampled_battle(hero, enemy, floor, samples, seed):
    wins=losses=rounds=damage=0
    rng=Random(seed)
    for _ in range(samples):
        run=initial_state(hero,floor)
        hp=run["hp"]
        opponent=dict(enemy)
        rep=combat.fight(run,opponent,rng)
        wins+=opponent["hp"]==0
        losses+=run["hp"]==0
        rounds+=rep.rounds
        damage+=hp-run["hp"]
    w=wins/samples
    return {"win":w,"loss":losses/samples,"draw":(samples-wins-losses)/samples,
            "rounds":rounds/samples,"hp_loss":damage/samples,"se":math.sqrt(w*(1-w)/samples),
            "method":"MC","n":samples}


def battle(hero, enemy, floor, samples=1000, seed=DEFAULT_SEED):
    return exact_battle(hero,enemy,floor) if can_solve_exact(hero) else sampled_battle(hero,enemy,floor,samples,seed)


def components(map_data,floor,elite):
    row=map_data.get("difficulty",{}).get("mixtures",{}).get("elite" if elite else "normal")
    if not row: return [(1.,raw_stats(map_data,floor,elite))]
    w=mix_weight(row,floor);tau=row.get("tail_weight",0);z=row.get("hard_weight",1)
    parts=[(1-w,row["weak"]),(w*(1-tau)*z,row["strong"])]
    if "hard_near" in row:
        parts.append((w*(1-tau)*(1-z),row["hard_near"]))
    if tau: parts.append((w*tau,row["tail"]))
    return parts


def map_battle(hero, map_data, floor, elite=False, samples=1000, seed=DEFAULT_SEED):
    bins=components(map_data,floor,elite)
    if len(bins)==1: return battle(hero,bins[0][1],floor,samples,seed)
    parts=[(weight,battle(hero,enemy,floor,samples,seeded(seed,"component",i)))
           for i,(weight,enemy) in enumerate(bins) if weight>0]
    return {**parts[0][1],"mixed":True,**{k:sum(q*r[k] for q,r in parts) for k in ("win","loss","draw","rounds","hp_loss")},
            "se":math.sqrt(sum((q*r["se"])**2 for q,r in parts))}


def flee_choice(hero,map_data,floor,elite=False,samples=1000,seed=DEFAULT_SEED):
    """One escape roll, then the actual undodgeable hit and forced combat on failure."""
    choices=components(map_data,floor,elite)
    escape=combat.flee_rate(initial_state(hero,floor))
    survive=escape;kill=hp_loss=0.
    for index,(weight,enemy) in enumerate(choices):
        run=initial_state(hero,floor)
        lost=combat.hurt(run,combat.damage(enemy["atk"],hero["stats"]["def"]))
        prob=(1-escape)*weight
        hp_loss+=prob*lost
        if run["hp"]:
            residual={**hero,"current_hp":run["hp"],"remaining_shield":run["shield"],"spent_last_stand":run["last_stand"]}
            r=battle(residual,enemy,floor,samples,seeded(seed,"flee",index))
            survive+=prob*(1-r["loss"])
            kill+=prob*r["win"]
            hp_loss+=prob*r["hp_loss"]
    return {"escape":escape,"survive":survive,"kill":kill,"hp_loss":hp_loss}


def bracket(hero, map_data, floor, elite, target, samples=800, seed=DEFAULT_SEED):
    """Find two attainable enemy panels straddling a requested conditional kill rate."""
    exact=can_solve_exact(hero)
    cache={}
    def at(scale):
        enemy=raw_stats(map_data,floor,elite,scale)
        key=tuple(enemy.values())
        if key not in cache: cache[key]=battle(hero,enemy,floor,samples,seed)["win"]
        return (scale,cache[key],enemy)
    lo,hi=at(.015625),at(32.0)
    if not hi[1]<=target<=lo[1]:
        raise ValueError(f"F{floor} 的可达胜率为 {hi[1]:.3f}～{lo[1]:.3f}，目标超出范围")
    for _ in range(22 if exact else 13):
        mid=at((lo[0]+hi[0])/2)
        if mid[1]>=target: lo=mid
        else: hi=mid
    weight=(lo[1]-target)/(lo[1]-hi[1]) if lo[1]>hi[1] else 0
    return {"floor":floor,"target":target,"weak":lo[2],"strong":hi[2],"weak_win":lo[1],
            "strong_win":hi[1],"strong_weight":weight,"scale":(1-weight)*lo[0]+weight*hi[0],
            "actual":(1-weight)*lo[1]+weight*hi[1],"method":"DP" if exact else "MC"}


def power(hero, samples=1000, seed=DEFAULT_SEED):
    ref={"multiplier":1.0}
    fresh=panel(BASE,PRESETS["new"][1])
    base=bracket(fresh,ref,5,False,.5)["scale"]
    try: own=bracket(hero,ref,5,False,.5,samples,seed)
    except ValueError:
        return {"index":None,"m50":None,"base_m50":base,"method":"范围外"}
    return {"index":100*own["scale"]/base,"m50":own["scale"],"base_m50":base,"method":own["method"]}


def tail_enemy(gold_hero,loss_target):
    """Find an elite template threatening a fixed graduation reference even at full health."""
    # All four tail opponents come from one ordered family, including their initiative.
    md={"multiplier":BASE["maps"]["abyss"]["multiplier"]}
    low,high=1.,100.
    for _ in range(23):
        mid=(low+high)/2
        stats=raw_stats(md,mid,True)
        if exact_battle(gold_hero,stats,1)["loss"]<loss_target: low=mid
        else: high=mid
    return raw_stats(md,high,True)


def calibrate_mix(hero,map_data,elite,w1,wend,half,tail=None,tail_weight=0):
    if not can_solve_exact(hero):
        raise ValueError("精确胜率校准请移除孤注一掷、反甲刺猬；这两种词条可用实战采样评估")
    w1=finite(w1,"首层胜率",.001,.9999)
    wend=finite(wend,"极限胜率",.001,.9999)
    half=finite(half,"半程层数",.25,80)
    tail_weight=finite(tail_weight,"深层精英占比",0,.5) if tail is not None else 0.
    endpoint=min(w1,wend)
    tail_win=exact_battle(hero,tail,1)["win"] if tail_weight else 0.
    regular=(endpoint-tail_weight*tail_win)/(1-tail_weight)
    m={**map_data,"difficulty":{**map_data.get("difficulty",{}),"mode":"live"}}
    def prototype(x):
        enemy=raw_stats(m,max(1,x),elite,min(1,x))
        return enemy,exact_battle(hero,enemy,1)["win"]
    easy,p_easy=prototype(1.0)
    if p_easy<max(w1,wend):
        easy,p_easy=prototype(.01)
    high=100.0
    hard,p_hard=prototype(high)
    if p_hard>regular or regular>prototype(.01)[1] or p_easy<max(w1,wend):
        raise ValueError("当前模板无法覆盖目标胜率，可调整目标或降低深层精英占比")
    low=.01
    near,p_near=prototype(low)
    for _ in range(26):
        mid=(high+low)/2
        enemy,p=prototype(mid)
        if p>regular: near,p_near,low=enemy,p,mid
        else: hard,p_hard,high=enemy,p,mid
    z=(p_near-regular)/(p_near-p_hard) if p_near>p_hard else 0.
    gap=p_easy-endpoint
    if gap<1e-12 and abs(w1-wend)>1e-12: raise ValueError("两种怪物的胜率差过小")
    return {"weak":easy,"hard_near":near,"strong":hard,"hard_weight":min(1.,max(0.,z)),
            "start_weight":(p_easy-w1)/gap if gap>=1e-12 else 1.,
            "end_weight":(p_easy-wend)/gap if gap>=1e-12 else 1.,
            "half":half,"weak_win":p_easy,"strong_win":endpoint,"hard_component_wins":[p_near,p_hard],
            "target_first":w1,"target_end":wend,**({"tail":tail,"tail_weight":tail_weight} if tail_weight else {}),
            "reference":{"stats":dict(hero["stats"]),"perks":dict(hero["perks"]),"hp_fraction":hero["hp_fraction"],
                         "shield":hero["shield"],"atk_boost":hero["atk_boost"]}}


MAP_BENCHMARK=dict(zip(MAP_ORDER,("new","green","blue","purple")))
TARGETS={"mall":((.9995,.72),(.995,.38)), "subway":((.999,.68),(.985,.32)),
         "abyss":((.998,.60),(.98,.28)), "auction":((.997,.55),(.97,.24))}


@lru_cache(maxsize=1)
def calibrated_proposal():
    plan=recommended()
    d=scenario(plan)
    gold=panel(d,PRESETS["gold"][1])
    for key,bench in MAP_BENCHMARK.items():
        hero=panel(d,PRESETS[bench][1])
        mix={}
        for i,kind in enumerate(("normal","elite")):
            a,b=TARGETS[key][i]
            loss_target=.45+.05*MAP_ORDER.index(key)
            tail=tail_enemy(gold,loss_target) if i else None
            mix[kind]=calibrate_mix(hero,d["maps"][key],bool(i),a,b,12,tail,.12 if i else 0.)
        plan["maps"][key]["difficulty"]["mixtures"]=mix
    return plan


def compare_map_order(hero,data,horizon,samples,seed):
    """Minimize the inter-map win-rate gap over integer floors for exponential mixtures."""
    output=[]
    for kind,elite in (("普通",False),("精英",True)):
        for earlier,later in zip(MAP_ORDER,MAP_ORDER[1:]):
            x,y=data["maps"][earlier],data["maps"][later]
            mixes=[v.get("difficulty",{}).get("mixtures",{}).get("elite" if elite else "normal") for v in (x,y)]
            if all(mixes) and can_solve_exact(hero):
                ends=[map_battle(hero,v,math.inf,elite)["win"] for v in (x,y)]
                starts=[map_battle(hero,v,1,elite)["win"] for v in (x,y)]
                amplitudes=[s-e for s,e in zip(starts,ends)]
                k1,k2=[math.log(2)/v["half"] for v in mixes]
                extremal=[(starts[0]-starts[1],"1"),(ends[0]-ends[1],"∞")]
                a1,a2=amplitudes
                if k1!=k2 and a1*a2>0:
                    z=math.log((k1*a1)/(k2*a2))/(k1-k2)
                    if math.isfinite(z) and z>=0:
                        for i in {math.floor(z),math.ceil(z)}:
                            gap=ends[0]-ends[1]+a1*math.exp(-k1*i)-a2*math.exp(-k2*i)
                            extremal.append((gap,str(i+1)))
                worst,floor=min(extremal)
                scope="全部整数楼层 · 解析"
            else:
                values=[(map_battle(hero,x,f,elite,samples,seeded(seed,f))["win"]-
                         map_battle(hero,y,f,elite,samples,seeded(seed,f))["win"],f) for f in range(1,horizon+1)]
                worst,floor=min(values)
                scope=f"F1–F{horizon} · "+("递推" if can_solve_exact(hero) else "抽样")
            output.append({"kind":kind,"pair":x["name"]+" → "+y["name"],"min_gap":worst,"floor":str(floor),"scope":scope})
    return output
