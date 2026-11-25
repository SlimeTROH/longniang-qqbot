from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
from random import Random
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from elevator_web_game import WebGame, rules
from elevator_web_sync import SnapshotCache


class PrismAndRush(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory)/"game.sqlite3"
        self.engine = rules.ElevatorGame(self.path,rng=Random(46))
        self.web = WebGame(self.path,game=self.engine)
        self.cat = self.engine.catalog
        self.now = 1795590000
        self.uid, self.gid = "101","202"
        self.engine.execute(self.uid,"测试玩家",self.gid,"pursue","1 8",now=self.now)
        self.engine.execute(self.uid,"测试玩家",self.gid,"rename","__keep_lobby__",now=self.now)
        self.enterContext(patch.object(rules.expedition,"enter_field",return_value=""))

    def state(self):
        return self.web.state(self.uid,self.gid,"测试玩家",now=self.now)

    def command(self, action, params=None):
        self.now += 3
        result=self.web.command(self.uid,self.gid,action,params or {},self.state()["revision"],"测试玩家",now=self.now)
        self.assertIsNone(result["code"],result["error"])
        return result

    def test_each_prism_and_historical_stage_gates(self):
        base={"hp":100,"atk":40,"def":20,"agi":10,"luk":10}
        for key in self.cat.rainbow:
            stats,perks=base.copy(),{}
            rules.expedition.apply_archive(stats,perks,{key:7},self.cat)
            run={"stats":stats,"perks":perks,"hp":stats["hp"],"floor":1,"tea_until":0,"field_mods":{}}
            if key=="prism_sun":
                self.assertEqual(stats["atk"],52)
                self.assertAlmostEqual(perks["prism_pierce"],.08)
            if key=="prism_sky":
                self.assertEqual((stats["hp"],stats["def"]),(120,24))
                exp = rules.expedition
                self.assertEqual([exp.ring_shield_limit({**run, "floor": f}) for f in (1,2,3,4,8,120)],
                                 [40,40,41,41,43,67])
                run.update(floor=2, shield=40, last_stand=False)
                exp.refill_ring_shield(run)
                self.assertEqual((run["shield"], run["ring_shield"]), (80,40))
                rules.combat.hurt(run, 25)
                for _ in range(2):
                    target=rules.combat.monster(self.cat.maps["mall"],1,Random(1))
                    target.update(hp=1,agi=0)
                    with patch.object(self.engine.rng,"random",return_value=.8):
                        rules.combat.fight(run,target,self.engine.rng)
                    exp.refill_ring_shield(run)
                    self.assertEqual((run["shield"], run["ring_shield"]), (55,15))
                run["floor"]=3
                exp.refill_ring_shield(run)
                self.assertEqual((run["shield"], run["ring_shield"]), (81,41))
                rules.combat.hurt(run, 51)
                self.assertEqual((run["shield"], run["ring_shield"]), (30,0))
            if key=="prism_wind":
                self.assertEqual(perks["prism_combo"],.14)
                old=deepcopy(run);old["perks"].pop("prism_flee")
                self.assertAlmostEqual(rules.combat.flee_rate(run),min(1,rules.combat.flee_rate(old)+.2))
                run["stats"]["agi"]=100
                self.assertEqual(rules.combat.flee_rate(run),1)
                self.assertEqual(rules.combat.flee_rate({**run,"perks":{}}),.9)
            if key=="prism_tide":
                self.assertEqual((perks["prism_leech"],perks["prism_boss"]),(.14,.20))
        self.assertEqual([rules.expedition.atlas_required(s,48) for s in self.cat.progression["atlas"]],
                         [2,8,16,26,34,40,44,48])

    def test_radar_receipt_survives_refresh_then_expires(self):
        self.command("start",{"map_id":"mall"})
        with self.engine.store.transaction() as db:
            t=rules.Turn(self.engine,db,self.uid,"测试玩家",self.gid,self.now)
            t.run.update(peek="elite",bag=[self.cat.ordinary_item("radar",self.engine.rng)])
            t.persist()
        result=self.command("use",{"index":1})
        expected={"floor":2,"room":"精英遭遇"}
        self.assertEqual(result["result"]["radar_scan"],expected)
        self.assertIn("精英遭遇",result["result"]["summary"])
        self.assertEqual(self.state()["run"]["radar_scan"],expected)
        next_floor=self.command("down")["state"]["run"]
        self.assertTrue(next_floor["event"]["enemy"]["elite"])
        self.assertIsNone(next_floor["radar_scan"])
        for floor,vault,label in ((7,True,"首领房"),(8,True,"隐藏金库")):
            with self.engine.store.transaction() as db:
                t=rules.Turn(self.engine,db,self.uid,"测试玩家",self.gid,self.now)
                t.run.update(floor=floor,gold_room=vault,bag=[self.cat.ordinary_item("radar",self.engine.rng)])
                t.persist()
            self.assertEqual(self.command("use",{"index":1})["result"]["radar_scan"]["room"],label)

    def test_rush_preferences_and_mandatory_stops(self):
        self.command("start",{"map_id":"mall"})
        with patch.object(rules.Turn,"_event_roll",return_value="loot"), \
             patch.object(rules.Turn,"_loot",side_effect=lambda *a,**kw:[{"id":"g01","quality":"","value":4000}]):
            first=self.command("rush")["result"]["rush"]
            self.assertEqual((len(first["floors"]),first["reason"]),(1,"拾到金红物资"))
            self.command("rush_settings",{"floors":10,"skip_valuable":True})
            second=self.command("rush")
            self.assertEqual(second["state"]["run"]["floor"],8)
            self.assertEqual(second["result"]["rush"]["reason"],"遇到首领")
            self.assertEqual(second["state"]["run"]["event"]["enemy"]["rounds_fought"],0)
            with self.engine.store.transaction() as db:
                t=rules.Turn(self.engine,db,self.uid,"测试玩家",self.gid,self.now)
                t.run.update(floor=30,event={"kind":"ready"});t.persist()
            long=self.command("rush")["result"]["rush"]
            self.assertEqual(len(long["floors"]),10)
        qq=self.engine.execute(self.uid,"测试玩家",self.gid,"rush_settings","5 遇金红暂停",now=self.now)
        self.assertIn("5 层",qq.text)
        self.assertEqual(self.state()["player"]["rush_floors"],5)
        with patch.object(rules.Turn,"_event_roll",return_value="elite"):
            elite=self.command("rush")["state"]["run"]["event"]["enemy"]
            self.assertTrue(elite["elite"])
            self.assertEqual(elite["rounds_fought"],0)

    def test_rush_ignores_odd_events_and_auto_fights_elites(self):
        self.command("start",{"map_id":"mall"})
        self.command("rush_settings",{"event_mode":"ignore","auto_elite":True})
        event = next(row for row in self.cat.events.values() if row["name"] == "神秘自动贩卖机")
        original = rules.Turn._floor_encounter
        monster = rules.combat.monster

        def encounter(turn):
            if turn.run["floor"] == 2:
                turn.run["event"] = {"kind":"choice","id":event["id"]}
                return event["name"]
            return original(turn)

        def weak_enemy(*args, **kwargs):
            return {**monster(*args, **kwargs), "hp":1,"max_hp":1,"agi":0}

        gold = self.state()["player"]["gold"]
        with (
            patch.object(rules.Turn,"_floor_encounter",encounter),
            patch.object(rules.Turn,"_event_roll",side_effect=["elite","loot"]),
            patch.object(rules.combat,"monster",side_effect=weak_enemy),
            patch.object(self.engine.rng,"random",return_value=.9),
            patch.object(rules.Turn,"_loot",return_value=[{"id":"snack","quality":"","value":12}]),
        ):
            result = self.command("rush")
        self.assertEqual(result["state"]["run"]["floor"],4)
        self.assertEqual(result["state"]["player"]["gold"],gold)
        first, second, _ = result["result"]["rush"]["floors"]
        self.assertEqual(first["adventure"]["outcome"],"ignored")
        self.assertEqual(second["battle"]["outcome"],"victory")

    def test_rush_random_choice_cannot_overdraw_and_landing_still_stops_on_boss(self):
        self.command("start",{"map_id":"mall"})
        self.command("rush_settings",{"event_mode":"random","auto_elite":True})
        originals = rules.Turn._floor_encounter
        events = {
            2: {"id":"test_paid", "name":"购买测试", "choices":[
                {"label":"买下报价", "buy_offer":True,"cost":10},
                {"label":"歇一歇", "grant_shield":11},
            ]},
            7: {"id":"test_jump", "name":"直降测试", "choices":[{"label":"直降", "skip_floors":2}]},
        }

        def encounter(turn):
            if event := events.get(turn.run["floor"]):
                turn.run["event"] = {"kind":"choice","id":event["id"],"price":2000,
                                     "offer":{"id":"coffee","quality":"","value":300}}
                return event["name"]
            return originals(turn)

        with (
            patch.dict(self.cat.events, {event["id"]:event for event in events.values()}),
            patch.object(rules.Turn,"_floor_encounter",encounter),
            patch.object(self.engine.rng,"choice",side_effect=lambda options:options[0]),
        ):
            available = self.state()["player"]["gold"]
            first = self.command("rush",{"floors":1})
            self.assertEqual(first["state"]["player"]["gold"],available)
            self.assertEqual(first["result"]["rush"]["floors"][0]["adventure"]["option"],"歇一歇")
            self.assertEqual(first["state"]["run"]["shield"],11)
            with self.engine.store.transaction() as db:
                turn = rules.Turn(self.engine,db,self.uid,"测试玩家",self.gid,self.now)
                turn.run["floor"] = 6
                turn.persist()
            last = self.command("rush")
        self.assertEqual([row["floor"] for row in last["result"]["rush"]["floors"]],[7,8])
        self.assertEqual(last["result"]["rush"]["reason"],"遇到首领")
        self.assertEqual(last["state"]["run"]["event"]["enemy"]["rounds_fought"],0)

    def test_exclusives_and_stat_breakdown(self):
        self.assertEqual(len(self.cat.collections),48)
        for zone in ("tide","zenith"):
            limited={k:v for k,v in self.cat.collections.items() if v.get("map")==zone}
            self.assertEqual(sorted(v["rarity"] for v in limited.values()),["gold","gold","gold","red"])
            key,item=next((k,i) for k,i in limited.items() if i["rarity"]=="red")
            for place in ("mall",zone):
                for floor in (item["min_floor"]-1,item["min_floor"]):
                    run={"map":place,"floor":floor,"stats":{"luk":9},"rare_bonus":0,"perks":{}}
                    observed=[]
                    def choose(pool):
                        observed.extend(pool)
                        return pool[0]
                    with patch.object(self.engine.rng,"random",return_value=0), \
                         patch.object(self.engine.rng,"choices",return_value=["red"]), \
                         patch.object(self.engine.rng,"choice",side_effect=choose):
                        self.cat.loot(run,self.engine.rng,chest=False)
                    self.assertEqual(key in observed,place==zone and floor>=item["min_floor"])
        with self.engine.store.transaction() as db:
            t=rules.Turn(self.engine,db,self.uid,"测试玩家",self.gid,self.now)
            t.deposit(self.cat.roll_gear(self.engine.rng,"gold"))
            t.autoequip("","");t.persist()
            db.execute("INSERT INTO collections VALUES(?,?,1,?)",(self.uid,"prism_sun",self.now))
        state=self.command("start",{"map_id":"mall"})["state"]
        for k,history in state["player"]["stat_sources"].items():
            self.assertEqual(history[-1]["result"],state["run"]["effective_stats"][k])
        self.assertEqual(state["player"]["stat_sources"]["atk"][-1]["value"],1.3)
        self.assertIsNone(state["player"]["secondary"]["hit"])
        with self.engine.store.transaction() as db:
            t=rules.Turn(self.engine,db,self.uid,"测试玩家",self.gid,self.now)
            en=rules.combat.monster(self.cat.maps["mall"],2,self.engine.rng)
            t.run["event"]={"kind":"combat","enemy":en};t.persist()
        sec=self.state()["player"]["secondary"]
        self.assertAlmostEqual(sec["hit"],1-rules.combat.dodge_rate(en["agi"]))

    def test_legacy_active_run_upgrades_once_and_preserves_ratio(self):
        self.command("start",{"map_id":"mall"})
        with self.engine.store.transaction() as db:
            db.execute("INSERT INTO collections VALUES(?,?,1,?)",(self.uid,"prism_sky",self.now))
            row=json.loads(db.execute("SELECT payload FROM active_runs WHERE user_id=?",(self.uid,)).fetchone()[0])
            row.pop("archive_version",None);row["stats"]["hp"]=120;row["hp"]=60
            db.execute("UPDATE active_runs SET payload=? WHERE user_id=?",(json.dumps(row),self.uid))
        before=self.state()["run"]
        self.assertEqual(before["hp"],round(before["max_hp"]/2))
        self.assertEqual(self.state()["run"]["max_hp"],before["max_hp"])
        self.command("rush_settings",{"floors":5,"skip_valuable":False})
        self.assertEqual(self.state()["run"]["hp"],before["hp"])

    def test_real_run_refills_only_the_ring_and_old_total_is_repaired_once(self):
        with self.engine.store.transaction() as db:
            db.execute("INSERT INTO collections VALUES(?,?,1,?)",(self.uid,"prism_sky",self.now))
        started = self.command("start", {"map_id":"mall"})["state"]
        self.assertEqual(started["run"]["shield"],40)
        self.assertEqual(started["player"]["secondary"]["ring_shield_limit"],40)
        with self.engine.store.transaction() as db:
            turn = rules.Turn(self.engine,db,self.uid,"测试玩家",self.gid,self.now)
            turn.run.update(floor=2, event={"kind":"ready"}, shield=55, ring_shield=15, peek="loot")
            turn.persist()
        self.assertEqual(self.command("down")["state"]["run"]["shield"],81)

        legacy = {"floor":120, "hp":10, "shield":2803, "perks":{"prism_round_shield":40, "prism_shield_growth":1}}
        self.assertTrue(rules.expedition.repair_legacy_ring_shield(legacy))
        self.assertEqual((legacy["shield"],legacy["ring_shield"]), (67,67))
        rules.combat.hurt(legacy, 15)
        self.assertFalse(rules.expedition.repair_legacy_ring_shield(legacy))
        self.assertEqual(legacy["shield"],52)


