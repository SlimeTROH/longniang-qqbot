"""单元测试：缓存前缀恒定性、OpenAI GPT Sol 前置意图审查、农场签到修复、钓鱼期望计算与自动化派单系统。"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_pkg_submodule(mod_name: str, file_name: str):
    pkg_dir = ROOT / "plugins" / "chat_bot"
    if "plugins" not in sys.modules:
        pkg_root = types.ModuleType("plugins")
        pkg_root.__path__ = [str(ROOT / "plugins")]
        sys.modules["plugins"] = pkg_root
    if "plugins.chat_bot" not in sys.modules:
        pkg_cb = types.ModuleType("plugins.chat_bot")
        pkg_cb.__path__ = [str(pkg_dir)]
        sys.modules["plugins.chat_bot"] = pkg_cb
    full = f"plugins.chat_bot.{mod_name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, pkg_dir / file_name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    setattr(sys.modules["plugins.chat_bot"], mod_name, mod)
    spec.loader.exec_module(mod)
    return mod


_load_pkg_submodule("guard", "guard.py")
gc = _load_pkg_submodule("game_controller", "game_controller.py")
ap = _load_pkg_submodule("automation_planner", "automation_planner.py")
llm = _load_pkg_submodule("llm", "llm.py")


class CacheInvarianceAndIntentTests(unittest.TestCase):
    def test_reply_schema_and_static_system_are_100_percent_invariant(self):
        s0 = llm.build_reply_schema(0)
        s80 = llm.build_reply_schema(80)
        s320 = llm.build_reply_schema(320)
        self.assertEqual(json.dumps(s0, sort_keys=True), json.dumps(s80, sort_keys=True))
        self.assertEqual(json.dumps(s80, sort_keys=True), json.dumps(s320, sort_keys=True))

        sys_a = llm.build_static_system("测试人设基底", "若娜瓦")
        sys_b = llm.build_static_system("测试人设基底", "若娜瓦")
        self.assertEqual(sys_a, sys_b)
        self.assertIn("ORD-", sys_a)

    def test_intent_system_exceeds_openai_1024_token_cache_threshold(self):
        self.assertGreater(len(llm.INTENT_SYSTEM), 3200)
        self.assertIn("automation", llm.INTENT_SYSTEM)
        self.assertIn("direct_actions", llm.INTENT_SYSTEM)
        self.assertEqual(llm.DEFAULT_INTENT_MODEL, "gpt-6.1-sol")
        self.assertEqual(llm.DEFAULT_INTENT_EFFORT, "low")

    def test_local_game_intent_distinguishes_query_direct_and_automation(self):
        exercise = llm.parse_local_game_intent("帮我解这道钓鱼题")
        self.assertFalse(exercise["need_fishing"])
        self.assertEqual(exercise["direct_actions"], [])

        # 1. 纯查询
        q = llm.parse_local_game_intent("我的农场现在怎么样？")
        self.assertTrue(q["need_farm"])
        self.assertEqual(q["direct_actions"], [])
        self.assertEqual(q["automation"], {})

        # 2. 单步即时操控
        d = llm.parse_local_game_intent("帮我签个到并把菜收了，再用钛金鱼竿和小鱼钓一竿")
        self.assertTrue(d["need_farm"])
        self.assertTrue(d["need_fishing"])
        acts = [(x["domain"], x["action"]) for x in d["direct_actions"]]
        self.assertIn(("farm", "sign_in"), acts)
        self.assertIn(("farm", "harvest"), acts)
        self.assertIn(("fishing", "fish"), acts)
        self.assertEqual(d["automation"], {})

        # 3. 自动化托管派单
        a = llm.parse_local_game_intent("帮我自动托管农场和钓鱼 3 小时，留 500 币保底")
        self.assertTrue(a["automation"].get("is_automation"))
        self.assertEqual(a["automation"]["domain"], "both")
        self.assertEqual(a["automation"]["duration_minutes"], 180)
        self.assertEqual(a["automation"]["reserve_coins"], 500)
        self.assertEqual(a["direct_actions"], [])

        # 4. 唯一标识符确认与取消
        c1 = llm.parse_local_game_intent("确认执行 ORD-7K9M2X")
        self.assertEqual(c1["order_op"], "confirm")
        self.assertEqual(c1["order_id"], "ORD-7K9M2X")

        c2 = llm.parse_local_game_intent("取消 ORD-7K9M2X")
        self.assertEqual(c2["order_op"], "cancel")
        self.assertEqual(c2["order_id"], "ORD-7K9M2X")


class FishingAndFarmSqliteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fishing_ev_recognizes_small_fish_bait_pool_removal_advantage(self):
        f_small = types.SimpleNamespace(
            name="小鱼", type="fish", weight=1000, price=5, amount=1,
            can_sell=True, can_buy=True, buy_price=15, sleep_time=15,
            props={"rm_fish": ["小鱼"]},
        )
        f_tuna = types.SimpleNamespace(
            name="金枪鱼", type="fish", weight=500, price=60, amount=1,
            can_sell=True, can_buy=False, buy_price=0, sleep_time=20, props=None,
        )
        f_oar = types.SimpleNamespace(
            name="皇带鱼", type="fish", weight=190, price=280, amount=1,
            can_sell=True, can_buy=False, buy_price=0, sleep_time=25, props=None,
        )
        rod_ti = types.SimpleNamespace(
            name="钛金鱼竿", type="item", weight=0, price=0, amount=1,
            can_sell=False, can_buy=True, buy_price=20, sleep_time=15,
            props={"no_fish": -0.08},
        )
        by_name = {x.name: x for x in (f_small, f_tuna, f_oar, rod_ti)}

        class _FakeDS:
            @staticmethod
            def get_adjusts_from_tools(tools):
                if "小鱼" in tools:
                    return ({"rm_fish": ["小鱼"]}, -0.08 if "钛金鱼竿" in tools else 0.0, 0.0)
                return ({}, 0.0, 0.0)

            @staticmethod
            def adjusted(adjusts):
                rm = set(adjusts.get("rm_fish") or [])
                pool = [x for x in (f_small, f_tuna, f_oar) if x.name not in rm]
                return [x.name for x in pool], [x.weight for x in pool]

        fake_env = {
            "cfg": types.SimpleNamespace(no_fish_probability=0.10),
            "ds": _FakeDS(),
            "fh": types.SimpleNamespace(
                get_fish_by_name=lambda n: by_name.get(n),
                config_fishes=[f_small, f_tuna, f_oar, rod_ti],
            ),
        }
        with patch.object(gc, "_fishing_env", return_value=fake_env):
            ev_bare = gc.calculate_fishing_ev([], owned_items={})
            ev_combo = gc.calculate_fishing_ev(["钛金鱼竿", "小鱼"], owned_items={"钛金鱼竿": 1, "小鱼": 3})

        self.assertIsNotNone(ev_bare)
        self.assertIsNotNone(ev_combo)
        self.assertGreater(ev_combo.net_ev, ev_bare.net_ev * 1.8)

    def test_farm_sqlite_sign_in_increments_days_and_awards_milestone_seeds(self):
        from contextlib import closing

        db_path = self.root / "farm.db"
        today_dt = datetime.now()
        yesterday = (today_dt - timedelta(days=1)).strftime("%Y-%m-%d")
        cur_month = today_dt.strftime("%Y-%m")

        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute(
                "CREATE TABLE user (uid TEXT PRIMARY KEY, name TEXT, exp INTEGER, point INTEGER, vipPoint INTEGER)"
            )
            conn.execute(
                "CREATE TABLE userSignLog (uid TEXT, signDate TEXT, isSupplement INTEGER, exp INTEGER, point INTEGER)"
            )
            conn.execute(
                "CREATE TABLE userSignSummary (uid TEXT PRIMARY KEY, totalSignDays INTEGER, currentMonth TEXT, monthSignDays INTEGER, lastSignDate TEXT, continuousDays INTEGER, supplementCount INTEGER)"
            )
            conn.execute(
                "CREATE TABLE userSeed (uid TEXT, seed TEXT, count INTEGER, PRIMARY KEY(uid, seed))"
            )
            conn.execute("INSERT INTO user VALUES ('1001', '旅行者', 10, 500, 0)")
            conn.execute(
                "INSERT INTO userSignSummary VALUES ('1001', 14, ?, 14, ?, 14, 0)",
                (cur_month, yesterday),
            )
            conn.commit()

        ok1, msg1 = gc.do_farm_sign_sqlite(str(db_path), "1001")
        self.assertTrue(ok1)
        self.assertIn("连续 15 天", msg1)
        self.assertIn("胡萝卜种子×3", msg1)

        # 同一天重复签到应返回 False 且正确报告本月已签天数（不再恒为 1）
        ok2, msg2 = gc.do_farm_sign_sqlite(str(db_path), "1001")
        self.assertFalse(ok2)
        self.assertIn("已经签过", msg2)
        if today_dt.day > 1:
            self.assertIn("本月已签 15 天", msg2)

        with closing(sqlite3.connect(db_path)) as conn:
            seeds = dict(conn.execute("SELECT seed, count FROM userSeed WHERE uid='1001'").fetchall())
        self.assertEqual(seeds, {"胡萝卜": 3})


class AsyncOpenAISolAndAutomationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_openai_gpt_sol_responses_api_route_and_low_effort(self):
        captured: dict = {}

        class _FakeResp:
            status_code = 200
            text = ""

            def json(self):
                return {
                    "id": "resp_test_sol",
                    "status": "completed",
                    "output": [
                        {"type": "reasoning", "summary": []},
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps(
                                        {
                                            "level": "medium",
                                            "max_chars": 88,
                                            "daily_affection": 0.15,
                                            "reason": "托管农场与钓鱼",
                                            "need_farm": True,
                                            "need_fishing": True,
                                            "direct_actions": [],
                                            "automation": {
                                                "is_automation": True,
                                                "domain": "both",
                                                "priority_mode": "profit",
                                                "duration_minutes": 120,
                                                "reserve_coins": 300,
                                            },
                                            "order_op": {"op": "", "order_id": ""},
                                            "need_web": False,
                                            "web_query": "",
                                        },
                                        ensure_ascii=False,
                                    ),
                                }
                            ],
                        },
                    ],
                    "usage": {
                        "input_tokens": 1420,
                        "input_tokens_details": {"cached_tokens": 1280},
                        "output_tokens": 145,
                        "output_tokens_details": {"reasoning_tokens": 32},
                    },
                }

        class _FakeClient:
            async def post(self, url, headers=None, json=None, timeout=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return _FakeResp()

        with patch.object(llm, "_chat_client", return_value=_FakeClient()):
            hint = await llm.decide_length(
                "帮我自动托管农场和钓鱼两个小时，留300保底",
                "sk-proj-testkey",
                "gpt-6.1-sol",
                farm_enabled=True,
                web_enabled=True,
                reasoning_effort="low",
            )

        self.assertTrue(hint.ok)
        self.assertEqual(captured["url"], "https://api.openai.com/v1/responses")
        self.assertEqual(captured["json"]["model"], "gpt-6.1-sol")
        self.assertEqual(captured["json"]["reasoning"], {"effort": "low"})
        self.assertEqual(captured["json"]["prompt_cache_key"], "qqbot:intent:v2")
        self.assertNotIn("temperature", captured["json"])
        self.assertNotIn("top_p", captured["json"])
        self.assertEqual(hint.usage["_cached_tokens"], 1280)
        self.assertEqual(hint.usage["_reasoning_tokens"], 32)
        self.assertTrue(hint.automation.get("is_automation"))
        self.assertEqual(hint.automation.get("duration_minutes"), 120)
        self.assertEqual(hint.direct_actions, [])

    async def test_automation_order_calculation_unique_id_and_confirm_lifecycle(self):
        fake_farm = gc.FarmSnapshot(
            uid="2002",
            exists=True,
            name="星澜农场",
            level=3,
            cur_exp=40,
            need_exp=200,
            point=1200,
            vip_point=0,
            signed_today=False,
            soil_total=4,
            soils=[
                gc.SoilInfo(index=1, level=1, plant="小麦", mature_at=int(time.time()) - 60, harvest_count=0),
                gc.SoilInfo(index=2, level=1, plant=""),
                gc.SoilInfo(index=3, level=1, plant=""),
                gc.SoilInfo(index=4, level=1, plant="生菜", issues=["枯萎"]),
            ],
            seeds={},
            warehouse={"土豆": 10},
            next_reclaim_cost=5000,
            next_reclaim_level=5,
        )
        fake_crops = [
            gc.CropInfo(name="胡萝卜", level=1, seed_cost=40, harvest=12, unit_price=10, hours=2.0, experience=15, times=1),
            gc.CropInfo(name="草莓", level=3, seed_cost=100, harvest=20, unit_price=15, hours=2.5, experience=30, times=1),
        ]
        fake_fish = gc.FishingSnapshot(
            uid="2002",
            exists=True,
            coin=800,
            coin_name="绿宝石",
            cooldown_left=0,
            frequency=2,
            rods={"钛金鱼竿": 1},
            baits={"小鱼": 5},
            fishes={"钛金鱼竿": 1, "小鱼": 5, "金枪鱼": 2},
            sellable={"金枪鱼": 2},
        )
        fake_ev = gc.FishingEV(
            tools=["钛金鱼竿", "小鱼"],
            gross_ev=140.0,
            tool_cost_per_cast=5.0,
            net_ev=135.0,
            no_fish_prob=0.02,
            expected_sleep_sec=20.0,
            note="库存小鱼免购饵费 + 钛金倍率",
        )

        temp_store = ap.OrderStore(self.root / "orders.json")
        temp_runner = ap.AutomationRunner(temp_store)

        with (
            patch.object(ap, "order_store", temp_store),
            patch.object(gc, "read_farm", AsyncMock(return_value=fake_farm)),
            patch.object(gc, "load_crop_market", AsyncMock(return_value=fake_crops)),
            patch.object(gc, "read_fishing", AsyncMock(return_value=fake_fish)),
            patch.object(gc, "recommend_best_fishing_loadout", return_value=fake_ev),
            patch.object(
                gc,
                "execute_single_action",
                AsyncMock(side_effect=lambda uid, act, **kw: gc.ActionOutcome(ok=True, domain=act["domain"], action=act["action"], summary=f"已执行 {act['domain']}.{act['action']}")),
            ),
        ):
            spec = ap.parse_automation_from_text("帮我自动托管农场和钓鱼 2 小时，留 200 保底")
            order = await ap.compute_and_create_order("2002", "99999", spec, raw_request="托管农场和钓鱼2小时")

            self.assertRegex(order.order_id, r"^ORD-[2-9A-HJ-NP-Z]{6}$")
            self.assertEqual(order.status, "pending_confirm")
            self.assertGreaterEqual(len(order.steps), 5)

            card = ap.format_order_card(order)
            self.assertIn(order.order_id, card)
            self.assertIn(f"确认 {order.order_id}", card)
            self.assertIn("草莓", card)
            self.assertIn("钛金鱼竿", card)

            # 他人冒充确认必须拒绝
            ok_wrong, msg_wrong = await temp_runner.confirm_order("9999", order.order_id)
            self.assertFalse(ok_wrong)
            self.assertIn("不属于你", msg_wrong)

            # 本人凭唯一标识符确认
            ok_owner, msg_owner = await temp_runner.confirm_order("2002", order.order_id)
            self.assertTrue(ok_owner)
            self.assertIn(order.order_id, msg_owner)

            # 取消后台运行中的派单
            ok_cancel, _ = temp_runner.cancel_order("2002", order.order_id)
            self.assertTrue(ok_cancel)
            self.assertEqual(temp_store.get(order.order_id).status, "cancelled")
            await temp_runner.shutdown()


if __name__ == "__main__":
    unittest.main()
