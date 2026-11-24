"""Counterfactual extraction policies executed by the production Turn rules in RAM."""
from __future__ import annotations

import copy
import math
import sqlite3
from contextlib import closing
from random import Random
from types import SimpleNamespace

import numpy as np

from model import DEFAULT_SEED, finite, game, make_catalog, seeded
from rules.storage import SCHEMA

DEFAULT_POLICY = {"combat":"fight","exit":"clear","event":"safe","coffee":0,"smoke":0,
                  "auto_use":False,"box":1,"insurance":"mature","blessing":False,"ticket_value":0}
STUDY_FLOORS = [1,2,3,4,5,6,8,10,12,15,20,25,30,40]
WALLET = 1_000_000


def policy(values=None):
    v={**DEFAULT_POLICY,**(values or {})}
    for k,options in {"combat":("fight","flee","elite_flee"),"exit":("clear","arrival","wait"),
                      "event":("safe","skip"),"insurance":("first","insured","mature")}.items():
        if v[k] not in options: raise ValueError(f"未知策略 {k}")
    for key,limit in (("coffee",8),("smoke",4),("box",4),("ticket_value",10000)):
        v[key]=finite(v[key],key,0,limit,True)
    return v


class Trial(game.Turn):
    def _stats(self):
        return dict(self.hero["stats"]),dict(self.hero["perks"])

    def _take_loadout(self,_supplies):
        return [self.catalog.ordinary_item(key,self.rng) for key in ("coffee","smoke")
                for _ in range(self.policy[key])]

    def _advance_quest(self,*args):
        # Daily, finite rewards are outside the repeatable value of a single ticket.
        pass

    def _eligible_relics(self,floor):
        return []

    def reply(self,text,*args,**kwargs):
        return game.Reply(text)

    def _record_battle_log(self,enemy,report):
        self.fights+=1
        self.rounds+=report.rounds
        return ""

    def deposit(self,item,**_kwargs):
        self.appraisal+=item["value"]
        self.sale+=self.catalog.equip_sell[item["rarity"]] if item["id"] in self.catalog.equipment else item["value"]

    def finish_run(self,extracted,reason=""):
        self.terminal=(bool(extracted),self.run["floor"])
        return super().finish_run(extracted,reason)

    def protect(self):
        r=self.run
        equipped={"coffee","smoke"} if self.policy["auto_use"] else set()
        items=r["safe"]+r["bag"]
        eligible=[i for i,it in enumerate(items) if it["id"] not in equipped and self._can_protect(it)]
        ordered=sorted(eligible,key=lambda i:items[i]["value"],reverse=True)
        protected=set(ordered[:self.policy["box"]])
        r["safe"]=[it for i,it in enumerate(items) if i in protected]
        r["bag"]=[it for i,it in enumerate(items) if i not in protected]

    def has_item(self,key):
        return any(item["id"]==key for item in self.run["bag"])

    def use_one(self,key):
        index=next(i for i,item in enumerate(self.run["bag"],1) if item["id"]==key)
        self.use(str(index),"")

    def heal(self):
        if self.policy["auto_use"]:
            while self.run["hp"] <= .6*self.run["stats"]["hp"] and self.has_item("coffee"):
                self.use_one("coffee")

    def handle_floor(self):
        kind=self.run["event"]["kind"]
        if kind=="combat":
            self.heal()
            elite=self.run["event"]["enemy"]["elite"]
            if self.policy["combat"]=="flee" or (self.policy["combat"]=="elite_flee" and elite):
                self.flee("","")
            else:
                self.fight("","")
        elif kind=="choice":
            options=self.catalog.events[self.run["event"]["id"]]["choices"]
            valid=[(i,o) for i,o in enumerate(options,1) if not any(k in o for k in (
                "guess","chance","agi_check","cost","hp_current_cost","buy_offer","skip_floors"))]
            if valid and self.policy["event"]=="safe":
                missing=1-self.run["hp"]/self.run["stats"]["hp"]
                index,_=max(valid,key=lambda pair: (
                    min(missing,pair[1].get("heal",0))*5 + pair[1].get("loot",0)
                    +len(pair[1].get("items",()))+.5*bool(pair[1].get("random_supply"))
                    +pair[1].get("grant_shield",0)/30+pair[1].get("atk_boost",0)*3
                    +pair[1].get("rare_boost",0)+.1*bool(pair[1].get("buff_loot"))))
                self.event_choice(str(index),"")
            else:
                self.leave("","")
        if self.run: self.protect()

    def branch_exit(self,seed):
        fork=copy.copy(self)
        fork.run=copy.deepcopy(self.run)
        fork.player=dict(self.player)
        fork.rng=Random(seed)
        fork.db.execute("SAVEPOINT plan_exit")
        try:
            fork.protect()
            if fork.policy["exit"]=="wait":
                fork.finish_run(True,"等待应急拉回")
            else:
                fork.extract("","")
                while fork.run:
                    if fork.policy["auto_use"] and fork.has_item("smoke"):
                        fork.use_one("smoke")
                    else:
                        fork.heal()
                        fork.fight("","")
            return fork.observation()
        finally:
            fork.db.execute("ROLLBACK TO plan_exit")
            fork.db.execute("RELEASE plan_exit")

    def observation(self):
        ok, floor=self.terminal
        costs=sum(self.policy[key]*self.catalog.shop[key] for key in ("coffee","smoke"))
        wallet=self.player["gold"]-WALLET-costs-self.policy["ticket_value"]
        return ok,floor,self.appraisal,self.sale+wallet,wallet,self.fights


