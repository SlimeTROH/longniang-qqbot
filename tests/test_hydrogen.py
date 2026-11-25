"""Hydrogen migration regressions: fake transport, no QQ or paid API calls."""
import ast
import asyncio
import copy
import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_chat():
    package = types.ModuleType("hydrogen_test_chat")
    package.__path__ = []
    sys.modules[package.__name__] = package
    guard = types.ModuleType(package.__name__ + ".guard")
    guard.SECURITY_BLOCK = "test safety rules"
    sys.modules[guard.__name__] = guard
    modules = []
    for name in ("llm", "vertex", "vision", "conversation_scope", "message_io", "google_vision"):
        spec = importlib.util.spec_from_file_location(
            package.__name__ + "." + name, ROOT / "plugins/chat_bot" / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        setattr(package, name, module)
        spec.loader.exec_module(module)
        modules.append(module)
    return modules[:3]


class HydrogenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.llm, self.v, self.vision = load_chat()
        self.temp = tempfile.TemporaryDirectory()
        self.v._CACHE_FILE = Path(self.temp.name) / "cache.json"
        self.v._REFS_CACHE = [(b"reference image", "image/png", "stable-image-hash")]
        self.v.CACHE_START_DELAY = 0
        self.persona = "stable persona " * 600
        self.messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)}
                         for i in range(20)] + [{"role": "user", "content": "hello\nruntime state"}]
        self.client = Mock(post=AsyncMock(return_value=httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": '{"reply":"hello"}'}]},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10000, "cachedContentTokenCount": 8000},
        })))

    async def asyncTearDown(self):
        await self.v.close_clients()
        await self.llm.close_clients()
        self.temp.cleanup()

    async def test_native_compaction_replays_all_items_and_checks_tool_continuation(self):
        output = [{"type": "message", "role": "user", "content": "retained"},
                  {"type": "compaction", "encrypted_content": "opaque"}]
        call = {"type": "function_call", "call_id": "lookup_1", "name": "lookup",
                "arguments": '{"notice":"Looking up the source."}'}
        client = Mock(post=AsyncMock(side_effect=[
            httpx.Response(200, json={"output": output}),
            httpx.Response(200, json={"input_tokens": 800}),
            httpx.Response(200, json={"output": [call], "usage": {"output_tokens": self.llm.OPENAI_INPUT_LIMIT}}),
        ]))
        with patch.object(self.llm, "_chat_client", return_value=client):
            state, error = await self.llm.compact_history(self.messages[:4], "fake", "gpt-6.1-terra")
            result = await self.llm.chat(self.messages, self.persona, "fake", "gpt-6.1-terra",
                                         compaction=state,
                                         tools={"lookup": self.llm.ChatTool("Find source", {}, AsyncMock(return_value="source"))})
        self.assertEqual(error, "")
        compact, count, reply = client.post.await_args_list
        self.assertTrue(compact.args[0].endswith("/compact"))
        transcript, = compact.kwargs["json"]["input"]
        self.assertEqual(transcript["role"], "assistant")
        self.assertEqual(json.loads(transcript["content"].split("\n", 1)[1]), self.messages[:4])
        self.assertTrue(count.args[0].endswith("/input_tokens"))
        self.assertEqual(count.kwargs["json"]["input"], output)
        self.assertEqual(reply.kwargs["json"]["input"][1:3], output)
        self.assertIn("短上下文输入预算", result.error)

    async def test_desktop_sticker_after_reply_and_middle_mention_reaches_responses(self):
        from nonebot.adapters.onebot.v11 import Message, MessageSegment
        from PIL import Image

        media = sys.modules["hydrogen_test_chat.message_io"]
        url = "https://example.test/sticker.gif"
        sticker = {"type": "mface", "data": {"summary": "[挥手]", "url": url}}
        original = Message([MessageSegment.reply(17), MessageSegment.at(100),
                            MessageSegment.text("这是哪来的？")])
        event = types.SimpleNamespace(self_id=100, group_id=5, original_message=original,
                                      get_message=lambda: Message([original[-1]]), is_tome=lambda: False)
        self.assertTrue(media.addressed_to_bot(event))
        inline = types.SimpleNamespace(get_message=lambda: Message([MessageSegment(**sticker), *original[1:]]))
        self.assertEqual(media.extract_message(inline)[1][0]["url"], url)
        self.assertTrue(media.extract_message(inline)[1][0]["_is_sticker"])
        self.assertEqual(media.parse_cq(f"[CQ:mface,summary=挥手,url={url}]")[1][0]["url"], url)
        bot = Mock(call_api=AsyncMock(return_value={"message_type": "group", "group_id": 5,
                                                   "message": [sticker], "sender": {"user_id": 300}}))
        text, attachments, _ = await media.fetch_quote(bot, event)
        self.assertEqual(text, "[挥手]")
        self.assertTrue(attachments[0]["_is_sticker"])
        bot.call_api.assert_awaited_once_with("get_msg", message_id="17")

        frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
        gif = io.BytesIO()
        frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:], duration=50)
        downloader = Mock(get=AsyncMock(return_value=httpx.Response(200, content=gif.getvalue())))
        api = Mock(post=AsyncMock(return_value=httpx.Response(200, json={"output_text": '{"reply":"hello"}'})))
        with patch.object(media, "_image_client", return_value=downloader):
            image = await media.fetch_image(attachments[0], bot)
        with patch.object(self.llm, "_chat_client", return_value=api):
            result = await self.llm.chat([{"role": "user", "content": "这张图"}], self.persona,
                                         "fake", "gpt-6.1-terra", images=[image])
        self.assertTrue(result.ok)
        payload = api.post.await_args.kwargs["json"]
        image_data = payload["input"][-1]["content"][-1]["image_url"]
        self.assertTrue(image_data.startswith("data:image/jpeg;base64,"))
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(image_data.split(",", 1)[1]))).size, (8, 8))

    async def test_qq_markdown_quote_includes_inline_images_in_responses(self):
        from nonebot.adapters.onebot.v11 import Message, MessageSegment
        from PIL import Image

        media = sys.modules["hydrogen_test_chat.message_io"]
        urls = [f"https://qqbot.ugcimg.cn/resource/test/{index}" for index in (1, 2)]
        content = f"![第一张 #8px #6px]({urls[0]})\n![第二张 #8px #6px](<{urls[1]}>)"
        markdown = {"type": "markdown", "data": {"content": content}}
        event = types.SimpleNamespace(group_id=5, original_message=Message(MessageSegment.reply(17)))
        bot = Mock(call_api=AsyncMock(return_value={
            "message_type": "group", "group_id": 5,
            "message": [{"type": "text", "data": {"text": "原消息"}}, markdown],
        }))
        text, attachments, _ = await media.fetch_quote(bot, event)
        self.assertEqual(text, "原消息")
        self.assertEqual([part["url"] for part in attachments], urls)
        self.assertEqual(media.parse_cq(str(MessageSegment(**markdown)))[1], attachments)

        picture = io.BytesIO()
        Image.new("RGB", (8, 6), "red").save(picture, format="PNG")
        transport = httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "image/png"}, content=picture.getvalue(),
        ))
        async with httpx.AsyncClient(transport=transport) as downloader:
            with patch.object(media, "_image_client", return_value=downloader):
                images = [await media.fetch_image(part, bot) for part in attachments]
        self.assertEqual(images, [picture.getvalue()] * 2)
        bot.call_api.assert_awaited_once_with("get_msg", message_id="17")

        api = Mock(post=AsyncMock(return_value=httpx.Response(200, json={"output_text": '{"reply":"hello"}'})))
        with patch.object(self.llm, "_chat_client", return_value=api):
            result = await self.llm.chat(
                [{"role": "user", "content": "看下引用里的图"}], self.persona, "fake", "gpt-6.1-terra", images=images,
            )
        self.assertTrue(result.ok)
        parts = api.post.await_args.kwargs["json"]["input"][-1]["content"]
        self.assertEqual([part["type"] for part in parts], ["input_text", "input_image", "input_image"])

    async def test_history_image_follows_a_quoted_bot_reply_to_same_group_source(self):
        from nonebot.adapters.onebot.v11 import Message, MessageSegment
        from nonebot.adapters.onebot.v11.event import Reply

        media = sys.modules["hydrogen_test_chat.message_io"]
        event = types.SimpleNamespace(
            self_id=100, group_id=5, message_id=91, message_type="group",
            original_message=Message(MessageSegment.reply(-17)),
            reply=Reply(time=1, message_type="group", group_id=5, message_id=-17, real_id=-17,
                        sender={"user_id": 100},
                        message=Message([MessageSegment.reply(-11), MessageSegment.text("我刚看过这张图")])),
        )
        image = {"file":"photo.jpg", "url":"https://gchat.qpic.cn/picture"}
        bot = Mock(self_id="100", call_api=AsyncMock(side_effect=[
            {"message_type":"group", "group_id":5, "sender":{"user_id":100},
             "raw_message":"[CQ:reply,id=-3]我引用另一次答复"},
            {"message_type":"group", "group_id":5, "sender":{"user_id":500},
             "message":[{"type":"image", "data":image}]},
        ]))
        self.assertEqual(await media.fetch_quoted_reply_images(bot, event), [image])
        self.assertEqual(bot.call_api.await_args_list, [
            call("get_msg", message_id="-11"), call("get_msg", message_id="-3"),
        ])

    async def test_history_image_does_not_follow_user_replies_or_leak_other_scopes(self):
        from nonebot.adapters.onebot.v11 import Message, MessageSegment

        media = sys.modules["hydrogen_test_chat.message_io"]
        event = types.SimpleNamespace(self_id=100, group_id=5, message_id=90, message_type="group",
                                      original_message=Message(MessageSegment.reply(17)))
        own_reply = {"message_type":"group", "group_id":5, "sender":{"user_id":100},
                     "message":[{"type":"reply", "data":{"id":"18"}}]}
        user_reply = {**own_reply, "sender":{"user_id":200}}
        image = {"type":"image", "data":{"file":"image.jpg"}}
        for responses, calls in (
            ([user_reply], 1),
            ([own_reply, user_reply], 2),
            ([own_reply, {**user_reply, "group_id":6, "message":[image]}], 2),
            ([own_reply, {**user_reply, "message_type":"private", "message":[image]}], 2),
            ([{**own_reply, "message":[{"type":"reply", "data":{"id":"17"}}]}], 1),
        ):
            with self.subTest(calls=calls, response=responses[-1]):
                bot = Mock(self_id="100", call_api=AsyncMock(side_effect=responses))
                self.assertEqual(await media.fetch_quoted_reply_images(bot, event), [])
                self.assertEqual(bot.call_api.await_count, calls)
        event.message_type = "private"
        bot = Mock(self_id="100", call_api=AsyncMock())
        self.assertEqual(await media.fetch_quoted_reply_images(bot, event), [])
        bot.call_api.assert_not_awaited()

    async def test_markdown_image_download_uses_qq_cdn_without_following_redirects(self):
        media = sys.modules["hydrogen_test_chat.message_io"]
        url = "https://qqbot.ugcimg.cn/resource/image"
        body = (
            f"![图]({url}) ![重复]({url}) [链接]({url}/page) "
            "![其他服务器](https://qqbot.ugcimg.cn.example.test/file) "
            "![本机](https://127.0.0.1/file) ![非标准端口](https://qqbot.ugcimg.cn:8080/file)"
        )
        _, attachments = media.segments_to_parts([{"type": "markdown", "data": {"content": body}}])
        self.assertEqual([part["url"] for part in attachments], [url])
        requests = []

        def redirect(request):
            requests.append(str(request.url))
            return httpx.Response(302, headers={"location": "https://127.0.0.1/file"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True) as downloader:
            with patch.object(media, "_image_client", return_value=downloader):
                self.assertIsNone(await media.fetch_image(attachments[0]))
        self.assertEqual(requests, [url])

    async def test_group_quote_respects_the_current_message_mentions(self):
        from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
        from nonebot.adapters.onebot.v11.bot import _check_at_me, _check_reply

        media = sys.modules["hydrogen_test_chat.message_io"]
        own = MessageSegment.at(100)
        other = MessageSegment.at(300)
        quote = MessageSegment.reply(17)
        text = MessageSegment.text(" 看看这道题")
        gap = MessageSegment.text(" ")

        for sender, segments, expected in (
            (100, [quote, other, text], False),
            (100, [quote, own, gap, other, text], False),
            (100, [quote, own, text], True),
            (100, [quote, text], True),
            (100, [quote, own, gap, other, text, own], True),
            (100, [quote, other, text, own], True),
            (300, [quote, other, text, own], True),
            (300, [quote, other, text], False),
            (300, [other, text, own], True),
            (300, [other, text], False),
        ):
            with self.subTest(sender=sender, segments=str(Message(segments))):
                event = GroupMessageEvent(
                    time=1, self_id=100, post_type="message", message_type="group", sub_type="normal",
                    user_id=200, group_id=5, message_id=18, raw_message=str(Message(segments)), font=0,
                    message=copy.deepcopy(Message(segments)), sender={"user_id": 200},
                )
                bot = Mock(get_msg=AsyncMock(return_value={
                    "time": 1, "message_type": "group", "message_id": 17, "real_id": 17,
                    "message": Message("原消息"), "sender": {"user_id": sender},
                }))
                await _check_reply(bot, event)
                _check_at_me(bot, event)
                self.assertIs(media.addressed_to_bot(event), expected)

    def test_trace_passes_more_than_three_ranked_pages_and_match_kind(self):
        vision = sys.modules["hydrogen_test_chat.google_vision"]
        pages = [{"url": f"https://example.test/page-{i}", "pageTitle": f"<b>Título {i}</b>"} for i in range(10)]
        pages[6]["fullMatchingImages"] = [{"url": "https://example.test/full.jpg"}]
        result = vision._web_result({"pagesWithMatchingImages": pages,
                                     "partialMatchingImages": [{"url": "https://example.test/partial.jpg"}]}, 10, 0)
        self.assertEqual(len(result.pages), 10)
        self.assertIn("[完全匹配] Título 6 https://example.test/page-6", result.text)
        self.assertIn(pages[-1]["url"], result.text)
        self.assertIn("https://example.test/partial.jpg", result.text)

    async def test_sol_caches_static_prefix_and_bounds_input_with_no_output_cap(self):
        response = httpx.Response(200, json={
            "status": "completed", "output_text": '{"reply":"hello"}',
        })
        client = Mock(post=AsyncMock(return_value=response))
        old = [{"role": "user", "content": "旧消息" * 8000},
               {"role": "assistant", "content": "过早的回复"}]
        extra_long = {"role": "user", "content": "发言人开头" + "大长句" * 30000 + "关键结尾"}
        recent = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "reply"},
                  {"role": "user", "content": "second"}, {"role": "assistant", "content": "reply"}]
        messages = [*old, *recent, extra_long]
        with patch.object(self.llm, "_chat_client", return_value=client), \
             patch.object(self.vision, "compress_image", return_value=(b"fake", "image/png")):
            result = await self.llm.chat(messages, "static persona", "fake", "gpt-6.2-sol", images=[b"fake"] * 8)
        self.assertTrue(result.ok)
        body = client.post.call_args.kwargs["json"]
        self.assertNotIn("max_output_tokens", body)
        self.assertEqual(body["service_tier"], "default")
        self.assertEqual(body["prompt_cache_options"], {"mode": "explicit", "ttl": "30m"})
        self.assertIn("prompt_cache_breakpoint", body["input"][0]["content"][0])
        self.assertIn("prompt_cache_breakpoint", body["input"][3]["content"][0])
        self.assertNotIn("过早的回复", str(body["input"]))
        current, *images = body["input"][-1]["content"]
        self.assertTrue(current["text"].startswith("发言人开头"))
        self.assertTrue(current["text"].endswith("关键结尾"))
        self.assertNotIn("prompt_cache_breakpoint", current)
        self.assertLessEqual(len(current["text"].encode("utf-8")), self.llm.CHAT_INPUT_BYTES // 2)
        self.assertEqual(len(images), self.llm.CHAT_IMAGE_LIMIT)

    async def test_sol_image_read_error_retries_once_within_budget_without_vertex(self):
        attempts = 0
        limits = []
        cancelled = 0
        async def stall(*args, **kwargs):
            nonlocal attempts, cancelled
            attempts += 1
            limits.append(kwargs["timeout"])
            if attempts == 1:
                return self.llm.ChatResult(ok=False, error="OpenAI 请求失败：ReadError: disconnected")
            try:
                await asyncio.Future()
            finally:
                cancelled += 1
        with patch.object(self.llm, "OPENAI_IMAGE_BUDGET", .08), \
             patch.object(self.llm, "_chat_openai", side_effect=stall), \
             patch.object(self.v, "vertex_chat", new_callable=AsyncMock) as vertex:
            result = await asyncio.wait_for(self.llm.chat(
                self.messages, self.persona, "fake", "gpt-6.2-sol", images=[b"test"],
                gemini_model="gemini-test", vertex_auth="fake",
            ), timeout=.5)
        self.assertFalse(result.ok)
        self.assertEqual(result.via, "openai")
        self.assertEqual(attempts, 2)
        self.assertEqual(limits[0], .08)
        self.assertLessEqual(limits[1], limits[0])
        self.assertEqual(cancelled, 1)
        vertex.assert_not_awaited()

    def test_image_description_is_optional_for_unrelated_attachments(self):
        description = self.llm.REPLY_SCHEMA["schema"]["properties"]["image_desc"]["description"]
        block = self.llm.build_dynamic_block(5, "初识", "自然接话", has_image=True)
        self.assertIn('没有用到图片时填空字符串 ""', description)
        self.assertIn("先按当前正文和引用语境判断对方的话题", block)
        self.assertIn("引用附图和随手表情包可以只是背景或语气", block)

    async def test_terra_reply_carries_daily_affection_in_the_same_response(self):
        client = Mock(post=AsyncMock(return_value=httpx.Response(200, json={
            "status": "completed",
            "output_text": '{"reply":"hello","affection_event":"none","daily_affection":0.12,'
                           '"reason":"friendly greeting","image_desc":""}',
        })))
        run_tool = AsyncMock()
        with patch.object(self.llm, "_chat_client", return_value=client):
            result = await self.llm.chat(
                [{"role": "user", "content": "hi"}], "persona", "fake", "gpt-6.1-terra",
                tools={"search_web": self.llm.ChatTool("Search public sources", {}, run_tool)},
            )
        client.post.assert_awaited_once()
        run_tool.assert_not_awaited()
        schema = client.post.call_args.kwargs["json"]["text"]["format"]["schema"]
        self.assertIn("daily_affection", schema["required"])
        self.assertEqual(result.reply, "hello")
        self.assertEqual(result.daily_affection, 0.12)
        self.assertEqual(self.llm._parse_reply('{"reply":"x","daily_affection":999}').daily_affection, 0.5)
        self.assertIsNone(self.llm._parse_reply('{"reply":"x"}').daily_affection)

    def test_reply_trims_only_the_final_period(self):
        from json import dumps

        for original, expected in (
            ("先这样。回头聊。", "先这样。回头聊"),
            ("Look at version 1.2.", "Look at version 1.2"),
            ("先这样。……", "先这样。……"),
            ("Hmm...", "Hmm..."),
            ("你确定？！", "你确定？！"),
            ("行。🐉", "行。🐉"),
            ("她说「走了。」", "她说「走了。」"),
        ):
            with self.subTest(original=original):
                self.assertEqual(self.llm._parse_reply(dumps({"reply": original})).reply, expected)

    async def test_terra_controls_tools_and_uses_their_output_without_repeating_a_lookup(self):
        from json import dumps

        reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"}
        choice = {
            "type": "function_call", "call_id": "call_1", "name": "trace_image",
            "arguments": dumps({"image_index": 1, "notice": "我找找这幅画最早在哪出现的。"}),
        }
        client = Mock(post=AsyncMock(side_effect=[
            httpx.Response(200, json={"status": "completed", "output": [reasoning, choice]}),
            httpx.RemoteProtocolError("connection closed before headers"),
            httpx.Response(200, json={"status": "completed", "output_text": '{"reply":"找到了来源。"}'}),
        ]))
        events = []

        async def announce(text):
            events.append(("notice", text))

        async def trace(args):
            events.append(("tool", args["image_index"]))
            return "https://example.invalid/mock-source"

        with patch.object(self.llm, "_chat_client", return_value=client):
            result = await self.llm.chat(
                [{"role": "user", "content": "这幅画你能不能认出来是从哪儿流传的？"}],
                "persona", "fake", "gpt-6.1-terra",
                tools={"trace_image": self.llm.ChatTool("Find the source", {
                    "image_index": {"type": "integer", "enum": [1]},
                }, trace)},
                on_tool_start=announce,
            )
        self.assertTrue(result.ok)
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(events, [("notice", "我找找这幅画最早在哪出现的"), ("tool", 1)])
        self.assertEqual(client.post.await_count, 3)
        continuation = client.post.call_args.kwargs["json"]
        self.assertFalse(continuation["store"])
        self.assertEqual(continuation["input"][-3:], [reasoning, choice, {
            "type": "function_call_output", "call_id": "call_1", "output": "https://example.invalid/mock-source",
        }])

    async def test_repeated_tool_arguments_reuse_the_result_with_a_new_call_id(self):
        def request_tool(call_id, arguments):
            return httpx.Response(200, json={"status": "completed", "output": [{
                "type": "function_call", "name": "search_web", "call_id": call_id, "arguments": arguments,
            }]})

        client = Mock(post=AsyncMock(side_effect=[
            request_tool("call_1", '{"query":"example claim","notice":"我先查一下。"}'),
            request_tool("call_2", '{"notice":"我再核对一次。","query":"example claim"}'),
            request_tool("call_3", '{"query":"example claim","notice":"继续核对。"}'),
            request_tool("call_4", '{"query":"example claim","notice":"核对同一来源。"}'),
            httpx.Response(200, json={"status": "completed", "output_text": '{"reply":"查到了。"}'}),
        ]))
        run = AsyncMock(return_value="https://example.invalid/source")
        announce = AsyncMock()
        with patch.object(self.llm, "_chat_client", return_value=client):
            result = await self.llm.chat(
                [{"role": "user", "content": "核对一下这个说法。"}], "persona", "fake", "gpt-6.1-terra",
                tools={"search_web": self.llm.ChatTool("Search public sources", {"query": {"type": "string"}}, run)},
                on_tool_start=announce,
            )

        self.assertTrue(result.ok)
        run.assert_awaited_once_with({"query": "example claim"})
        announce.assert_awaited_once_with("我先查一下")
        final_request = client.post.call_args.kwargs["json"]
        self.assertEqual(final_request["tool_choice"], "none")
        outputs = [item for item in final_request["input"] if item.get("type") == "function_call_output"]
        self.assertEqual([(item["call_id"], item["output"]) for item in outputs], [
            ("call_1", "https://example.invalid/source"), ("call_2", "https://example.invalid/source"),
            ("call_3", "https://example.invalid/source"), ("call_4", "https://example.invalid/source"),
        ])

    async def test_image_identification_can_verify_the_official_chinese_name_with_the_next_tool(self):
        def tool_call(name, call_id, arguments):
            return httpx.Response(200, json={"output": [{
                "type": "function_call", "name": name, "call_id": call_id,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            }]})

        lookup_args = {"query": "原神 Ronova 官方 中文名", "recent": False, "foreign": False}
        client = Mock(post=AsyncMock(side_effect=[
            tool_call("trace_image", "call_trace", {"image_index": 1, "notice": "我看看是哪位。"}),
            tool_call("search_web", "call_name", {**lookup_args, "notice": "我核对一下官方中文名。"}),
            httpx.Response(200, json={"output_text": '{"reply":"这是《原神》的若娜瓦。"}'}),
        ]))
        trace = AsyncMock(return_value="Google Vision 线索：Ronova, Genshin Impact")
        lookup = AsyncMock(return_value="《原神》官方中文版：「死之执政」若娜瓦。")
        tools = {
            "trace_image": self.llm.ChatTool("Find an image", {"image_index": {"type": "integer"}}, trace),
            "search_web": self.llm.ChatTool("Verify names", {
                "query": {"type": "string"}, "recent": {"type": "boolean"}, "foreign": {"type": "boolean"},
            }, lookup),
        }
        with patch.object(self.llm, "_chat_client", return_value=client):
            result = await self.llm.chat([{"role": "user", "content": "图里是谁？"}],
                                         "persona", "fake", "gpt-6.1-terra", tools=tools)

        self.assertTrue(result.ok)
        self.assertEqual(result.tool_calls, 2)
        self.assertIn("若娜瓦", result.reply)
        trace.assert_awaited_once_with({"image_index": 1})
        lookup.assert_awaited_once_with(lookup_args)
        second = client.post.await_args_list[1].kwargs["json"]
        self.assertIn("官方中文名", str(second["input"][0]["content"]))
        self.assertIn("foreign 设为 false", str(second["input"][0]["content"]))
        self.assertIn("Ronova", str(second["input"][-1]["output"]))
        self.assertIn("若娜瓦", str(client.post.await_args.kwargs["json"]["input"][-1]["output"]))

    async def test_inventory_tools_can_read_six_pages_and_create_one_order(self):
        page_name = "query_elevator_inventory_page"
        order_name = "create_elevator_order"
        summary_name = "query_my_elevator_status"
        empty_filter = {"query": "", "rarity": "all"}
        calls = [(summary_name, {"section": "all"})]
        calls += [(page_name, {**empty_filter, "page": page}) for page in range(1, 7)]
        calls.append((order_name, {"summary": "清理指定物品"}))
        replies = [httpx.Response(200, json={"output": [{
            "type": "function_call", "name": name, "call_id": f"call_{index}",
            "arguments": json.dumps(args),
        }]}) for index, (name, args) in enumerate(calls)]
        replies.append(httpx.Response(200, json={"output_text": '{"reply":"已生成一张派单。"}'}))
        client = Mock(post=AsyncMock(side_effect=replies))
        summary = AsyncMock(return_value="仓库 221 项")
        page_read = AsyncMock(return_value="当前页库存")
        order = AsyncMock(return_value="派单 E1234 待确认")
        tools = {
            summary_name: self.llm.ChatTool("Summary", {"section": {"type": "string"}}, summary, silent=True),
            page_name: self.llm.ChatTool(
                "Inventory page", {"query": {"type": "string"}, "rarity": {"type": "string"}, "page": {"type": "integer"}},
                page_read, silent=True, blank_fields=frozenset({"query"}),
            ),
            order_name: self.llm.ChatTool("Order", {"summary": {"type": "string"}}, order, silent=True),
        }
        with patch.object(self.llm, "_chat_client", return_value=client):
            result = await self.llm.chat([{"role": "user", "content": "按条件整理整个仓库"}],
                                         "persona", "fake", "gpt-6.1-terra", tools=tools)

        self.assertTrue(result.ok)
        self.assertEqual(result.tool_calls, 8)
        self.assertEqual([record.args[0] for record in page_read.await_args_list],
                         [{**empty_filter, "page": page} for page in range(1, 7)])
        order.assert_awaited_once_with({"summary": "清理指定物品"})

    async def test_long_elevator_inventory_drops_old_chat_and_retries_final_without_duplicate_order(self):
        spec = importlib.util.spec_from_file_location(
            "hydrogen_test_chat.elevator_knowledge", ROOT / "plugins/chat_bot/elevator_knowledge.py",
        )
        guide = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guide)
        output = guide.ElevatorToolOutput()
        steps = ["sell|#1723 1|卖指定物资一件"]
        history = [{"role": "user", "content": "OLD_CHAT " + "x" * 22_000},
                   {"role": "assistant", "content": "结束以前的话题"},
                   {"role": "user", "content": "当前问题：筛完六页再整理物资"}]
        calls = [
            ("query_my_elevator_status", {"section": "plan"}),
            *(("query_elevator_inventory_page", {"area": "stock", "rarity": "all", "query": "", "page": p})
              for p in range(1, 7)),
            ("create_elevator_order", {"summary": "清理指定物资", "steps": steps}),
        ]
        responses = [httpx.Response(200, json={
            "output": [{"type": "function_call", "name": tool, "call_id": f"call_{i}",
                        "arguments": json.dumps(args)}],
            "usage": {"output_tokens": 150},
        }) for i, (tool, args) in enumerate(calls)]
        responses.extend([
            httpx.Response(503), httpx.Response(200, json={"output_text": '{"reply":"查完了。"}'}),
        ])
        game = Mock()
        game.has_run.return_value = False
        game.create_order.return_value = {
            "order_id": "E1723", "summary": "清理指定物资",
            "steps": [{"action": "sell", "argument": "#1723 1", "note": "卖指定物资一件"}],
        }
        captured_payloads = []
        async def capture_post(*args, **kwargs):
            captured_payloads.append(copy.deepcopy(kwargs["json"]))
            return responses[len(captured_payloads) - 1]

        with patch.object(self.llm, "_chat_client", return_value=Mock(post=capture_post)), \
             patch.object(self.llm, "OPENAI_INPUT_LIMIT", 49_000), \
             patch.object(guide, "_get_live_elevator_game", return_value=game), \
             patch.object(guide, "lookup_player_status", return_value="仓库共有 221 项"), \
             patch.object(guide, "lookup_inventory_page", side_effect=lambda _u, _a, _r, _q, page:
                          f"唯一第 {page} 页" + "物品×1 34金；" * 160):
            result = await self.llm.chat(
                history, "persona", "fake", "gpt-6.1-terra",
                tools=guide.elevator_tools("101", output=output),
            )
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.tool_calls, 8)
        self.assertEqual(len(captured_payloads), 10)
        self.assertIn("OLD_CHAT", str(captured_payloads[0]["input"]))
        final_input = str(captured_payloads[-1]["input"])
        self.assertNotIn("OLD_CHAT", final_input)
        self.assertIn("当前问题", final_input)
        for page in range(1, 7):
            self.assertIn(f"唯一第 {page} 页", final_input)
        game.create_order.assert_called_once()
        self.assertIn("确认 E1723", output.card())

    async def test_elevator_order_requires_matching_confirmation_and_keeps_receipt_on_incomplete(self):
        spec = importlib.util.spec_from_file_location(
            "hydrogen_test_chat.elevator_knowledge", ROOT / "plugins/chat_bot/elevator_knowledge.py",
        )
        guide = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guide)
        game = Mock()
        game.execute.return_value = types.SimpleNamespace(text="派单 E1723 完成 1/1 步｜钱包 10 金")
        result_output = guide.ElevatorToolOutput()
        call = {"type": "function_call", "name": "confirm_elevator_order", "call_id": "confirm_1",
                "arguments": '{"order_id":"E1723"}'}
        with patch.object(guide, "_get_live_elevator_game", return_value=game):
            for text in ("E1723 是哪个派单", "确认 E1724", "同意", "通过"):
                tools = guide.elevator_tools("101", user_text=text)
                await tools["confirm_elevator_order"].run({"order_id": "E1723"})
            game.execute.assert_not_called()
            client = Mock(post=AsyncMock(side_effect=[
                httpx.Response(200, json={"output": [call]}),
                httpx.Response(200, json={"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}),
            ]))
            with patch.object(self.llm, "_chat_client", return_value=client):
                failed = await self.llm.chat(
                    [{"role": "user", "content": "确认 E1723"}], "persona", "fake", "gpt-6.1-terra",
                    tools=guide.elevator_tools("101", user_text="确认 E1723", output=result_output),
                )
        self.assertFalse(failed.ok)
        self.assertEqual(failed.tool_calls, 1)
        game.execute.assert_called_once_with("101", "群友", "private", "confirm_order", "E1723")
        self.assertEqual(result_output.card(), "派单 E1723 完成 1/1 步｜钱包 10 金")

    async def request(self, **kwargs):
        with patch.object(self.v, "_load_service_account", return_value=({}, "p", "global")), \
             patch.object(self.v, "_get_token", AsyncMock(return_value="fake")), \
             patch.object(self.v, "_get_client", return_value=self.client):
            return await self.v._vertex_request(self.messages, self.persona, auth_file=__file__,
                model="gemini-4-hydrogen", cache_scope="private:test", **kwargs)

    async def test_hydrogen_skips_even_existing_explicit_caches_and_reports_actual_hits(self):
        with patch.object(self.v, "_lookup_persona_cache") as persona, \
             patch.object(self.v, "_lookup_session_cache") as session, \
             patch.object(self.v, "_schedule_persona_cache") as build_persona, \
             patch.object(self.v, "_schedule_session_cache") as build_session:
            result = await self.request()
        for mocked in (persona, session, build_persona, build_session):
            mocked.assert_not_called()
        payload = self.client.post.call_args.kwargs["json"]
        self.assertNotIn("cachedContent", payload)
        self.assertEqual(payload["contents"][0]["parts"][0]["text"], self.persona.strip())
        self.assertEqual(payload["contents"][2]["parts"][0]["inlineData"]["mimeType"], "image/png")
        self.assertTrue(result["ok"])
        self.assertEqual(result["usage"]["_cached_tokens"], 8000)
        self.assertEqual(result["usage"]["_prompt_tokens"], 10000)
        self.assertFalse(self.v._BACKGROUND_TASKS)

    async def test_length_and_runtime_changes_preserve_prefix_schema_and_input(self):
        original = copy.deepcopy(self.messages)
        await self.request(min_chars=30, temperature=.2)
        first = self.client.post.call_args.kwargs["json"]
        self.messages[-1]["content"] = "another question\nchanged runtime state"
        await self.request(min_chars=200, temperature=1.5)
        second = self.client.post.call_args.kwargs["json"]
        self.assertEqual(first["contents"][:-1], second["contents"][:-1])
        self.assertEqual(first["generationConfig"], second["generationConfig"])
        self.assertEqual(first["generationConfig"]["thinkingConfig"], {"thinkingLevel": "low"})
        self.assertEqual(first["generationConfig"]["maxOutputTokens"], 4096)
        self.assertNotIn("temperature", first["generationConfig"])
        self.assertIn("30", first["contents"][-1]["parts"][-1]["text"])
        self.assertIn("200", second["contents"][-1]["parts"][-1]["text"])
        self.assertEqual(self.messages[:-1], original[:-1])
        self.assertEqual(len(self.messages[-1]), 2)

    async def test_cache_builders_and_startup_never_create_gemini4_explicit_caches(self):
        for model in ("gemini-4-hydrogen", "gemini-4-argon", "projects/p/models/gemini-4-carbon"):
            args = dict(model=model, persona=self.persona, project="p", location="global",
                        token="fake", proxy="http://127.0.0.1:10808")
            with patch.object(self.v, "_get_client") as client:
                self.assertEqual(await self.v._get_or_create_cache(**args), "")
                self.assertEqual(await self.v._get_or_create_session_cache(
                    scope="s", prefix=self.messages[:8], **args), "")
                self.v._schedule_persona_cache(**args)
                self.v._schedule_session_cache(scope="s", prefix=self.messages[:8], **args)
                client.assert_not_called()
            self.assertFalse(self.v._BACKGROUND_TASKS)
        with patch.object(self.v, "_load_service_account", return_value=({}, "p", "global")), \
             patch.object(self.v, "_get_token", AsyncMock(return_value="fake")), \
             patch.object(self.v, "_schedule_persona_cache") as build:
            await self.v.warmup(__file__, "gemini-4-hydrogen", self.persona)
            build.assert_not_called()
        self.assertTrue(self.v.supports_explicit_cache("gemini-3.8-flash"))

    async def test_relay_uses_same_persona_reference_and_history_prefix(self):
        self.client.post.return_value = httpx.Response(200, json={
            "choices": [{"message": {"content": '{"reply":"hello"}'}}]})
        with patch.object(self.llm, "_chat_client", return_value=self.client):
            result = await self.llm._chat_once(self.messages, self.persona, "fake", "gemini-4-hydrogen",
                                              "http://127.0.0.1:8317/v1/chat/completions", min_chars=30)
        first = self.client.post.call_args.kwargs["json"]
        self.assertEqual(first["reasoning_effort"], "low")
        self.assertNotIn("max_tokens", first)
        self.assertNotIn("max_completion_tokens", first)
        identity = self.llm._prompt_with_model(self.persona, "gemini-4-hydrogen", "low")
        native = self.v._implicit_contents(self.messages, identity, None, 30)
        for wire, expected in zip(first["messages"], native):
            self.assertEqual(wire["role"], "assistant" if expected["role"] == "model" else "user")
            texts = [x["text"] for x in wire["content"] if x["type"] == "text"]
            self.assertEqual(texts, [x["text"] for x in expected["parts"] if "text" in x])
        self.assertEqual(first["messages"][2]["content"][0]["type"], "image_url")
        self.assertTrue(result.ok)
        with patch.object(self.llm, "_chat_client", return_value=self.client):
            await self.llm._chat_once(self.messages, self.persona, "fake", "gemini-4-hydrogen",
                                      "http://127.0.0.1:8317/v1/chat/completions", min_chars=200)
        second = self.client.post.call_args.kwargs["json"]
        self.assertEqual(first["response_format"], second["response_format"])

    async def test_relay_receives_configured_vertex_thinking_level(self):
        with patch.object(self.llm, "_chat_once", AsyncMock(return_value=self.llm.ChatResult(ok=True))) as request:
            result = await self.llm.chat([], "persona", "", gemini_key="fake",
                gemini_model="gemini-4-hydrogen", gemini_base="http://127.0.0.1:8317",
                vertex_thinking="medium")
        self.assertTrue(result.ok)
        self.assertEqual(request.call_args.kwargs["thinking_level"], "medium")

    async def test_openai_request_receives_selected_model_and_effort_as_developer_context(self):
        self.client.post.return_value = httpx.Response(200, json={"output_text": '{"reply":"hello"}'})
        with patch.object(self.llm, "_chat_client", return_value=self.client):
            result = await self.llm.chat(self.messages, self.persona, "fake", "gpt-6.2-sol", openai_effort="high")
        payload = self.client.post.await_args.kwargs["json"]
        self.assertTrue(result.ok)
        self.assertEqual((payload["model"], payload["reasoning"]["effort"]), ("gpt-6.2-sol", "high"))
        developer = payload["input"][0]
        self.assertEqual(developer["role"], "developer")
        self.assertIn("本轮回复使用 gpt-6.2-sol。", developer["content"][0]["text"])
        self.assertIn("本轮思考档位为 high。", developer["content"][0]["text"])

    async def test_fallback_prompt_reports_gemini_instead_of_the_failed_openai_route(self):
        self.client.post.return_value = httpx.Response(200, json={
            "choices": [{"message": {"content": '{"reply":"hello"}'}}],
        })
        failed = self.llm.ChatResult(ok=False, error="HTTP 503")
        with patch.object(self.llm, "_chat_openai", AsyncMock(return_value=failed)), \
             patch.object(self.llm, "_chat_client", return_value=self.client):
            result = await self.llm.chat(self.messages, self.persona, "fake", "gpt-6.2-sol", gemini_key="fake",
                                         gemini_model="gemini-4-hydrogen", gemini_base="http://127.0.0.1:8317",
                                         vertex_thinking="medium")
        payload = self.client.post.await_args.kwargs["json"]
        self.assertTrue(result.ok)
        self.assertEqual(result.model_used, "gemini-4-hydrogen")
        rendered = str(payload["messages"])
        self.assertIn("本轮回复使用 gemini-4-hydrogen。", rendered)
        self.assertIn("本轮思考档位为 medium。", rendered)
        self.assertNotIn("本轮回复使用 gpt-6.2-sol", rendered)

    async def test_other_chat_models_do_not_receive_gemini_thinking_setting(self):
        self.client.post.return_value = httpx.Response(200, json={
            "choices": [{"message": {"content": '{"reply":"hello"}'}}]})
        with patch.object(self.llm, "_chat_client", return_value=self.client):
            result = await self.llm._chat_once([], "persona", "fake", "doubao-model",
                                              "https://example.invalid")
        self.assertTrue(result.ok)
        self.assertNotIn("reasoning_effort", self.client.post.call_args.kwargs["json"])

    async def test_vision_uses_low_only_for_gemini4(self):
        self.client.post.return_value = httpx.Response(200, json={
            "choices": [{"message": {"content": "image description"}}]})
        with patch.object(httpx, "AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = self.client
            for model in ("gemini-4-hydrogen", "glm-4.6v-flash"):
                result = await self.vision._describe_once(b"image", "fake", model)
                self.assertTrue(result.ok)
                payload = self.client.post.call_args.kwargs["json"]
                self.assertEqual(payload.get("reasoning_effort"),
                                 "low" if model.startswith("gemini-4") else None)

    async def test_erp_relay_uses_same_thinking_level_only_for_gemini4(self):
        # Load only the request function; importing ERP would register live QQ handlers.
        source = ast.parse((ROOT / "plugins/chat_bot/erp.py").read_text(encoding="utf-8"))
        function = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef)
                        and n.name == "_relay_chat")
        env = {"CHAT_GEMINI_BASE": "http://127.0.0.1:8317", "CHAT_GEMINI_KEY": "fake"}
        namespace = {"_env": lambda key, default: env.get(key, default),
                     "VERTEX_THINKING": "low", "proxy_for_url": lambda _: None}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "erp.py", "exec"), namespace)
        self.client.post.return_value = httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"}}]})
        with patch.object(httpx, "AsyncClient") as factory:
            factory.return_value.__aenter__.return_value = self.client
            for model in ("gemini-4-hydrogen", "gemini-3.8-flash"):
                env["CHAT_GEMINI_MODEL"] = model
                result = await namespace["_relay_chat"]([], "persona")
                self.assertTrue(result["ok"])
                payload = self.client.post.call_args.kwargs["json"]
                self.assertEqual(payload.get("reasoning_effort"),
                                 "low" if model.startswith("gemini-4") else None)

    def test_user_images_stay_on_final_turn_after_stable_reference_images(self):
        contents = self.v._implicit_contents(self.messages, self.persona, [b"user image"])
        self.assertIn("inlineData", contents[2]["parts"][0])
        self.assertIn("inlineData", contents[-1]["parts"][-1])
        self.assertEqual(contents[-1]["parts"][0]["text"], self.messages[-1]["content"])
        # With only references, the final turn must still be a user turn.
        self.assertEqual(self.v._implicit_contents([], "", None)[-1]["role"], "user")


if __name__ == "__main__":
    unittest.main()
