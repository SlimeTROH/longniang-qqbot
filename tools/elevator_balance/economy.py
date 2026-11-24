"""Exact conditional value of a loot roll, including integer appraisal rounding."""
from functools import lru_cache

import numpy as np

from model import game
from rules.catalog import RARITIES


@lru_cache(maxsize=2048)
def _uniform_value(low,high,multiplier):
    return float(np.maximum(1,np.rint(np.arange(low,high+1)*multiplier)).mean())


def loot(data,hero,map_key,floor,*,chest=True,boost=0,minimum="white",blessing=False):
    m=data["maps"][map_key]
    stats,perks=hero["stats"],hero["perks"]
    bonus=sum(data["sets"][s].get("rare_bonus",0) for s in hero["sets"])
    value_bonus=sum(data["sets"][s].get("value_bonus",0) for s in hero["sets"])
    rare=min(.75,m["rare_rate"]*(1+(floor-1)*.12)*(1+max(0,stats["luk"])*.012)*(1+bonus)
             *(1+perks.get("treasure",0) if chest else 1)*(1.2 if blessing else 1))
    multiplier=m["value_multiplier"]*(1+perks.get("greed",0)+value_bonus)
    grades=dict.fromkeys(RARITIES,0.)
    for alternatives,probability in ((RARITIES[:4],1-rare),(RARITIES[4:],rare)):
        weight=sum(data["rarities"][g]["weight"] for g in alternatives)
        for g in alternatives:
            rank=max(RARITIES.index(minimum),min(5,RARITIES.index(g)+boost))
            grades[RARITIES[rank]]+=data["rarities"][g]["weight"]*probability/weight
    ev=sale=collection=equipment=red=gold=0.
    items=data["items"]
    def value(item,mult):
        low,high=(item["value"],item["value"]) if "value" in item else data["rarities"][item["rarity"]]["range"]
        return _uniform_value(low,high,mult)
    def item_branch(prob,specs,quality=False):
        nonlocal ev,sale,collection,red,gold
        if not prob: return
        share=prob/len(specs)
        for item in specs:
            if quality:
                qs=data["qualities"].values()
                price=sum(q["weight"]*value(item,multiplier*q["multiplier"]) for q in qs)/sum(q["weight"] for q in qs)
            else: price=value(item,multiplier)
            ev+=share*price
            sale+=share*price
            collection+=share*(item["kind"]=="collection")
            red+=share*(item["rarity"]=="red")
            gold+=share*(item["rarity"]=="gold")
    for grade,p in grades.items():
        if not p: continue
        if grade in ("gold","red"):
            special=.06 if chest and grade=="gold" else 0
            item_branch(p*special,[items["crate_gold"],items["vip_card"]])
            pool=[v for v in items.values() if v["kind"]=="collection" and v["rarity"]==grade
                  and v.get("map",map_key)==map_key and v.get("min_floor",1)<=floor]
            item_branch(p*(1-special),pool)
            continue
        gear=.16 if grade!="white" else 0.
        crate=.06 if grade in ("blue","purple") else 0.
        supplies=[v for k,v in items.items() if v["kind"]=="supply" and v["rarity"]==grade and k!="doll"]
        supply=(.35-gear-crate) if supplies else 0.
        doll=.005 if grade=="purple" else 0.
        if gear:
            equipment+=p*gear
            ev+=p*gear*_uniform_value(*data["rarities"][grade]["range"],multiplier)
            sale+=p*gear*data["gear_sell"][grade]
        if crate: item_branch(p*crate,[items["crate_"+grade]])
        item_branch(p*supply,supplies)
        item_branch(p*doll,[items["doll"]])
        pool=[v for v in items.values() if v["kind"]=="material" and v.get("map")==map_key and v["rarity"]==grade]
        item_branch(p*(1-gear-crate-supply-doll),pool,True)
    return {"gross":ev,"sale":sale,"collection":collection,"gear":equipment,"red":red,"gold":gold,"rare_roll":rare}


def box_values(data):
    result={}
    for key,(name,grades,weights) in game.GEAR_BOX_POOLS.items():
        expected=sum(p*data["gear_sell"][g] for p,g in zip(weights,grades))/sum(weights)
        cost=data["shop"][key]
        result[key]={"name":name,"cost":cost,"sale":expected,"net":expected-cost,"return_ratio":expected/cost}
    return result
