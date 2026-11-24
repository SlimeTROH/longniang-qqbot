"""Season boundaries exercised against real catalogues, commands and isolated saves."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from random import Random
from unittest.mock import AsyncMock, patch

from elevator_web_game import WebGame, rules
from scripts import elevator_maintenance


class RedSeason(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.root = Path(area.name)
        self.db = self.root/"state/endless_elevator.sqlite3"
        self.engine = rules.ElevatorGame(self.db, rng=Random(38))
        self.web = WebGame(self.db, game=self.engine)
        self.cat = self.engine.catalog
        self.now = 1795520000
        self.uid, self.group = "101","202"
        self.engine.execute(self.uid,"试验员",self.group,"rename","__keep_lobby__",now=self.now)

    def state(self):
        return self.web.state(self.uid,self.group,"试验员",now=self.now)

    def command(self,name,params):
        return self.web.command(self.uid,self.group,name,params,self.state()["revision"],"试验员",now=self.now)

    def test_box_grades_prices_and_recycle_expectations(self):
        values=[]
        for key in self.cat.gear_boxes:
            box=self.cat.progression["gear_boxes"][key]
            self.assertAlmostEqual(sum(box["weights"]),100)
            self.assertEqual(len(box["weights"]),len(box["grades"]))
            ev=sum(self.cat.gear_sell[g]*p/100 for g,p in zip(box["grades"],box["weights"]))
            price=self.cat.shop[key]
            self.assertGreater(ev/price,.90)
            self.assertLess(ev/price,1)
            values.append((box["tier"],price,round(ev,1)))
        self.assertEqual(values,[("Ⅰ级",400,376),("Ⅱ级",1600,1510.2),("Ⅲ级",5500,5199)])
        beginner,advanced,premium=self.cat.progression["gear_boxes"].values()
        self.assertEqual(beginner["grades"][-1],"gold")
        self.assertEqual(dict(zip(advanced["grades"],advanced["weights"]))["red"],.2)
        self.assertEqual(dict(zip(premium["grades"],premium["weights"]))["red"],3)
        rows=self.state()["shop"]
        self.assertEqual({row["category"] for row in rows},{"装备箱","回复药剂","战术补给","通行与扩容"})
        self.assertEqual([r["rarity"] for r in rows if r["category"]=="装备箱"],["green","blue","gold"])
        menu=self.engine.execute(self.uid,"试验员",self.group,"shop",now=self.now)
        self.assertEqual({c.argument for c in menu.choices},{"装备箱","回复药剂","战术补给","通行与扩容"})

    def test_red_suit_stats_and_rollable_pools(self):
        self.assertEqual(len(self.cat.red_bases),32)
        self.assertEqual(len(self.cat.gear_sets),8)
        for code in self.cat.gear_sets:
            members=[k for k in self.cat.red_bases if self.cat.equipment[k]["gear_set"]==code]
            self.assertEqual({self.cat.equipment[k]["slot"] for k in members},set(rules.SLOTS))
            for count in (1,2,3,4):
                stats,perks=dict(self.cat.base),{}
                self.cat.apply_gear_sets(stats,perks,members[:count])
                expected_stats,expected_perks=dict(self.cat.base),{}
                for size,effect in self.cat.gear_sets[code]["bonuses"].items():
                    if int(size)<=count:
                        for k,v in effect.get("stats",{}).items(): expected_stats[k]+=v
                        for k,v in effect.get("perks",{}).items(): expected_perks[k]=expected_perks.get(k,0)+v
                self.assertEqual((stats,perks),(expected_stats,expected_perks))
        for grade in self.cat.gear_sell:
            for _ in range(70):
                rolled=self.cat.roll_gear(self.engine.rng,grade)
                self.assertEqual(rolled["id"] in self.cat.red_bases,grade=="red")
                if grade=="red":
                    self.assertEqual(len(set(rolled["affixes"])),3)
                    self.assertEqual(rolled["value"],24000)

    def test_red_purchase_wear_reroll_and_sell(self):
        with self.engine.store.transaction() as conn:
            conn.execute("UPDATE players SET gold=50000 WHERE user_id=?",(self.uid,))
        with patch.object(self.engine.rng,"choices",return_value=["red"]):
            result=self.command("buy",{"id":"gear_box_high","quantity":1})
        self.assertIsNone(result["code"])
        equipment=result["state"]["equipment"][0]
        self.assertEqual((equipment["rarity"],len(equipment["affixes"]),equipment["value"]),("red",3,24000))
        self.assertTrue(equipment["set_name"])
        self.assertIn("4件",equipment["description"])
        ident=equipment["equip_id"]
        self.assertIn("确认",self.engine.execute(self.uid,"试验员",self.group,"sell","红装",now=self.now).text)
        with self.engine.store.transaction() as db:
            t=rules.Turn(self.engine,db,self.uid,"试验员",self.group,self.now)
            t.deposit(self.cat.ordinary_item("reroll_stone",self.engine.rng))
        self.assertEqual(len(self.command("reroll",{"equip_id":ident})["state"]["equipment"][0]["affixes"]),3)
        self.assertTrue(self.command("wear",{"equip_id":ident})["state"]["equipment"][0]["equipped"])
        self.assertTrue(self.state()["equipment"])

    def test_boss_drop_is_single_additional_roll_and_excludes_simulation(self):
        for map_id, zone in self.cat.maps.items():
            chances=[rules.expedition.boss_red_gear_rate(n,zone["multiplier"]) for n in (1,2,3,6,20,60)]
            self.assertEqual(chances,sorted(chances))
            self.assertGreaterEqual(chances[0],.004)
            self.assertLessEqual(chances[-1],.03)
            rate=chances[0]
            for roll,sim,extra in ((rate-1e-8,False,True),(rate,False,False),(0,True,False)):
                run={"map":map_id,"floor":8,"perks":{},"sim":sim}
                rng=Random(19)
                with patch.object(rng,"random",side_effect=[.3,.3,roll]), patch.object(rng,"uniform",return_value=1.0):
                    loot=self.cat.boss_rewards(run,rng,1)
                self.assertEqual(len(loot),2+int(extra))
                if extra: self.assertIn(loot[-1]["id"],self.cat.red_bases)

    def test_equipping_four_red_set_pieces_applies_both_bonuses_to_actual_run(self):
        with self.engine.store.transaction() as db:
            turn=rules.Turn(self.engine,db,self.uid,"试验员",self.group,self.now)
            for suffix in ("weapon","armor","charm","core"):
                turn.deposit({"id":"red_breakwater_"+suffix,"rarity":"red","stats":{},"affixes":[],
                              "value":24000,"quality":"","rules_version":2})
            turn.autoequip("","")
            turn.persist()
        run=self.command("start",{"map_id":"mall"})["state"]["run"]
        self.assertEqual(run["stats"]["def"],self.cat.base["def"]+5)
        self.assertEqual(run["max_hp"],self.cat.base["hp"]+32)
        self.assertEqual(run["shield"],25)

    def test_both_new_maps_have_loot_in_every_ordinary_grade_and_exclusive_field_items(self):
        for key, required, fee in (("tide",12,4200),("zenith",20,6300)):
            self.now += 5
            self.assertEqual((self.cat.maps[key]["extracts"],self.cat.maps[key]["entry"]),(required,fee))
            mats=[i["rarity"] for i in self.cat.items.values() if i.get("map")==key and i["kind"]=="material"]
            self.assertEqual(set(mats),{"white","green","blue","purple"})
            self.assertEqual(sum(i["map"]==key for i in self.cat.field_items.values()),10)
            self.assertEqual(self.command("start",{"map_id":key})["code"],"invalid")
            sim=self.command("simulate",{"map_id":key})
            self.assertIsNone(sim["code"])
            self.assertEqual(sim["state"]["run"]["map"],key)
            with self.engine.store.transaction() as db:
                t=rules.Turn(self.engine,db,self.uid,"试验员",self.group,self.now)
                t.finish_run(True);t.persist()

    def test_medicine_and_paused_boss_rounds(self):
        self.command("start",{"map_id":"mall"})
        with self.engine.store.transaction() as db:
            t=rules.Turn(self.engine,db,self.uid,"试验员",self.group,self.now)
            t.run.update(hp=10,shield=0,field_items={},event={"kind":"ready"},bag=[
                self.cat.ordinary_item(k,self.engine.rng) for k in
                ("critical_serum","barrier_serum","slow_serum","field_surgery")])
            t.run.pop("field_offer",None)
            t.persist()
        run=self.command("use",{"index":1})["state"]["run"]
        self.assertEqual(run["hp"],min(run["max_hp"],10+round(.9*run["max_hp"])))
        run=self.command("use",{"index":1})["state"]["run"]
        self.assertEqual(run["shield"],round(.25*run["max_hp"]))
        run=self.command("use",{"index":1})["state"]["run"]
        self.assertEqual(run["recovery"]["left"],3)
        self.assertEqual(self.command("use",{"index":1})["code"],"invalid")
        with self.engine.store.transaction() as db:
            t=rules.Turn(self.engine,db,self.uid,"试验员",self.group,self.now)
            t.run.update(hp=10,floor=2,peek="loot");t._enter_floor()
            self.assertEqual(t.run["med_regen"]["left"],2)
            self.assertEqual(t.run["hp"],10+round(.16*t.run["stats"]["hp"]))
            t.run.update(hp=100,shield=1000,floor=8,field_offer=None)
            enemy=rules.combat.monster(self.cat.maps["mall"],8,self.engine.rng,boss=True)
            enemy["hp"]=enemy["max_hp"]=20000;t.run["event"]={"kind":"combat","enemy":enemy}
            t.persist()
        first=self.command("fight",{})
        self.assertEqual(first["result"]["battle"]["rounds"],4)
        self.assertEqual(self.command("use",{"index":1})["state"]["run"]["hp"],run["max_hp"])
        self.assertEqual(self.command("fight",{})["result"]["battle"]["rounds"],4)
        with self.engine.store.read() as db:
            payload = json.loads(db.execute("SELECT payload FROM active_runs WHERE user_id=?", (self.uid,)).fetchone()[0])
            self.assertEqual(payload["event"]["enemy"]["rounds_fought"], 8)

    def test_maintenance_blocks_both_entry_points_and_resumes_same_run(self):
        self.command("start",{"map_id":"mall"})
        with self.engine.store.read() as db:
            original=tuple(db.execute("SELECT updated_at,payload FROM active_runs").fetchone())
        with patch.object(elevator_maintenance,"ROOT",self.root), patch.object(elevator_maintenance,"notify",new_callable=AsyncMock) as notify, \
             contextlib.redirect_stdout(io.StringIO()):
            with patch("sys.argv",["maintenance","pause"]), patch.object(elevator_maintenance.time,"time",return_value=self.now):
                elevator_maintenance.main()
            notify.assert_awaited_once()
            with self.assertRaises(rules.GameMaintenance): self.state()
            with self.assertRaises(rules.GameMaintenance): self.engine.execute(self.uid,"试验员",self.group,"down",now=self.now)
            with self.assertRaises(rules.GameMaintenance): self.engine.sweep(self.now+4000)
            with patch("sys.argv",["maintenance","resume"]), patch.object(elevator_maintenance.time,"time",return_value=self.now+4000):
                elevator_maintenance.main();elevator_maintenance.main()
        with self.engine.store.read() as db:
            after=tuple(db.execute("SELECT updated_at,payload FROM active_runs").fetchone())
        self.assertEqual(after,(original[0]+4000,original[1]))


if __name__=="__main__":
    unittest.main()
