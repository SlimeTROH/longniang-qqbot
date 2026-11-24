"""Scoped OneBot context tools and proactive chat, tested without external messages."""
import asyncio
import importlib
import json
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from test_chat_group_settings import load_handlers

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("group_awareness_test")
package.__path__ = [str(ROOT / "plugins/chat_bot")]
sys.modules[package.__name__] = package
awareness = importlib.import_module(f"{package.__name__}.awareness")
tools = importlib.import_module(f"{package.__name__}.context_tools")
llm = importlib.import_module(f"{package.__name__}.llm")


def event(message_id=10, text="大家周六有空吗", *, group_id=321, user_id=100, private=False):
    body = Message(MessageSegment.text(text))
    return SimpleNamespace(
        group_id=group_id, self_id=999, user_id=user_id, message_id=message_id, time=1_790_000_000,
        message_type="private" if private else "group", get_message=lambda: body,
        sender=SimpleNamespace(card=f"成员{user_id}"),
    )


class AwarenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_off_skips_group_judgment_and_turning_off_cancels_it(self):
        monitor = awareness.GroupAwareness()
        self.addAsyncCleanup(monitor.close)
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def decide(*_args):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        namespace = {
            "awareness": awareness, "OPENAI_KEY": "mock", "Command": lambda: None,
            "_is_private": lambda _: False, "_current_group": lambda e: str(e.group_id),
            "_group_allowed": lambda _: True, "persona": SimpleNamespace(name="龙娘"),
            "message_io": SimpleNamespace(addressed_to_bot=lambda _: False), "context_tools": tools,
        }
        load_handlers(
            "plugins/chat_bot/__init__.py",
            {"_can_observe_group", "observe_group_message", "_may_join_group"}, namespace,
        )
        incoming = event()
        observe = namespace["observe_group_message"]
        with TemporaryDirectory() as directory, \
             patch.object(awareness, "_STATE_FILE", Path(directory) / "switch.json"), \
             patch.object(awareness, "_enabled", False), \
             patch.object(awareness, "rooms", monitor), \
             patch.object(awareness, "decide", AsyncMock(side_effect=decide)) as judgment:
            self.assertFalse(awareness._load_enabled())
            self.assertFalse(namespace["_can_observe_group"](incoming))
            await observe(None, incoming)
            judgment.assert_not_awaited()
            self.assertEqual(monitor.history(incoming), [])

            await awareness.set_enabled(True)
            self.assertTrue(awareness._load_enabled())
            await observe(None, incoming)
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(namespace["_may_join_group"](incoming))
            join = AsyncMock()
            pending = asyncio.create_task(monitor.join(incoming, join))

            await awareness.set_enabled(False)
            await asyncio.wait_for(pending, 1)
            self.assertFalse(awareness._load_enabled())
            self.assertTrue(cancelled.is_set())
            self.assertFalse(namespace["_may_join_group"](incoming))
            await observe(None, incoming)
            judgment.assert_awaited_once()
            join.assert_not_awaited()

    async def test_each_message_is_judged_with_scoped_recent_chat_and_direct_never_joins(self):
        monitor = awareness.GroupAwareness()
        self.addAsyncCleanup(monitor.close)
        direct = event(1, "已经联系你", user_id=101)
        other_group = event(2, "其他群的秘密", group_id=456)
        current = event(3, "你们说去哪里")
        join = AsyncMock()
        with patch.object(awareness, "decide", AsyncMock(return_value="商量周六去哪儿")) as decide:
            monitor.observe(None, direct, "mock", "龙娘", eligible=False, direct=True)
            monitor.record_reply(direct, "龙娘", "要不要去公园", {"message_id": 41})
            monitor.observe(None, other_group, "mock", "龙娘", eligible=True, direct=False)
            monitor.observe(None, current, "mock", "龙娘", eligible=True, direct=False)
            await monitor.join(direct, join)
            await monitor.join(current, join)
        join.assert_awaited_once_with("商量周六去哪儿")
        self.assertEqual(decide.await_count, 3)
        histories = [call.args[2] for call in decide.await_args_list]
        current_history = next(rows for rows in histories if rows[-1]["message_id"] == "3")
        self.assertEqual([row["content"] for row in current_history], [
            "已经联系你", "要不要去公园", "你们说去哪里",
        ])
        self.assertEqual(len({row["speaker_id"] for row in current_history}), 3)
        self.assertIn(False, [call.args[3] for call in decide.await_args_list])

    async def test_direct_message_preempts_an_active_proactive_reply(self):
        monitor = awareness.GroupAwareness()
        self.addAsyncCleanup(monitor.close)
        entered = asyncio.Event()
        aborted = asyncio.Event()

        async def reply(_focus):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                aborted.set()

        current = event(1)
        with patch.object(awareness, "decide", AsyncMock(return_value="接话")):
            monitor.observe(None, current, "mock", "龙娘", eligible=True, direct=False)
            pending = asyncio.create_task(monitor.join(current, reply))
            await asyncio.wait_for(entered.wait(), 1.0)
            monitor.observe(None, event(2), "mock", "龙娘", eligible=False, direct=True)
            await asyncio.wait_for(pending, 1.0)
        self.assertTrue(aborted.is_set())

    async def test_luna_decision_does_not_emit_text_or_join_a_handled_event(self):
        response = httpx.Response(200, json={"status": "completed", "output_text": '{"join":true,"focus":"建议去公园"}'})
        client = Mock(post=AsyncMock(return_value=response))
        history = [{"content": "上次去公园的事", "speaker_name": "群友"}, {"content": "这次去哪儿？"}]
        with patch.object(llm, "_chat_client", return_value=client):
            self.assertEqual(await awareness.decide("mock", "龙娘", history, False), "")
            self.assertEqual(await awareness.decide("mock", "龙娘", history, True), "建议去公园")
        payload = client.post.await_args.kwargs["json"]
        self.assertEqual(payload["model"], "gpt-6-luna")
        self.assertEqual(payload["reasoning"]["effort"], "none")
        request = json.loads(payload["input"][1]["content"][0]["text"])
        self.assertEqual(request["recent_messages"], history)


class ContextToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_member_lookup_can_supply_a_real_image_and_only_mention_verified_members(self):
        roster = [
            {"group_id": 321, "user_id": 220, "card": "小王", "role": "member"},
            {"group_id": 456, "user_id": 221, "card": "外群", "role": "member"},
        ]
        bot = SimpleNamespace(call_api=AsyncMock(side_effect=[roster, {
            **roster[0], "title": "群友", "age": 27,
        }]))
        lookup = tools.ConversationLookup(bot, event(), [])
        entries = json.loads(await lookup.list_members({"query": "小", "page": 1}))
        self.assertEqual(entries["members"], [{"user_id": "220", "card": "小王", "role": "member"}])
        with patch.object(lookup, "_download_avatar", AsyncMock(return_value=(b"avatar", "image/png"))):
            person = await lookup.member({"member": "小王", "with_avatar": True})
        self.assertEqual(person[1]["type"], "input_image")
        self.assertNotIn("age", person[0]["text"])
        bot.call_api.assert_awaited_with("get_group_member_info", group_id=321, user_id=220)
        outgoing = await lookup.reply(
            "[CQ:at,qq=all] 来试一下", mentions=("220", "220", "221", "all", "999"), avatar="220",
        )
        self.assertEqual([segment.type for segment in outgoing], ["reply", "at", "text", "text", "image"])
        self.assertEqual(str(outgoing[1].data["qq"]), "220")
        self.assertIn("[CQ:at,qq=all]", outgoing[3].data["text"])
        ordinary = await lookup.reply("我插一句", quote=False)
        self.assertEqual([segment.type for segment in ordinary], ["text"])

    async def test_history_search_uses_current_group_and_excludes_unrelated_rows(self):
        message = {"group_id": 321, "message_type": "group", "user_id": 220, "sender": {"card": "小王"}}
        bot = SimpleNamespace(call_api=AsyncMock(return_value={"messages": [
            {**message, "message_id": 10, "time": 1_790_000_050, "raw_message": "这是当前消息，不该命中公园"},
            {**message, "message_id": 9, "message_seq": 90, "time": 1_790_000_000, "raw_message": "周六在公园集合"},
            {**message, "group_id": 456, "message_id": 8, "raw_message": "外群公园"},
        ]}))
        lookup = tools.ConversationLookup(bot, event(), [])
        result = json.loads(await lookup.search_history({"keywords": ["公园"], "speaker": "小王", "before": "0"}))
        self.assertEqual([row["text"] for row in result["messages"]], ["周六在公园集合"])
        self.assertEqual(result["next_cursor"], "90")
        bot.call_api.assert_awaited_once_with("get_group_msg_history", group_id=321, count=40)
        await lookup.search_history({"keywords": [], "speaker": "*", "before": "90"})
        bot.call_api.assert_awaited_with("get_group_msg_history", group_id=321, count=40, message_seq=90)
        private = tools.ConversationLookup(bot, event(private=True), [{"content": "我的私聊记录", "speaker_id": "100"}])
        self.assertEqual(set(private.tools()), {"search_chat_history"})
        self.assertIn("我的私聊记录", await private.search_history({"keywords": [], "speaker": "*", "before": "0"}))
        self.assertEqual(bot.call_api.await_count, 2)

    async def test_silent_multimodal_tool_reaches_the_model_without_a_progress_message(self):
        avatar = [{"type": "input_text", "text": "头像"},
                  {"type": "input_image", "image_url": "data:image/png;base64," + "AAAA" * 40000, "detail": "low"}]
        client = Mock(post=AsyncMock(side_effect=[
            httpx.Response(200, json={"status": "completed", "output": [{
                "type": "function_call", "call_id": "call_member", "name": "get_group_member",
                "arguments": '{"member":"220","with_avatar":true}',
            }]}),
            httpx.Response(200, json={"status": "completed", "output_text": '{"reply":"找到了","mentions":[],"avatar":"220"}'}),
        ]))
        lookup = llm.ChatTool(
            "群成员头像", {"member": {"type": "string"}, "with_avatar": {"type": "boolean"}},
            AsyncMock(return_value=avatar), silent=True,
        )
        announce = AsyncMock()
        with patch.object(llm, "_chat_client", return_value=client):
            result = await llm.chat(
                [{"role": "user", "content": "看头像"}], "persona", "mock", "gpt-6.2-sol",
                tools={"get_group_member": lookup}, on_tool_start=announce,
            )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.avatar, "220")
        announce.assert_not_awaited()
        payload = client.post.await_args.kwargs["json"]
        self.assertNotIn("notice", payload["tools"][0]["parameters"]["properties"])
        self.assertEqual(payload["input"][-1]["output"], avatar)
