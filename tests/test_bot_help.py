"""Help routes, OneBot quote selection, and permission boundaries."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import nonebot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent

ROOT = Path(__file__).resolve().parents[1]


def load_help():
    spec = importlib.util.spec_from_file_location("bot_help_test", ROOT / "plugins" / "bot_help.py")
    module = importlib.util.module_from_spec(spec)
    matcher = SimpleNamespace(handle=lambda: lambda handler: handler)
    with patch.object(nonebot, "on_message", return_value=matcher):
        spec.loader.exec_module(module)
    return module


def event(text, *, group=123, user=42, quote=None, bot_id=999):
    parts = Message(MessageSegment.text(text))
    if quote is not None:
        parts.insert(0, MessageSegment.reply(quote))
    fields = dict(
        time=1795000000, self_id=bot_id, post_type="message", user_id=user, message_id=100,
        message=parts, original_message=parts, raw_message=text, font=0, sender={"nickname": "test"},
    )
    if group is None:
        return PrivateMessageEvent(message_type="private", sub_type="friend", **fields)
    return GroupMessageEvent(message_type="group", sub_type="normal", group_id=group, **fields)


class BotHelpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.help = load_help()
        self.clock = Mock(return_value=0.0)
        self.help.monotonic = self.clock
        self.chat = SimpleNamespace(
            _admin_set=lambda: {"42"}, persona=SimpleNamespace(name="GPT龙娘"),
            awareness=SimpleNamespace(is_enabled=lambda: False),
        )
        self.plugins = {
            "chat_bot": SimpleNamespace(module=self.chat),
            "endless_elevator": SimpleNamespace(module=SimpleNamespace()),
            "poke": SimpleNamespace(module=SimpleNamespace(COOLDOWN_SEC=5)),
        }
        self.help.get_plugin = self.plugins.get
        self.bot = SimpleNamespace(send=AsyncMock(return_value={"message_id": -812}))

    async def send_public(self, text, **kwargs):
        message = event(text, **kwargs)
        state = {}
        if not self.help._match_help(message, state):
            return False
        await self.help.send_public_help(self.bot, message, state)
        return True

    def latest_text(self):
        return str(self.bot.send.await_args.args[1])

    async def test_overview_cooldown_and_separate_topics(self):
        await self.send_public("帮助")
        self.assertIn("功能总览", self.latest_text())
        self.assertLessEqual(len(self.latest_text().splitlines()), 16)
        self.clock.return_value = 5
        await self.send_public("菜单", user=43)
        self.bot.send.assert_awaited_once()
        await self.send_public("帮助 做题", user=43)
        self.assertIn("领", self.latest_text())
        self.assertIn("源文件", self.latest_text())
        await self.send_public("帮助 解题", user=44)
        self.assertEqual(self.bot.send.await_count, 2)
        await self.send_public("帮助 生图", user=44)
        self.assertIn("30,000", self.latest_text())
        await self.send_public("帮助 做题", group=555)
        self.assertEqual(self.bot.send.await_count, 4)
        self.clock.return_value = 59.9
        await self.send_public("功能")
        self.assertEqual(self.bot.send.await_count, 4)
        self.clock.return_value = 60
        await self.send_public("功能")
        self.assertEqual(self.bot.send.await_count, 5)

    async def test_quote_uses_only_current_conversation_and_expires(self):
        await self.send_public("帮助")
        self.clock.return_value = 10
        self.assertFalse(await self.send_public("2"))
        self.assertFalse(await self.send_public("2", quote=812))
        self.assertFalse(await self.send_public("2", quote=-812, group=99))
        self.assertFalse(await self.send_public("2", quote=-812, bot_id=455))
        self.assertFalse(await self.send_public("2", quote=-812, group=None))
        self.assertTrue(await self.send_public("2", quote=-812, user=55))
        self.assertIn("智能做题", self.latest_text())
        self.clock.return_value = 14.9
        await self.send_public("2", quote=-812, user=55)
        self.assertEqual(self.bot.send.await_count, 2)
        await self.send_public("2", quote=-812, user=56)
        self.assertEqual(self.bot.send.await_count, 3)
        self.clock.return_value = 60
        self.assertFalse(await self.send_public("2", quote=-812, user=55))

    async def test_elevator_topics_are_left_to_the_game(self):
        for topic in ("地图", "道具", "词条", "合成", "黑市", "兑换", "难度", "药品", "流派"):
            self.assertFalse(await self.send_public(f"帮助 {topic}"))
        for command in ("电梯", "电梯帮助", "电梯菜单", "怎么玩", "查 秃鹫嗅觉"):
            self.assertFalse(await self.send_public(command))
        await self.send_public("帮助 无尽电梯")
        self.assertIn("跨群搜打撤", self.latest_text())

    async def test_admin_help_only_sends_to_an_allowed_private_user(self):
        for message in (event("管理帮助"), event("管理帮助", group=None, user=90)):
            self.assertFalse(self.help._match_admin_help(message))
            await self.help.send_admin_help(self.bot, message)
        self.bot.send.assert_not_awaited()
        self.assertFalse(await self.send_public("帮助", group=None, user=90))

        private = event("/admin help", group=None)
        self.assertTrue(self.help._match_admin_help(private))
        await self.help.send_admin_help(self.bot, private)
        self.assertIn("/reload", self.latest_text())
        self.assertIn("聊天 意识层", self.latest_text())
        self.assertIn("聊天 生图间隔", self.latest_text())
        self.assertIn("色色 帮助", self.latest_text())
        self.assertEqual(self.bot.send.await_args.args[1].type, "text")
        await self.help.send_admin_help(self.bot, private)
        self.bot.send.assert_awaited_once()

    def test_public_text_uses_live_values_and_never_contains_the_manual(self):
        self.chat.image_quota = SimpleNamespace(ZERO_AFFECTION_LIMIT=1800, MAX_DAILY_LIMIT=33_000)
        self.chat.memory = SimpleNamespace(MEMORY_THRESHOLD=53, MAX_MEMORIES=31)
        self.chat.aff = SimpleNamespace(DAILY_GAIN_CAP=9, DAILY_LOSS_CAP=13)
        self.assertIn("1,800", self.help._images(self.chat))
        self.assertIn("33,000", self.help._images(self.chat))
        self.assertIn("北京时间零点", self.help._images(self.chat))
        affection = self.help._affection(self.chat)
        self.assertIn("超过 53 分", affection)
        self.assertIn("31 条", affection)
        self.assertIn("9 分、扣 13 分", affection)
        self.assertIn("主动接话当前关闭", self.help._conversation(self.chat))
        self.chat.awareness.is_enabled = lambda: True
        self.assertIn("主动接话已开启", self.help._conversation(self.chat))
        self.plugins.pop("endless_elevator")
        self.assertIn("暂未加载", self.help._elevator(self.chat))
        public = [self.help._overview(self.chat), *[render(self.chat) for render in self.help._RENDER.values()]]
        for text in public:
            self.assertLessEqual(len(text.splitlines()), 16)
            for token in ("管理帮助", "群列表", "加机器人", "/reload", "色色"):
                self.assertNotIn(token, text)