def wilson(success,n):
    z=1.95996398454
    p=success/n
    center=(p+z*z/(2*n))/(1+z*z/n)
    radius=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
    return max(0,center-radius),min(1,center+radius)


def summarize(paths,floors):
    summaries=[]
    previous=None
    for j,f in enumerate(floors):
        x=paths[:,j,:]
        ok=x[:,0].astype(bool)
        reached=x[:,1]>=f
        success=ok & reached
        net=x[:,3]
        n=len(x)
        lo,hi=wilson(int(success.sum()),n)
        se=float(net.std(ddof=1)/math.sqrt(n))
        change=net-previous if previous is not None else None
        summaries.append({"floor":f,"n":n,"reach":float(reached.mean()),"target_exit":float(success.mean()),
                          "ci_low":lo,"ci_high":hi,"any_exit":float(ok.mean()),"death":float(1-ok.mean()),
                          "gross":float(x[:,2].mean()),"net":float(net.mean()),"net_se":se,
                          "wallet":float(x[:,4].mean()),"fights":float(x[:,5].mean()),
                          "p10":float(np.quantile(net,.1)),"median":float(np.median(net)),
                          "p90":float(np.quantile(net,.9)),"negative":float((net<0).mean()),
                          "cvar10":float(np.sort(net)[:max(1,math.ceil(n*.1))].mean()),
                          "delta_net":float(change.mean()) if change is not None else None,
                          "delta_se":float(change.std(ddof=1)/math.sqrt(n)) if change is not None else None})
        previous=net
    return summaries


def runs(data,hero,map_key,policy_values=None,floors=STUDY_FLOORS,n=1000,seed=DEFAULT_SEED,return_paths=False):
    """Fork extraction decisions off independent, continuing trajectories.

    Columns in the optional path matrix are (extracted, terminal floor, appraisal,
    net liquidation, wallet net of supplies/ticket, fights). A branch uses a new
    RNG and a rolled-back SQLite savepoint so it cannot change the continuing run.
    """
    n=finite(n,"远征样本",2,100000,True)
    floors=sorted(set(finite(f,"目标层",1,100,True) for f in floors))
    if not floors: raise ValueError("至少选择一个目标楼层")
    rules=policy(policy_values)
    cat=make_catalog(data)
    positions={f:i for i,f in enumerate(floors)}
    paths=np.empty((n,len(floors),6),dtype=float)
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory=sqlite3.Row
        db.executescript(SCHEMA)
        if rules["blessing"]: db.execute("INSERT INTO group_buffs VALUES ('study',2200000000)")
        for trial in range(n):
            db.execute("SAVEPOINT independent_run")
            rng=Random(seeded(seed,"run",trial))
            shell=SimpleNamespace(catalog=cat,rng=rng)
            t=Trial(shell,db,"test","测算","study",1795430000)
            t.hero,t.policy=hero,rules
            t.fights=t.rounds=t.sale=t.appraisal=0
            t.terminal=None
            t.completed_sets=hero["sets"]
            t.player.update(gold=WALLET,tickets=12,name_confirmed=1,safebox=rules["box"],
                            extracts=max(hero["extracts"],cat.maps[map_key]["extracts"]),
                            runs={"first":0,"insured":1,"mature":3}[rules["insurance"]])
            t.start(map_key,"")
            t.run.update(hp=max(1,round(hero["stats"]["hp"]*hero["hp_fraction"])),
                         shield=t.run["shield"]+hero["shield"],atk_boost=hero["atk_boost"])
            done=0
            while t.run:
                f=t.run["floor"]
                t.protect()
                branch_seed=seeded(seed,"exit",trial,f)
                if f in positions and rules["exit"] in ("arrival","wait"):
                    paths[trial,done]=t.branch_exit(branch_seed)
                    done+=1
                if done==len(floors): break
                t.handle_floor()
                if t.run and f in positions and rules["exit"]=="clear":
                    paths[trial,done]=t.branch_exit(branch_seed)
                    done+=1
                if done==len(floors): break
                if t.run: t.down("","")
            if done<len(floors):
                paths[trial,done:,:]=t.observation()
            db.execute("ROLLBACK TO independent_run")
            db.execute("RELEASE independent_run")
    return (summarize(paths,floors),paths) if return_paths else summarize(paths,floors)
