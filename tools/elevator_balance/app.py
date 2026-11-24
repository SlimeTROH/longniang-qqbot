"""Local GUI and JSON entry points for the Endless Elevator balance model."""
from __future__ import annotations

import argparse
import ast
import copy
import io
import json
import os
import sys
import threading
import webbrowser
import zipfile
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

os.environ.setdefault("OPENBLAS_NUM_THREADS","1")
import numpy as np
import economy
import model as m
import voyages
import season_model
from tuning_rules import mix_weight


def bootstrap():
    return {"maps":{k:{a:b for a,b in v.items() if a not in ("monsters","elites")} for k,v in m.BASE["maps"].items()},
            "stats":m.BASE["stats"],"gear":m.BASE["equipment"],"perks":m.BASE["affixes"],"sets":m.BASE["sets"],
            "grades":m.GRADES,"grade_labels":m.GRADE_LABELS,"sell":m.game.EQUIP_SELL_PRICES,
            "boxes":economy.box_values(m.scenario()),"presets":m.PRESETS,"tuning":m.DEFAULT_TUNING,
            "targets":m.TARGETS,"benchmark":m.MAP_BENCHMARK,"proposal":m.calibrated_proposal(),
            "soft":m.recommended(),"weights":m.BASE["event_weights"],"policy":voyages.DEFAULT_POLICY,
            "version":m.MANIFEST["captured"],"seed":m.DEFAULT_SEED}


def enemy_view(map_data,f,elite):
    mix=map_data.get("difficulty",{}).get("mixtures",{}).get("elite" if elite else "normal")
    if mix: return {"weak":mix["weak"],"strong":mix["strong"],"hard_near":mix.get("hard_near"),
                    "tail":mix.get("tail"),"tail_probability":mix_weight(mix,f)*mix.get("tail_weight",0),
                    "hard_weight":mix.get("hard_weight"),"weight":mix_weight(mix,f)}
    return {"weak":m.raw_stats(map_data,f,elite),"weight":0}


def calculate(req):
    if req.get("operation") == "season":
        return season_model.calculate(req)
    key=req.get("map","mall")
    if key not in m.MAP_ORDER: raise ValueError("未知地图")
    candidate=m.scenario(req.get("config",{}))
    base=m.scenario()
    spec=req.get("hero", m.PRESETS[m.MAP_BENCHMARK[key]][1])
    sample=m.finite(req.get("samples",800),"样本数",50,12000,True)
    seed=m.finite(req.get("seed",m.DEFAULT_SEED),"随机种子",0,2**32-1,True)
    floor=m.finite(req.get("floor",8),"观察楼层",1,100,True)
    horizon=m.finite(req.get("horizon",25),"计算楼层",floor,100,True)
    operation=req.get("operation","battle")
    if operation=="order":
        return {"rows":m.compare_map_order(m.panel(candidate,spec),candidate,horizon,sample,seed)}
    if operation=="gearfit":
        item,stat=req["item"],req["stat"]
        if stat not in m.STATS or item not in [p["id"] for p in spec["gear"]]:
            raise ValueError("请选择参考玩家正在穿戴的底材")
        if not m.can_solve_exact(m.panel(candidate,spec)):
            raise ValueError("底材反推需移除反伤与孤注一掷；含这两种词条可用抽样比较")
        target=m.finite(req["target"],"目标胜率",.001,.9999)
        values=candidate["equipment"][item]["stats"]
        current=values.get(stat,0)
        def at(value):
            values[stat]=value
            h=m.panel(candidate,spec)
            return m.map_battle(h,candidate["maps"][key],floor,bool(req.get("elite")))["win"]
        origin=at(current)
        low,high=current,400
        if origin>=target: high=current
        elif at(high)<target: raise ValueError("单独提高这项底材至 400 仍达不到目标；可比较其他属性或调整敌方")
        else:
            while high-low>1:
                mid=(high+low)//2
                if at(mid)>=target: high=mid
                else: low=mid
        return {"item":item,"stat":stat,"original":current,"suggested":high,"win_before":origin,
                "win_after":at(high),"win_previous":at(high-1) if high>current else None}
    if operation=="calibrate":
        h=m.panel(candidate,spec)
        targets=req["targets"]
        out={}
        for k,elite in (("normal",False),("elite",True)):
            first,last=targets[k]
            rate=targets.get("tail_weight",0) if elite else 0
            tail=m.tail_enemy(m.panel(candidate,m.PRESETS["gold"][1]),.45+.05*m.MAP_ORDER.index(key)) if rate else None
            out[k]=m.calibrate_mix(h,candidate["maps"][key],elite,first,last,targets["half"],tail,rate)
        trial=copy.deepcopy(candidate["maps"][key])
        trial.setdefault("difficulty",{})["mixtures"]=out
        error=max(abs(m.map_battle(h,trial,f,e)["win"]-(out[k]["target_end"]+
                      (out[k]["target_first"]-out[k]["target_end"])*2**(-(f-1)/out[k]["half"])))
                  for k,e in (("normal",False),("elite",True)) for f in (1,2,5,8,20,40,100,1000))
        return {"mixtures":out,"max_error":error}
    if operation=="expedition":
        floors=range(1,horizon+1)
        results={}
        paths=[]
        risk=m.finite(req.get("risk",.70),"最低目标层撤离率",0,1)
        for label,data in (("A",base),("B",candidate)):
            rows,samples=voyages.runs(data,m.panel(data,spec),key,req.get("policy",{}),floors,sample,seed,True)
            valid=[row for row in rows if row["ci_low"]>=risk]
            results[label]={"rows":rows,"best":max(valid,key=lambda x:x["net"])["floor"] if valid else None}
            paths.append(samples[:,:,3])
        difference=paths[1]-paths[0]
        results["paired"]=[{"mean":float(v),"se":float(e)} for v,e in zip(difference.mean(0),difference.std(0,ddof=1)/np.sqrt(sample))]
        return results
    if operation!="battle": raise ValueError("未知测算类型")
    result={}
    for label,data in (("A",base),("B",candidate)):
        hero=m.panel(data,spec)
        md=data["maps"][key]
        curve=[{"floor":f,**{k:m.map_battle(hero,md,f,e,sample,m.seeded(seed,"fight",f,e))
                for k,e in (("normal",False),("elite",True))}} for f in range(1,horizon+1)]
        last=curve[floor-1]["normal"]
        strength=m.power(hero,min(sample,1600),seed)
        sens=[]
        for stat,step in (("hp",10),("atk",1),("def",1),("agi",1),("luk",1)):
            upgraded=copy.deepcopy(hero)
            upgraded["stats"][stat]+=step
            b=m.map_battle(upgraded,md,floor,False,sample,m.seeded(seed,"fight",floor,False))
            sens.append({"stat":stat,"delta":step,"win":b["win"]-last["win"],"loss_hp":b["hp_loss"]-last["hp_loss"]})
        result[label]={"hero":hero,"strength":strength,"curve":curve,"sensitivity":sens,
            "boxes":economy.box_values(data),
            "normal_enemy":enemy_view(md,floor,False),"elite_enemy":enemy_view(md,floor,True),
            "flee":m.combat.flee_rate(m.initial_state(hero,floor)),
            "flee_choice":m.flee_choice(hero,md,floor,False,sample,seed),
            "chest":economy.loot(data,hero,key,floor,blessing=bool(req.get("policy",{}).get("blessing")))}
    return result


