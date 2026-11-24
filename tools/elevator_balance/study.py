"""Rebuild the offline study. Run python study.py --workers 6 [--census private-snapshot.json]."""
import argparse
import copy
import csv
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor

os.environ["OPENBLAS_NUM_THREADS"]="1"
os.environ["OMP_NUM_THREADS"]="1"
import numpy as np

import economy
import model as m
import voyages

RESULTS=m.ROOT/"results"
FLOORS=[*range(1,41),50,60,75,100]
RUN_FLOORS=[*range(1,31),35,40]
POLICIES={
    "hard":{},"kit":{"auto_use":True,"coffee":2,"smoke":1},
    "evade":{"auto_use":True,"coffee":2,"smoke":1,"combat":"flee"},
    "elite_flee":{"auto_use":True,"coffee":2,"smoke":1,"combat":"elite_flee"},
    "arrival":{"auto_use":True,"coffee":2,"smoke":1,"exit":"arrival"},
    "wait":{"auto_use":True,"coffee":2,"smoke":1,"exit":"wait"},
    "first":{"insurance":"first"},"insured":{"insurance":"insured"},
    "box0":{"auto_use":True,"coffee":2,"smoke":1,"box":0},
    "box4":{"auto_use":True,"coffee":2,"smoke":1,"box":4},
}


def save(name,rows):
    target=RESULTS/(name+".csv")
    scratch=target.with_suffix(".tmp")
    columns=list(dict.fromkeys(k for row in rows for k in row))
    with scratch.open("w",newline="",encoding="utf-8-sig") as f:
        w=csv.DictWriter(f,fieldnames=columns)
        w.writeheader()
        w.writerows(rows)
    scratch.replace(target)


def configuration(plan):
    return m.scenario(m.calibrated_proposal() if plan=="B" else {})


def travel_job(args):
    plan,key,build,pol,n=args
    data=configuration(plan)
    hero=m.panel(data,m.PRESETS[build][1])
    rows=voyages.runs(data,hero,key,POLICIES[pol],RUN_FLOORS,n,m.DEFAULT_SEED)
    return [{"plan":plan,"map":key,"build":build,"policy":pol,**r} for r in rows]


def curves():
    curve,price,gear,sensitivity=[],[],[],[]
    for plan in ("A","B"):
        data=configuration(plan)
        for b,(_,spec) in m.PRESETS.items():
            hero=m.panel(data,spec)
            for key in m.MAP_ORDER:
                for f in FLOORS:
                    for elite in (False,True):
                        fight=m.map_battle(hero,data["maps"][key],f,elite)
                        curve.append({"plan":plan,"build":b,"map":key,"floor":f,"class":"elite" if elite else "normal",**fight})
        for key,b in m.MAP_BENCHMARK.items():
            hero=m.panel(data,m.PRESETS[b][1])
            for f in FLOORS:
                chest=economy.loot(data,hero,key,f)
                normal=economy.loot(data,hero,key,f,chest=False)
                e1=economy.loot(data,hero,key,f,chest=False,boost=1)
                e2=economy.loot(data,hero,key,f,chest=False,boost=2)
                price.append({"plan":plan,"map":key,"floor":f,"build":b,**chest,
                              "normal_kill_sale":normal["sale"],"elite_kill_sale":e1["sale"]+e2["sale"]})
            f=7
            base=m.map_battle(hero,data["maps"][key],f)
            score=m.power(hero)["index"]
            for stat,step in (("hp",10),("atk",1),("def",1),("agi",1),("luk",1)):
                alt=copy.deepcopy(hero)
                alt["stats"][stat]+=step
                x=m.map_battle(alt,data["maps"][key],f)
                sensitivity.append({"plan":plan,"map":key,"build":b,"floor":f,"stat":stat,"increment":step,
                                    "win_delta":x["win"]-base["win"],"hp_loss_delta":x["hp_loss"]-base["hp_loss"],
                                    "strength_delta":m.power(alt)["index"]-score})
        for item,definition in data["equipment"].items():
            spec=m.preset(10,"blue")
            for piece in spec["gear"]:
                if data["equipment"][piece["id"]]["slot"]==definition["slot"]: piece["id"]=item
            h=m.panel(data,spec)
            gear.append({"plan":plan,"item":item,"name":definition["name"],"slot":definition["slot"],
                         **{s:definition["stats"].get(s,0) for s in m.STATS},
                         "strength":m.power(h)["index"],
                         "abyss_f8_win":m.map_battle(h,data["maps"]["abyss"],8)["win"],
                         "abyss_f8_chest_sale":economy.loot(data,h,"abyss",8)["sale"]})
    save("单场曲线",curve)
    save("掉落期望",price)
    save("属性边际",sensitivity)
    save("装备测算",gear)


