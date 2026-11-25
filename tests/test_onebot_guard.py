"""Offline regression tests for account binding and duplicate-message handling."""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qqctl
from onebot_guard import ActionFailed, Adapter, OneBotAdapter


def message(self_id=12345, message_id=1, user_id=12346, text="/roll"):
    return {
        "time": 42, "self_id": self_id, "post_type": "message",
        "message_type": "private", "sub_type": "friend", "message_id": message_id,
        "user_id": user_id, "message": text, "raw_message": text,
        "font": 0, "sender": {"user_id": user_id, "nickname": "test"},
    }


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        driver = SimpleNamespace(config=SimpleNamespace(qqbot_account="12345"))
        with patch.object(OneBotAdapter, "__init__", return_value=None):
            self.adapter = Adapter(driver)

    def test_wrong_account_cannot_register_or_deliver_an_event(self):
        self.assertIsNone(self.adapter.json_to_event({
            "post_type": "meta_event", "meta_event_type": "lifecycle",
            "sub_type": "connect", "time": 42, "self_id": 99999,
        }))
        self.assertIsNone(self.adapter.json_to_event(message(self_id=99999)))
        self.assertIsNotNone(self.adapter.json_to_event(message()))

    def test_replay_is_ignored_but_new_messages_and_other_peers_are_accepted(self):
        self.assertIsNotNone(self.adapter.json_to_event(message()))
        self.assertIsNone(self.adapter.json_to_event(message()))
        self.assertIsNotNone(self.adapter.json_to_event(message(message_id=2)))
        self.assertIsNotNone(self.adapter.json_to_event(message(user_id=12347)))

    def test_only_admin_private_messages_and_pokes_reach_matchers(self):
        admins = {"12346"}
        self.adapter._private_admins = lambda: admins
        self.assertIsNotNone(self.adapter.json_to_event(message()))
        self.assertIsNone(self.adapter.json_to_event(message(user_id=12347)))
        poke = dict(time=42, self_id=12345, post_type="notice", notice_type="notify",
                    sub_type="poke", user_id=12347, target_id=12345)
        self.assertIsNone(self.adapter.json_to_event(poke))
        self.assertIsNotNone(self.adapter.json_to_event({**poke, "group_id": 42}))
        admins.clear()
        self.assertIsNone(self.adapter.json_to_event(message(message_id=2)))

    async def test_private_outputs_to_non_admins_are_blocked_before_send(self):
        self.adapter._private_admins = lambda: {"12346"}
        bot = SimpleNamespace(self_id="12345")
        with patch.object(OneBotAdapter, "_call_api", new_callable=AsyncMock) as forward:
            for action in ("send_private_msg", "send_msg", "send_private_forward_msg", "friend_poke"):
                with self.assertRaises(ActionFailed):
                    await self.adapter._call_api(bot, action, user_id=12347)
            forward.assert_not_awaited()
            await self.adapter._call_api(bot, "send_private_msg", user_id=12346)
            self.assertEqual(forward.await_count, 1)

    async def test_wrong_account_cannot_send_an_api_request(self):
        with patch.object(OneBotAdapter, "_call_api", new_callable=AsyncMock) as forward:
            with self.assertRaises(ActionFailed):
                await self.adapter._call_api(SimpleNamespace(self_id="99999"), "send_msg")
            forward.assert_not_awaited()

    def test_non_admin_private_access_is_limited_to_own_history_command(self):
        self.adapter._private_admins = lambda: {"12346"}
        for i, text in enumerate(("好感度历史", "/好感度 记录", "/affection history"), 1):
            self.assertIsNotNone(self.adapter.json_to_event(message(message_id=i, user_id=12347, text=text)))
        for i, text in enumerate(("好感度 重置", "好感度 历史 12346", "你好"), 4):
            self.assertIsNone(self.adapter.json_to_event(message(message_id=i, user_id=12347, text=text)))

    async def test_private_elevator_reply_is_paced_and_bound_to_the_requester(self):
        self.adapter._private_admins = lambda: {"12346"}
        bot = SimpleNamespace(self_id="12345")
        async def play(_bot, _event, send):
            await send("电梯大厅")
            return True
        module = SimpleNamespace(
            private_candidate=lambda event: event.get_plaintext() == "电梯",
            handle_private_event=AsyncMock(side_effect=play),
        )
        with patch("onebot_guard.get_plugin", return_value=SimpleNamespace(module=module)), \
                patch.object(OneBotAdapter, "_call_api", new_callable=AsyncMock) as forward:
            self.assertIsNone(self.adapter.json_to_event(message(user_id=12347, text="你好")))
            event = self.adapter.json_to_event(message(user_id=12347, text="电梯"))
            self.assertIsNotNone(event)
            self.assertTrue(self.adapter.uses_private_elevator(event))
            self.assertTrue(await self.adapter.dispatch_private_elevator(bot, event))
            forward.assert_awaited_once_with(bot, "send_private_msg", user_id=12347, message="电梯大厅")
            with self.assertRaises(ActionFailed):
                await self.adapter._call_api(bot, "send_private_msg", user_id=12347, message="无关消息")
            self.assertIsNone(self.adapter.json_to_event(message(user_id=12347, text="电梯")))

    async def test_private_elevator_dispatch_preserves_existing_query_routes(self):
        self.adapter._private_admins = lambda: {"12346"}
        bot = SimpleNamespace(self_id="12345")
        for index, text in enumerate(("好感度历史", "图像配额"), 1):
            event = self.adapter.json_to_event(message(user_id=12347, message_id=index, text=text))
            self.assertFalse(self.adapter.uses_private_elevator(event))
            self.assertFalse(await self.adapter.dispatch_private_elevator(bot, event))

    async def test_private_image_quota_returns_only_to_the_requester(self):
        self.adapter._private_admins = lambda: {"12346"}
        bot = SimpleNamespace(self_id="12345")
        event = self.adapter.json_to_event(message(user_id=12347, text="/图像配额"))
        self.assertIsNotNone(event)
        self.assertIsNone(self.adapter.json_to_event(message(message_id=2, user_id=12347, text="图像配额 12346")))
        ordinary = self.adapter.json_to_event(message(message_id=3, user_id=12346, text="你好"))
        with patch.object(OneBotAdapter, "_call_api", new_callable=AsyncMock) as forward:
            await self.adapter.send_image_quota(bot, event, "剩余 4,000 token")
            self.assertEqual(forward.await_args.kwargs["user_id"], 12347)
            with self.assertRaises(ActionFailed):
                await self.adapter.send_image_quota(bot, ordinary, "不可发送")
            forward.assert_awaited_once()

    async def test_group_history_is_sent_only_to_the_requester(self):
        self.adapter._private_admins = lambda: {"12346"}
        self.adapter._send_interval = 0
        bot = SimpleNamespace(self_id="12345")
        event = self.adapter.json_to_event({
            **message(user_id=12347, text="好感度历史"),
            "message_type": "group", "sub_type": "normal", "group_id": 67890,
        })
        ordinary = self.adapter.json_to_event(message(text="你好"))
        with patch.object(OneBotAdapter, "_call_api", new_callable=AsyncMock) as forward:
            forward.side_effect = [[], ActionFailed(wording="no such temp session"), {}, {}]
            await self.adapter.send_affection_history(bot, event, ["[CQ:at,qq=all]", "下一页"])
            self.assertEqual([item.args[1] for item in forward.await_args_list], [
                "get_friend_list", "send_private_msg", "send_private_msg", "send_private_msg",
            ])
            private_calls = [item.kwargs for item in forward.await_args_list[1:]]
            self.assertTrue(all(item["user_id"] == event.user_id for item in private_calls))
            self.assertEqual([item.get("group_id") for item in private_calls], [event.group_id, None, None])
            self.assertEqual(private_calls[1]["message"][0].type, "text")
            with self.assertRaises(ActionFailed):
                await self.adapter.send_affection_history(bot, ordinary, ["不发送"])
            self.assertEqual(forward.await_count, 4)

    async def test_concurrent_sends_are_spaced_without_delaying_status_queries(self):
        bot = SimpleNamespace(self_id="12345")
        calls = []

        async def forward(_bot, api, **data):
            calls.append((api, time.monotonic()))

        with patch.object(OneBotAdapter, "_call_api", side_effect=forward):
            await asyncio.gather(
                self.adapter._call_api(bot, "send_private_msg"),
                self.adapter._call_api(bot, "send_group_msg"),
                self.adapter._call_api(bot, "get_status"),
            )
        self.assertEqual([api for api, _ in calls], [
            "send_private_msg", "get_status", "send_group_msg",
        ])
        self.assertGreaterEqual(calls[2][1] - calls[0][1], 0.49)


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def test_console_rejects_wrong_login_before_sending(self):
        client = qqctl.OneBot()
        with patch.object(qqctl, "expected_account", return_value="12345"):
            with patch.object(client, "_request", new_callable=AsyncMock) as request:
                request.return_value = {"retcode": 0, "data": {"user_id": 99999}}
                with self.assertRaises(PermissionError):
                    await client.call("send_private_msg", user_id=84848, message="test")
                request.assert_awaited_once_with("get_login_info")

    async def test_console_allows_the_verified_login(self):
        client = qqctl.OneBot()
        with patch.object(qqctl, "expected_account", return_value="12345"):
            with patch.object(client, "_request", new_callable=AsyncMock) as request:
                request.side_effect = [
                    {"retcode": 0, "data": {"user_id": 12345}},
                    {"retcode": 0, "data": {"message_id": 7}},
                ]
                response = await client.call("send_private_msg", user_id=84848, message="test")
                self.assertEqual(response["data"]["message_id"], 7)
                self.assertEqual(request.await_args_list, [
                    call("get_login_info"),
                    call("send_private_msg", user_id=84848, message="test"),
                ])
