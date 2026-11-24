"""Regression coverage for image token budgets and the generation tool."""
from __future__ import annotations

import asyncio
import base64
import importlib
import io
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from nonebot.adapters.onebot.v11 import MessageSegment
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
package = types.ModuleType("image_generation_test")
package.__path__ = [str(ROOT / "plugins" / "chat_bot")]
sys.modules[package.__name__] = package
generation = importlib.import_module(f"{package.__name__}.image_generation")
quota = importlib.import_module(f"{package.__name__}.image_quota")
settings = importlib.import_module(f"{package.__name__}.image_settings")
llm = importlib.import_module(f"{package.__name__}.llm")


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.quota = quota.ImageQuota(Path(self.directory.name) / "quota.sqlite3")
        self.low = settings.ImageSettings(1024, 1024, "low")

    def test_calculator_matches_published_square_costs_and_rounding(self):
        expected = [823, 1449, 1756, 3122, 7024]
        self.assertEqual(
            [settings.ImageSettings(1024, 1024, quality).output_tokens for quality in settings.QUALITY_LEVELS],
            expected,
        )
        self.assertEqual(self.low.reservation_tokens, 824)
        for width, height in ((1024, 1080), (512, 512), (3840, 1000), (3072, 3072)):
            with self.subTest(size=(width, height)), self.assertRaises(ValueError):
                settings.ImageSettings(width, height, "medium")

    def test_quota_tracks_affection_threshold_gaps_and_rounds_to_hundreds(self):
        thresholds = [0, 15, 30, 45, 55, 75, 90]
        limits = [quota.daily_limit(max(0.001, tier), tier, thresholds[-1]) for tier in thresholds]
        self.assertEqual(limits, [8000, 11700, 15300, 19000, 21400, 26300, 30000])
        self.assertEqual(quota.daily_limit(0, 0, 90), 2000)
        self.assertEqual(quota.daily_limit(-0.1, -25, 90), 0)

    def test_crossing_zero_raises_the_limit_in_the_same_day(self):
        zero_limit = quota.daily_limit(0, 0, 90)
        self.assertIsNotNone(self.quota.reserve("zero", "qq1", zero_limit, self.low).settings)
        self.quota.settle("zero", 824)

        positive_limit = quota.daily_limit(0.001, 0, 90)
        balance = self.quota.balance("qq1", positive_limit)
        self.assertEqual((balance.limit, balance.used, balance.remaining), (8000, 824, 7176))

    def test_gif_budget_matches_the_measured_render_sheet(self):
        animation = settings.ImageSettings(816, 816, "low", 2, 200)
        still = settings.ImageSettings(816, 816, "low")
        self.assertEqual(animation.reservation_tokens, 987)
        self.assertEqual(animation.reservation_tokens, settings.ImageSettings(3264, 1632, "low").reservation_tokens)
        self.assertNotEqual(animation.reservation_tokens, still.reservation_tokens * 2)
        reservation = self.quota.reserve("gif", "qq1", 4000, animation)
        self.assertEqual(reservation.settings, animation)
        self.assertEqual(self.quota.balance("qq1", 4000).reserved, 987)
        self.quota.settle("gif", 980)
        self.assertEqual(self.quota.balance("qq1", 4000).used, 980)

    def test_gif_fit_preserves_frame_count_and_timing(self):
        request = settings.ImageSettings.from_request(1024, 1024, "high", 2, 240)
        self.assertEqual(request.size, "960x960")
        lower_quality = settings.fit_image(request, 1200)
        self.assertEqual((lower_quality.quality, lower_quality.frame_count, lower_quality.frame_duration_ms),
                         ("low", 2, 240))
        smaller = settings.fit_image(request, 700)
        self.assertLess(smaller.width, request.width)
        self.assertEqual((smaller.frame_count, smaller.frame_duration_ms), (2, 240))
        self.assertLessEqual(smaller.reservation_tokens, 700)
        for count, delay in ((0, 200), (17, 200), (2, 10), (2, 2001)):
            with self.subTest(count=count, delay=delay), self.assertRaises(ValueError):
                settings.ImageSettings.from_request(816, 816, "low", count, delay)

    def test_final_image_can_use_exactly_ten_percent_extra_only_once(self):
        self.quota.set_cooldown_seconds(0)
        self.quota.reserve("existing", "qq1", 4000, self.low)
        self.quota.settle("existing", 3576)
        with ThreadPoolExecutor(max_workers=2) as pool:
            receipts = list(pool.map(lambda req: self.quota.reserve(req, "qq1", 4000, self.low), ["a", "b"]))
        self.assertEqual(sum(r.settings == self.low for r in receipts), 1)
        current = self.quota.balance("qq1", 4000)
        self.assertEqual((current.used, current.reserved, current.remaining), (3576, 824, 0))
        self.assertEqual(self.quota.balance("qq2", 4000).remaining, 4000)

    def test_above_grace_lowers_quality_or_size_to_available_budget(self):
        self.quota.set_cooldown_seconds(0)
        self.quota.reserve("prior", "qq1", 4000, self.low)
        self.quota.settle("prior", 2500)
        request = settings.ImageSettings(1024, 1024, "xhigh")
        receipt = self.quota.reserve("downgraded", "qq1", 4000, request)
        self.assertEqual(receipt.settings, settings.ImageSettings(1024, 1024, "medium"))
        scale = settings.fit_image(settings.ImageSettings(2048, 2048, "low"), 900)
        self.assertLess(scale.width, 2048)
        self.assertEqual(scale.width, scale.height)
        self.assertLessEqual(scale.reservation_tokens, 900)
        self.assertIsNone(settings.fit_image(self.low, 200))

    def test_daily_balance_resets_at_beijing_midnight_and_keeps_late_usage_on_the_original_day(self):
        midnight = datetime(2026, 11, 23, tzinfo=quota.SHANGHAI)
        with patch.object(quota, "_now", return_value=midnight - timedelta(seconds=1)):
            self.quota.reserve("first", "qq1", 4000, self.low)
            self.assertTrue(self.quota.reserve("first", "qq1", 4000, self.low).duplicate)
            self.assertEqual(self.quota.balance("qq1", 4000).reserved, 824)
        with patch.object(quota, "_now", return_value=midnight):
            self.assertEqual(self.quota.balance("qq1", 4000).remaining, 4000)
            self.quota.settle("first", 801)
            self.quota.settle("first", 1234)
            snapshot = self.quota.balance("qq1", 6000)
            self.assertEqual((snapshot.day, snapshot.used, snapshot.reserved, snapshot.remaining, snapshot.lifetime),
                             ("2026-11-23", 0, 0, 6000, 801))
        with patch.object(quota, "_now", return_value=midnight - timedelta(seconds=1)):
            self.assertEqual(self.quota.balance("qq1", 4000).remaining, 3199)

    def test_monthly_records_survive_the_transition_as_lifetime_usage(self):
        with closing(sqlite3.connect(self.quota.path)) as db, db:
            db.execute(
                "CREATE TABLE image_usage (request_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, "
                "month TEXT NOT NULL, estimated_tokens INTEGER NOT NULL, output_tokens INTEGER)"
            )
            db.execute("INSERT INTO image_usage VALUES ('old', 'qq1', '2026-11', 824, 801)")
        with patch.object(quota, "_now", return_value=datetime(2026, 11, 22, tzinfo=quota.SHANGHAI)):
            snapshot = self.quota.balance("qq1", 4000)
            self.assertEqual((snapshot.used, snapshot.remaining, snapshot.lifetime), (0, 4000, 801))
            self.assertIsNotNone(self.quota.reserve("new", "qq1", 4000, self.low).settings)
            self.quota.settle("new", 824)
            self.assertEqual(self.quota.balance("qq1", 4000).lifetime, 1625)

    def test_cooldown_is_atomic_per_account_and_can_be_adjusted_across_midnight(self):
        start = datetime(2026, 11, 22, 23, 59, tzinfo=quota.SHANGHAI)
        self.assertEqual(self.quota.cooldown_seconds(), 180)
        with patch.object(quota, "_now", return_value=start), ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(lambda req: self.quota.reserve(req, "qq1", 4000, self.low), ["a", "b"])
        self.assertEqual(sorted(result.retry_after for result in (first, second)), [0, 180])
        with patch.object(quota, "_now", return_value=start + timedelta(seconds=90)):
            other = self.quota.reserve("other", "qq2", 4000, self.low)
            self.assertIsNotNone(other.settings)
            self.assertEqual(self.quota.reserve("blocked", "qq1", 4000, self.low).retry_after, 90)
            self.quota.set_cooldown_seconds(90)
            reopened = quota.ImageQuota(self.quota.path)
            self.assertEqual(reopened.cooldown_seconds(), 90)
            self.assertIsNotNone(reopened.reserve("allowed", "qq1", 4000, self.low).settings)


class ImageToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.quota = quota.ImageQuota(Path(self.directory.name) / "quota.sqlite3")
        self.choice = {
            "name": "generate_image",
            "arguments": json.dumps({"prompt": "an orange circle", "width": 1024, "height": 1024,
                                     "quality": "low", "frame_count": 1, "frame_duration_ms": 200,
                                     "model": generation.FLARE_MODEL, "reference_image_indexes": [],
                                     "notice": "等着，我去画。"}),
        }

    def tool(self, notify, images=()):
        return generation.image_tool(ledger=self.quota, message_key="qq:1", user_id="qq",
                                     api_key="mock", get_limit=lambda: 4000, notify=notify, images=images)

    async def test_model_notice_precedes_request_and_result_counts_only_image_tokens(self):
        notifier = AsyncMock()
        ordinary_notice = AsyncMock()
        release = asyncio.Event()

        async def render(*args, **kwargs):
            self.assertEqual(notifier.await_args_list[0].args[0].text, "等着，我去画")
            self.assertEqual(kwargs, {"model": generation.FLARE_MODEL, "references": ()})
            await release.wait()
            return generation.Generation(b"test-image", 824)

        with patch.object(generation, "generate", side_effect=render) as mock_render:
            tools = {"generate_image": self.tool(notifier)}
            response = await llm._execute_chat_tool(self.choice, tools, ordinary_notice, {})
            self.assertIn("已开始", response)
            self.assertEqual(self.quota.balance("qq", 4000).reserved, 824)
            release.set()
            await asyncio.gather(*generation._tasks)

        mock_render.assert_awaited_once()
        ordinary_notice.assert_not_awaited()
        progress, final = [entry.args[0] for entry in notifier.await_args_list]
        self.assertTrue(progress.started)
        self.assertEqual(final.image, b"test-image")
        self.assertIn("剩余 3,176 / 4,000", final.text)
        self.assertEqual(self.quota.balance("qq", 4000).lifetime, 824)

    async def test_failed_start_notice_releases_reservation_without_generating(self):
        notifier = AsyncMock(side_effect=RuntimeError("test transport error"))
        with patch.object(generation, "generate", new_callable=AsyncMock) as mock_render:
            await llm._execute_chat_tool(self.choice, {"generate_image": self.tool(notifier)}, None, {})
        mock_render.assert_not_awaited()
        self.assertEqual(self.quota.balance("qq", 4000).remaining, 4000)
        retry = self.quota.reserve("retry", "qq", 4000, settings.ImageSettings(1024, 1024, "low"))
        self.assertIsNotNone(retry.settings)

    async def test_image_api_prefers_image_output_details_over_total(self):
        api = AsyncMock()
        api.__aenter__.return_value = api
        api.post.return_value = httpx.Response(200, json={
            "data": [{"b64_json": "aW1hZ2U="}],
            "usage": {"total_tokens": 1500, "output_tokens": 900,
                      "output_tokens_details": {"image_tokens": 824, "text_tokens": 76}},
        })
        with patch.object(generation.httpx, "AsyncClient", return_value=api):
            image = await generation.generate("mock", "qq", "orange", settings.ImageSettings(1024, 1024, "low"))
        self.assertEqual((image.image, image.output_tokens), (b"image", 824))
        request = api.post.await_args.kwargs["json"]
        self.assertEqual(api.post.await_args.args[0], generation.URL)
        self.assertEqual(request["model"], generation.FLARE_MODEL)
        self.assertEqual(request["size"], "1024x1024")
        self.assertEqual(request["output_format"], "png")
        self.assertTrue({"output_compression", "frame_count", "frame_duration_ms"}.isdisjoint(request))

    async def test_gif_tool_preserves_the_original_file_through_onebot_encoding(self):
        buffer = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(
            buffer, format="GIF", save_all=True, append_images=[Image.new("RGB", (16, 16), "blue")],
            duration=200, loop=0,
        )
        original = buffer.getvalue()
        api = AsyncMock()
        api.__aenter__.return_value = api
        api.post.return_value = httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(original).decode()}],
            "usage": {"output_tokens_details": {"image_tokens": 987}},
        })
        notifier = AsyncMock()
        args = json.loads(self.choice["arguments"])
        args.update(width=816, height=816, frame_count=2, frame_duration_ms=200)
        call = {**self.choice, "arguments": json.dumps(args)}
        with patch.object(generation.httpx, "AsyncClient", return_value=api):
            response = await llm._execute_chat_tool(call, {"generate_image": self.tool(notifier)}, None, {})
            self.assertIn("已开始", response)
            await asyncio.gather(*generation._tasks)
        payload = api.post.await_args.kwargs["json"]
        self.assertEqual((payload["output_format"], payload["frame_count"], payload["frame_duration_ms"]),
                         ("gif", 2, 200))
        self.assertNotIn("output_compression", payload)
        delivery = notifier.await_args.args[0]
        self.assertIn("GIF 2 帧", delivery.text)
        self.assertIn("本次输出 987 token", delivery.text)
        self.assertEqual(self.quota.balance("qq", 4000).remaining, 3013)
        encoded = MessageSegment.image(delivery.image).data["file"]
        self.assertEqual(base64.b64decode(encoded.removeprefix("base64://")), original)

    async def test_sunburst_edit_sends_the_selected_reference_files_in_order(self):
        originals = []
        for format, color in (("PNG", "red"), ("JPEG", "green")):
            buffer = io.BytesIO()
            Image.new("RGB", (16, 16), color).save(buffer, format=format)
            originals.append(buffer.getvalue())
        api = AsyncMock()
        api.__aenter__.return_value = api
        api.post.return_value = httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(originals[0]).decode()}],
            "usage": {"output_tokens_details": {"image_tokens": 1234}},
        })
        notifier = AsyncMock()
        args = json.loads(self.choice["arguments"])
        args.update(model=generation.SUNBURST_MODEL, quality="medium", reference_image_indexes=[2, 1],
                    prompt="Use the first file as the base and replace its background using the second file.")
        call = {**self.choice, "arguments": json.dumps(args)}
        tools = {"generate_image": self.tool(notifier, images=tuple(originals))}
        with patch.object(generation.httpx, "AsyncClient", return_value=api):
            response = await llm._execute_chat_tool(call, tools, None, {})
            self.assertIn("已开始", response)
            await asyncio.gather(*generation._tasks)
        request = api.post.await_args.kwargs
        self.assertEqual(api.post.await_args.args[0], generation.EDIT_URL)
        self.assertNotIn("json", request)
        self.assertEqual(request["data"]["model"], generation.SUNBURST_MODEL)
        self.assertEqual(request["data"]["quality"], "medium")
        self.assertEqual(request["files"], [
            ("image[]", ("reference-1.jpg", originals[1], "image/jpeg")),
            ("image[]", ("reference-2.png", originals[0], "image/png")),
        ])
        encoded_request = httpx.Request("POST", generation.EDIT_URL, data=request["data"], files=request["files"])
        self.assertTrue(encoded_request.headers["content-type"].startswith("multipart/form-data"))
        self.assertEqual(encoded_request.read().count(b'name="image[]"'), 2)
        final = notifier.await_args.args[0]
        self.assertIn("Sunburst", final.text)
        self.assertEqual(final.image, originals[0])
        self.assertEqual(self.quota.balance("qq", 4000).used, 1234)

    async def test_missing_or_malformed_reference_is_rejected_before_reserving_quota(self):
        notifier = AsyncMock()
        tools = {"generate_image": self.tool(notifier)}
        for indexes, expected in (([1], "参考图编号"), ([True], "工具参数"), (["1"], "工具参数")):
            args = {**json.loads(self.choice["arguments"]), "reference_image_indexes": indexes}
            call = {**self.choice, "arguments": json.dumps(args)}
            response = await llm._execute_chat_tool(call, tools, None, {})
            self.assertIn(expected, response)
        notifier.assert_not_awaited()
        self.assertEqual(self.quota.balance("qq", 4000).reserved, 0)
