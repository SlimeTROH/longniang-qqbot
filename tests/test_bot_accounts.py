"""Offline tests for the ignored bot account list."""
import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from nonebot.adapters.onebot.v11 import (
    FriendRequestEvent, GroupBanNoticeEvent, GroupMessageEvent, Message, PokeNotifyEvent,
    PrivateMessageEvent,
)
from nonebot.message import _apply_event_preprocessors

ROOT = Path(__file__).resolve().parents[1]
BOT = 123456
IGNORED = 123457
HUMAN = 123458
EVENT_FIELDS = {"time": 42, "self_id": BOT}


def message(user_id, kind):
    event = GroupMessageEvent if kind == "group" else PrivateMessageEvent
    return event.model_validate({
        **EVENT_FIELDS,
        "post_type": "message",
        "message_type": kind,
        "sub_type": "normal" if kind == "group" else "friend",
        "group_id": 100 if kind == "group" else None,
        "user_id": user_id,
        "message_id": 1,
        "message": "机器人列表",
        "raw_message": "机器人列表",
        "font": 0,
        "sender": {"user_id": user_id},
    })


class BotAccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("test_bot_account_plugin", ROOT / "plugins/bot_accounts.py")
        self.plugin = importlib.util.module_from_spec(spec)
        with patch("nonebot.on_command", side_effect=lambda *args, **kwargs: SimpleNamespace(
            handle=lambda: (lambda fn: fn), finish=AsyncMock(),
        )):
            spec.loader.exec_module(self.plugin)
        self.addCleanup(self.plugin.shutdown)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.plugin.DATA_FILE = Path(temp.name) / "bot_accounts.json"
        self.plugin._accounts = set()

    async def dispatches(self, event):
        return await _apply_event_preprocessors(SimpleNamespace(), event, {}, show_log=False)

    async def test_listed_senders_are_stopped_before_matchers(self):
        self.plugin._save_accounts({str(IGNORED)})
        events = (
            message(IGNORED, "group"),
            message(IGNORED, "private"),
            FriendRequestEvent(**EVENT_FIELDS, post_type="request", request_type="friend", user_id=IGNORED, flag="x"),
            PokeNotifyEvent(**EVENT_FIELDS, post_type="notice", notice_type="notify", sub_type="poke",
                            user_id=IGNORED, target_id=BOT, group_id=100),
        )
        for event in events:
            with self.subTest(kind=type(event).__name__):
                self.assertFalse(await self.dispatches(event))
        self.assertTrue(await self.dispatches(message(HUMAN, "group")))

    async def test_notice_uses_actor_and_unload_removes_the_guard(self):
        self.plugin._save_accounts({str(IGNORED)})
        notice_fields = {
            **EVENT_FIELDS, "post_type": "notice", "notice_type": "group_ban",
            "sub_type": "ban", "group_id": 100, "duration": 60,
        }
        self.assertFalse(await self.dispatches(GroupBanNoticeEvent(
            **notice_fields, user_id=HUMAN, operator_id=IGNORED,
        )))
        self.assertTrue(await self.dispatches(GroupBanNoticeEvent(
            **notice_fields, user_id=IGNORED, operator_id=HUMAN,
        )))
        self.plugin.shutdown()
        self.assertTrue(await self.dispatches(message(IGNORED, "group")))

    async def test_commands_manage_accounts_that_persist_and_apply_immediately(self):
        admin = message(HUMAN, "group")
        account = Message(str(IGNORED))
        await self.plugin.handle_add(admin, account)
        self.plugin.add_cmd.finish.assert_awaited_once_with("已加入机器人名单")
        self.assertFalse(await self.dispatches(message(IGNORED, "group")))
        self.plugin._accounts = self.plugin._load_accounts()
        self.assertFalse(await self.dispatches(message(IGNORED, "group")))
        await self.plugin.handle_list()
        self.assertIn(str(IGNORED), self.plugin.list_cmd.finish.await_args.args[0])
        await self.plugin.handle_remove(admin, account)
        self.plugin.remove_cmd.finish.assert_awaited_once_with("已从机器人名单移除")
        self.assertEqual(self.plugin._load_accounts(), set())
        self.assertTrue(await self.dispatches(message(IGNORED, "group")))

    async def test_admin_permission_is_required_in_group_and_private(self):
        config = SimpleNamespace(admin_set=lambda: {str(HUMAN)})
        with patch.dict(sys.modules, {"plugins.chat_bot.env_config": config}):
            for kind in ("group", "private"):
                self.assertTrue(await self.plugin._is_admin(message(HUMAN, kind)))
                self.assertFalse(await self.plugin._is_admin(message(IGNORED, kind)))