def population(snapshot):
    """Only aggregate results leave this process. The optional input carries no identifiers."""
    c=json.loads(snapshot.read_text(encoding="utf-8"))["census"]
    rows=[]
    for plan in ("A","B"):
        data=configuration(plan)
        panels=[]
        for count,lv,gear,sets in c["panels"]:
            spec=m.preset(lv)
            spec["sets"]=sets
            for _,_,stats,perks in gear:
                spec["perks"].extend(perks)
                for stat,value in stats.items(): spec["bonus"][stat]+=value
            panels.append((count,m.panel(data,spec)))
        total=sum(n for n,_ in panels)
        for key in m.MAP_ORDER:
            for f in (1,3,5,8,10,15,20,25,30):
                for elite in (False,True):
                    results=[(n,m.map_battle(h,data["maps"][key],f,elite,4000,m.seeded(m.DEFAULT_SEED,key,f,elite,i)))
                             for i,(n,h) in enumerate(panels)]
                    rows.append({"plan":plan,"map":key,"floor":f,"class":"elite" if elite else "normal","players":total,
                                 **{k:sum(n*r[k] for n,r in results)/total for k in ("win","draw","loss","rounds","hp_loss")},
                                 "mc_se":sum((n*r["se"]/total)**2 for n,r in results)**.5})
    save("玩家面板等权汇总",rows)


def random_gear():
    from random import Random
    rows=[]
    for plan in ("A","B"):
        data=configuration(plan);cat=m.make_catalog(data)
        for grade,level,key in (("green",3,"subway"),("blue",10,"abyss"),("purple",18,"auction"),("gold",25,"auction")):
            outcomes={(f,e):[] for f in (5,8,12) for e in (False,True)}
            for i in range(160):
                rng=Random(m.seeded(m.DEFAULT_SEED,"loadout",grade,i))
                by_slot={}
                while len(by_slot)<3:
                    item=cat.roll_gear(rng,grade)
                    by_slot.setdefault(cat.equipment[item["id"]]["slot"],item)
                spec=m.preset(level)
                for it in by_slot.values():
                    spec["perks"].extend(it["affixes"])
                    for attr,v in it["stats"].items():spec["bonus"][attr]+=v
                hero=m.panel(data,spec)
                for f,elite in outcomes:
                    outcomes[f,elite].append(m.map_battle(hero,data["maps"][key],f,elite,300,
                                                    m.seeded(m.DEFAULT_SEED,"gear_fight",grade,i,f,elite))["win"])
            for (f,elite),values in outcomes.items():
                rows.append({"plan":plan,"grade":grade,"map":key,"floor":f,"class":"elite" if elite else "normal",
                             "loadouts":len(values),"mc_per_dynamic":300,"mean":float(np.mean(values)),
                             "se":float(np.std(values,ddof=1)/np.sqrt(len(values))),
                             **{label:float(np.quantile(values,q)) for label,q in (("p10",.1),("median",.5),("p90",.9))}})
    save("随机装备分布",rows)


