"""Print the red gear season economy and paired boss trials from the active game catalogue."""
from __future__ import annotations

import json
from random import Random

from season_model import battle, build, expedition, game


# Picked bases are specified presets; attributes and all affixes are independently rolled.
CASES = [
    ("mall","green",8,["field_drive"]),
    ("subway","blue",18,["field_drive","field_siphon"]),
    ("abyss","purple",18,["field_incubator","field_drive","field_siphon"]),
    ("auction","gold",30,["field_drive","field_ratchet"]),
    ("tide","gold",30,["field_tide_impeller","field_tide_cutter","field_tide_rhythm"]),
    ("zenith","gold",30,["field_zenith_lens","field_zenith_prism","field_zenith_chart"]),
    ("zenith","red",50,["field_zenith_lens","field_zenith_prism","field_zenith_chart"]),
]


def compare(region, grade, floor, fields, cohorts=32, fights=32, suit=None):
    spec={"preset":grade,"map":region}
    if suit:
        spec["gear"]=[f"red_{suit}_{slot}" for slot in ("weapon","armor","charm","core")]
    cat, _, hero = build(spec)
    panel = object.__new__(game.Turn)
    panel.catalog, panel.archives, panel.completed_sets = cat, hero["archives"], hero["sets"]
    panel.player = {"extracts":hero["extracts"]}
    factor = cat.progression["grade_factors"][grade]
    totals = [0.,0.,0.]
    for batch in range(cohorts):
        rng=Random(261124+batch)
        gear=[]
        for row in hero["gear"]:
            key=row["base_id"]
            stats={k:round(n*(factor if n>0 else 1)*rng.uniform(.9,1.1)) for k,n in cat.equipment[key]["stats"].items()}
            gear.append({"base_id":key,"stats":json.dumps(stats),"affixes":json.dumps(cat.roll_affixes(grade,rng))})
        stats,perks=panel._gear_panel(gear)
        fighter={"stats":stats,"perks":perks}
        seed=1124000+batch*fights
        bare=battle(cat,region,fighter,floor,"boss",[],fights,seed)
        planned=battle(cat,region,fighter,floor,"boss",fields,fights,seed,
                       medicines=["barrier_serum","critical_serum"])
        totals[0]+=bare["win"]
        totals[1]+=planned["win"]
        totals[2]+=planned["meds_used"]
    return [n/cohorts for n in totals]


def report():
    cat=game.Catalog()
    print("# 无尽电梯 · 红装赛季数值验算\n")
    print("## 装备箱\n")
    print("每箱恰好开出一件。期望为各品质概率乘以该品质固定回收价之和。\n")
    print("|箱子|售价（金）|绿|蓝|紫|金|红|期望回收（金）|回收率|")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for key, box in cat.progression["gear_boxes"].items():
        price=cat.shop[key]
        odds=dict(zip(box["grades"],box["weights"]))
        ev=sum(cat.gear_sell[g]*p/100 for g,p in odds.items())
        perc=[f"{odds.get(g,0):g}%" for g in ("green","blue","purple","gold","red")]
        print("|"+"|".join([box["tier"]+" "+box["name"],f"{price:,}",*perc,f"{ev:,.1f}",f"{ev/price:.2%}"])+"|")
    print("\n回收价依次为绿 160、蓝 400、紫 1,500、金 5,700、红 24,000 金。\n")
    print("## 红装与套装\n")
    print("红装正属性倍率为 4.25，生成时各属性独立浮动 ±10%，带三个不同词条。两件和四件效果可以同时生效。\n")
    print("|套装|武器、护甲、挂件、核心|两件效果|四件追加效果|\n|---|---|---|---|")
    for code,suit in cat.gear_sets.items():
        members=[v["name"] for v in cat.equipment.values() if v.get("gear_set")==code]
        _,two,four=cat.gear_set_description(code).splitlines()
        print(f"|{suit['name']}|{'、'.join(members)}|{two.partition(' · ')[2]}|{four.partition(' · ')[2]}|")
    print("\n## 新区域与药剂\n")
    print("|区域|解锁撤离数|入场费|怪物强度倍率|专属临装|\n|---|---:|---:|---:|---:|")
    for key in ("tide","zenith"):
        zone=cat.maps[key]
        print(f"|{zone['name']}|{zone['extracts']}|{zone['entry']:,}|{zone['multiplier']:.2f}|"
              f"{sum(i['map']==key for i in cat.field_items.values())}|")
    print("\n|药剂|单价（金）|效果|\n|---|---:|---|")
    for key,item in cat.items.items():
        if item.get("medicine"):
            print(f"|{item['name']}|{cat.shop[key]:,}|{item['description']}|")
    print("## 首领掉红装\n")
    print("正式首领每次胜利额外独立判定一次。红装掉入局内背包，需要成功带出。\n")
    print("|地图|F8|F18|F30|F50|F90|\n|---|---:|---:|---:|---:|---:|")
    for m in cat.maps.values():
        odds=[f"{expedition.boss_red_gear_rate(expedition.boss_number(f),m['multiplier']):.2%}" for f in (8,18,30,50,90)]
        print("|"+"|".join([m["name"],*odds])+"|")
    print("\n## 同装备的首领胜率\n")
    print("每行比较 32 套随机属性与随机词条，每套打 32 场，共 1,024 场/策略。两组满血入场，使用相同装备与种子。"
          "绿、蓝、紫、金组使用撬棍、反光背心、续命腰包和双路稳频芯，分别沿用计算器对应的资深与图鉴进度。"
          "红装分别测试日冕破阵和玄堤守卫四件套。运营组已取得表中临装，交锋间生命不超过 72% 时依次使用至多一支屏障灌注剂和一支临界复苏针。"
          "不使用支援、裁决、手雷或虹彩四件共鸣。胜率以到达该首领并满足上述条件为前提。\n")
    print("|地图与首领|装备|已使用的局内道具|直接硬打|运营后|平均耗药|\n|---|---|---|---:|---:|---:|")
    names={"green":"绿","blue":"蓝","purple":"紫","gold":"金","red":"红"}
    for zone,grade,floor,fields in CASES:
        bare,planned,used=compare(zone,grade,floor,fields)
        label="日冕破阵（红）" if grade=="red" else names[grade]
        print(f"|{cat.maps[zone]['name']} F{floor}|{label}|{'、'.join(cat.field_items[k]['name'] for k in fields)}|"
              f"{bare:.1%}|{planned:.1%}|{used:.2f}|")
    defensive=["field_zenith_cradle","field_zenith_lens","field_zenith_guard","field_zenith_chart"]
    bare,planned,used=compare("zenith","red",50,defensive,suit="breakwater")
    print(f"|失重天文台 F50|玄堤守卫（红）|{'、'.join(cat.field_items[k]['name'] for k in defensive)}|"
          f"{bare:.1%}|{planned:.1%}|{used:.2f}|")


if __name__=="__main__":
    report()
