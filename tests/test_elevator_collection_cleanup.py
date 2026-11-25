"""Collection cleanup reads the whole warehouse and executes only the confirmed quote."""
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from random import Random


ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("elevator_collection_cleanup_test")
package.__path__ = [str(ROOT / "plugins" / "endless_elevator")]
sys.modules[package.__name__] = package
game = importlib.import_module(f"{package.__name__}.game")


class CollectionCleanup(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.game = game.ElevatorGame(Path(temporary.name) / "save.sqlite3", rng=Random(17))
        self.at = 1795470000
        self.do("rename", "__keep_lobby__")

    def do(self, action, argument, user_id="1"):
        self.at += 12
        return self.game.execute(user_id, "乘客", "2", action, argument, now=self.at)

    def plan(self, *, order=False):
        return self.game.plan_collection_cleanup("1", "乘客", "2", create_order=order, now=self.at)

    @staticmethod
    def stock(db, item_id, value, quantity, *, quality=""):
        return db.execute(
            "INSERT INTO inventory(user_id,item_id,quality,value,quantity) VALUES('1',?,?,?,?)",
            (item_id, quality, value, quantity),
        ).lastrowid

    def test_full_warehouse_keeps_one_highest_unit_per_visible_name_and_rarity(self):
        catalog = self.game.catalog
        name = catalog.items["g01"]["name"]
        catalog.items["cleanup_same_gold"] = dict(catalog.items["g01"])
        catalog.items["cleanup_same_red"] = {**catalog.items["r01"], "name": name}
        catalog.items["cleanup_gold_material"] = {**catalog.items["g01"], "kind": "material"}
        rainbow_key = next(iter(catalog.rainbow))
        legacy_quality = next(f"{quality}:{index}" for quality, spec in catalog.qualities.items()
                              for index, prefix in enumerate(spec["prefixes"]) if prefix)
        self.assertNotEqual(catalog.name({"id": "g01", "quality": legacy_quality}, symbol=False), name)
        with self.game.store.transaction() as db:
            for unit_price in range(1, 230):
                self.stock(db, "g01", unit_price, 1)
            kept_gold = self.stock(db, "g01", 230, 3)
            self.stock(db, "cleanup_same_gold", 230, 4)
            self.stock(db, "g01", 230, 2, quality=legacy_quality)
            self.stock(db, "cleanup_same_red", 210, 1)
            kept_red = self.stock(db, "cleanup_same_red", 280, 2)
            singleton = self.stock(db, "g02", 600, 1)
            other = {
                self.stock(db, "vip_card", 999, 3): 3,
                self.stock(db, "cleanup_gold_material", 888, 4): 4,
                self.stock(db, rainbow_key, 777, 2): 2,
            }
            gear = db.execute(
                "INSERT INTO equipment(user_id,base_id,rarity,stats,affixes,rules_version) "
                "VALUES('1','crowbar','gold','{}','[]',2)"
            ).lastrowid
            balance_before = db.execute("SELECT gold FROM players WHERE user_id='1'").fetchone()[0]

        plan = self.plan()
        self.assertEqual(plan["sale_count"], 239)
        self.assertEqual(plan["gold"], sum(range(1, 230)) + 8 * 230 + 210 + 280)
        in_order = self.plan(order=True)
        self.assertEqual(plan, {key: value for key, value in in_order.items() if key != "order_id"})
        self.assertEqual(plan["collections"], [
            {"name": name, "rarity": "red", "keep_value": 280, "sale_count": 2, "gold": 490},
            *sorted([
                {"name": name, "rarity": "gold", "keep_value": 230, "sale_count": 237,
                 "gold": sum(range(1, 230)) + 8 * 230},
                {"name": catalog.items["g02"]["name"], "rarity": "gold", "keep_value": 600,
                 "sale_count": 0, "gold": 0},
            ], key=lambda row: row["name"]),
        ])

        order_id = in_order["order_id"]
        with self.game.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM inventory WHERE user_id='1'").fetchone()[0], 238)
            steps = json.loads(db.execute("SELECT steps_json FROM ai_orders WHERE order_id=?", (order_id,)).fetchone()[0])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["action"], "curate_collectibles")
        self.assertIn("本人", self.do("confirm_order", order_id, "3").text)
        self.assertIn("派单", self.do("curate_collectibles", steps[0]["argument"]).text)

        with self.game.store.transaction() as db:
            material = next(stock_id for stock_id, quantity in other.items() if quantity == 4)
            db.execute("UPDATE inventory SET quantity=5 WHERE stock_id=?", (material,))
            other[material] = 5
        reply = self.do("confirm_order", order_id)
        self.assertIn("完成 1/1 步", reply.text)
        self.assertIn(f"到账 {plan['gold']:,} 金", reply.text)
        self.assertIn("已经执行过了", self.do("confirm_order", order_id).text)
        self.assertNotIn("order_id", self.plan(order=True))

        with self.game.store.read() as db:
            stocks = dict(db.execute("SELECT stock_id,quantity FROM inventory WHERE user_id='1' AND item_id!='ice_tea'"))
            self.assertEqual(stocks, {kept_gold: 1, kept_red: 1, singleton: 1, **other})
            self.assertIsNotNone(db.execute("SELECT 1 FROM equipment WHERE equip_id=?", (gear,)).fetchone())
            self.assertEqual(db.execute("SELECT gold FROM players WHERE user_id='1'").fetchone()[0],
                             balance_before + plan["gold"])

    def test_confirmation_rejects_stale_relevant_stock_without_selling_or_crediting_gold(self):
        with self.game.store.transaction() as db:
            self.stock(db, "g01", 300, 2)
            self.stock(db, "g01", 100, 1)
            balance_before = db.execute("SELECT gold FROM players WHERE user_id='1'").fetchone()[0]
        in_order = self.plan(order=True)
        self.assertEqual((in_order["sale_count"], in_order["gold"]), (2, 400))
        with self.game.store.transaction() as db:
            self.stock(db, "g01", 500, 1)
        reply = self.do("confirm_order", in_order["order_id"])
        self.assertIn("重新核算", reply.text)
        self.assertIn("完成 0/1 步", reply.text)
        with self.game.store.read() as db:
            self.assertEqual(dict(db.execute("SELECT value,quantity FROM inventory WHERE user_id='1'")),
                             {300: 2, 100: 1, 500: 1})
            self.assertEqual(db.execute("SELECT gold FROM players WHERE user_id='1'").fetchone()[0], balance_before)
        fresh = self.plan(order=True)
        self.assertEqual((fresh["sale_count"], fresh["gold"]), (3, 700))
        self.assertIn("完成 1/1 步", self.do("confirm_order", fresh["order_id"]).text)

    def test_active_run_allows_preview_but_prevents_order_creation(self):
        with self.game.store.transaction() as db:
            self.stock(db, "g01", 300, 2)
        self.do("pursue", "1 8")
        self.do("start", "mall")
        self.assertTrue(self.game.has_run("1"))
        self.assertEqual(self.plan()["sale_count"], 1)
        with self.assertRaisesRegex(game.GameError, "回大厅"):
            self.plan(order=True)
        with self.game.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM ai_orders").fetchone()[0], 0)
