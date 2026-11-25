import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from elevator_web_game import WebGame, rules


class ShiningSeasonTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.web = WebGame(Path(self.temp) / "season.sqlite3")
        self.game = self.web.game
        self.now = 1795590000
        self.uid, self.gid = "101", "202"

    def state(self):
        return self.web.state(self.uid, self.gid, "试验员", now=self.now)

    def command(self, command, params, *, expect=None):
        revision = self.state()["revision"]
        self.now += 1
        result = self.web.command(self.uid, self.gid, command, params, revision, "试验员", now=self.now)
        self.assertEqual(result["code"], expect, result.get("error"))
        return result

    def start(self):
        self.command("start", {"map_id":"mall", "focus":["B01","B08"]})

    def turn(self, db):
        return rules.Turn(self.game, db, self.uid, "试验员", self.gid, self.now)

    def test_focus_slot_and_legacy_run(self):
        previous = self.state()["player"]["tickets"]
        self.command("start", {"map_id":"mall"}, expect="invalid")
        self.assertEqual(previous, self.state()["player"]["tickets"])
        with self.game.store.transaction() as db:
            db.execute("INSERT INTO heirloom_inventory VALUES(?,?,?)", (self.uid,"B01-1",2))
        self.command("equip_blessing", {"slot":1,"piece":"B01-1"})
        self.command("equip_blessing", {"slot":2,"piece":"B01-1"}, expect="invalid")
        self.start()
        self.command("pursue", {"focus":["B02","B03"]}, expect="invalid")
        self.command("equip_blessing", {"slot":1,"piece":None}, expect="invalid")
        with self.game.store.transaction() as db:
            turn=self.turn(db)
            del turn.run["heirloom_focus"]
            turn.persist()
        self.assertTrue(self.state()["heirlooms"]["needs_focus"])
        self.command("pursue", {"focus":["B02","B03"]})
        self.assertEqual(self.state()["heirlooms"]["focus"],["B02","B03"])

    def test_two_piece_bonuses_break_the_soft_caps_and_depth_scaling(self):
        h = rules.heirlooms
        stats = {"hp":1000,"atk":400,"def":200,"agi":140,"luk":180}
        perks = {"pierce":.8,"prism_leech":.5,"prism_combo":.8,"high_roller":2.2}
        expected = {"hit":1., "crit":.75, "mitigation":.65, "dodge":.60,
                    "crit_multiplier":2.55,"pierce":.87,"combo":.82,"leech":.50}
        extra = {"crit":.1,"dodge":.2,"mitigation":.55}
        for code,(key,_) in h.BONUSES.items():
            attrs, buffs = stats.copy(),perks.copy()
            h.apply(attrs,buffs,[f"{code}-{i}" for i in range(1,5)])
            run = {"stats":attrs,"hp":1000,"perks":buffs,"floor":310,"tea_until":0,"field_mods":extra,
                   "event":{"enemy":{"name":"靶","def":140,"agi":0}}}
            values = rules.combat.secondary_stats(run,details=True)
            self.assertAlmostEqual(values[key],expected[key])
            self.assertAlmostEqual(values["sources"][key][-1]["after"],values[key])
            self.assertGreater(h.depth_shred(310,buffs),h.depth_shred(150,buffs))
            self.assertEqual(h.depth_shred(50,buffs),0)
        stats, perks = stats.copy(),{}
        h.apply(stats,perks,["B01-1","B01-2","B06-1","B06-2"])
        self.assertEqual(perks["heirloom_depth_cap"],h.DEPTH_PAIR_CAP)
        self.assertNotIn("heirloom_full",perks)

    def test_boss_drop_is_banked_and_limited_to_the_run_focus(self):
        self.start()
        with self.game.store.transaction() as db:
            turn = self.turn(db)
            for floor in (8,18):
                enemy = rules.combat.monster(self.game.catalog.maps["mall"],floor,random.Random(1),boss=True)
                enemy["hp"] = 0
                turn.run.update(floor=floor,event={"kind":"combat","enemy":enemy})
                with patch.object(rules.heirlooms,"roll",return_value="B08-4") as drop:
                    turn._enemy_win(enemy)
                self.assertEqual(drop.call_args.args[1],["B01","B08"])
                self.assertFalse(any(item["id"]=="B08-4" for item in turn.run["bag"]))
            turn.persist()
        self.assertEqual(self.state()["heirlooms"]["owned"]["B08-4"],2)
        probs=[rules.heirlooms.probability(i) for i in (8,18,150,310,100_000)]
        self.assertEqual(probs,sorted(probs))
        self.assertLessEqual(max(probs),.10)

    def test_f500_crossing_and_wish_clears_only_found_items_after_confirmation(self):
        self.start()
        with self.game.store.transaction() as db:
            turn = self.turn(db)
            run = turn.run
            run.update(floor=503,field_seen=499)
            run.pop("field_offer",None)
            with patch.object(rules.expedition,"WISH_CHANCE",0):
                rules.expedition.enter_field(run,turn.catalog.field_items)
                self.assertEqual(run["field_items"][rules.expedition.WISH],1)
                run.pop("field_offer",None)
                run["floor"] += 1
                rules.expedition.enter_field(run,turn.catalog.field_items)
                self.assertEqual(run["field_items"][rules.expedition.WISH],1)
            run.pop("field_offer",None)
            run["field_items"]["field_wrap"]=1
            enemy=rules.combat.monster(turn.catalog.maps["mall"],504,random.Random(2),elite=True)
            run["event"]={"kind":"combat","enemy":enemy}
            run["bag"]=[turn.catalog.ordinary_item("coffee",turn.rng)]
            run["safe"]=[{**turn.catalog.ordinary_item("g01",turn.rng),"run_pickup":True}]
            turn._give([turn.catalog.ordinary_item("snack",turn.rng)])
            turn.persist()
        first=self.command("field_use",{"id":rules.expedition.WISH})["result"]["wish_preview"]
        self.assertEqual((first["bag"],first["safe"]),(1,1))
        with self.game.store.transaction() as db:
            turn=self.turn(db)
            turn.run["event"]["enemy"]["hp"] -= 1
            turn.persist()
        second=self.command("wish_confirm",{"token":first["token"]})["result"]["wish_preview"]
        self.assertNotEqual(first["token"],second["token"])
        result=self.command("wish_confirm",{"token":second["token"]})["result"]
        self.assertEqual(result["battle"]["outcome"],"victory")
        with self.game.store.read() as db:
            run=json.loads(db.execute("SELECT payload FROM active_runs WHERE user_id=?",(self.uid,)).fetchone()[0])
        self.assertEqual(run["safe"],[])
        self.assertTrue(any(item["id"]=="coffee" and not item.get("run_pickup") for item in run["bag"]))
        self.assertEqual(run["field_items"]["field_wrap"],1)
        self.assertNotIn(rules.expedition.WISH,run["field_items"])
        self.command("wish_confirm",{"token":second["token"]},expect="invalid")

    def test_elite_two_exchanges_and_ring_curve(self):
        self.start()
        with self.game.store.transaction() as db:
            turn = self.turn(db)
            run = turn.run
            run.pop("field_offer",None)
            enemy=rules.combat.monster(turn.catalog.maps["mall"],152,random.Random(2),elite=True)
            enemy.update(atk=1,hp=10000,max_hp=10000)
            run["event"]={"kind":"combat","enemy":enemy}
            self.assertIn("精英中场",turn.fight("","").text)
            self.assertEqual(enemy["rounds_fought"],8)
            self.assertIn("平手退场",turn.fight("","").text)
            self.assertEqual(enemy["rounds_fought"],16)
            self.assertEqual(run["event"]["kind"],"ready")
        exp=rules.expedition
        limit=lambda f:exp.ring_shield_limit({"floor":f,"perks":{"prism_floor_shield":40}})
        self.assertEqual([limit(f) for f in (1,97,120,226,10**7)],[40,64,67,74,88])
