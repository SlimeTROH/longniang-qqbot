"""Integration regressions for real game actions, using an isolated SQLite save."""
import copy
import importlib
import json
import sqlite3
import tempfile
import types
import sys
import unittest
from contextlib import closing
from pathlib import Path
from random import Random
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
pkg=types.ModuleType("boss_season_test")
pkg.__path__=[str(ROOT/"plugins/endless_elevator")]
sys.modules[pkg.__name__]=pkg
game=importlib.import_module(pkg.__name__+".game")
rules=game.expedition
combat=game.combat


class BossSeason(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path=Path(tmp.name)/"player.sqlite3"
        self.g=game.ElevatorGame(self.path,rng=Random(27))
        self.cat=self.g.catalog
        self.at=1795470000

    def do(self,action,arg=""):
        self.at+=12
        if action=="start" or (action=="rename" and arg=="__keep_start__"):
            if not self.g.has_run("1"):
                self.g.execute("1","玩家","2","pursue","1 8",now=self.at)
        return self.g.execute("1","玩家","2",action,arg,now=self.at)

    def turn(self,db):
        return game.Turn(self.g,db,"1","玩家","2",self.at)

    def decline_field_offer(self, run):
        if offer := run.get("field_offer"):
            rules.choose_field(run, offer["id"], offer["floor"], 0, self.cat.field_items)

    def test_boss_gate_and_all_paths_into_it(self):
        self.assertEqual([f for f in range(1,112) if rules.boss_number(f)],[8,18,30,50,70,90,110])
        self.do("rename","__keep_start__")
        with self.g.store.transaction() as db:
            t=self.turn(db)
            for origin,target in ((7,8),(17,18),(29,30),(49,50)):
                t.run.update(floor=origin,event={"kind":"ready"},gold_room=True,peek="loot")
                self.assertIn("首领",t.rush("5","").text)
                self.assertEqual(t.run["floor"],target)
                self.assertTrue(t.run["event"]["enemy"]["boss"])
                self.assertTrue(t.run["gold_room"])
                with self.assertRaises(game.GameError):t.down("","")
                with self.assertRaises(game.GameError):t.flee("","")
                self.decline_field_offer(t.run)
                t.run.update(floor=origin,event={"kind":"ready"},bag=[self.cat.ordinary_item("warp_card",t.rng)])
                t.use("warp_card","")
                self.assertEqual(t.run["floor"],target)
                self.assertTrue(t.run["event"]["enemy"]["boss"])
                self.decline_field_offer(t.run)
            skipping=next((ev,i) for ev in self.cat.events.values() for i,o in enumerate(ev["choices"],1) if o.get("skip_floors"))
            ev,i=skipping
            t.run.update(floor=7,event={"kind":"choice","id":ev["id"]})
            t.player["gold"]=100000
            with patch.object(t.rng,"random",return_value=0):
                t.event_choice(str(i),"")
            self.assertEqual(t.run["floor"],8)
            self.assertTrue(t.run["event"]["enemy"]["boss"])

    def test_once_per_real_boss_exact_probability_and_permanent_set(self):
        self.do("rename","__keep_start__")
        with self.g.store.transaction() as db:
            t=self.turn(db)
            self.decline_field_offer(t.run)
            keys = tuple(self.cat.rainbow)
            drops = iter([keys[0], keys[0], *keys[1:]])
            ordinary_choice = t.rng.choice
            def choose(pool):
                if tuple(pool) == keys:
                    return next(drops)
                return ordinary_choice(pool)
            for i,(f,roll) in enumerate(((8,.005),(18,.004999),(30,0),(50,0),(70,0),(90,0))):
                t.run.update(floor=f,event={"kind":"ready"},hp=1)
                t._enter_floor()
                self.decline_field_offer(t.run)
                en=t.run["event"]["enemy"]
                en["hp"]=0
                with patch.object(t.rng,"random",return_value=roll), patch.object(t.rng,"choice",side_effect=choose):
                    note = t._enemy_win(en)
                self.assertEqual(len(set(t.archives)&t.catalog.rainbow.keys()),(0,1,1,2,3,4)[i])
                self.assertEqual(sum(s["quantity"] for s in t._stock() if s["id"] in keys), i)
                if i:
                    self.assertTrue(any(item.get("banked") for item in t._floor_result()["loot"]))
                if i == 1:
                    first_panel = (t.run["stats"].copy(), t.run["perks"].copy())
                if i == 2:
                    self.assertIn("同款", note)
                    self.assertEqual((t.run["stats"], t.run["perks"]), first_panel)
                    for mode in ("品质", "价值", "珍藏"):
                        self.assertIn(self.cat.rainbow[keys[0]]["name"], t.warehouse(mode,"").text)
                self.assertEqual(t.run["hp"],t.run["stats"]["hp"])
                with self.assertRaises(game.GameError): t._enemy_win(en)
            self.assertAlmostEqual(t.run["perks"]["prism_combo"],.20)
            self.assertEqual(t.run["perks"]["prism_shield"],30)
            self.assertGreater(t.run["stats"]["atk"],25)
            self.assertFalse(any(it["id"] in self.cat.rainbow for it in t.run["safe"]+t.run["bag"]))
            t.persist()
            t.finish_run(False)
            t.persist()
        self.do("start","mall")
        with self.g.store.read() as db:
            run=json.loads(db.execute("SELECT payload FROM active_runs").fetchone()[0])
            self.assertEqual(run["perks"]["prism_shield"],30)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM collections WHERE item_id LIKE 'prism_%'").fetchone()[0],4)
            self.assertEqual(db.execute("SELECT quantity FROM inventory WHERE item_id=?", (keys[0],)).fetchone()[0], 2)

    def test_legacy_rainbow_stock_backfill_only_once(self):
        self.do("rename","__keep_lobby__")
        key = next(iter(self.cat.rainbow))
        with self.g.store.transaction() as db:
            uid = self.turn(db).uid
            db.execute("DELETE FROM game_migrations WHERE name='rainbow_stock_v1'")
            db.execute("INSERT INTO collections VALUES(?,?,2,?)", (uid,key,123456789))
            db.execute("INSERT INTO inventory(user_id,item_id,quality,value,quantity) VALUES(?,?,'',0,1)", (uid,key))
        for _ in range(2):
            reopened = game.ElevatorGame(self.g.store.path)
            with reopened.store.read() as db:
                self.assertEqual(tuple(db.execute("SELECT count,first_got_at FROM collections WHERE item_id=?", (key,)).fetchone()),
                                 (2,123456789))
                self.assertEqual(db.execute("SELECT SUM(quantity) FROM inventory WHERE item_id=?", (key,)).fetchone()[0], 2)

    def test_simulation_smoke_and_normal_kills_cannot_award_rainbow(self):
        self.do("rename","__keep_lobby__")
        self.do("simulate","auction")
        with self.g.store.transaction() as db:
            t=self.turn(db); t.run["floor"]=8;t._enter_floor()
            en=t.run["event"]["enemy"]; en["hp"]=0
            with patch.object(t.rng,"random",return_value=0):t._enemy_win(en)
            self.assertEqual(t.run["hp"],t.run["stats"]["hp"])
            self.assertFalse(set(t.archives)&t.catalog.rainbow.keys())
            t.finish_run(True);t.persist()
        self.do("start","mall")
        with self.g.store.transaction() as db:
            t=self.turn(db)
            self.decline_field_offer(t.run)
            t.run["floor"]=8;t._enter_floor()
            self.decline_field_offer(t.run)
            t.run["bag"].append(self.cat.ordinary_item("smoke",t.rng))
            t.use("smoke","");self.assertIsNone(t.run);t.persist()
            self.assertFalse(set(t.archives)&t.catalog.rainbow.keys())
            for key in self.cat.rainbow:
                with self.assertRaises(ValueError):self.cat.ordinary_item(key,t.rng)

    def test_four_slots_migrate_without_changing_ids_and_wear_one_weapon(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            schema=importlib.import_module(pkg.__name__+".storage").SCHEMA
            db.executescript(schema.replace(", rules_version INTEGER NOT NULL DEFAULT 1",""))
            db.execute("INSERT INTO players(user_id,nickname,loadout,name_confirmed) VALUES('1','玩家',?,1)",(json.dumps({"武器":11}),))
            db.execute("INSERT INTO equipment(equip_id,user_id,base_id,rarity,stats,affixes) VALUES(11,'1','shotgun','gold',?,?)",
                       ('{"atk":68,"agi":-4}','["pierce","vampire"]'))
        with self.g.store.transaction() as db:
            t=self.turn(db)
            r=db.execute("SELECT * FROM equipment WHERE equip_id=11").fetchone()
            self.assertEqual(json.loads(r["stats"]),{"atk":54,"agi":-5})
            self.assertEqual(json.loads(r["affixes"]),["pierce","vampire"])
            self.assertEqual(json.loads(t.player["loadout"]),{"武器":11})
            for key in ("vest","belt","dual_core","crowbar"):
                item={"id":key,"value":10,"quality":"","rarity":"blue","affixes":["heavy_strike"],
                      "stats":dict(self.cat.equipment[key]["stats"]),"rules_version":2}
                t.deposit(item)
            t.autoequip("","");t.persist()
            self.assertEqual(set(json.loads(t.player["loadout"])),set(game.SLOTS))
            t.wear("#15","")
            self.assertEqual(len(json.loads(t.player["loadout"])),4)
            self.assertEqual(json.loads(t.player["loadout"])["武器"],15)
        with game.ElevatorGame(self.path).store.read() as db:
            self.assertEqual(json.loads(db.execute("SELECT stats FROM equipment WHERE equip_id=11").fetchone()[0])["atk"],54)

    def test_field_pickup_weights_favor_healing_and_reduce_equipped_duplicates(self):
        run = {"map": "abyss", "floor": 1, "field_seed": "weight-test", "hp": 100, "stats": {"hp": 100},
               "field_slots": ["field_dialysis", "field_dialysis"]}
        with patch.object(rules, "Random") as random:
            rng = random.return_value
            rng.choices.return_value = ["field_wrap"]
            rng.randrange.return_value = 0
            rules.enter_field(run, self.cat.field_items)
            pool, weights = rng.choices.call_args.args
            actual = dict(zip(pool, weights))
            self.assertEqual(set(actual), {key for key, item in self.cat.field_items.items()
                                           if item["map"] in {"all", "abyss"} and not item.get("wish")})
            self.assertEqual(actual["field_wrap"], self.cat.field_items["field_wrap"]["weight"] * rules.FIELD_HEAL_WEIGHT)
            self.assertEqual(actual["field_incubator"],
                             self.cat.field_items["field_incubator"]["weight"] * rules.FIELD_HEAL_WEIGHT)
            self.assertAlmostEqual(actual["field_dialysis"], self.cat.field_items["field_dialysis"]["weight"]
                                   * rules.FIELD_HEAL_WEIGHT * rules.FIELD_EQUIPPED_WEIGHT)
            self.assertEqual(actual["field_harness"], self.cat.field_items["field_harness"]["weight"])
            rng.random.assert_not_called()
            self.assertEqual(run["field_items"], {"field_wrap": 1})
            rng.choices.reset_mock()
            run["floor"] = 2
            rng.random.return_value = rules.FIELD_CHANCE
            self.assertFalse(rules.enter_field(run, self.cat.field_items))
            rng.choices.assert_not_called()

    def test_field_rng_map_limits_and_disposal(self):
        self.assertEqual(len(self.cat.field_items), 62)
        self.do("rename","__keep_start__")
        with self.g.store.transaction() as db:
            t=self.turn(db)
            for map_key in self.cat.maps:
                run=copy.deepcopy(t.run)
                run.update(map=map_key,field_items={},field_seen=0)
                run.pop("field_offer", None)
                saved=t.rng.getstate()
                offered = set()
                for f in range(1,501):
                    run["floor"]=f
                    rules.enter_field(run,self.cat.field_items)
                    previous=dict(run["field_items"])
                    offer = run.get("field_offer")
                    self.assertFalse(rules.enter_field(run,self.cat.field_items))
                    self.assertEqual(previous,run["field_items"])
                    self.assertEqual(offer, run.get("field_offer"))
                    if offer:
                        key = offer["id"]
                        self.assertNotIn(key, run["field_items"])
                        self.assertNotIn(key, rules.field_slots(run))
                        offered.add(key)
                        rules.choose_field(run,key,f,0,self.cat.field_items)
                self.assertEqual(saved,t.rng.getstate())
                picked = offered | set(run["field_items"])
                self.assertEqual({self.cat.field_items[k]["map"] for k in picked},{"all",map_key})
                self.assertEqual(picked, {k for k,v in self.cat.field_items.items() if v["map"] in {"all",map_key}})
                left,right=Random(600),Random(600)
                base=copy.deepcopy(run);base["field_items"]={}
                self.assertEqual([self.cat.loot(base,left,chest=True) for _ in range(30)],
                                 [self.cat.loot(run,right,chest=True) for _ in range(30)])
            t.run["field_items"]={"field_wrap":9}
            t.finish_run(False);t.persist()
            for table in ("inventory","collections"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE item_id LIKE 'field_%'").fetchone()[0],0)
            self.assertNotIn("field_wrap","".join(r[0] for r in db.execute("SELECT items FROM relics")))

    def test_field_durations_and_combat_effects(self):
        self.do("rename","__keep_start__")
        with self.g.store.transaction() as db:
            t=self.turn(db)
            for key,item in self.cat.field_items.items():
                if item.get("judgement"):
                    continue
                run=copy.deepcopy(t.run);run.update(hp=50,floor=7,shield=0,field_items={key:1})
                rules.activate(run,key,self.cat.field_items)
                self.assertNotIn(key,run["field_items"])
                if item["mode"]=="timed":
                    end=7+item["floors"]-1
                    run["floor"]=end;rules.refresh(run,self.cat.field_items);self.assertIn(key,run["field_buffs"])
                    run["floor"]+=1;rules.refresh(run,self.cat.field_items);self.assertNotIn(key,run["field_buffs"])
            run=t.run
            run.update(hp=1, shield=0, blood_used=True, field_items={"field_wrap": 2},
                       burst={"id": "field_singularity", "left": 1, "mult": 6, "sure": True, "blood": True})
            t.persist()
            t = self.turn(db)
            run = t.run
            with self.assertRaises(game.GameError):t.use_field("field_wrap","")
            target={"hp":5000,"def":0,"agi":0,"atk":1}
            roll=Random(2)
            with patch.object(roll,"random",return_value=.99),patch.object(roll,"uniform",return_value=1):
                _,hit=combat._player_attack(run,target,roll,combat.FightResult(),1)
            self.assertEqual(hit,run["stats"]["atk"]*6);self.assertNotIn("burst",run)
            run.update(hp=40,field_mods={"leech":.4,"combo":.7},perks={},stats={"atk":45,"def":50,"hp":110,"agi":100,"luk":0})
            enemy={"hp":60,"max_hp":60,"atk":1,"def":0,"agi":0}
            with patch.object(roll,"random",return_value=.2),patch.object(roll,"uniform",return_value=1):
                result=combat.fight(run,enemy,roll)
            self.assertEqual(enemy["hp"],0);self.assertEqual(result.rounds,1)
            self.assertIn("追击",result.log[0]);self.assertEqual(run["hp"],51)

    def test_field_damage_scales_after_defense_and_crit_before_target_hp(self):
        roll = Random(2)
        for factor, peak, dealt in ((None, 8, 8), (1.5, 12, 9), (2.25, 18, 9)):
            with self.subTest(factor=factor):
                run = {"hp": 110, "stats": {"hp": 110, "atk": 6, "luk": 0}, "perks": {}}
                if factor is not None:
                    run["field_damage_multiplier"] = factor
                enemy = {"hp": 9, "def": 8, "agi": 0}
                report = combat.FightResult()
                with patch.object(roll, "random", side_effect=(.99, 0)), patch.object(roll, "uniform", return_value=1):
                    _, actual = combat._player_attack(run, enemy, roll, report, 1)
                self.assertEqual((actual, report.total_dealt, report.peak, report.crits), (dealt, dealt, peak, 1))
                self.assertEqual(enemy["hp"], 9 - dealt)
                self.assertEqual(combat.scale_dealt_damage(run, 0), 0)

    def test_field_damage_scales_only_incoming_strikes_after_guard_and_before_shield(self):
        template = {"hp": 100, "shield": 8, "perks": {}, "field_mods": {"mitigation": .2},
                    "guard": {"reduction": .5, "left": 1}}
        original = copy.deepcopy(template)
        changed = copy.deepcopy(template)
        changed["field_damage_multiplier"] = 1.5
        self.assertEqual(combat.hurt(original, 19, strike=True), 0)
        self.assertEqual(combat.hurt(changed, 19, strike=True), 2)
        self.assertEqual((changed["hp"], changed["shield"]), (98, 0))
        self.assertNotIn("guard", changed)
        for run in (template, {**template, "field_damage_multiplier": 1.5}):
            self.assertEqual(combat.hurt(run, 19), 7)
            self.assertEqual(run["guard"]["left"], 1)

    def test_field_damage_reports_scaled_reflection_and_uses_real_hit_for_lifesteal(self):
        template = {"floor": 1, "tea_until": 0, "hp": 40, "shield": 4, "field_damage_multiplier": 1.5,
                    "stats": {"hp": 120, "atk": 8, "def": 0, "agi": 100, "luk": 0}, "perks": {},
                    "field_mods": {"leech": .4, "thorns": .5}}
        roll = Random(3)
        with patch.object(roll, "random", return_value=.8), patch.object(roll, "uniform", return_value=1):
            run = copy.deepcopy(template)
            enemy = {"hp": 20, "atk": 10, "def": 0, "agi": 0}
            report = combat.fight(run, enemy, roll)
            self.assertEqual((report.rounds, report.total_dealt, report.total_taken), (1, 20, 11))
            self.assertEqual((run["hp"], enemy["hp"]), (33, 0))
            self.assertIn("吸血 +4", report.log[0])
            self.assertIn("反伤 8", report.log[0])

            run = copy.deepcopy(template)
            enemy = {"hp": 8, "atk": 10, "def": 0, "agi": 0}
            report = combat.fight(run, enemy, roll)
            self.assertEqual((report.total_dealt, report.total_taken, run["hp"]), (8, 0, 43))
            self.assertIn("吸血 +3", report.log[0])

    def test_judgement_refuses_without_a_live_enemy_or_with_an_unresolved_offer(self):
        key = "field_singularity"
        self.do("rename", "__keep_start__")
        with self.g.store.transaction() as db:
            turn = self.turn(db)
            self.decline_field_offer(turn.run)
            turn.run.update(event={"kind": "ready"}, field_items={key: 1})
            turn.persist()
        with patch.object(self.g.rng, "random") as draw:
            self.assertRegex(self.do("field_use", key).text, "敌|怪")
            draw.assert_not_called()
        with self.g.store.transaction() as db:
            turn = self.turn(db)
            run = turn.run
            self.assertEqual(run["field_items"], {key: 1})
            enemy = combat.monster(self.cat.maps[run["map"]], run["floor"], turn.rng)
            run.update(event={"kind": "combat", "enemy": enemy},
                       field_offer={"id": "field_harness", "floor": run["floor"]})
            turn.persist()
        with patch.object(self.g.rng, "random") as draw:
            self.assertIn("临装", self.do("field_use", key).text)
            draw.assert_not_called()
        with self.g.store.read() as db:
            run = json.loads(db.execute("SELECT payload FROM active_runs WHERE user_id='1'").fetchone()[0])
            self.assertEqual(run["field_items"], {key: 1})
            self.assertEqual(run["event"]["enemy"]["hp"], enemy["hp"])

    def test_judgement_loss_bypasses_insurance_and_dolls_and_destroys_safe(self):
        key = "field_singularity"
        self.do("rename", "__keep_start__")
        with self.g.store.transaction() as db:
            turn = self.turn(db)
            self.decline_field_offer(turn.run)
            self.assertLessEqual(turn.player["runs"], 3)
            bag = [self.cat.ordinary_item("doll", turn.rng), self.cat.ordinary_item("coffee", turn.rng)]
            safe = [self.cat.ordinary_item("doll", turn.rng)]
            enemy = combat.monster(self.cat.maps["mall"], 1, turn.rng)
            turn.run.update(bag=bag, safe=safe, field_items={key: 1}, event={"kind": "combat", "enemy": enemy})
            expected_loss = sum(item["value"] for item in bag + safe)
            turn.persist()
        with patch.object(self.g.rng, "random", return_value=.499999) as draw:
            reply = self.do("field_use", key)
            draw.assert_called_once_with()
        self.assertNotIn("替身娃娃替你", reply.text)
        with self.g.store.read() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM active_runs WHERE user_id='1'").fetchone())
            self.assertEqual(db.execute("SELECT extracts FROM players WHERE user_id='1'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM inventory WHERE user_id='1'").fetchone()[0], 0)
            relic = db.execute("SELECT items,value FROM relics WHERE owner_id='1'").fetchone()
            self.assertIsNotNone(relic)
            self.assertEqual(json.loads(relic["items"]), bag)
            self.assertEqual(relic["value"], sum(item["value"] for item in bag))
            loss = db.execute("SELECT death_value FROM season_stats WHERE user_id='1'").fetchone()[0]
            self.assertEqual(loss, expected_loss)

    def test_judgement_kills_boss_at_midpoint_and_can_be_used_again_on_ambush(self):
        key = "field_singularity"
        self.assertEqual(self.cat.field_items[key]["name"], "概念集合体-公正")
        self.do("rename", "__keep_start__")
        with self.g.store.transaction() as db:
            turn = self.turn(db)
            self.decline_field_offer(turn.run)
            boss = combat.monster(self.cat.maps["mall"], 8, turn.rng, boss=True)
            turn.run.update(floor=8, hp=1, blood_used=True, field_items={key: 2},
                            event={"kind": "combat", "enemy": boss})
            turn.persist()
        with patch.object(self.g.rng, "random", return_value=.5):
            self.do("field_use", key)
        with self.g.store.transaction() as db:
            turn = self.turn(db)
            run = turn.run
            self.assertEqual(run["hp"], run["stats"]["hp"])
            self.assertEqual(run["paid_boss_floor"], 8)
            self.assertEqual(run["field_items"], {key: 1})
            self.assertEqual(run["event"]["kind"], "ready")
            self.assertGreaterEqual(len(run["bag"]), 2)
            battle = run["floor_result"]["battle"]
            self.assertEqual((battle["outcome"], battle["enemy_hp"]), ("victory", 0))
            self.assertGreaterEqual(len(battle["loot"]), 2)
            enemy = combat.monster(self.cat.maps["mall"], 9, turn.rng, elite=True, ambush=True)
            run.update(floor=9, event={"kind": "combat", "enemy": enemy})
            turn.persist()
        with patch.object(self.g.rng, "random", return_value=.500001):
            self.assertIn("安全撤离", self.do("use", "概念集合体-公正").text)
        with self.g.store.read() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM active_runs WHERE user_id='1'").fetchone())
            self.assertEqual(db.execute("SELECT extracts FROM players WHERE user_id='1'").fetchone()[0], 1)
            self.assertGreater(db.execute("SELECT count(*) FROM inventory WHERE user_id='1'").fetchone()[0], 0)

    def test_field_wear_migrates_old_slot_and_equip_requires_a_choice_when_full(self):
        items = self.cat.field_items
        apron, lamp, harness = "field_apron", "field_lamp", "field_harness"
        run = {"floor": 2, "hp": 70, "stats": {"hp": 100}, "field_worn": apron,
               "field_items": {apron: 1, lamp: 1, harness: 1}}
        rules.refresh(run, items)
        self.assertNotIn("field_worn", run)
        self.assertEqual(rules.field_slots(run), [apron, None])
        self.assertEqual(rules.field_choices(run, lamp), [2])
        rules.activate(run, lamp, items)
        self.assertEqual(rules.field_slots(run), [apron, lamp])
        self.assertEqual(run["field_mods"]["defense"], .3)
        self.assertEqual(run["field_mods"]["crit"], .13)

        with self.assertRaises(ValueError):
            rules.activate(run, harness, items)
        self.assertEqual(run["field_items"], {apron: 1, harness: 1})
        run["field_offer"] = {"id": harness, "floor": 2}
        self.assertEqual(rules.field_choices(run, harness), [1, 2])
        with self.assertRaises(ValueError):
            rules.choose_field(run, lamp, 2, 1, items)
        with self.assertRaises(ValueError):
            rules.choose_field(run, harness, 1, 1, items)
        run["floor"] = 3
        self.assertFalse(rules.enter_field(run, items))
        self.assertEqual(run["field_offer"], {"id": harness, "floor": 2})
        run["floor"] = 2
        message = rules.choose_field(run, harness, 2, 1, items)
        self.assertIn(items[apron]["name"], message)
        self.assertNotIn("field_offer", run)
        self.assertEqual(rules.field_slots(run), [harness, lamp])
        self.assertEqual(run["field_items"], {apron: 1, harness: 1})
        self.assertEqual(run["field_mods"]["defense"], .15)
        self.assertEqual(run["field_mods"]["crit"], .13)
        rules.remove_field(run, harness, items)
        self.assertEqual(rules.field_slots(run), [None, lamp])
        self.assertNotIn("defense", run["field_mods"])

    def test_duplicate_wear_stacks_to_modifier_caps_and_can_be_removed_by_slot(self):
        items = self.cat.field_items
        armor = "field_armor"
        run = {"floor": 3, "hp": 70, "stats": {"hp": 100}, "field_items": {armor: 1}}
        rules.activate(run, armor, items)
        self.assertEqual(rules.field_choices(run, armor), [2])
        run["field_offer"] = {"id": armor, "floor": 3}
        rules.choose_field(run, armor, 3, 2, items)
        self.assertEqual(rules.field_slots(run), [armor, armor])
        self.assertEqual(run["field_mods"], {"defense": 1.4, "mitigation": .24, "agi": -30})
        with self.assertRaises(ValueError):
            rules.remove_field(run, armor, items)
        with self.assertRaises(ValueError):
            rules.remove_field(run, "field_apron", items, slot=1)
        rules.remove_field(run, armor, items, slot=1)
        self.assertEqual(rules.field_slots(run), [None, armor])
        self.assertEqual(run["field_mods"], items[armor]["mods"])

    def test_atlas_thresholds_and_economy_are_explicit(self):
        self.assertEqual([len(rules.milestones(self.cat.progression,n,40)) for n in (0,2,8,16,26,34,40)],
                         [0,1,2,3,4,5,6])
        for key,(_,grades,weights) in self.cat.gear_boxes.items():
            self.assertEqual(sum(weights),100)
            self.assertLess(sum(self.cat.gear_sell[g]*w/100 for g,w in zip(grades,weights)),self.cat.shop[key])
        for map_key,map_data in self.cat.maps.items():
            last=0
            for f in range(1,151):
                h=rules.enemy_stats(map_data["multiplier"],f,elite=False)["hp"]
                self.assertGreater(h,last);last=h
                self.assertLess(h,int(38*map_data["multiplier"]*(1+.18*f+.008*f*f)))

    def test_assisted_boss_pays_only_the_owner_and_only_once(self):
        self.do("rename","__keep_start__")
        with self.g.store.transaction() as db:
            owner=self.turn(db)
            owner.run["floor"]=8
            owner._enter_floor()
            owner.run["event"]["enemy"]["hp"]=1
            owner.run["hp"]=2
            owner.persist()
            owner.flare("","")
            owner.persist()
            helper=game.Turn(self.g,db,"3","队友","2",self.at+1)
            with patch.object(owner.rng,"random",return_value=0):
                helper.support("","1")
            with self.assertRaises(game.GameError):helper.support("","1")
            owner=self.turn(db)
            self.assertEqual(owner.run["event"]["kind"],"ready")
            self.assertEqual(owner.run["hp"],owner.run["stats"]["hp"])
            self.assertEqual(len(self.cat.rainbow.keys()&owner.archives.keys()),1)
            self.assertFalse(self.cat.rainbow.keys()&helper.archives.keys())

    def test_crates_trophy_prices_and_atlas_unlock_note(self):
        self.do("rename","__keep_lobby__")
        with self.g.store.transaction() as db:
            t=self.turn(db)
            t.deposit(self.cat.ordinary_item("crate_gold",t.rng),quantity=65)
            with patch.object(t.rng,"random",return_value=0):
                for _ in range(65):
                    item=t._open_one_crate(next(row for row in t._stock() if row["id"]=="crate_gold"))
                    self.assertIn(self.cat.rarity(item),("red","gold"))
            self.assertFalse(self.cat.rainbow.keys()&t.archives.keys())
            self.assertTrue(any("图鉴点亮 2/48" in note for note in t._quest_notes))
            for key,map_info in self.cat.maps.items():
                run={"map":key,"floor":8,"perks":{"greed":.25},"value_bonus":.20}
                map_multiplier=min(1.60,1+(map_info["value_multiplier"]-1)*(.60/3.30))
                multiplier=map_multiplier*1.45*.48
                with patch.object(t.rng,"random",return_value=.30):
                    for item in self.cat.boss_rewards(run,t.rng,1):
                        self.assertEqual(item["value"],round(self.cat.items[item["id"]]["value"]*multiplier))

    def test_new_collection_values_scale_gently_by_map_and_existing_values_stay(self):
        self.assertEqual(rules.collection_value_multiplier({"value_multiplier":9}),1.60)
        self.do("rename","__keep_start__")
        for rarity in ("gold","red"):
            key=next(key for key,item in self.cat.collections.items()
                     if item["rarity"]==rarity and "map" not in item and item.get("min_floor",1)<=8)
            base=self.cat.items[key]["value"]
            # Use the same collectible in every eligible pool to isolate appraisal from drop odds.
            def choose_collection(pool):
                self.assertIn(key,pool)
                return key

            with self.g.store.transaction() as db:
                turn=self.turn(db)
                old={**self.cat.ordinary_item(key,turn.rng),"value":base*3}
                turn.deposit(old)
                turn.run["bag"].append(old.copy())
                turn.persist()
            for map_key,map_info in self.cat.maps.items():
                with self.subTest(rarity=rarity,map=map_key):
                    run={"map":map_key,"floor":8,"perks":{"greed":.25},"stats":{"luk":0},
                         "rare_bonus":0,"value_bonus":.20}
                    rng=Random(4)
                    with patch.object(rng,"random",return_value=0),patch.object(rng,"choices",return_value=[rarity]), \
                            patch.object(rng,"choice",side_effect=choose_collection):
                        fresh=self.cat.loot(run,rng,chest=False)
                    self.assertEqual(fresh["id"],key)
                    map_multiplier=min(1.60,1+(map_info["value_multiplier"]-1)*(.60/3.30))
                    self.assertEqual(fresh["value"],round(base*(map_multiplier*1.45)))

            with self.g.store.transaction() as db:
                self.turn(db).deposit(fresh)
            with game.ElevatorGame(self.path).store.read() as db:
                stored=db.execute("SELECT value FROM inventory WHERE user_id=? AND item_id=? ORDER BY value",
                                  ("1",key)).fetchall()
                self.assertEqual([row["value"] for row in stored],[fresh["value"],base*3])
                active=json.loads(db.execute("SELECT payload FROM active_runs WHERE user_id=?",("1",)).fetchone()[0])
                self.assertIn(old,active["bag"])

    def test_old_active_run_rehydrates_once_without_replaying_field_reward(self):
        self.do("rename","__keep_start__")
        with self.g.store.transaction() as db:
            t=self.turn(db)
            run=t.run
            run.update(floor=7,hp=55,stats={**run["stats"],"hp":110},event={"kind":"ready"})
            for key in list(run):
                if key.startswith("field_") or key=="rules_version": del run[key]
            t.persist()
        with self.g.store.transaction() as db:
            t=self.turn(db)
            self.assertEqual(t.run["hp"],round(t.run["stats"]["hp"]*.5))
            t.down("","")
            self.assertTrue(t.run["event"]["enemy"]["boss"])
            self.assertEqual(sum(t.run["field_items"].values()) + bool(t.run.get("field_offer")),1)
            t.persist()
            t=self.turn(db)
            self.assertFalse(rules.enter_field(t.run,self.cat.field_items))
            self.assertEqual(sum(t.run["field_items"].values()) + bool(t.run.get("field_offer")),1)
