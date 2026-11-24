"""Offline regressions: no QQ messages, real API keys, or model requests."""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import network_config


def load_vertex():
    package = types.ModuleType("network_test_chat")
    package.__path__ = []
    llm = types.ModuleType("network_test_chat.llm")
    llm.build_reply_schema = lambda _: {"schema": {}}
    sys.modules[package.__name__] = package
    sys.modules[llm.__name__] = llm
    spec = importlib.util.spec_from_file_location("network_test_chat.vertex", ROOT / "plugins/chat_bot/vertex.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProxySettingsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / ".env"
        self.environment = patch.object(network_config, "_ENV_FILE", self.path)
        self.environment.start()
        network_config._cache_signature = None
        network_config._cache_proxy = None

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_fixed_default_does_not_inherit_a_stale_https_proxy(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:9910"}, clear=True):
            self.assertEqual(network_config.get_proxy(), "http://127.0.0.1:10808")

    def test_reads_quoted_proxy_and_reloads_changed_config(self):
        self.path.write_text('QQBOT_PROXY="http://127.0.0.1:10808" # fixed\nAPI_KEY=must-not-be-returned\n')
        self.assertEqual(network_config.get_proxy(), "http://127.0.0.1:10808")
        self.path.write_text('QQBOT_PROXY=http://localhost:10808\n')
        self.assertEqual(network_config.get_proxy(), "http://localhost:10808")
        self.assertNotIn("API_KEY", network_config.proxy_status())

    def test_rejects_proxy_rotation_list(self):
        self.path.write_text('QQBOT_PROXY=http://127.0.0.1:10808,http://127.0.0.1:9910\n')
        with self.assertRaises(ValueError):
            network_config.get_proxy()

    def test_loopback_bypass_and_process_only_environment(self):
        with patch.dict(os.environ, {"http_proxy": "http://old:9910", "NO_PROXY": "intranet.local"}, clear=True):
            network_config.configure_process_proxy()
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                self.assertEqual(os.environ[key], "http://127.0.0.1:10808")
            for host in ("localhost", "127.0.0.1", "::1", "intranet.local"):
                self.assertIn(host, os.environ["NO_PROXY"].split(","))

    def test_relay_bypasses_proxy_only_for_loopback(self):
        for url in ('http://127.0.0.1:8317/v1', 'http://localhost:3001', 'http://[::1]:8317'):
            self.assertIsNone(network_config.proxy_for_url(url))
        self.assertEqual(network_config.proxy_for_url('https://example.invalid/v1'), 'http://127.0.0.1:10808')


class VertexNetworkTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.v = load_vertex()
        self.temp = tempfile.TemporaryDirectory()
        self.auth = Path(self.temp.name) / "fake-auth.json"
        self.auth.write_text('{}')
        self.v._CACHE_FILE = Path(self.temp.name) / "cache.json"
        self.v._CACHE_LOADED = True
        self.v._REFS_CACHE = []
        self.v.CACHE_START_DELAY = 0

    async def asyncTearDown(self):
        await self.v.close_clients()
        self.temp.cleanup()

    def transport(self, handler):
        original = httpx.AsyncClient
        def factory(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            kwargs.pop("proxy", None)
            return original(transport=httpx.MockTransport(handler), **kwargs)
        return patch.object(self.v.httpx, "AsyncClient", side_effect=factory)

    def response(self):
        return {"candidates": [{"content": {"parts": [{"text": "offline reply"}]}, "finishReason": "STOP"}]}

    async def test_proxy_clients_are_reused_without_closing_another_route(self):
        with self.transport(lambda _: httpx.Response(200, json={})):
            a = self.v._get_client("http://127.0.0.1:10808", 10)
            b = self.v._get_client("http://127.0.0.1:10809", 300)
            await asyncio.sleep(0)
            self.assertFalse(a.is_closed)
            self.assertFalse(b.is_closed)
            self.assertIs(a, self.v._get_client("http://127.0.0.1:10808", 180))
            await self.v.close_clients()
            self.assertTrue(a.is_closed)
            self.assertTrue(b.is_closed)
            await self.v.close_clients()

    async def test_cache_and_reply_have_independent_request_timeouts(self):
        timeouts = []
        def handler(request):
            timeouts.append(request.extensions["timeout"]["read"])
            if str(request.url).endswith("cachedContents"):
                return httpx.Response(200, json={"name": "cache/" + str(len(timeouts))})
            return httpx.Response(200, json=self.response())
        with self.transport(handler), patch.object(self.v, "_load_service_account", return_value=({}, "p", "global")), patch.object(self.v, "_get_token", AsyncMock(return_value="fake")):
            kwargs = dict(project="p", location="global", model="m", persona="x" * 600, token="fake", proxy="http://127.0.0.1:10808")
            await self.v._get_or_create_cache(**kwargs)
            await self.v._get_or_create_session_cache(scope="test", prefix=[{"role": "user", "content": "fake"}] * 20, **kwargs)
            result = await self.v._vertex_request([{"role": "user", "content": "fake"}], "x" * 600,
                auth_file=str(self.auth), model="m", proxy=kwargs["proxy"], timeout=0.5, plain_text=True)
            self.assertTrue(result["ok"])
        self.assertEqual(timeouts, [180.0, 300.0, 0.5])

    async def test_cold_persona_cache_does_not_block_reply_and_is_deduplicated(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow_cache(**_):
            started.set()
            await release.wait()
            return "cache/ready"
        with self.transport(lambda _: httpx.Response(200, json=self.response())), patch.object(self.v, "_load_service_account", return_value=({}, "p", "global")), patch.object(self.v, "_get_token", AsyncMock(return_value="fake")), patch.object(self.v, "_get_or_create_cache", AsyncMock(side_effect=slow_cache)) as cache:
            async def request():
                return await self.v._vertex_request([{"role": "user", "content": "fake"}], "x" * 600,
                    auth_file=str(self.auth), model="m", timeout=1, plain_text=True)
            result = await asyncio.wait_for(request(), timeout=0.5)
            self.assertTrue(result["ok"])
            await asyncio.wait_for(started.wait(), timeout=0.5)
            await request()
            self.assertEqual(cache.await_count, 1)
            release.set()
            await asyncio.gather(*list(self.v._BACKGROUND_TASKS))

    async def test_fixed_proxy_retries_same_route_without_probing(self):
        request = AsyncMock(side_effect=[{"ok": False, "error": "ConnectError"}, {"ok": True, "text": "yes"}])
        with patch.object(self.v, "_vertex_request", request):
            result = await self.v.vertex_chat([], "", auth_file="fake", model="m", proxy=["http://127.0.0.1:10808", "http://old:9910"])
        self.assertTrue(result["ok"])
        self.assertEqual([call.kwargs["proxy"] for call in request.await_args_list], ["http://127.0.0.1:10808"] * 2)

    async def test_http_errors_are_not_retried(self):
        request = AsyncMock(return_value={"ok": False, "error": "HTTP 403: forbidden"})
        with patch.object(self.v, "_vertex_request", request):
            await self.v.vertex_chat([], "", auth_file="fake", model="m")
        self.assertEqual(request.await_count, 1)

    async def test_total_deadline_covers_slow_auth_or_cache(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(5)
        before = time.monotonic()
        with patch.object(self.v, "_vertex_request", side_effect=slow):
            result = await self.v.vertex_chat([], "", auth_file="fake", model="m", total_budget=0.08, per_try=0.05)
        self.assertFalse(result["ok"])
        self.assertIn("TimeoutError", result["error"])
        self.assertLess(time.monotonic() - before, 0.5)

    async def test_cancelled_caller_does_not_cancel_shared_token_refresh(self):
        started, release = threading.Event(), threading.Event()
        def refresh(auth, proxy):
            started.set()
            self.assertTrue(release.wait(2))
            return "token-for-" + auth, time.time() + 3600
        with patch.object(self.v, "_refresh_token_sync", side_effect=refresh) as mock:
            caller = asyncio.create_task(self.v._get_token("account-a", "http://127.0.0.1:10808"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            second = asyncio.create_task(self.v._get_token("account-a", "http://127.0.0.1:10808"))
            release.set()
            self.assertEqual(await second, "token-for-account-a")
            self.assertEqual(mock.call_count, 1)
            self.assertEqual(await self.v._get_token("account-a"), "token-for-account-a")
            self.assertEqual(mock.call_count, 1)

    async def test_token_cache_does_not_mix_accounts(self):
        def refresh(auth, proxy):
            return auth + "-token", time.time() + 3600
        with patch.object(self.v, "_refresh_token_sync", side_effect=refresh) as mock:
            a, b = await asyncio.gather(self.v._get_token("a"), self.v._get_token("b"))
        self.assertEqual((a, b), ("a-token", "b-token"))
        self.assertEqual(mock.call_count, 2)

    def test_sync_refresh_is_bounded_and_closes_session(self):
        import datetime
        import google.auth.transport.requests
        import google.oauth2.service_account
        import requests
        session = MagicMock()
        session.__enter__.return_value = session
        request = Mock()
        credentials = Mock(token='fake-token', expiry=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        credentials.refresh.side_effect = lambda transport: transport('https://oauth2.googleapis.com/token', method='POST')
        with patch.object(self.v, '_load_service_account', return_value=({}, 'p', 'global')), patch.object(requests, 'Session', return_value=session), patch.object(google.auth.transport.requests, 'Request', return_value=request), patch.object(google.oauth2.service_account.Credentials, 'from_service_account_info', return_value=credentials):
            token, _ = self.v._refresh_token_sync('not-a-real-credential', 'http://127.0.0.1:10808')
        self.assertEqual(token, 'fake-token')
        self.assertFalse(session.trust_env)
        self.assertEqual(request.call_args.kwargs['timeout'], (5.0, 10.0))
        session.__exit__.assert_called_once()

    async def test_failed_refresh_can_be_retried(self):
        with patch.object(self.v, '_refresh_token_sync', side_effect=[RuntimeError('offline auth error'), ('new-token', time.time()+3600)]) as refresh:
            with self.assertRaises(RuntimeError):
                await self.v._get_token('account')
            self.assertEqual(await self.v._get_token('account'), 'new-token')
            self.assertEqual(refresh.call_count, 2)

    async def test_cache_file_replace_is_atomic_and_keeps_old_file_on_failure(self):
        self.v._CACHE_FILE.write_text('{"old": 1}')
        self.v._CACHE_REGISTRY = {"new": {"name": "fake", "expire": 1}}
        with patch.object(self.v.os, "replace", side_effect=OSError("simulated disk error")):
            self.v._save_cache_registry()
        self.assertEqual(self.v._CACHE_FILE.read_text(), '{"old": 1}')
        self.v._save_cache_registry()
        self.assertIn('"new"', self.v._CACHE_FILE.read_text())


if __name__ == "__main__":
    unittest.main()
