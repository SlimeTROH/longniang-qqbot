"""Game and menu regressions using a temporary database and no QQ messages."""
from __future__ import annotations

import importlib
import io
import json
import asyncio
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import datetime
from pathlib import Path
from random import Random
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nonebot.adapters.onebot.v11 import Message, MessageSegment, PrivateMessageEvent
from PIL import Image

from test_chat_group_settings import load_handlers

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("endless_elevator_test")
package.__path__ = [str(ROOT / "plugins" / "endless_elevator")]
sys.modules[package.__name__] = package
elevator = importlib.import_module(f"{package.__name__}.game")
cards = importlib.import_module(f"{package.__name__}.cards")


class ElevatorTests(unittest.TestCase):
    def setUp(self):
        area = tempfile.TemporaryDirectory()
        self.addCleanup(area.cleanup)
        self.game = elevator.ElevatorGame(Path(area.name) / "player.sqlite3", rng=Random(17))
        self.at = datetime(2026, 11, 22, 10, 0, tzinfo=elevator.SHANGHAI).timestamp()
        # These scenarios cover other game systems, with no wearable choice on the way.
        field_rng = self.enterContext(patch.object(elevator.expedition, "Random")).return_value
        field_rng.choices.return_value = ["field_wrap"]
        field_rng.random.return_value = 1.0
        field_rng.randrange.return_value = 0

    def command(self, action, text="", *, user="101", group="10", target=""):
        self.at += 12
        if action=="start" or (action=="rename" and text=="__keep_start__"):
            self.game.execute(user,f"玩家{user}",group,"pursue","1 8",now=self.at-6)
        if action=="start":
            # Play the onboarding choice before tests that begin directly at a named map.
            with self.game.store.read() as db:
                record=db.execute("SELECT name_confirmed FROM players WHERE user_id=?",(user,)).fetchone()
            if record is None or not record[0]:
                self.game.execute(user,f"玩家{user}",group,"rename","__keep_lobby__",now=self.at-5)
            if not text: text="mall"
        return self.game.execute(user, f"玩家{user}", group, action, text, target=target, now=self.at)

    def player(self, user="101"):
        with self.game.store.read() as db:
            return dict(db.execute("SELECT * FROM players WHERE user_id=?", (user,)).fetchone())

    def active_run(self, user="101"):
        with self.game.store.read() as db:
            row = db.execute("SELECT payload FROM active_runs WHERE user_id=?", (user,)).fetchone()
            return json.loads(row["payload"]) if row else None

    def modify_run(self, update, user="101"):
        with self.game.store.transaction() as db:
            payload = json.loads(db.execute("SELECT payload FROM active_runs WHERE user_id=?", (user,)).fetchone()[0])
            update(payload)
            db.execute("UPDATE active_runs SET payload=? WHERE user_id=?", (json.dumps(payload), user))

    def stock(self, user="101"):
        with self.game.store.read() as db:
            return {row["item_id"]: row["num"] for row in db.execute(
                "SELECT item_id,sum(quantity) AS num FROM inventory WHERE user_id=? GROUP BY item_id", (user,),
            )}

    def loot(self, item_id, value=None):
        item = self.game.catalog.ordinary_item(item_id, self.game.rng)
        if value is not None:
            item["value"] = value
        return item

    def test_first_message_starts_and_cross_group_extract_uses_the_same_run(self):
        opening = self.command("menu")
        self.assertIn("昵称",opening.text)
        opening=self.command("rename","__keep_start__")
        self.assertEqual(self.active_run()["floor"],1)
        self.assertIn("500 金", opening.text)
        self.assertTrue(opening.plain_numbers)
        self.assertEqual(self.player()["tickets"], 19)
        self.modify_run(lambda run: run.update(bag=[self.loot("snack", 100), self.loot("r01")]))
        self.assertIn("非洲之心", self.command("save_box", group="20").text)
        self.assertEqual(self.active_run()["safe"][0]["id"], "r01")
        ended = self.command("extract", group="20")
        self.assertIn("安全撤离", ended.text)
        self.assertFalse(self.game.has_run("101"))
        self.assertEqual(self.stock(), {"r01": 1, "snack": 1})
        self.assertEqual(self.player()["extracts"], 1)
        self.assertIn("1/48", self.command("gallery").text)

    def test_twenty_daily_tickets_and_twenty_purchases_with_carryover(self):
        self.command("help")
        self.assertEqual(self.player()["tickets"], 20)
        self.assertEqual(self.player()["paid_tickets"], 0)
        with self.game.store.transaction() as db:
            db.execute("UPDATE players SET gold=10000 WHERE user_id='101'")

        self.command("buy", "ticket 20")
        self.assertEqual(self.player()["tickets"], 40)
        self.assertEqual(self.player()["paid_tickets"], 20)
        self.assertEqual(self.player()["bought_tickets"], 20)
        self.assertEqual(self.player()["gold"], 3000)

        self.command("start")
        self.command("extract")
        self.command("buy", "ticket 1")
        self.assertEqual(self.player()["tickets"], 39)
        self.assertEqual(self.player()["paid_tickets"], 20)
        self.assertEqual(self.player()["bought_tickets"], 20)
        self.assertEqual(self.player()["gold"], 3000)

        self.at += 86400
        self.command("help")
        self.assertEqual(self.player()["tickets"], 40)
        self.assertEqual(self.player()["paid_tickets"], 20)
        self.assertEqual(self.player()["bought_tickets"], 0)

        self.command("start")
        self.command("extract")
        self.command("buy", "ticket 1")
        self.command("buy", "补票")
        self.assertEqual(self.player()["tickets"], 39)
        self.assertEqual(self.player()["paid_tickets"], 20)
        self.assertEqual(self.player()["bought_tickets"], 0)

        for _ in range(19):
            self.command("start")
            self.command("extract")
        self.assertEqual(self.player()["tickets"], 20)
        self.assertEqual(self.player()["paid_tickets"], 20)

        self.command("start")
        self.command("extract")
        self.assertEqual(self.player()["tickets"], 19)
        self.assertEqual(self.player()["paid_tickets"], 19)
        gold_before = self.player()["gold"]
        self.command("buy", "补票")
        self.assertEqual(self.player()["tickets"], 20)
        self.assertEqual(self.player()["paid_tickets"], 20)
        self.assertEqual(self.player()["bought_tickets"], 1)
        self.assertEqual(self.player()["gold"], gold_before - 350)

    def test_daily_purchase_limit_remains_after_spending_a_purchased_ticket(self):
        self.command("help")
        with self.game.store.transaction() as db:
            db.execute("UPDATE players SET gold=10000, tickets=0 WHERE user_id='101'")
        self.command("buy", "ticket 20")
        self.command("start")
        self.command("extract")

        self.assertEqual(self.player()["paid_tickets"], 19)
        self.command("buy", "ticket 1")
        self.assertEqual(self.player()["tickets"], 19)
        self.assertEqual(self.player()["paid_tickets"], 19)
        self.assertEqual(self.player()["bought_tickets"], 20)

    def test_ticket_refill_upgrade_grants_the_same_day_difference_once(self):
        storage = importlib.import_module(f"{package.__name__}.storage")
        legacy_schema = re.sub(r"^    (?:daily_ticket_refill|paid_tickets)[^\n]*\n", "", storage.SCHEMA, flags=re.MULTILINE)
        today = datetime.fromtimestamp(self.at, elevator.SHANGHAI).date().isoformat()
        with closing(sqlite3.connect(self.game.store.path)) as db, db:
            db.executescript(legacy_schema)
            db.execute(
                "INSERT INTO players (user_id, nickname, tickets, bought_tickets, ticket_day) "
                "VALUES ('101', '玩家101', 5, 2, ?)",
                (today,),
            )

        self.command("help")
        self.assertEqual(self.player()["tickets"], 15)
        self.assertEqual(self.player()["bought_tickets"], 2)
        self.assertEqual(self.player()["paid_tickets"], 2)
        self.assertEqual(self.player()["daily_ticket_refill"], 20)

        self.command("help")
        self.assertEqual(self.player()["tickets"], 15)
        self.assertEqual(self.player()["bought_tickets"], 2)

        self.at += 86400
        self.command("help")
        self.assertEqual(self.player()["tickets"], 22)
        self.assertEqual(self.player()["paid_tickets"], 2)
        self.assertEqual(self.player()["bought_tickets"], 0)

    def test_automatic_extract_settles_without_another_user_command(self):
        self.command("start")
        self.modify_run(lambda run: run.update(bag=[self.loot("r01")]))
        self.game.sweep(self.at + elevator.RUN_TIMEOUT - 1)
        self.assertTrue(self.game.has_run("101"))
        self.game.sweep(self.at + elevator.RUN_TIMEOUT)
        self.assertFalse(self.game.has_run("101"))
        self.assertEqual(self.stock(), {"r01": 1})
        self.at += elevator.RUN_TIMEOUT
        reply = self.command("help")
        self.assertIn("自动把你拉回了地面", reply.text)
        self.assertTrue(reply.notice_ids)
        self.game.acknowledge_notices("101", reply.notice_ids)
        self.assertFalse(self.command("help").notice_ids)

    def test_death_insurance_safe_storage_and_exclusive_relic_claim(self):
        self.command("start", user="101")
        with self.game.store.transaction() as db:
            turn = elevator.Turn(self.game, db, "101", "失物主人", "10", int(self.at))
            turn.run["bag"] = [self.loot("snack", 900)]
            first = turn.fallen()
            turn.persist()
            self.assertIn("保险", first)
            self.assertEqual(db.execute("SELECT count(*) FROM relics").fetchone()[0], 0)
            db.execute("UPDATE players SET runs=3 WHERE user_id='101'")
        self.command("start", user="101")
        with self.game.store.transaction() as db:
            turn = elevator.Turn(self.game, db, "101", "失物主人", "10", int(self.at))
            turn.run.update(bag=[self.loot("r02")], safe=[self.loot("g01")])
            turn.fallen()
            turn.persist()
            box_id = db.execute("SELECT box_id FROM relics").fetchone()[0]
        self.assertEqual(self.stock(), {"g01": 1, "snack": 1})
        self.command("start", user="102", group="20")
        with patch.object(elevator.Turn, "_event_roll", return_value="relic"):
            encountered = self.command("down", user="102", group="20")
        self.assertIn("遗物盒", encountered.text)
        self.assertNotIn("失物主人", encountered.text)
        self.command("start", user="103", group="20")
        self.modify_run(lambda run: run.update(event={"kind": "relic", "box_id": box_id}), user="103")
        at = self.at + 10
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = [pool.submit(self.game.execute, uid, uid, "20", "rob", now=at)
                             for uid in ("102", "103")]
            replies = first.result(), second.result()
        winners = [uid for uid, reply in zip(("102", "103"), replies) if "1 件遗物全塞进了临时背包" in reply.text]
        self.assertEqual(len(winners), 1)
        winner = winners[0]
        self.assertTrue(all(not reply.mention for reply in replies))
        self.assertNotIn("r02", self.stock(winner))
        self.at = at
        self.command("extract", user=winner, group="20")
        self.assertEqual(self.stock(winner)["r02"], 1)

    def test_sos_stays_in_current_group_and_increases_neither_helper_risk_nor_ticket_spend(self):
        self.command("start", user="101")
        specs = self.game.catalog.maps["mall"]
        enemy = elevator.combat.monster(specs, 2, self.game.rng, elite=True)
        enemy.update(hp=5)
        self.modify_run(lambda run: run.update(event={"kind": "combat", "enemy": enemy}))
        self.assertIn("3 分钟内", self.command("flare").text)
        self.assertTrue(self.game.has_signal("10", "201", self.at))
        self.assertIn("没有可支援", self.command("support", user="201", group="20").text)
        good = self.command("support", user="201", group="10")
        self.assertIn("支援盲盒已进仓库", good.text)
        self.assertEqual(self.player("201")["tickets"], 20)
        self.assertFalse(self.game.has_run("201"))
        self.assertEqual(self.stock("201"), {"crate_blue": 1})
        self.assertFalse(self.game.has_signal("10", "201", self.at))
        self.assertEqual(self.active_run()["event"]["kind"], "ready")

    def test_mall_can_extract_while_a_signal_is_waiting(self):
        self.command("start")
        encounter = elevator.combat.monster(self.game.catalog.maps["mall"], 5, self.game.rng, elite=True)
        self.modify_run(lambda run: run.update(floor=5, event={"kind": "combat", "enemy": encounter},
                                               bag=[self.loot("snack", 680)]))
        waiting = self.command("flare")
        self.assertIn("extract", [choice.action for choice in waiting.choices])
        self.assertTrue(self.game.has_signal("10", "102", self.at))
        result = self.command("extract")
        self.assertIn("第 5 层安全撤离", result.text)
        self.assertFalse(self.game.has_run("101"))
        self.assertFalse(self.game.has_signal("10", "102", self.at))
        self.assertEqual(self.stock(), {"snack": 1})
        self.assertEqual(self.player()["extracts"], 1)

    def test_high_depth_ambush_still_requires_dealing_with_the_gate(self):
        self.command("help")
        with self.game.store.transaction() as db:
            db.execute("UPDATE players SET gold=2000 WHERE user_id='101'")
        self.command("start", "深渊实验室")
        self.modify_run(lambda run: run.update(floor=5, event={"kind": "ready"},
                                               bag=[self.loot("smoke"), self.loot("snack")]))
        with patch.object(self.game.rng, "random", return_value=0):
            ambush = self.command("extract")
        self.assertIn("守门怪", ambush.text)
        self.assertNotIn("extract", [choice.action for choice in ambush.choices])
        still_blocked = self.command("extract")
        self.assertIn("电梯门被守门怪扒住了", still_blocked.text)
        gone = self.command("use", "烟雾弹")
        self.assertIn("安全撤离", gone.text)
        self.assertFalse(self.game.has_run("101"))
        self.assertEqual(self.stock(), {"snack": 1})

    def test_failed_black_market_exchange_rolls_back_materials_and_bringing_supplies_is_atomic(self):
        self.command("help")
        with self.game.store.transaction() as db:
            turn = elevator.Turn(self.game, db, "101", "玩家", "10", int(self.at))
            turn.deposit(self.loot("powerbank"), quantity=3)
            turn.deposit(self.loot("coffee"), quantity=1)
            turn.persist()
        requirements = next(recipe["requires"] for recipe in self.game.catalog.weekly_recipes("2026-47")
                            if recipe["output"] == "grenade")
        if "rgb" in requirements:
            self.assertIn("没有", self.command("craft", "手雷").text)
            self.assertEqual(self.stock(), {"coffee": 1, "powerbank": 3})
        self.assertIn("没有 2 件", self.command("start", "废弃商场 | 冰美式 2").text)
        self.assertEqual(self.player()["tickets"], 20)
        self.assertEqual(self.stock()["coffee"], 1)
        self.command("start", "废弃商场 | 冰美式")
        self.assertTrue(any(item["id"] == "coffee" for item in self.active_run()["bag"]))
        self.modify_run(lambda run: run.update(hp=25))
        restored = round(self.active_run()["stats"]["hp"] * .40)
        self.assertIn(f"回血 {restored} HP", self.command("use").text)
        self.assertEqual(self.active_run()["hp"], 25 + restored)

    def test_month_end_is_idempotent_and_does_not_reset_assets(self):
        self.command("start")
        self.command("extract")
        previous_gold = self.player()["gold"]
        next_month = datetime(2026, 12, 1, 0, 1, tzinfo=elevator.SHANGHAI).timestamp()
        self.game.sweep(next_month)
        self.game.sweep(next_month + 31)
        with self.game.store.read() as db:
            count = db.execute("SELECT count(*) FROM season_titles WHERE title='深渊开拓王'").fetchone()[0]
            self.assertEqual(count, 1)
        self.assertEqual(self.player()["gold"], previous_gold)

    def test_depth_ranking_preserves_a_shallower_actual_extraction(self):
        self.command("start")
        self.modify_run(lambda run: run.update(floor=2, bag=[self.loot("snack", 1040)]))
        self.command("extract")
        self.command("start")
        self.modify_run(lambda run: run.update(floor=6, event={"kind": "ready"}))
        with patch.object(elevator.Turn, "_event_roll", return_value="loot"):
            self.command("down")
        self.command("start", user="102")
        with patch.object(elevator.Turn, "_event_roll", return_value="loot"):
            self.command("down", user="102")
        board = self.command("rank", "商场")
        self.assertIn("到达 7 层 · 最高撤离 2 层，带出 1,040 金", board.text)
        self.assertIn("到达 2 层 · 探险中，带出待结算", board.text)
        self.assertNotIn("带出 0 金", board.text)
        self.command("start", user="103")
        self.modify_run(lambda run: run.update(bag=[], safe=[]), user="103")
        self.command("extract", user="103")
        self.assertIn("最高撤离 1 层，带出 0 金", self.command("rank", "商场").text)

    def test_rank_overview_shows_map_and_stat_boards_without_old_total_depth(self):
        self.command("start", "商场")
        self.modify_run(lambda run: run.update(floor=4, bag=[self.loot("snack", 600)]))
        self.command("extract")
        with self.game.store.transaction() as db:
            db.execute("UPDATE players SET extracts=? WHERE user_id='101'",
                       (self.game.catalog.maps["subway"]["extracts"],))
        self.command("start", "地铁")
        self.modify_run(lambda run: run.update(floor=6, bag=[self.loot("snack", 900)]))
        self.command("extract")
        self.command("start", "商场", user="102")
        self.modify_run(lambda run: run.update(floor=4, bag=[self.loot("snack", 800)]), user="102")
        self.command("extract", user="102")
        self.command("start", "商场", user="103", group="20")
        self.modify_run(lambda run: run.update(floor=5, bag=[]), user="103")
        self.command("extract", user="103", group="20")

        self.command("help", user="104")
        with self.game.store.transaction() as db:
            db.execute("UPDATE season_stats SET max_floor=100,floor_value=20000 WHERE user_id='104'")
            db.execute("UPDATE season_stats SET collection_count=2,rob_value=600,rescues=3,death_value=800 "
                       "WHERE user_id='102'")
        overview = self.command("rank")
        arguments = ["mall", "subway", "abyss", "auction", "tide", "zenith", "收藏", "老六", "雷锋", "倒霉", ""]
        self.assertEqual([choice.argument for choice in overview.choices], arguments)
        for heading in ("废弃商场", "赛博旧地铁", "深渊实验室", "午夜地下拍卖行", "潮汐动力站", "失重天文台", "收藏榜", "老六榜", "雷锋榜", "倒霉榜"):
            self.assertIn(f"【{heading}】", overview.text)
        for score in ("2 件", "600 金", "3 次", "800 金"):
            self.assertIn(f"玩家102 · {score}", overview.text)
        self.assertNotIn("玩家104", overview.text)
        self.assertEqual(overview.choices[-1].action, "all_rank")
        mall = self.command("rank", "探索 商场")
        self.assertIn("本群探索榜 · 废弃商场", mall.text)
        self.assertLess(mall.text.index("玩家102"), mall.text.index("玩家101"))
        self.assertNotIn("玩家103", mall.text)
        self.assertIn("玩家103 · 到达 5 层", self.command("all_rank", "mall").text)
        subway = self.command("rank", "地铁")
        self.assertIn("玩家101 · 到达 6 层", subway.text)
        self.assertNotIn("玩家102", subway.text)
        obsolete = self.command("all_rank", "总深度")
        self.assertIn("全服月榜 · 各榜前三", obsolete.text)
        self.assertNotIn("玩家104", obsolete.text)
        self.assertNotIn("总深度", obsolete.text)

    def test_new_map_schema_preserves_running_expedition_and_assets(self):
        self.command("start")
        self.modify_run(lambda run: run.update(hp=71, floor=3, bag=[self.loot("snack", 660)]))
        running, player, stock = self.active_run(), self.player(), self.stock()
        with self.game.store.transaction() as db:
            db.execute("DROP TABLE season_map_stats")
            old_rank = tuple(db.execute("SELECT * FROM season_stats WHERE user_id='101'").fetchone())
        upgraded = elevator.ElevatorGame(self.game.store.path)
        with upgraded.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM season_map_stats").fetchone()[0], 0)
            self.assertEqual(tuple(db.execute("SELECT * FROM season_stats WHERE user_id='101'").fetchone()), old_rank)
        self.assertEqual(self.active_run(), running)
        self.assertEqual(self.player(), player)
        self.assertEqual(self.stock(), stock)

    def test_old_season_rows_keep_known_values_and_do_not_invent_extractions(self):
        database = self.game.store.path.parent / "old.sqlite3"
        with closing(sqlite3.connect(database)) as conn:
            conn.executescript("""
                CREATE TABLE season_stats (
                    month TEXT NOT NULL, user_id TEXT NOT NULL,
                    max_floor INTEGER NOT NULL DEFAULT 0, floor_value INTEGER NOT NULL DEFAULT 0,
                    collection_count INTEGER NOT NULL DEFAULT 0, rob_value INTEGER NOT NULL DEFAULT 0,
                    rescues INTEGER NOT NULL DEFAULT 0, death_value INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (month, user_id)
                );
                INSERT INTO season_stats(month,user_id,max_floor,floor_value)
                VALUES ('2026-11','101',2,1040),('2026-11','102',7,0);
            """)
        upgraded = elevator.ElevatorGame(database)
        with upgraded.store.read() as conn:
            rows = conn.execute("SELECT best_extract_floor,best_extract_value FROM season_stats ORDER BY user_id").fetchall()
            self.assertFalse(conn.execute("SELECT 1 FROM season_map_stats").fetchone())
        self.assertEqual([tuple(row) for row in rows], [(2, 1040), (0, 0)])

    def test_catalog_has_core_content_and_gallery_card_is_a_readable_png(self):
        self.assertEqual(len(self.game.catalog.collections), 48)
        self.assertEqual(len(self.game.catalog.affixes), 20)
        self.assertGreaterEqual(len(self.game.catalog.events), 15)
        reply = self.command("gallery")
        rainbow_names = [item["name"] for item in self.game.catalog.rainbow.values()]
        for lines in (reply.card_lines, reply.text.splitlines()):
            self.assertTrue(lines[-5].startswith("【虹彩套装】虹彩共鸣 0/4"))
            self.assertCountEqual([line[2:] for line in lines[-4:]], rainbow_names)
            self.assertFalse(any("虹彩共鸣" in line for line in lines[:-5]))
        ordinary = next(name for name in self.game.catalog.sets if name != "虹彩共鸣")
        self.assertNotIn("【虹彩套装】", self.command("gallery", ordinary).text)
        try:
            image = cards.render_card(reply.card, reply.card_lines)
        except OSError as error:
            self.skipTest(str(error))
        with Image.open(io.BytesIO(image)) as card:
            self.assertEqual(card.format, "PNG")
            self.assertEqual(card.width, 800)
            self.assertGreater(card.height, 1000)

    def test_item_encyclopedia_and_exact_lookup_request_an_image(self):
        for category in ("道具", "词条", "装备"):
            with self.subTest(category=category):
                reply = self.command("help", category)
                self.assertTrue(reply.as_image)
                self.assertTrue(reply.choices)
        self.assertTrue(self.command("lookup", "装备盲盒").as_image)
        matches = self.command("lookup", "盲盒")
        self.assertTrue(matches.as_image)
        self.assertGreater(len(matches.choices), 1)
        self.assertTrue(self.command("help", "攻略").as_image)

    def test_item_lists_including_shops_and_inventory_request_images(self):
        for action in ("shop", "buy", "black_market", "craft", "warehouse", "equipment"):
            with self.subTest(action=action):
                self.assertTrue(self.command(action).as_image)
        self.command("start")
        self.assertTrue(self.command("bag").as_image)

    def test_black_market_navigation_preserves_recipe_numbers(self):
        first = self.command("black_market")
        next_page = first.choices[-1]
        self.assertEqual(next_page, elevator.Choice("下一页", "black_market", "2"))
        second = self.command(next_page.action, next_page.argument)
        self.assertTrue(second.as_image)
        self.assertIn("第 2/2 页", second.text)
        self.assertIn("\n6. ", second.text)
        self.assertNotIn("\n1. ", second.text)
        previous = second.choices[-1]
        self.assertEqual(previous, elevator.Choice("上一页", "black_market", "1"))
        self.assertEqual(self.command(previous.action, previous.argument).message(), first.message())

    def test_catalog_quotes_turn_pages_without_losing_the_category(self):
        first = self.command("help", "道具")
        env = {"Choice": elevator.Choice,"re":re}
        load_handlers("plugins/endless_elevator/__init__.py", {"PendingMenu", "Route", "menu_choice","_parse_menu_input","_MENU_NUM_RE","_MENU_EXTRA_RE"}, env)
        pending = env["PendingMenu"]("10", "123", 60, first.choices, first.plain_numbers)
        route = env["menu_choice"](str(len(first.choices)), "10", "123", pending, 10)
        self.assertEqual(route.argument, "道具 页2")
        second = self.command(route.command, route.argument)
        self.assertTrue(second.as_image)
        self.assertIn("【黑市专属（材料以物易物）】", second.text)
        self.assertNotIn("【商店常驻（金币直购）】", second.text)
        self.assertEqual(second.choices[-2].argument, "道具 页1")


