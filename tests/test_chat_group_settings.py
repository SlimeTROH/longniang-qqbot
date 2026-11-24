import ast
import asyncio
import importlib.util
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from nonebot.adapters.onebot.v11 import Message, MessageSegment

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("chat_group_settings", ROOT / "plugins/chat_bot/groups.py")
groups = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = groups
spec.loader.exec_module(groups)
config_spec = importlib.util.spec_from_file_location("chat_runtime_config", ROOT / "plugins/chat_bot/env_config.py")
chat_config = importlib.util.module_from_spec(config_spec)
config_spec.loader.exec_module(chat_config)


class GroupSettingsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        groups.DATA_FILE = Path(tmp.name) / "groups.json"

    def test_removing_a_configured_group_persists_until_it_is_added_back(self):
        groups.DATA_FILE.write_text('{"groups":["123","234"]}', encoding="utf-8")
        configured = {"123", "345"}

        self.assertTrue(groups.remove("123", configured))
        settings = groups.load()
        self.assertEqual(settings.enabled(configured), {"234", "345"})
        self.assertIn("123", settings.disabled)
        self.assertFalse(groups.remove("123", configured))
        self.assertTrue(groups.add("123", configured))
        self.assertEqual(groups.load().enabled(configured), {"123", "234", "345"})

    def test_removing_the_last_enabled_group_does_not_reopen_all_groups(self):
        self.assertTrue(groups.add("123", set()))
        self.assertTrue(groups.remove("123", set()))
        self.assertEqual(groups.load().enabled(set()), set())
        self.assertFalse(groups.load().allows("234", set()))

    def test_removing_one_group_in_open_mode_leaves_other_groups_open(self):
        self.assertTrue(groups.remove("123", set()))
        self.assertFalse(groups.load().allows("123", set()))
        self.assertTrue(groups.load().allows("234", set()))
        self.assertTrue(groups.add("123", set()))
        self.assertIsNone(groups.load().enabled(set()))


def load_handlers(path, names, env):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name in names]
    for node in selected:
        if not isinstance(node, ast.ClassDef):
            node.decorator_list = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            selected.append(node)
    env.update(Bot=SimpleNamespace, Message=Message, MessageEvent=SimpleNamespace, CommandArg=lambda: None,
               dataclass=dataclass)
    exec(compile(ast.Module(body=selected, type_ignores=[]), path, "exec"), env)


class CommandVisibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.admins = {"42"}
        self.bot = SimpleNamespace(send=AsyncMock())
        self.matcher = SimpleNamespace(finish=AsyncMock())
        self.group_store = SimpleNamespace(add=Mock(return_value=True), remove=Mock(return_value=True), load=Mock())
        self.env = dict(re=re, group_store=self.group_store, _configured_groups=lambda: set(),
                        _admin_set=lambda: self.admins, _is_private=lambda event: event.message_type == "private",
                        list_group_cmd=self.matcher, status_cmd=self.matcher, model_menu_reply=self.matcher,
                        solve_model_cmd=self.matcher, solve_effort_cmd=self.matcher,
                        _chat_model_menus={}, _reply_to=lambda event, text: text)
        load_handlers("plugins/chat_bot/__init__.py", {
            "_require_private_admin", "_group_number", "_current_group", "_change_group", "handle_list_group", "handle_chat_cmd",
            "_model_menu_key", "_handle_model_command", "handle_solve_model_cmd", "handle_solve_effort_cmd",
        }, self.env)

    async def test_group_lists_stay_private_and_nonadmins_cannot_change_groups(self):
        events = [SimpleNamespace(message_type="group", user_id=99),
                  SimpleNamespace(message_type="private", user_id=99)]
        for event in events:
            for enable in (True, False):
                await self.env["_change_group"](self.matcher, event, Message("123"), enable=enable)
            await self.env["handle_list_group"](event)
            await self.env["handle_chat_cmd"](self.bot, event, Message("状态"))
        group_admin = SimpleNamespace(message_type="group", user_id=42, group_id=123)
        await self.env["handle_list_group"](group_admin)
        await self.env["handle_chat_cmd"](self.bot, group_admin, Message("状态"))
        self.admins.clear()
        await self.env["_change_group"](
            self.matcher, SimpleNamespace(message_type="private", user_id=42), Message("123"), enable=True,
        )
        self.group_store.add.assert_not_called()
        self.group_store.remove.assert_not_called()
        self.group_store.load.assert_not_called()
        self.assertEqual(self.matcher.finish.await_count, 11)
        self.assertTrue(all(not call.args for call in self.matcher.finish.await_args_list))

    async def test_image_cooldown_is_adjustable_by_a_group_or_private_admin(self):
        ledger = SimpleNamespace(cooldown_seconds=Mock(return_value=180), set_cooldown_seconds=Mock())
        self.env.update(image_quota_store=ledger, asyncio=asyncio)
        handler = self.env["handle_chat_cmd"]
        non_admin = SimpleNamespace(message_type="group", user_id=99)
        await handler(self.bot, non_admin, Message("生图间隔 20"))
        self.assertEqual(self.matcher.finish.await_args.args, ())
        ledger.set_cooldown_seconds.assert_not_called()

        group_admin = SimpleNamespace(message_type="group", user_id=42)
        await handler(self.bot, group_admin, Message("生图间隔"))
        self.assertIn("180 秒/次", self.matcher.finish.await_args.args[0])
        await handler(self.bot, group_admin, Message("生图间隔 45"))
        ledger.set_cooldown_seconds.assert_called_once_with(45)
        self.assertIn("45 秒/次", self.matcher.finish.await_args.args[0])
        private_admin = SimpleNamespace(message_type="private", user_id=42)
        await handler(self.bot, private_admin, Message("图像间隔 0"))
        ledger.set_cooldown_seconds.assert_called_with(0)
        self.assertIn("关闭", self.matcher.finish.await_args.args[0])

    async def test_group_admin_can_change_current_or_named_group_with_result_only(self):
        event = SimpleNamespace(message_type="group", user_id=42, group_id=123)
        await self.env["_change_group"](self.matcher, event, Message("abc"), enable=True)
        self.assertEqual(self.matcher.finish.await_args.args, ("请填写有效群号。",))
        self.group_store.add.assert_not_called()

        await self.env["_change_group"](self.matcher, event, Message(), enable=True)
        self.group_store.add.assert_called_once_with("123", set())
        self.assertEqual(self.matcher.finish.await_args.args, ("加群成功。",))

        await self.env["_change_group"](self.matcher, event, Message("456"), enable=False)
        self.group_store.remove.assert_called_once_with("456", set())
        self.assertEqual(self.matcher.finish.await_args.args, ("退群成功。",))

        self.group_store.remove.return_value = False
        await self.env["_change_group"](self.matcher, event, Message(), enable=False)
        self.assertEqual(self.matcher.finish.await_args.args, ("当前已停用。",))
        self.group_store.load.assert_not_called()

    async def test_private_admin_changes_exact_numeric_group(self):
        event = SimpleNamespace(message_type="private", user_id=42)
        for invalid in (Message(), Message("abc"), Message(MessageSegment.at(123))):
            await self.env["_change_group"](self.matcher, event, invalid, enable=True)
        self.group_store.add.assert_not_called()
        await self.env["_change_group"](self.matcher, event, Message("123"), enable=True)
        await self.env["_change_group"](self.matcher, event, Message("123"), enable=False)
        self.group_store.add.assert_called_once_with("123", set())
        self.group_store.remove.assert_called_once_with("123", set())

    async def test_private_admin_can_clear_a_specified_group_session(self):
        entered = []

        @asynccontextmanager
        async def scope(key):
            entered.append(key)
            yield

        session = SimpleNamespace(recent=[{}], archive_text=lambda: "archive")
        histories = {"group:123": session, "private:42": session}
        self.env.update(_history_scope=scope, histories=histories, _save_histories=Mock(return_value=True))
        event = SimpleNamespace(message_type="private", user_id=42)
        await self.env["handle_chat_cmd"](self.bot, event, Message("清空上下文　123"))
        self.assertEqual(entered, ["group:123"])
        self.assertEqual(histories, {"private:42": session})

    async def test_model_and_effort_commands_require_an_admin_in_group_or_private(self):
        change = AsyncMock()
        self.env["_change_chat_model"] = change
        for event in (SimpleNamespace(message_type="group", user_id=99),
                      SimpleNamespace(message_type="private", user_id=99)):
            await self.env["handle_chat_cmd"](self.bot, event, Message("模型 gpt-6.2-sol high"))
            await self.env["handle_solve_model_cmd"](self.bot, event, Message("gpt-6.2-sol high"))
            await self.env["handle_solve_effort_cmd"](self.bot, event, Message("high"))
        change.assert_not_awaited()

    async def test_awareness_switch_is_admin_only_and_works_in_group_or_private(self):
        switch = SimpleNamespace(is_enabled=lambda: False, set_enabled=AsyncMock())
        self.env["awareness"] = switch
        await self.env["handle_chat_cmd"](
            self.bot, SimpleNamespace(message_type="group", user_id=99), Message("意识层 开启"),
        )
        switch.set_enabled.assert_not_awaited()
        admin = SimpleNamespace(message_type="group", user_id=42)
        await self.env["handle_chat_cmd"](self.bot, admin, Message("意识层"))
        self.assertIn("当前关闭", self.matcher.finish.await_args.args[0])
        await self.env["handle_chat_cmd"](self.bot, admin, Message("意识层 开启"))
        await self.env["handle_chat_cmd"](
            self.bot, SimpleNamespace(message_type="private", user_id=42), Message("意识层 关闭"),
        )
        self.assertEqual([call.args[0] for call in switch.set_enabled.await_args_list], [True, False])

    async def test_private_admin_switches_model_and_effort_and_persists_the_pair(self):
        change = AsyncMock(return_value="")
        self.env.update(env_config=chat_config, llm=SimpleNamespace(check_openai_model_access=change),
                        OPENAI_KEY="fake", OPENAI_CHAT_MODEL="gpt-6.1-terra", OPENAI_CHAT_EFFORT="none",
                        _EFFORT_ALIASES={"高": "high"}, _model_change_lock=asyncio.Lock())
        load_handlers("plugins/chat_bot/__init__.py", {"_change_chat_model"}, self.env)
        event = SimpleNamespace(message_type="private", self_id=100, user_id=42)
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(chat_config, "CHAT_MODEL_PATH", Path(temp) / "chat_model.json"), \
             patch.object(chat_config, "get_env", side_effect=lambda name, default: default):
            await self.env["handle_chat_cmd"](self.bot, event, Message("模型 gpt-6.2-sol"))
            change.assert_awaited_once_with("fake", "gpt-6.2-sol")
            self.assertEqual(chat_config.load_chat_model("gpt-6.1-terra", "none"), ("gpt-6.2-sol", "low"))
            self.assertEqual(self.env["OPENAI_CHAT_EFFORT"], "low")

            await self.env["handle_chat_cmd"](self.bot, event, Message("思考 none"))
            self.assertEqual(self.env["OPENAI_CHAT_EFFORT"], "low")
            await self.env["handle_chat_cmd"](self.bot, event, Message("思考 高"))
            self.assertEqual(self.env["OPENAI_CHAT_EFFORT"], "high")
            self.assertEqual(chat_config.load_chat_model("gpt-6.1-terra", "none"), ("gpt-6.2-sol", "high"))

            change.return_value = "该模型暂时不可用。"
            await self.env["handle_chat_cmd"](self.bot, event, Message("模型 gpt-6-luna none"))
            self.assertEqual(self.env["OPENAI_CHAT_MODEL"], "gpt-6.2-sol")
            self.assertEqual(chat_config.load_chat_model("gpt-6.1-terra", "none"), ("gpt-6.2-sol", "high"))

    def prepare_model_menu(self):
        callbacks = []
        self.addCleanup(lambda: [coro.close() for coro in callbacks])
        sleep = AsyncMock()
        self.env.update(
            env_config=chat_config, OPENAI_KEY="fake", OPENAI_CHAT_MODEL="gpt-6.1-terra", OPENAI_CHAT_EFFORT="low",
            time=SimpleNamespace(monotonic=lambda: 100.0), asyncio=SimpleNamespace(create_task=callbacks.append, sleep=sleep),
            problem_solver=SimpleNamespace(
                model_settings=lambda: ("gpt-6.2-sol", "max"), SOLVE_MODELS=tuple(chat_config.CHAT_MODEL_EFFORTS),
                model_efforts=chat_config.chat_model_efforts,
            ),
            _change_chat_model=AsyncMock(return_value="已切换"),
        )
        load_handlers("plugins/chat_bot/__init__.py", {
            "_ChatModelMenu", "_MODEL_MENU_TIMEOUT", "_MODEL_MENU_CANCEL", "_expire_model_menu", "_open_model_menu",
            "_is_model_menu_reply", "handle_model_menu_reply",
        }, self.env)
        return callbacks, sleep

    @staticmethod
    def menu_event(text, *, user=42, group=123):
        return SimpleNamespace(self_id=100, user_id=user, group_id=group, get_plaintext=lambda: text,
                               message_type="private" if group is None else "group")

    async def test_group_model_menu_uses_its_displayed_numbers_and_own_session(self):
        self.prepare_model_menu()
        self.admins.add("99")
        await self.env["handle_chat_cmd"](self.bot, self.menu_event("聊天 模型"), Message("模型"))
        self.assertIn("2. gpt-6.2-sol", self.matcher.finish.await_args.args[0])
        self.assertIn("60 秒", self.matcher.finish.await_args.args[0])
        for other in (self.menu_event("2", user=99), self.menu_event("2", group=321), self.menu_event("2", group=None)):
            self.assertFalse(self.env["_is_model_menu_reply"](other))
        self.assertFalse(self.env["_is_model_menu_reply"](self.menu_event("随便聊聊")))
        await self.env["handle_model_menu_reply"](self.menu_event("99"))
        self.env["_change_chat_model"].assert_not_awaited()
        valid = self.menu_event("2")
        self.assertTrue(self.env["_is_model_menu_reply"](valid))
        await self.env["handle_model_menu_reply"](valid)
        self.env["_change_chat_model"].assert_awaited_once_with(
            "gpt-6.2-sol", change_model=True, expected_model="", for_solve=False,
        )
        self.assertFalse(self.env["_is_model_menu_reply"](valid))

    async def test_solve_menu_targets_the_solve_settings_after_replacing_a_chat_menu(self):
        self.prepare_model_menu()
        event = self.menu_event("做题 思考")
        await self.env["handle_chat_cmd"](self.bot, event, Message("模型"))
        await self.env["handle_solve_effort_cmd"](self.bot, event, Message())
        self.assertIn("做题模型 gpt-6.2-sol", self.matcher.finish.await_args.args[0])
        self.assertIn("5. max（当前）", self.matcher.finish.await_args.args[0])
        await self.env["handle_model_menu_reply"](self.menu_event("3"))
        self.env["_change_chat_model"].assert_awaited_once_with(
            "high", change_model=False, expected_model="gpt-6.2-sol", for_solve=True,
        )

    async def test_solve_model_and_effort_are_saved_separately_from_chat(self):
        model_access = AsyncMock(return_value="")
        self.env.update(
            env_config=chat_config, llm=SimpleNamespace(check_openai_model_access=model_access),
            OPENAI_KEY="fake", OPENAI_CHAT_MODEL="gpt-6.2-sol", OPENAI_CHAT_EFFORT="low",
            _EFFORT_ALIASES={"高": "high"}, _model_change_lock=asyncio.Lock(),
            problem_solver=SimpleNamespace(
                model_settings=lambda: chat_config.load_solve_model("gpt-6.2-sol", "max"),
                model_efforts=chat_config.chat_model_efforts,
            ),
        )
        load_handlers("plugins/chat_bot/__init__.py", {"_change_chat_model"}, self.env)
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(chat_config, "SOLVE_MODEL_PATH", Path(temp) / "solve_model.json"), \
             patch.object(chat_config, "CHAT_MODEL_PATH", Path(temp) / "chat_model.json"), \
             patch.object(chat_config, "get_env", side_effect=lambda name, default: default):
            chat_config.save_chat_model("gpt-6.2-sol", "low")
            event = self.menu_event("答题 模型")
            await self.env["handle_solve_model_cmd"](self.bot, event, Message("gpt-6.1-terra 高"))
            self.assertEqual(chat_config.load_solve_model("gpt-6.2-sol", "max"), ("gpt-6.1-terra", "high"))
            await self.env["handle_solve_effort_cmd"](self.bot, event, Message("none"))
            self.assertEqual(chat_config.load_solve_model("gpt-6.2-sol", "max"), ("gpt-6.1-terra", "none"))
            self.assertEqual(chat_config.load_chat_model("gpt-6.1-sol", "none"), ("gpt-6.2-sol", "low"))
            model_access.assert_awaited_once_with("fake", "gpt-6.1-terra")

    async def test_effort_menu_can_be_cancelled_or_select_a_number_in_private(self):
        callbacks, _ = self.prepare_model_menu()
        event = self.menu_event("聊天 思考", group=None)
        await self.env["handle_chat_cmd"](self.bot, event, Message("思考"))
        self.assertIn("4. high", self.matcher.finish.await_args.args[0])
        await self.env["handle_model_menu_reply"](self.menu_event("取消", group=None))
        self.assertEqual(self.matcher.finish.await_args.args[0], "已取消选择。")
        self.env["_change_chat_model"].assert_not_awaited()
        await callbacks[0]
        self.bot.send.assert_not_awaited()

        await self.env["handle_chat_cmd"](self.bot, event, Message("思考"))
        await self.env["handle_model_menu_reply"](self.menu_event("4", group=None))
        self.env["_change_chat_model"].assert_awaited_once_with(
            "high", change_model=False, expected_model="gpt-6.1-terra", for_solve=False,
        )

    async def test_unanswered_menu_cancels_after_sixty_seconds(self):
        callbacks, sleep = self.prepare_model_menu()
        event = self.menu_event("聊天 模型")
        await self.env["handle_chat_cmd"](self.bot, event, Message("模型"))
        await callbacks[0]
        sleep.assert_awaited_once_with(60)
        self.bot.send.assert_awaited_once_with(event, "选择超时，已取消。")
        self.assertFalse(self.env["_is_model_menu_reply"](self.menu_event("2")))
        self.env["_change_chat_model"].assert_not_awaited()

    def test_plugin_maintenance_checks_channel_and_explicit_allowlist(self):
        env = {"_allowlist": lambda: self.admins}
        load_handlers("plugins/hot_reload.py", {"_can_manage"}, env)
        check = env["_can_manage"]
        private_admin = SimpleNamespace(message_type="private", user_id=42)
        self.assertTrue(check(private_admin))
        self.assertFalse(check(SimpleNamespace(message_type="group", user_id=42)))
        self.assertFalse(check(SimpleNamespace(message_type="private", user_id=99)))
        self.admins.clear()
        self.assertFalse(check(private_admin))
