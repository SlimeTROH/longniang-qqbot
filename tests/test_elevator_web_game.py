"""Browser actions, QQ commands and notifications share transactional state."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from elevator_web_game import WebGame, rules


AT = 1795430012


class GameStateTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "game.sqlite3"
        self.web = WebGame(self.path)
        self.now = AT
        field_rng = self.enterContext(patch.object(rules.expedition, "Random")).return_value
        field_rng.choices.return_value = ["field_wrap"]
        field_rng.random.return_value = 1.0
        field_rng.randrange.return_value = 0

    def state(self, uid="101", group="110"):
        return self.web.state(uid, group, "群友甲", now=self.now)

    def act(self, name, argument="", view=None):
        view = view or self.state()
        return self.web.act("101", "110", name, argument, view["revision"], "群友甲", now=self.now)

    def command(self, name, params=None, view=None):
        view = view or self.state()
        return self.web.command("101", "110", name, params or {}, view["revision"], "群友甲", now=self.now)

    def test_qq_and_browser_immediately_see_each_others_actions(self):
        qq = rules.ElevatorGame(self.path)
        initial = self.state()
        qq.execute("101", "QQ 群友", "110", "rename", "我的游戏名", now=self.now)
        stale = self.act("start", "mall", initial)
        self.assertEqual(stale["code"], "stale")
        self.assertIsNone(stale["state"]["run"])
        self.assertEqual(stale["state"]["player"]["nickname"], "我的游戏名")
        run = self.act("start", "mall", stale["state"])
        self.assertEqual(run["state"]["run"]["floor"], 1)
        self.assertTrue(qq.has_run("101"))
        self.assertEqual(run["state"]["player"]["tickets"], 19)
        self.now += 4
        qq.execute("101", "QQ 群友", "110", "down", now=self.now)
        stale = self.act("extract", view=run["state"])
        self.assertEqual(stale["code"], "stale")
        self.assertEqual(stale["state"]["run"]["floor"], 2)
        self.assertIsNone(self.act("extract")["state"]["run"])

    def test_poll_and_view_changes_never_renew_run_expiry(self):
        self.act("rename", "__keep_start__")
        self.assertEqual(self.state()["run"]["expires_at"], AT + rules.RUN_TIMEOUT)
        self.now += 120
        revision = self.state()["revision"]
        self.assertEqual(self.act("bag")["state"]["revision"], revision)
        self.assertEqual(self.state()["run"]["expires_at"], AT + rules.RUN_TIMEOUT)
        self.now = AT + rules.RUN_TIMEOUT + 1
        final = self.state()
        self.assertIsNone(final["run"])
        self.assertTrue(final["warehouse"] or final["equipment"])
        self.assertTrue(any("自动" in row["text"] for row in final["notices"]))

    def test_rank_navigation_keeps_active_run_and_does_not_apply_chat_cooldown(self):
        initial = self.command("start", {"map_id": "mall"})["state"]
        menu = self.act("rank", view=initial)
        self.assertIn("本群月榜 · 各榜前三", menu["reply"]["text"])
        self.assertEqual([choice["argument"] for choice in menu["reply"]["choices"]],
                         ["mall", "subway", "abyss", "auction", "tide", "zenith", "收藏", "老六", "雷锋", "倒霉", ""])
        for action, map_id, phrase in (
            ("rank", "mall", "本群探索榜 · 废弃商场"),
            ("rank", "subway", "本群探索榜 · 赛博旧地铁"),
            ("rank", "mall", "本群探索榜 · 废弃商场"),
            ("all_rank", "mall", "全服探索榜 · 废弃商场"),
        ):
            response = self.act(action, map_id)
            self.assertIsNone(response["code"])
            self.assertIn(phrase, response["reply"]["text"])
            self.assertEqual(response["state"]["run"], initial["run"])

    def test_double_submission_charges_once_even_across_threads(self):
        self.act("rename", "__keep_lobby__")
        view = self.state()
        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(self.act, "buy", "coffee", view)
            second = workers.submit(self.act, "buy", "coffee", view)
            statuses = {first.result()["code"], second.result()["code"]}
        self.assertEqual(statuses, {None, "stale"})
        final = self.state()
        self.assertEqual(final["player"]["gold"], 200)
        self.assertEqual(final["warehouse"][0]["quantity"], 1)

    def test_callbacks_share_the_transaction_and_report_only_committed_state(self):
        class Callback:
            def before(self, db, uid, gid, action):
                return db.execute("SELECT gold FROM players WHERE user_id=?", (uid,)).fetchone()[0]

            def after(self, db, uid, gid, action, before):
                db.execute("CREATE TABLE IF NOT EXISTS web_probe (action TEXT,before INTEGER,after INTEGER)")
                after = db.execute("SELECT gold FROM players WHERE user_id=?", (uid,)).fetchone()[0]
                db.execute("INSERT INTO web_probe VALUES(?,?,?)", (action, before, after))

        self.web.on_action = Callback()
        initial = self.state()
        self.assertEqual(self.act("buy", "coffee 4", initial)["code"], "invalid")
        self.assertIsNone(self.act("buy", "coffee", initial)["code"])
        self.assertEqual(self.act("buy", "coffee", initial)["code"], "stale")
        self.assertEqual(self.act("buy", "coffee")["code"], "cooldown")
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT * FROM web_probe").fetchall(), [("buy", 500, 200)])

    def test_precombat_choice_exposes_labels_without_internal_random_state(self):
        self.act("rename", "__keep_start__")
        with self.web.game.store.transaction() as db:
            turn = rules.Turn(self.web.game, db, "101", "群友甲", "110", self.now)
            identifier, event = next((k, v) for k, v in turn.catalog.events.items()
                                     if any("guess" in o for o in v["choices"]))
            turn.run["event"] = {"kind": "choice", "id": identifier, "secret": 42109}
            turn.persist()
        snapshot = self.state()
        shown = snapshot["run"]["event"]
        self.assertEqual(shown["name"], event["name"])
        self.assertTrue(any(a["action"] == "event" for a in snapshot["actions"]))
        self.assertNotIn("secret", shown)
        self.assertNotIn("field_seed", snapshot["run"])
        with self.assertRaises(ValueError):
            self.act("create_order", view=snapshot)

    def test_current_floor_pickups_survive_refresh_and_clear_on_next_floor(self):
        first = self.act("rename", "__keep_start__")["state"]["run"]
        self.assertEqual(first["floor_result"]["title"], "打开宝箱")
        self.assertEqual(first["floor_result"]["loot"][0]["name"], first["bag"][0]["name"])
        self.assertEqual(first["floor_result"]["field"][0]["name"], first["field"][0]["name"])
        self.assertEqual(self.state()["run"]["floor_result"], first["floor_result"])
        self.now += 4
        with self.web.game.store.transaction() as db:
            turn = rules.Turn(self.web.game, db, "101", "群友甲", "110", self.now)
            turn.run["peek"] = "normal"
            turn.persist()
        second = self.act("down")["state"]["run"]
        self.assertEqual(second["event"]["kind"], "combat")
        self.assertEqual(second["floor_result"]["floor"], 2)
        self.assertEqual(second["floor_result"]["loot"], [])
        self.assertTrue(second["bag"])
        with self.web.game.store.transaction() as db:
            turn = rules.Turn(self.web.game, db, "101", "群友甲", "110", self.now)
            turn.run["event"]["enemy"]["hp"] = 0
            turn._enemy_win(turn.run["event"]["enemy"])
            turn.persist()
        result = self.state()["run"]["floor_result"]
        self.assertEqual(result["floor"], 2)
        self.assertEqual(len(result["loot"]), 1)

    def test_typed_first_launch_returns_pickups_and_keeps_onboarding_atomic(self):
        initial = self.state()
        bad = self.command("start", {"map_id": "mall", "nickname": "探员",
                                     "supplies": [{"id": "coffee", "quantity": 1}]}, initial)
        self.assertEqual(bad["code"], "invalid")
        self.assertIsNone(bad["state"]["run"])
        self.assertFalse(self.state()["player"]["name_confirmed"])
        mission = self.command("start", {"map_id": "mall", "nickname": "探员"})
        self.assertIsNone(mission["error"])
        self.assertEqual(mission["state"]["player"]["nickname"], "探员")
        self.assertEqual(mission["result"]["floor"], 1)
        self.assertEqual(mission["result"]["pickups"], mission["state"]["run"]["floor_result"])
        self.assertTrue(mission["result"]["pickups"]["loot"])
        self.assertTrue(mission["result"]["pickups"]["field"])
        self.assertEqual(mission["state"]["controls"][0], {"label": "深入一层", "command": "down", "params": {}})
        duplicate = self.command("start", {"map_id": "mall"}, initial)
        self.assertEqual(duplicate["code"], "stale")
        self.assertEqual(duplicate["state"]["player"]["tickets"], 19)

    def test_typed_fast_navigation_and_exact_inventory_target(self):
        self.assertIsNone(self.command("buy", {"id": "coffee"})["error"])
        launch = self.command("start", {"map_id": "mall", "supplies": [{"id": "coffee", "quantity": 1}]})
        self.assertEqual(launch["state"]["run"]["bag"][0]["id"], "coffee")
        loot = launch["state"]["run"]["bag"][1]
        locked = self.command("save_box", {"index": loot["index"]})
        self.assertIsNone(locked["error"])
        self.assertEqual(locked["state"]["run"]["safe"][0]["id"], loot["id"])
        with self.assertRaises(ValueError):
            self.command("use", {"index": True})
        for floor in (2, 3):
            with self.web.game.store.transaction() as db:
                turn = rules.Turn(self.web.game, db, "101", "群友甲", "110", self.now)
                turn.run["peek"] = "loot"
                turn.persist()
            move = self.command("down")
            self.assertEqual(move["result"]["floor"], floor)
        departure = self.command("extract")["result"]
        self.assertEqual(departure["outcome"], "extracted")
        self.assertGreater(departure["departure"]["value"], 0)

    def test_ticket_offer_reports_the_shared_purchase_quota(self):
        def offer(state):
            return next(item for item in state["shop"] if item["id"] == "ticket")

        initial = self.state()
        self.assertEqual(initial["player"]["tickets"], 20)
        self.assertEqual(initial["player"]["paid_tickets"], 0)
        self.assertEqual(initial["player"]["paid_ticket_cap"], 20)
        self.assertEqual(offer(initial)["max_quantity"], 20)
        with self.web.game.store.transaction() as db:
            db.execute("UPDATE players SET gold=10000 WHERE user_id='101'")

        purchased = self.command("buy", {"id": "ticket", "quantity": 20})
        self.assertIsNone(purchased["error"])
        self.assertEqual(purchased["state"]["player"]["tickets"], 40)
        self.assertEqual(purchased["state"]["player"]["paid_tickets"], 20)
        self.assertEqual(offer(purchased["state"])["max_quantity"], 0)

        self.now += 4
        self.command("start", {"map_id": "mall"})
        self.command("extract")
        available_space = self.state()
        self.assertEqual(available_space["player"]["tickets"], 39)
        self.assertEqual(offer(available_space)["max_quantity"], 0)
        refused = self.command("buy", {"id": "ticket", "quantity": 1}, available_space)
        self.assertEqual(refused["code"], "invalid")

        self.now += 86400
        self.command("start", {"map_id": "mall"})
        self.command("extract")
        carried_paid_tickets = self.state()
        self.assertEqual(carried_paid_tickets["player"]["tickets"], 39)
        self.assertEqual(carried_paid_tickets["player"]["paid_tickets"], 20)
        self.assertEqual(offer(carried_paid_tickets)["max_quantity"], 0)

    def test_fight_shows_rounds_and_only_items_awarded_by_this_fight(self):
        self.command("start", {"map_id": "mall"})
        with self.web.game.store.transaction() as db:
            turn = rules.Turn(self.web.game, db, "101", "群友甲", "110", self.now)
            enemy = rules.combat.monster(turn.catalog.maps["mall"], 1, turn.rng)
            enemy.update(hp=1, max_hp=1, atk=1, agi=0)
            turn.run["event"] = {"kind": "combat", "enemy": enemy}
            turn.persist()
        result = self.command("fight")
        battle = result["result"]["battle"]
        self.assertEqual(battle["outcome"], "victory")
        self.assertEqual(len(battle["log"]), battle["rounds"])
        self.assertEqual(len(battle["loot"]), 1)
        self.assertEqual(self.state()["run"]["floor_result"]["battle"], battle)
        self.assertGreater(len(result["state"]["run"]["floor_result"]["loot"]), len(battle["loot"]))

    def test_adventure_explains_reward_after_a_jump_and_survives_refresh(self):
        self.command("start", {"map_id": "mall"})
        self.web.game.catalog.events["web_receipt_check"] = {
            "name": "藏箱直降梯", "text": "栏杆上挂着一杯饮料。",
            "choices": [{"label": "带走饮料，乘直降梯", "skip_floors": 2, "items": ["coffee"], "heal": .4}],
        }
        with self.web.game.store.transaction() as db:
            turn = rules.Turn(self.web.game, db, "101", "群友甲", "110", self.now)
            turn.run.update(event={"kind": "choice", "id": "web_receipt_check"}, peek="loot", hp=40)
            turn.persist()
        choice = self.command("event", {"option": 1})
        receipt = choice["result"]["adventure"]
        self.assertEqual((receipt["floor"], receipt["destination"]), (1, 3))
        self.assertEqual(receipt["outcome"], "success")
        self.assertEqual([item["id"] for item in receipt["loot"]], ["coffee"])
        self.assertTrue(any("恢复" in line for line in receipt["lines"]))
        self.assertEqual(self.state()["run"]["adventure"], receipt)
        self.assertIsNone(self.command("down")["state"]["run"]["adventure"])

    def test_full_safe_box_supports_exact_swap_and_taking_items_back(self):
        self.command("start", {"map_id": "mall"})
        with self.web.game.store.transaction() as db:
            turn = rules.Turn(self.web.game, db, "101", "群友甲", "110", self.now)
            turn.run["safe"] = [turn.catalog.ordinary_item("ice_tea", turn.rng)]
            turn.run["bag"] = [turn.catalog.ordinary_item("coffee", turn.rng)]
            turn.persist()
        full = self.state()
        self.assertEqual(full["run"]["safe"][0]["actions"][0]["control"],
                         {"command": "take_box", "params": {"safe_index": 1}})
        swapped = self.command("swap_box", {"index": 1, "safe_index": 1}, full)["state"]
        self.assertEqual([item["id"] for item in swapped["run"]["safe"]], ["coffee"])
        self.assertEqual([item["id"] for item in swapped["run"]["bag"]], ["ice_tea"])
        self.assertEqual(self.command("take_box", {"safe_index": 1}, full)["code"], "stale")
        emptied = self.command("take_box", {"safe_index": 1})["state"]
        self.assertEqual(emptied["run"]["safe"], [])
        self.assertEqual([item["id"] for item in emptied["run"]["bag"]], ["ice_tea", "coffee"])
        stocked = self.command("save_box", {"index": 2})["state"]
        self.assertEqual([item["id"] for item in stocked["run"]["safe"]], ["coffee"])


    def test_field_equipment_snapshot_updates_effective_stats_and_status(self):
        self.command("start", {"map_id": "mall"})
        field_id = "field_apron"
        with self.web.game.store.transaction() as db:
            turn = rules.Turn(self.web.game, db, "101", "群友甲", "110", self.now)
            turn.run["field_items"] = {field_id: 1}
            turn.run.pop("field_offer", None)
            turn.persist()
        before = self.state()["run"]
        self.assertEqual(before["effective_stats"], before["stats"])
        self.assertEqual(before["field"][0]["actions"][0]["label"], "装备到槽 1")

        worn = self.command("field_use", {"id": field_id})["state"]["run"]
        self.assertEqual(worn["stats"], before["stats"])
        self.assertEqual(worn["effective_stats"]["def"], round(before["stats"]["def"] * 1.3, 2))
        self.assertEqual(worn["effective_stats"]["agi"], max(0, before["stats"]["agi"] - 3))
        self.assertEqual(worn["field_slots"][0]["id"], field_id)
        self.assertIn("防御+30%", worn["field_slots"][0]["description"])
        self.assertEqual(self.state()["run"]["effective_stats"], worn["effective_stats"])

        removed = self.command("field_remove", {"id": field_id})["state"]["run"]
        self.assertEqual(removed["field_slots"], [None, None])
        self.assertEqual(removed["effective_stats"], before["effective_stats"])

    def test_wear_pickup_pauses_exploration_and_resolves_in_shared_state(self):
        apron, lamp = "field_apron", "field_lamp"

        def find(command, params, item_id):
            with patch.object(rules.expedition, "Random") as random:
                random.return_value.choices.return_value = [item_id]
                random.return_value.random.return_value = 0
                return self.command(command, params)["state"]

        first = find("start", {"map_id": "mall"}, apron)
        first_run = first["run"]
        pending = first_run["field_offer"]
        self.assertEqual(first_run["field_slots"], [None, None])
        self.assertEqual(first_run["field"], [])
        self.assertEqual(first_run["effective_stats"], first_run["stats"])
        self.assertEqual(self.state()["run"]["field_offer"], pending)
        self.assertEqual(first["controls"], [])
        self.assertEqual(self.command("down", view=first)["code"], "invalid")
        self.assertEqual([choice["control"]["params"]["slot"] for choice in pending["choices"]], [1, 0])

        qq = self.web.game
        menu = qq.execute("101", "群友甲", "110", "field", now=self.now)
        self.assertEqual(menu.choices[0].action, "field_pick")
        qq.execute("101", "群友甲", "110", menu.choices[0].action, menu.choices[0].argument, now=self.now)
        self.assertIsNone(self.state()["run"]["field_offer"])

        with qq.store.transaction() as db:
            turn = rules.Turn(qq, db, "101", "群友甲", "110", self.now)
            turn.run["peek"] = "loot"
            turn.persist()
        second = find("rush", {"floors": 3}, apron)
        self.assertEqual(second["run"]["floor"], 2)
        self.assertEqual(second["run"]["field_offer"]["choices"][0]["control"]["params"]["slot"], 2)
        worn = self.command("field_pick", {"id": apron, "floor": 2, "slot": 2}, second)["state"]["run"]
        self.assertEqual([item["id"] for item in worn["field_slots"]], [apron, apron])
        self.assertEqual(worn["effective_stats"]["def"], round(first_run["stats"]["def"] * 1.6, 2))
        self.assertEqual(self.command("field_pick", {"id": apron, "floor": 2, "slot": 2}, second)["code"], "stale")

        with qq.store.transaction() as db:
            turn = rules.Turn(qq, db, "101", "群友甲", "110", self.now)
            turn.run["peek"] = "loot"
            turn.persist()
        third = find("down", {}, lamp)
        choices = third["run"]["field_offer"]["choices"]
        self.assertEqual([choice["control"]["params"]["slot"] for choice in choices], [1, 2, 0])
        self.assertEqual(self.command("field_pick", {"id": lamp, "floor": 2, "slot": 1}, third)["code"], "invalid")
        mixed = self.command("field_pick", {"id": lamp, "floor": 3, "slot": 1}, third)["state"]["run"]
        self.assertEqual([item["id"] for item in mixed["field_slots"]], [lamp, apron])
        self.assertEqual(mixed["effective_stats"]["def"], round(first_run["stats"]["def"] * 1.3, 2))
        self.assertEqual(mixed["field_slots"][1]["action"]["control"]["params"], {"id": apron, "slot": 2})
        removed = self.command("field_remove", {"id": apron, "slot": 2})["state"]["run"]
        self.assertEqual([item["id"] if item else None for item in removed["field_slots"]], [lamp, None])

        with qq.store.transaction() as db:
            turn = rules.Turn(qq, db, "101", "群友甲", "110", self.now)
            turn.run["peek"] = "loot"
            turn.persist()
        declined = find("down", {}, apron)
        final = self.command("field_pick", {"id": apron, "floor": 4, "slot": 0}, declined)["state"]["run"]
        self.assertIsNone(final["field_offer"])
        self.assertEqual(final["field_slots"], removed["field_slots"])

    def test_judgement_is_labelled_and_returns_battle_and_defeat_settlements(self):
        self.command("start", {"map_id": "mall"})
        with self.web.game.store.transaction() as db:
            turn = rules.Turn(self.web.game, db, "101", "群友甲", "110", self.now)
            enemy = rules.combat.monster(turn.catalog.maps["mall"], 1, turn.rng)
            turn.run.update(field_items={"field_singularity": 1}, event={"kind": "combat", "enemy": enemy})
            turn.persist()
        state = self.state()
        item = state["run"]["field"][0]
        self.assertEqual(item["name"], "概念集合体-公正")
        self.assertIn("50%", item["description"])
        self.assertEqual(item["actions"][0]["label"], "发动公正裁决")
        with patch.object(self.web.game.rng, "random", return_value=.25):
            result = self.command("field_use", {"id": item["id"]}, state)["result"]
        self.assertEqual(result["battle"]["outcome"], "defeat")
        self.assertFalse(result["departure"]["extracted"])
        self.assertEqual(result["departure"]["value"], 0)
        self.assertIsNone(self.state()["run"])

    def test_shared_damage_equipment_stacks_and_grenade_retains_boss_floor(self):
        self.command("start", {"map_id": "mall"})
        item_id = "field_game_balance"
        with self.web.game.store.transaction() as db:
            turn = rules.Turn(self.web.game, db, "101", "群友甲", "110", self.now)
            turn.run.update(field_items={item_id: 2},
                            bag=[turn.catalog.ordinary_item("grenade", turn.rng) for _ in range(2)])
            turn.persist()
        before = self.state()["run"]
        self.assertEqual(before["damage_multiplier"], 1)
        worn = self.command("field_use", {"id": item_id})["state"]["run"]
        self.assertEqual(worn["damage_multiplier"], 2)
        self.assertEqual(worn["effective_stats"], before["effective_stats"])
        with self.web.game.store.transaction() as db:
            turn = rules.Turn(self.web.game, db, "101", "群友甲", "110", self.now)
            enemy = rules.combat.monster(turn.catalog.maps["mall"], 1, turn.rng)
            enemy.update(hp=100, max_hp=100)
            turn.run["event"] = {"kind": "combat", "enemy": enemy}
            turn.persist()
        hit = self.command("use", {"index": 1})["state"]["run"]
        self.assertEqual(hit["event"]["enemy"]["hp"], 40)

        doubled = self.command("field_use", {"id": item_id})["state"]["run"]
        self.assertEqual(doubled["damage_multiplier"], 4)
        with self.web.game.store.transaction() as db:
            turn = rules.Turn(self.web.game, db, "101", "群友甲", "110", self.now)
            boss = rules.combat.monster(turn.catalog.maps["mall"], 8, turn.rng, boss=True)
            boss["hp"] = 2
            turn.run.update(floor=8, event={"kind": "combat", "enemy": boss})
            turn.persist()
        guarded = self.command("use", {"index": 1})["state"]["run"]
        self.assertEqual(guarded["event"]["enemy"]["hp"], 1)
        removed = self.command("field_remove", {"id": item_id, "slot": 2})["state"]["run"]
        self.assertEqual(removed["damage_multiplier"], 2)


if __name__ == "__main__":
    unittest.main()