class DeltaTests(unittest.TestCase):
    def test_stock_updates_removals_and_reordering_keep_exact_ids(self):
        cache=SnapshotCache()
        rows=[{"stock_id":i,"quantity":2} for i in range(221)]
        first=cache.pack("u",{"revision":"1","warehouse":rows})
        after=[{"stock_id":333,"quantity":1}, *rows[2:100], {"stock_id":1,"quantity":4}, *rows[100:]]
        delta=cache.pack("u",{"revision":"2","warehouse":after},first["sync_token"],"1")["delta"]
        patch=delta["lists"]["warehouse"]
        self.assertNotIn("warehouse",delta["fields"])
        self.assertEqual(len(patch["upsert"]),2)
        by_id={r["stock_id"]:r for r in rows}
        by_id.update((r["stock_id"],r) for r in patch["upsert"])
        self.assertEqual([by_id[i] for i in patch["order"]],after)

    def test_scoped_token_and_reconstruction(self):
        cache=SnapshotCache(3)
        owner=("1","2")
        old={"revision":"r1","player":{"gold":5,"hp":100},"run":None,"warehouse":[{"id":i} for i in range(221)]}
        packet=cache.pack(owner,old)
        new={**old,"revision":"r2","run":{"floor":1,"hp":99},"player":{"gold":5,"hp":99}}
        packed=cache.pack(owner,new,packet["sync_token"],"r1")
        diff=packed["delta"]
        self.assertNotIn("warehouse",diff["fields"])
        restored={**old,**diff["fields"]}
        for k,v in diff["sections"].items():restored[k]={**restored[k],**v}
        self.assertEqual(restored,new)
        self.assertIn("state",cache.pack(("other","2"),new,packet["sync_token"],"r1"))
        self.assertIn("state",cache.pack(owner,new,packet["sync_token"],"bad"))
        self.assertIn("state",cache.pack(owner,new,packet["sync_token"],"r1"))


if __name__=="__main__":
    unittest.main()
