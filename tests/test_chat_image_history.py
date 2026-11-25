import ast
import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "plugins/chat_bot"
spec = importlib.util.spec_from_file_location("chat_image_history_test_module", SOURCE / "historical_images.py")
historical_images = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = historical_images
spec.loader.exec_module(historical_images)


class ImageHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_of_bot_reply_recalls_the_original_image_without_redownloading(self):
        scope = ("555", "group:10")
        cache = historical_images.ImageHistory()
        raw = b"the-original-picture"
        cache.remember(scope, "234", "-12", [raw])
        cache.record_reply(scope, "-12", {"message_id": -13})
        quote_chain = AsyncMock(return_value=[])
        fetch_image = AsyncMock()
        event = SimpleNamespace(quoted="-13")
        namespace = {
            "Bot": object, "MessageEvent": object, "historical_images": historical_images,
            "message_io": SimpleNamespace(quote_id=lambda event: event.quoted, fetch_quoted_reply_images=quote_chain),
            "_image_history": cache, "_fetch_image": fetch_image, "llm": SimpleNamespace(CHAT_IMAGE_LIMIT=6),
            "asyncio": asyncio,
        }
        path = SOURCE / "__init__.py"
        root = ast.parse(path.read_text(encoding="utf-8"))
        function = next(node for node in root.body if getattr(node, "name", "") == "_recall_turn_images")
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
        recall = namespace["_recall_turn_images"]

        recalled = await recall(Mock(), event, scope, "999", "你再看看右边是啥呢")
        self.assertEqual(recalled, [("引用回复关联的历史原图",raw)])
        quote_chain.assert_not_awaited()
        fetch_image.assert_not_awaited()
        self.assertFalse(await recall(Mock(), event, scope, "999", "你今天忙吗"))

        self.assertFalse(await recall(Mock(), SimpleNamespace(quoted=None), scope, "999", "图右边是谁"))
        self.assertEqual(await recall(Mock(), SimpleNamespace(quoted=None), scope, "234", "图右边是谁"),
                         [("该发言人近期发过的原图",raw)])
        self.assertFalse(await recall(Mock(), event, ("555", "group:11"), "234", "再看看右边"))
        quote_chain.assert_awaited_once()

        quote_chain.return_value = [{"file":"old"}]
        fetch_image.return_value = b"found-old-qq-image"
        old = await recall(Mock(), SimpleNamespace(quoted="unseen-reply"), scope, "234", "再看看图")
        self.assertEqual(old,[("沿引用找回的历史原图",b"found-old-qq-image")])

        original_generation = b"last-generated-result"
        cache.remember_generated(scope, "234", {"message_id": -14}, original_generation,
                                 openai_response_id="resp_one")
        cache.remember(scope, "234", "-15", [b"unrelated-image-sent-later"])
        recent = SimpleNamespace(quoted=None)
        self.assertEqual(await recall(Mock(), recent, scope, "234", "继续使用豆姐改图"),
                         [("龙娘上一版生成的图片，可作为改图底图", original_generation)])
        self.assertFalse(await recall(Mock(), recent, scope, "999", "继续使用豆姐改图"))
        self.assertFalse(await recall(Mock(), recent, ("555", "group:11"), "234", "继续改图"))
        edited = await recall(Mock(), SimpleNamespace(quoted="-14"), scope, "999", "再改一下")
        self.assertEqual(edited, [("龙娘上一版生成的图片，可作为改图底图", original_generation)])
        self.assertEqual(cache.generated_source(scope, "234", None).openai_response_id, "resp_one")
        self.assertEqual(cache.generated_source(scope, "999", "-14").openai_response_id, "")

    def test_scope_eviction_expiry_and_no_unrelated_quote_fallback(self):
        now = [0]
        cache = historical_images.ImageHistory(max_bytes=7,max_turns=4,ttl=20,recent_ttl=5,clock=lambda:now[0])
        scope = ("555", "group:10")
        cache.remember(scope, "234", "90", [b"1234"])
        cache.record_reply(scope, "90", 91)
        self.assertFalse(cache.recover(scope,"234","unrelated"))
        self.assertFalse(cache.recover(("555","private:234"),"234","91"))
        self.assertFalse(cache.recover(("556","group:10"),"234","91"))
        now[0] = 6
        self.assertFalse(cache.recover(scope,"234",None))
        self.assertEqual(cache.recover(scope,"234","91"),(b"1234",))
        cache.remember(scope, "234", "92", [b"5678"])
        self.assertFalse(cache.recover(scope,"234","91"))
        cache.forget(scope)
        self.assertFalse(cache.recover(scope,"234","92"))
        cache.remember(scope, "234", "93", [b"a"])
        now[0] = 27
        self.assertFalse(cache.recover(scope,"234","93"))