class MenuTests(unittest.TestCase):
    def test_item_picture_does_not_repeat_notices_or_choices_as_text(self):
        env = {"Reply": elevator.Reply, "GroupMessageEvent": SimpleNamespace,
               "MessageSegment": MessageSegment}
        load_handlers("plugins/endless_elevator/__init__.py", {"_format_reply"}, env)
        reply = elevator.Reply(
            "📮 通知\n🔍 道具原文",
            (elevator.Choice("选项", "help", "道具"),),
            notice_ids=(5,), as_image=True,
        )
        event = SimpleNamespace(message_id=123)
        outgoing = env["_format_reply"](event, reply, b"rendered PNG")
        self.assertEqual([segment.type for segment in outgoing], ["reply", "image"])
        self.assertEqual(env["_format_reply"](event, reply, None).extract_plain_text(), reply.message())

    def test_naked_digits_are_scoped_and_expire_but_valid_quote_survives(self):
        env = {"Choice": elevator.Choice,"re":re}
        load_handlers("plugins/endless_elevator/__init__.py", {"PendingMenu", "Route", "menu_choice","_parse_menu_input","_MENU_NUM_RE","_MENU_EXTRA_RE"}, env)
        pending = env["PendingMenu"]("111", "9001", 60, (elevator.Choice("撤离", "extract"),), True)
        pick = env["menu_choice"]
        self.assertIsNone(pick("1", "111", None, None, 10))
        self.assertIsNone(pick("1", "222", None, pending, 10))
        self.assertIsNone(pick("1", "111", None, pending, 61))
        self.assertIsNone(pick("1", "111", "1234", pending, 10))
        self.assertEqual(pick("1", "111", None, pending, 10).command, "extract")
        self.assertEqual(pick("1", "111", "9001", pending, 400).command, "extract")
        shop = env["PendingMenu"]("111", "9002", 60, (elevator.Choice("购买", "buy", "ticket"),), False)
        self.assertIsNone(pick("1", "111", None, shop, 10))
        self.assertEqual(pick("1", "111", "9002", shop, 10).argument, "ticket")

    def test_stale_battle_menu_reply_is_claimed_by_game_instead_of_chat(self):
        env = {
            "Choice": elevator.Choice, "GroupMessageEvent": SimpleNamespace, "T_State": dict,
            "time": SimpleNamespace(monotonic=lambda: 113.0), "_group_allowed": lambda _: True,
            "_menus": {}, "_menu_refs": {}, "_menu_history":{}, "re":re,
        }
        load_handlers("plugins/endless_elevator/__init__.py", {
            "PendingMenu", "Route", "menu_choice", "_quote_id", "_menu_ref", "_is_old_game_menu",
            "_parse_menu_input","_MENU_NUM_RE","_MENU_EXTRA_RE","_REPEATABLE_MENU_ACTIONS", "_scope",
            "_number_route", "_command_text", "_matches_quoted_menu",
        }, env)
        env["_menus"]["101"] = env["PendingMenu"](
            "111", "-82", 120, (elevator.Choice("硬刚（打）", "fight"),), True,
        )
        body = Message(MessageSegment.text(
            "🛗 废弃商场 · 第 5 层 · HP 100/100\n暴走保安队长\n👉 1.硬刚（打） ｜ 2.摇人",
        ))
        menu = SimpleNamespace(message_type="group", group_id=111, sender=SimpleNamespace(user_id=999), message=body)

        def choose(quote, *, reply=menu, user_id=101):
            original = Message(MessageSegment.text("1"))
            if quote is not None:
                original.insert(0, MessageSegment.reply(quote))
            event = SimpleNamespace(user_id=user_id, self_id=999, group_id=111, reply=reply,
                                    get_plaintext=lambda: "1", original_message=original)
            state = {}
            return state["elevator_route"] if env["_matches_quoted_menu"](event, state) else None

        self.assertEqual(choose(-82).command, "fight")
        self.assertEqual(choose(1200224703).command, "refresh_menu")
        self.assertIsNone(choose(None))
        env["_menu_refs"][("999", "111", "437")] = ("992", 140)
        self.assertEqual(choose(437).command, "foreign_menu")
        ordinary_help = SimpleNamespace(**{**vars(menu), "message": Message("功能总览\n1. 做题\n2. 生图")})
        self.assertIsNone(choose(436, reply=ordinary_help))
        other_group = SimpleNamespace(**{**vars(menu), "group_id": 222})
        self.assertIsNone(choose(436, reply=other_group))


class PrivateElevatorMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_menu_uses_shared_save_and_its_own_numbered_choices(self):
        area = tempfile.TemporaryDirectory()
        self.addCleanup(area.cleanup)
        game = elevator.ElevatorGame(Path(area.name) / "game.sqlite3", rng=Random(17))
        game.execute("101", "玩家", "321", "pursue", "1 8")
        game.execute("101", "玩家", "321", "rename", "熟悉的乘客")
        with game.store.transaction() as db:
            db.execute("UPDATE players SET runs=1 WHERE user_id='101'")
            db.execute("INSERT INTO inventory(user_id,item_id,quality,value,quantity) "
                       "VALUES('101','coffee','',300,1)")
        env = {
            "game": game, "Choice": elevator.Choice, "Reply": elevator.Reply, "re": re,
            "GroupMessageEvent": SimpleNamespace, "PrivateMessageEvent": PrivateMessageEvent,
            "MessageSegment": MessageSegment, "Awaitable": Awaitable, "Callable": Callable,
            "time": SimpleNamespace(monotonic=lambda: 10.0), "asyncio": asyncio,
            "_menus": {}, "_menu_refs": {}, "_menu_history": {}, "_locks": {},
            "GameMaintenance": RuntimeError, "sqlite3": sqlite3, "RUN_TIMEOUT": elevator.RUN_TIMEOUT,
        }
        load_handlers("plugins/endless_elevator/__init__.py", {
            "PendingMenu", "Route", "_quote_id", "_menu_ref", "_scope", "_is_old_game_menu",
            "_command_text", "_looks_like_loadout", "_parse_menu_input", "menu_choice", "_number_route",
            "_known_lookup", "private_candidate", "_route", "_format_reply", "handle_private_event",
            "_handle_route", "_MENU_NUM_RE", "_MENU_EXTRA_RE", "_LOADOUT_KEYWORDS", "_ACTIONS",
            "_ORDER_CMD_RE", "_EVENT_WORDS", "_PREFIX_ONLY", "_HELP_TOPICS", "_REQUIRES_RUN",
            "_SUPPORT_ALIAS", "_DEFAULT_ARGUMENTS", "_MUTATING_COMMANDS", "_REPEATABLE_MENU_ACTIONS",
        }, env)

        def event(text, *, quote=None):
            message = Message(text)
            if quote:
                message.insert(0, MessageSegment.reply(quote))
            return PrivateMessageEvent(
                time=1, self_id=999, post_type="message", message_type="private", sub_type="friend",
                message_id=700, user_id=101, message=message, original_message=message,
                raw_message=text, font=0, sender={"user_id":101, "nickname":"玩家"},
            )

        group_choice = env["PendingMenu"]("321", "300", 60, (elevator.Choice("撤离", "extract"),), True)
        env["_menus"]["101"] = group_choice
        dispatch = env["handle_private_event"]
        accept = env["private_candidate"]
        bot = SimpleNamespace()
        send = AsyncMock(side_effect=[{"message_id": 301}, {"message_id": 302}, {"message_id": 303}])
        self.assertFalse(accept(event("做题 1+1")))
        self.assertFalse(accept(event("帮助文档")))
        self.assertFalse(accept(event("1")))
        self.assertFalse(accept(event("1", quote=300)))
        temporary = SimpleNamespace(message_type="private", user_id=101, group_id=321)
        self.assertEqual(env["_scope"](temporary), ("private:101", "private:101"))
        self.assertTrue(await dispatch(bot, event("电梯"), send))
        self.assertIn("熟悉的乘客", send.await_args.args[0].extract_plain_text())
        self.assertTrue(await dispatch(bot, event("开局"), send))
        self.assertFalse(await dispatch(bot, event("1", quote=300), send))
        self.assertTrue(await dispatch(bot, event("1", quote=302), send))
        self.assertEqual(send.await_count, 3)
        self.assertIs(env["_menus"]["101"], group_choice)
        with game.store.read() as db:
            private_run = db.execute("SELECT group_id, payload FROM active_runs WHERE user_id='101'").fetchone()
            self.assertEqual(private_run["group_id"], "private:101")
        self.assertIn("F1", game.execute("101", "玩家", "321", "help").text)

    def test_private_session_cannot_launch_group_actions_even_inside_an_order(self):
        area = tempfile.TemporaryDirectory()
        self.addCleanup(area.cleanup)
        game = elevator.ElevatorGame(Path(area.name) / "game.sqlite3", rng=Random(17))
        game.execute("101", "玩家", "321", "rename", "乘客")
        group_order = game.create_order(
            "101", "乘客", "321", "求援",
            [{"action":"flare", "argument":"", "note":"发信号"}],
        )
        reply = game.execute("101", "乘客", "private:101", "confirm_order", group_order["order_id"])
        self.assertIn("已开放电梯的群", reply.text)
        self.assertIn("全服", game.execute("101", "乘客", "private:101", "rank").text)
        with game.store.read() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM signals").fetchone())
