"""Verify analytic combat, money estimates and exported code against the pinned game."""
import hashlib
import importlib
import io
import json
import math
import os
import sys
import tempfile
import types
import zipfile
from pathlib import Path
from random import Random

os.environ["OPENBLAS_NUM_THREADS"]="1"
import numpy as np

import app
import economy
import model as m
import voyages


def run():
    results={}
    for name,sha in m.MANIFEST["source_sha256"].items():
        assert hashlib.sha256((m.ROOT/"rules"/name).read_bytes()).hexdigest()==sha,name
    a=m.scenario(); proposed=m.calibrated_proposal(); b=m.scenario(proposed)
    trials=0
    for k in m.MAP_ORDER:
        for f in range(1,101):
            for elite in (False,True):
                actual=m.combat.monster(a["maps"][k],f,Random(11),elite=elite)
                expected=m.raw_stats(a["maps"][k],f,elite)
                assert all(actual[stat]==val for stat,val in expected.items())
                trials+=1
    results["native_enemy_panels"]=trials

    methods=[]
    for k,build,f,elite,perks,hp in [
        ("mall","new",7,False,[],1),("mall","new",3,True,[],.8),
        ("subway","green",8,False,["last_stand","greed"],.85),
        ("abyss","blue",5,True,[],1),("auction","purple",9,False,[],1),
        ("auction","gold",12,True,["berserk","last_stand"],.75),
    ]:
        spec={**m.PRESETS[build][1],"perks":[*m.PRESETS[build][1]["perks"],*perks],"hp_fraction":hp}
        hero=m.panel(a,spec); enemy=m.raw_stats(a["maps"][k],f,elite)
        exact=m.exact_battle(hero,enemy,f)
        n=25000
        sampled=m.sampled_battle(hero,enemy,f,n,m.seeded(81221,k,build,elite))
        assert abs(sum(exact[j] for j in ("win","draw","loss"))-1)<1e-10
        errors={}
        for j in ("win","draw","loss"):
            err=abs(exact[j]-sampled[j])
            assert err<=5*math.sqrt(exact[j]*(1-exact[j])/n)+2/n,(j,exact,sampled)
            errors[j]=err
        assert abs(exact["rounds"]-sampled["rounds"])<=5*15/(2*math.sqrt(n))
        assert abs(exact["hp_loss"]-sampled["hp_loss"])<=5*hero["stats"]["hp"]/(2*math.sqrt(n))
        methods.append({"map":k,"build":build,"floor":f,"elite":elite,"n":n,"prob_abs_error":errors,
                        "rounds_abs_error":abs(exact["rounds"]-sampled["rounds"]),
                        "hp_abs_error":abs(exact["hp_loss"]-sampled["hp_loss"])})
    results["combat_native_checks"]=methods

    max_error=0.; order=[]
    for k,build in m.MAP_BENCHMARK.items():
        h=m.panel(b,m.PRESETS[build][1])
        for i,kind in enumerate(("normal","elite")):
            start,end=m.TARGETS[k][i]
            w=[]
            for f in (*range(1,101),200,1000):
                val=m.map_battle(h,b["maps"][k],f,bool(i))["win"]
                max_error=max(max_error,abs(val-(end+(start-end)*2**(-(f-1)/12))))
                w.append(val)
            gaps=np.array(w[:99])-np.array(w[1:100])
            assert gaps.min()>-1e-12 and np.diff(gaps).max()<1e-12
    assert max_error<1e-10
    for build,(_,spec) in m.PRESETS.items():
        hero=m.panel(b,spec)
        for elite in (False,True):
            for f in (1,1000):
                ps=[m.map_battle(hero,b["maps"][k],f,elite)["win"] for k in m.MAP_ORDER]
                for i in range(3):
                    if ps[i+1]>ps[i]+1e-9: order.append([build,elite,f,m.MAP_ORDER[i],m.MAP_ORDER[i+1],ps[i+1]-ps[i]])
    results["target_max_error"]=max_error
    results["cross_map_inversions"]=order
    assert not order,order
    for build,(_,spec) in m.PRESETS.items():
        check=m.compare_map_order(m.panel(b,spec),b,40,100,m.DEFAULT_SEED)
        assert all(row["min_gap"]>=-1e-9 and "解析" in row["scope"] for row in check),build
    # Increasing target win rate models a decreasing difficulty that approaches a lower bound.
    hero=m.panel(a,m.PRESETS["new"][1]);mix=m.calibrate_mix(hero,a["maps"]["mall"],False,.40,.90,6)
    md={**a["maps"]["mall"],"difficulty":{"mixtures":{"normal":mix}}}
    assert abs(m.map_battle(hero,md,7)["win"]-.65)<1e-10

    draws=[]
    for i,k in enumerate(m.MAP_ORDER):
        hero=m.panel(a,m.PRESETS[m.MAP_BENCHMARK[k]][1])
        hero["sets"]=["午夜拍卖行"];hero["perks"]["greed"]=.4
        f=8;boost=i%3;chest=i%2==0
        guess=economy.loot(a,hero,k,f,chest=chest,boost=boost,blessing=True)
        run={"floor":f,"map":k,"stats":hero["stats"],"perks":hero["perks"],"rare_bonus":0,"value_bonus":.10}
        cat=m.make_catalog(a);rng=Random(98221+i);arr=[]
        for _ in range(50000):
            item=cat.loot(run,rng,chest=chest,boost=boost,blessing=True)
            arr.append(m.game.EQUIP_SELL_PRICES[item["rarity"]] if item["id"] in cat.equipment else item["value"])
        se=float(np.std(arr,ddof=1)/math.sqrt(len(arr)))
        z=(float(np.mean(arr))-guess["sale"])/se
        assert abs(z)<5,(k,z)
        draws.append({"map":k,"n":len(arr),"z":z})
    results["loot_native_checks"]=draws
    r,p=voyages.runs(a,m.panel(a,m.PRESETS["new"][1]),"mall",{"insurance":"first"},[1,3,7],2000,66001,True)
    predicted=economy.loot(a,m.panel(a,m.PRESETS["new"][1]),"mall",1)["sale"]
    assert r[0]["target_exit"]==1 and abs(r[0]["net"]-predicted)<5*r[0]["net_se"]
    _,p2=voyages.runs(a,m.panel(a,m.PRESETS["new"][1]),"mall",{"insurance":"first"},[1,3,7],2000,66001,True)
    assert np.array_equal(p,p2)
    assert all(x["target_exit"]<=x["reach"] and abs(x["any_exit"]+x["death"]-1)<1e-12 for x in r)
    results["voyages_deterministic"]=True

    with tempfile.TemporaryDirectory() as folder:
        z=zipfile.ZipFile(io.BytesIO(app.export_patch(proposed)))
        z.extractall(folder)
        directory=Path(folder)/"plugins/endless_elevator"
        pkg=types.ModuleType("_numeric_export_verify");pkg.__path__=[str(directory)];sys.modules[pkg.__name__]=pkg
        ec=importlib.import_module(pkg.__name__+".combat")
        cat=importlib.import_module(pkg.__name__+".catalog").Catalog(directory/"content.json")
        import ast
        config_tree=ast.parse((directory/"game.py").read_bytes())
        sell=next(ast.literal_eval(node.value) for node in config_tree.body if isinstance(node,ast.Assign)
                  and any(isinstance(t,ast.Name) and t.id=="EQUIP_SELL_PRICES" for t in node.targets))
        assert sell==b["gear_sell"] and cat.shop==b["shop"]
        ours=m.make_catalog(b)
        for k in m.MAP_ORDER:
            for f in (1,5,9,30,100,300):
                for e in (False,True):
                    for seed in range(10):
                        assert ec.monster(cat.maps[k],f,Random(seed),elite=e)==m.monster(b["maps"][k],f,Random(seed),elite=e)
        for grade in m.GRADES:
            for seed in range(100):
                assert cat.roll_gear(Random(seed),grade)==ours.roll_gear(Random(seed),grade)
    results["export_equivalent"]=True
    assert all(v["net"]<0 for v in economy.box_values(b).values())
    assert economy.box_values(a)["gear_box"]["net"]==132
    assert economy.box_values(a)["gear_box_mid"]["net"]==203
    fit=app.calculate({"operation":"gearfit","config":{},"hero":m.PRESETS["green"][1],"map":"subway","floor":8,
                       "item":"crowbar","stat":"atk","target":.90,"elite":False,"samples":200})
    assert fit["win_after"]>=.90 and fit["win_previous"]<.90
    results["gear_fit"]=fit
    (m.ROOT/"results/验证.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
    print("VERIFIED",trials,"panels",max_error,"target error","map inversions",order,"gear fit",fit,flush=True)


if __name__=="__main__":run()