def export_patch(config):
    """Build candidate files from the pinned baseline; this function never installs files."""
    data=m.scenario(config)
    source=m.ROOT/"rules"
    combat=(source/"combat.py").read_text(encoding="utf-8")
    node=next(n for n in ast.parse(combat).body if isinstance(n,ast.FunctionDef) and n.name=="monster")
    final=node.body[-1]
    assert isinstance(final,ast.Return) and isinstance(final.value,ast.Dict)
    lines=combat.splitlines(keepends=True)
    lines[final.lineno-1]=lines[final.lineno-1].replace("return {","enemy = {",1)
    lines.insert(final.end_lineno,"    return apply_monster_settings(map_data, floor, enemy, rng, elite=elite)\n")
    combat="".join(lines).replace("from random import Random","from random import Random\nfrom .balance_curve import apply_monster_settings",1)
    catalog=(source/"catalog.py").read_text(encoding="utf-8")
    marker='factor = {"green": 1.25, "blue": 1.85, "purple": 2.75, "gold": 4.0}[grade]'
    assert catalog.count(marker)==1
    catalog=catalog.replace(marker,"factor = self.gear_factors[grade]").replace(
        'self.base = data["stats"]','self.base = data["stats"]\n        self.gear_factors = data.get("gear_factors", '+repr(m.GRADES)+')',1)
    compile(combat,"combat.py","exec")
    compile(catalog,"catalog.py","exec")
    rules_game=(source/"game.py").read_text(encoding="utf-8")
    original_line=next(line for line in rules_game.splitlines() if line.startswith("EQUIP_SELL_PRICES = "))
    rules_game=rules_game.replace(original_line,"EQUIP_SELL_PRICES = "+repr(data["gear_sell"]),1)
    compile(rules_game,"game.py","exec")
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED) as z:
        prefix="plugins/endless_elevator/"
        z.writestr(prefix+"content.json",json.dumps(data,ensure_ascii=False,indent=2))
        z.writestr(prefix+"combat.py",combat)
        z.writestr(prefix+"catalog.py",catalog)
        if data["gear_sell"]!=m.game.EQUIP_SELL_PRICES:
            z.writestr(prefix+"game.py",rules_game)
        z.write(m.ROOT/"tuning_rules.py",prefix+"balance_curve.py")
        z.writestr("scenario.json",json.dumps(config,ensure_ascii=False,indent=2))
        z.writestr("基线校验.json",json.dumps(m.MANIFEST["source_sha256"],indent=2))
        z.writestr("应用说明.txt","这些候选文件基于 "+m.MANIFEST["captured"]+" 的源码。先对照基线校验值审阅差异。装备底材与品质倍率影响新掉落，库存里的旧装备保留已存属性。游戏和知识库中写死的说明文案需要随定案更新。")
    return out.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _fmt, *_args): pass

    def send_bytes(self,code,ctype,body):
        self.send_response(code)
        self.send_header("Content-Type",ctype)
        self.send_header("Content-Length",str(len(body)))
        self.send_header("Cache-Control","no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path=unquote(urlsplit(self.path).path)
        if path=="/api/season-defaults":
            self.send_bytes(200,"application/json; charset=utf-8",json.dumps(season_model.bootstrap(),ensure_ascii=False).encode())
            return
        if path=="/api/defaults":
            self.send_bytes(200,"application/json; charset=utf-8",json.dumps(bootstrap(),ensure_ascii=False).encode())
            return
        files={"/":("season.html","text/html; charset=utf-8"),"/season":("season.html","text/html; charset=utf-8"),
               "/legacy":("index.html","text/html; charset=utf-8"),"/season.js":("season.js","application/javascript; charset=utf-8"),
               "/season-report":("红装赛季数值验算.md","text/plain; charset=utf-8"),"/app.js":("app.js","application/javascript; charset=utf-8"),
               "/style.css":("style.css","text/css; charset=utf-8"),"/report":("难度分析.html","text/html; charset=utf-8")}
        if path in files:
            name,ctype=files[path]
            p=m.ROOT/name
            if p.is_file():
                self.send_bytes(200,ctype,p.read_bytes())
                return
        if path.startswith("/results/"):
            name=path[len("/results/"):]
            if Path(name).name==name and name in {p.name for p in (m.ROOT/"results").glob("*.csv")}:
                self.send_bytes(200,"text/csv; charset=utf-8",(m.ROOT/"results"/name).read_bytes())
                return
        self.send_error(404)

    def do_POST(self):
        if self.path not in ("/api/calculate","/api/export"):
            self.send_error(404)
            return
        # The local workbench only accepts JSON from its own page or a local CLI.
        if self.headers.get_content_type()!="application/json":
            self.send_error(415)
            return
        try:
            size=int(self.headers.get("Content-Length","0"))
            if not 0<size<=1_000_000: raise ValueError("方案文件大小无效")
            req=json.loads(self.rfile.read(size))
            if not isinstance(req,dict): raise ValueError("需要 JSON 对象")
            if self.path=="/api/export":
                self.send_bytes(200,"application/zip",export_patch(req.get("config",{})))
            else:
                data=calculate(req)
                self.send_bytes(200,"application/json; charset=utf-8",json.dumps(data,ensure_ascii=False,allow_nan=False).encode())
        except (ValueError,KeyError,TypeError,IndexError,AttributeError) as ex:
            self.send_bytes(400,"application/json; charset=utf-8",json.dumps({"error":str(ex)},ensure_ascii=False).encode())
        except (BrokenPipeError,ConnectionResetError):
            pass


def main():
    parser=argparse.ArgumentParser(description="无尽电梯数值工作台")
    parser.add_argument("--port",type=int,default=0)
    parser.add_argument("--no-browser",action="store_true")
    parser.add_argument("--calculate",type=Path,metavar="REQUEST.json")
    parser.add_argument("--export",type=Path,metavar="SCENARIO.json")
    parser.add_argument("--out",type=Path)
    args=parser.parse_args()
    if args.calculate:
        result=calculate(json.loads(args.calculate.read_text(encoding="utf-8-sig")))
        text=json.dumps(result,ensure_ascii=False,indent=2)
        if args.out: args.out.write_text(text,encoding="utf-8")
        else: print(text)
        return
    if args.export:
        if not args.out: parser.error("--export 需要 --out 文件.zip")
        source=json.loads(args.export.read_text(encoding="utf-8-sig"))
        args.out.write_bytes(export_patch(source.get("config",source)))
        return
    server=ThreadingHTTPServer(("127.0.0.1",args.port),Handler)
    url=f"http://127.0.0.1:{server.server_port}/"
    print("无尽电梯数值工作台",url,"\n按 Ctrl+C 关闭",flush=True)
    if not args.no_browser: threading.Timer(.3,partial(webbrowser.open,url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__=="__main__": main()
