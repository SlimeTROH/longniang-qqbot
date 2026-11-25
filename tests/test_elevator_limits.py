"""Capped actions report the existing limit without changing the rules."""
import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from random import Random


ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("elevator_limits_test")
package.__path__ = [str(ROOT / "plugins" / "endless_elevator")]
sys.modules[package.__name__] = package
game = importlib.import_module(f"{package.__name__}.game")
rules = game.expedition


class ElevatorLimits(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.game = game.ElevatorGame(Path(temp.name) / "save.sqlite3", rng=Random(71))
        self.game.execute("1","玩家","2","pursue","1 8",now=1795470011)
        self.game.execute("1", "玩家", "2", "rename", "__keep_start__", now=1795470012)

    def turn(self, db):
        turn = game.Turn(self.game, db, "1", "玩家", "2", 1795470024)
        turn.run.pop("field_offer", None)
        return turn

    def act(self, turn, action, argument=""):
        return self.game.apply_turn(turn, action, argument, cooldown=False)

    def test_adrenaline_exact_and_over_cap_warn_and_an_unrelated_action_does_not(self):
        with self.game.store.transaction() as db:
            turn = self.turn(db)
            for previous in (.25, .35):
                with self.subTest(previous=previous):
                    turn.run["atk_boost"] = previous
                    turn.run["bag"] = [turn.catalog.ordinary_item("adrenaline", turn.rng) for _ in range(2)]
                    turn, reply, code = self.act(turn, "use", "1")
                    self.assertIsNone(code)
                    self.assertEqual(turn.run["atk_boost"], game.ADRENALINE_ATK_CAP)
                    self.assertEqual(len(turn.limit_notices), 1)
                    self.assertIn("+50% 上限", reply.text)
                    turn, reply, code = self.act(turn, "bag")
                    self.assertIsNone(code)
                    self.assertEqual(turn.limit_notices, [])
                    turn, reply, code = self.act(turn, "use", "adrenaline")
                    self.assertEqual(code, "invalid")
                    self.assertIn("+50%", reply.text)
                    self.assertEqual(len(turn.run["bag"]), 1)

    def test_quest_attack_and_gold_red_report_their_own_limits(self):
        with self.game.store.transaction() as db:
            turn = self.turn(db)
            for previous in (.40, .50, .60):
                with self.subTest(previous=previous):
                    turn.run["event"] = {"kind": "choice", "id": "repair_bench"}
                    turn.run["atk_boost"] = previous
                    turn, reply, code = self.act(turn, "event", "1")
                    self.assertIsNone(code)
                    self.assertEqual(turn.run["atk_boost"], game.EVENT_ATK_CAP)
                    self.assertIn("+60% 上限", reply.text)
                    self.assertNotIn("+50% 上限", reply.text)
            turn.player["gold"] = 500
            turn.run["event"] = {"kind": "choice", "id": "cyber_temple"}
            turn.run["rare_bonus"] = 99.0
            turn, reply, code = self.act(turn, "event", "1")
            self.assertIsNone(code)
            self.assertEqual(turn.run["rare_bonus"], 99.25)
            self.assertEqual(turn._rare_rate(turn.run["floor"]), rules.RARE_CAP)
            self.assertIn("基础金红率已达 68% 上限", reply.text)

    def test_duplicate_wear_reports_caps_without_repeating_on_unchanged_replacement(self):
        with self.game.store.transaction() as db:
            turn = self.turn(db)
            key = "field_armor"
            turn.run["field_slots"] = [key, None]
            turn.run["field_items"] = {key: 1}
            turn, reply, code = self.act(turn, "field_use", key)
            self.assertIsNone(code)
            self.assertEqual(turn.run["field_mods"]["defense"], rules.MOD_CAPS["defense"][1])
            self.assertEqual(turn.run["field_mods"]["agi"], rules.MOD_CAPS["agi"][0])
            self.assertIn("防御修正已达上限 +140%", reply.text)
            self.assertIn("敏捷修正已达下限 -30", reply.text)
            turn.run["field_items"] = {key: 1}
            turn, reply, code = self.act(turn, "field_use", f"{key} 1")
            self.assertIsNone(code)
            self.assertEqual(turn.limit_notices, [])

    def test_ticket_bulk_and_permanent_safebox_report_reached_caps(self):
        with self.game.store.transaction() as db:
            turn = self.turn(db)
            turn.run = None
            turn.player.update(gold=500_000, safebox=3)
            turn, reply, code = self.act(turn, "buy", "补票")
            self.assertIsNone(code)
            self.assertEqual(turn.player["paid_tickets"], rules.TICKET_PAID_CAP)
            self.assertIn("购买资格零点刷新", reply.text)
            self.assertIn("未用购买票已存满", reply.text)
            turn, reply, code = self.act(turn, "buy", "safebox")
            self.assertIsNone(code)
            self.assertIn("永久安全箱已达到 4 格上限", reply.text)
            turn, reply, code = self.act(turn, "buy", "safebox")
            self.assertEqual(code, "invalid")
            self.assertIn("永久安全箱已达到 4 格上限", reply.text)

    def test_healing_caps_only_when_the_item_reaches_max_health(self):
        with self.game.store.transaction() as db:
            turn = self.turn(db)
            max_hp = turn.run["stats"]["hp"]
            turn.run["hp"] = max_hp // 2
            turn.run["bag"] = [turn.catalog.ordinary_item("coffee", turn.rng)]
            turn, reply, code = self.act(turn, "use", "coffee")
            self.assertIsNone(code)
            self.assertEqual(turn.limit_notices, [])
            turn.run["bag"] = [turn.catalog.ordinary_item("coffee", turn.rng)]
            turn, reply, code = self.act(turn, "use", "coffee")
            self.assertIsNone(code)
            self.assertEqual(turn.run["hp"], max_hp)
            self.assertIn(f"生命上限 {max_hp} HP", reply.text)