def ablation(key_n):
    key,n=key_n
    full=m.calibrated_proposal()
    alternatives={"A":{},"仅装备":{"equipment":full["equipment"],"gear_factors":full["gear_factors"]},
        "仅怪物":{"maps":{key:{"difficulty":full["maps"][key]["difficulty"]}}},
        "仅经济":{"gear_sell":full["gear_sell"],"maps":{key:{k:v for k,v in full["maps"][key].items() if k!="difficulty"}}},
        "B":full}
    result=[]
    base=None
    fs=[3,5,8,12,20]
    for name,cfg in alternatives.items():
        data=m.scenario(cfg);h=m.panel(data,m.PRESETS[m.MAP_BENCHMARK[key]][1])
        rows,paths=voyages.runs(data,h,key,POLICIES["kit"],fs,n,m.DEFAULT_SEED,True)
        if base is None: base=paths.copy()
        for j,(f,r) in enumerate(zip(fs,rows)):
            delta=paths[:,j,3]-base[:,j,3]
            surv=(paths[:,j,0]*(paths[:,j,1]>=f))-(base[:,j,0]*(base[:,j,1]>=f))
            result.append({"map":key,"variant":name,"floor":f,"n":n,"target_exit":r["target_exit"],"net":r["net"],
                           "paired_net":float(delta.mean()),"paired_net_se":float(delta.std(ddof=1)/np.sqrt(n)),
                           "paired_exit":float(surv.mean()),"paired_exit_se":float(surv.std(ddof=1)/np.sqrt(n))})
    return result


def endurance(args):
    name,key=args
    if name=="B0":
        plan=copy.deepcopy(m.calibrated_proposal())
        data=m.scenario(plan)
        reference=m.panel(data,m.PRESETS[m.MAP_BENCHMARK[key]][1])
        plan["maps"][key]["difficulty"]["mixtures"]["elite"]=m.calibrate_mix(
            reference,data["maps"][key],True,*m.TARGETS[key][1],12)
        data=m.scenario(plan)
    else:
        data=configuration(name)
    h=m.panel(data,m.PRESETS["gold"][1])
    rows=voyages.runs(data,h,key,POLICIES["kit"],[20,40,60,100],1200,m.DEFAULT_SEED)
    return [{"plan":name,"map":key,"build":"gold","policy":"kit",**r} for r in rows]


def main():
    from pathlib import Path
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers",type=int,default=4)
    ap.add_argument("--n",type=int,default=6000)
    ap.add_argument("--census",type=Path)
    ap.add_argument("--ablation-only",action="store_true")
    ap.add_argument("--endurance-only",action="store_true")
    args=ap.parse_args()
    start=time.perf_counter()
    RESULTS.mkdir(exist_ok=True)
    long_jobs=[(p,k) for p in ("A","B0","B") for k in ("mall","auction")]
    if args.endurance_only:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            save("毕业装百层压力测试",[r for group in pool.map(endurance,long_jobs) for r in group])
        return
    if args.ablation_only:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            rows=[row for group in pool.map(ablation,[(k,args.n) for k in m.MAP_ORDER]) for row in group]
        save("拆分调参与配对差",rows)
        return
    jobs=[]
    for plan in ("A","B"):
        for key,build in m.MAP_BENCHMARK.items():
            jobs.extend((plan,key,build,p,args.n) for p in ("hard","kit","evade","elite_flee","arrival","wait"))
            if build!="blue": jobs.append((plan,key,"blue","kit",args.n))
        jobs.extend((plan,"mall","new",p,args.n) for p in ("first","insured"))
        jobs.extend((plan,"auction","purple",p,args.n) for p in ("box0","box4"))
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures=[pool.submit(travel_job,job) for job in jobs]
        extra=pool.submit(population,args.census) if args.census else None
        gear=pool.submit(random_gear)
        ablations=[pool.submit(ablation,(k,args.n)) for k in m.MAP_ORDER]
        long_tests=[pool.submit(endurance,j) for j in long_jobs]
        curves()
        output=[]
        for i,future in enumerate(futures,1):
            output.extend(future.result())
            if i%10==0: print("voyage scenarios",i,"/",len(jobs),flush=True)
        if extra: extra.result()
        gear.result()
        save("拆分调参与配对差",[row for fu in ablations for row in fu.result()])
        save("毕业装百层压力测试",[row for fu in long_tests for row in fu.result()])
    save("远征收益",output)
    (RESULTS/"建议方案.json").write_text(json.dumps(m.calibrated_proposal(),ensure_ascii=False,indent=2),encoding="utf-8")
    print("STUDY_OK",len(jobs),"scenarios",len(jobs)*args.n,"expeditions",round(time.perf_counter()-start,1),"seconds",flush=True)


if __name__=="__main__": main()
