"""Offline behavior tests. No QQ login, outbound messages or paid model requests."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from test_network_stability import load_vertex
import qq_recovery as qr


class PolicyTests(unittest.TestCase):
    def test_running_napcat_is_not_spawned_twice(self):
        with patch.object(qr,'configuration',return_value=('12345',{})), patch.object(qr,'owned_process_count',return_value=1), patch.object(qr.subprocess,'Popen') as spawn:
            self.assertFalse(qr._launch_napcat_unlocked())
            spawn.assert_not_called()

    def test_failed_process_inventory_never_means_qq_is_absent(self):
        with patch.object(qr,'configuration',return_value=('12345',{})), patch.object(qr,'owned_process_count',side_effect=OSError('denied')), patch.object(qr.subprocess,'Popen') as spawn:
            with self.assertRaises(OSError): qr._launch_napcat_unlocked()
            spawn.assert_not_called()

    def test_grace_debounce_backoff_and_bounded_retries(self):
        p=qr.RecoveryPolicy(0)
        for t in (15,30,119): self.assertFalse(p.observe(False,t))
        self.assertTrue(p.observe(False,120)); p.attempted(120)
        self.assertFalse(p.observe(False,179))
        self.assertTrue(p.observe(False,180)); p.attempted(180)
        self.assertTrue(p.observe(False,300)); p.attempted(300)
        self.assertFalse(p.observe(False,10000))
        p.observe(True,10001)
        self.assertEqual(p.attempts,0)
        self.assertFalse(p.observe(False,10002))

    def test_napcat_child_env_strips_proxies_and_aligns_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            napcat = Path(tmp) / 'NapCat.41842.Shell'
            data_dir = Path(tmp) / 'qq-data'
            napcat.mkdir()
            for name in ('qqnt.json', 'loadNapCat.js', 'napcat.mjs'):
                (napcat / name).write_text('x', encoding='utf-8')
            base = {
                'HTTP_PROXY': 'http://127.0.0.1:10808',
                'HTTPS_PROXY': 'http://127.0.0.1:10808',
                'ALL_PROXY': 'http://127.0.0.1:10808',
                'http_proxy': 'http://127.0.0.1:10808',
                'NAPCAT_PROXY_PORT': '10808',
                'NAPCAT_QUICK_PASSWORD': 'secret',
                'PATH': 'C:\\Windows',
            }
            with patch.object(qr, 'NAPCAT', napcat), patch.object(qr, 'QQ_DATA_DIR', data_dir):
                env = qr._napcat_child_env(base)
            for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'NAPCAT_PROXY_PORT', 'NAPCAT_QUICK_PASSWORD'):
                self.assertNotIn(k, env)
            self.assertEqual(env['NAPCAT_PROXY_CLOSE'], '1')
            self.assertEqual(env['NAPCAT_WORKDIR'], str(napcat))
            self.assertEqual(env['NAPCAT_QQ_DATA_DIR'], str(data_dir))
            self.assertEqual(env['NAPCAT_PATCH_PACKAGE'], str(napcat / 'qqnt.json'))
            self.assertEqual(env['NAPCAT_LOAD_PATH'], str(napcat / 'loadNapCat.js'))
            self.assertEqual(env['NAPCAT_MAIN_PATH'], str(napcat / 'napcat.mjs'))


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.monitor=qr.RecoveryMonitor()
        self.monitor.policy=qr.RecoveryPolicy(0)
        self.monitor.policy.failures=3
        self.monitor.account='12345'
        self.monitor.api=Mock()
        self.monitor.api.call=AsyncMock()
        self.monitor.report=Mock()

    async def test_cached_quick_login_uses_only_configured_account(self):
        self.monitor.api.call.side_effect=[{'isLogin':False,'loginPhase':'waiting_qrcode'},{},None]
        await self.monitor.tick(121)
        self.monitor.api.call.assert_any_await('SetQuickLogin',uin='12345')
        self.assertEqual(self.monitor.policy.attempts,1)

    async def test_online_resets_retries_and_never_requests_login(self):
        self.monitor.policy.attempts=2
        self.monitor.api.call.side_effect=[{'isLogin':True,'coreReady':True},{'uin':'12345'}]
        await self.monitor.tick(500)
        self.assertEqual(self.monitor.policy.attempts,0)
        self.assertEqual(self.monitor.api.call.await_count,2)

    async def test_wrong_online_account_never_restarts_or_logs_in(self):
        self.monitor.api.call.side_effect=[{'isLogin':True,'coreReady':True},{'uin':'99999'}]
        await self.monitor.tick(500)
        self.assertEqual(self.monitor.policy.attempts,0)
        self.assertIn('不同', self.monitor.report.call_args.args[0])

    async def test_pending_qr_confirmation_is_not_interrupted(self):
        self.monitor.api.call.side_effect=[{'isLogin':False,'loginPhase':'qrcode_scanned'},{}]
        await self.monitor.tick(121)
        self.assertEqual(self.monitor.api.call.await_count,2)

    async def test_unreachable_webui_does_not_spawn_over_existing_qq(self):
        self.monitor.api.call.side_effect=httpx.ConnectError('offline')
        with patch.object(qr, 'launch_napcat', return_value=False) as launch:
            await self.monitor.tick(121)
            launch.assert_called_once()
        self.assertEqual(self.monitor.policy.attempts,1)

    async def test_silent_offline_requests_scoped_worker_restart(self):
        self.monitor.api.call.side_effect=[{'isOffline':True,'loginPhase':'offline'},{'uin':'12345'},None]
        await self.monitor.tick(310)
        self.monitor.api.call.assert_any_await('RestartNapCat')

    async def test_session_revoked_login_error_pauses_retries(self):
        self.monitor.api.call.side_effect=[
            {'isLogin':False,'loginPhase':'waiting_qrcode','loginError':'[KickedOffLine] 你的账号当前登录已失效，请重新登录。'},
            {'uin':'12345'},
        ]
        await self.monitor.tick(121)
        self.assertTrue(self.monitor.manual_required)
        self.assertEqual(self.monitor.policy.attempts,0)
        self.assertEqual(self.monitor.api.call.await_count,2)
        self.assertIn('已失效', self.monitor.report.call_args.args[0])

    async def test_shutdown_cancels_monitor(self):
        self.monitor.api.close=AsyncMock()
        self.monitor.tick=AsyncMock()
        self.monitor.task=asyncio.create_task(self.monitor.run())
        await asyncio.sleep(0)
        await self.monitor.close()
        self.assertIsNone(self.monitor.task)
        self.monitor.api.close.assert_awaited_once()


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.v=load_vertex()
        self.v._CACHE_LOADED=True
        self.v._REFS_CACHE=[]
        self.temp=tempfile.TemporaryDirectory()
        self.v._CACHE_FILE=Path(self.temp.name)/'cache.json'
        self.v.CACHE_START_DELAY=0

    async def asyncTearDown(self):
        await self.v.close_clients()
        self.temp.cleanup()

    def test_short_sessions_and_growth_boundaries(self):
        self.assertEqual([self.v._session_boundary(n) for n in [7,8,19,20,39,40,79]], [0,8,8,20,20,40,60])

    async def test_near_expiry_persona_serves_while_refresh_is_deduplicated(self):
        p='x'*600
        key=self.v._persona_cache_key(p,'m')
        self.v._CACHE_REGISTRY[key]={'name':'cache/existing','expire':time.time()+100}
        self.assertEqual(self.v._lookup_persona_cache(p,'m'),'cache/existing')
        with patch.object(self.v,'_get_or_create_cache',AsyncMock(return_value='cache/new')) as build:
            for _ in range(5):
                self.v._schedule_persona_cache(persona=p,model='m')
            await asyncio.gather(*list(self.v._BACKGROUND_TASKS))
            build.assert_awaited_once()

    async def test_fresh_persona_does_not_schedule_paid_rebuild(self):
        p='x'*600
        self.v._CACHE_REGISTRY[self.v._persona_cache_key(p,'m')]={'name':'cache/fresh','expire':time.time()+900}
        self.v._schedule_persona_cache(persona=p,model='m')
        self.assertFalse(self.v._BACKGROUND_TASKS)

    async def test_growth_uses_existing_short_prefix_while_building_longer(self):
        msgs=[{'role':'user' if i%2==0 else 'assistant','content':str(i)} for i in range(21)]
        key=self.v._session_key('user',msgs[:8],'x'*600,[],'m')
        self.v._CACHE_REGISTRY[key]={'name':'cache/short','expire':time.time()+900}
        client=Mock()
        client.post=AsyncMock(return_value=httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':'hello'}]}}]}))
        with patch.object(self.v,'_load_service_account',return_value=({},'p','global')), patch.object(self.v,'_get_token',AsyncMock(return_value='token')), patch.object(self.v,'_get_client',return_value=client), patch.object(self.v,'_schedule_session_cache') as build:
            await self.v._vertex_request(msgs,'x'*600,auth_file=__file__,model='m',cache_scope='user')
            payload=client.post.call_args.kwargs['json']
            self.assertEqual(payload['cachedContent'],'cache/short')
            self.assertEqual(payload['contents'][0]['parts'][0]['text'],'8')
            self.assertEqual(len(build.call_args.kwargs['prefix']),20)

    def test_prefix_role_content_scope_and_persona_must_match(self):
        prefix=[{'role':'user','content':str(i)} for i in range(8)]
        key=self.v._session_key('a',prefix,'persona',[],'m')
        self.v._CACHE_REGISTRY[key]={'name':'cache/a','expire':time.time()+900}
        changed=[dict(x) for x in prefix];changed[0]['content']='different'
        self.assertEqual(self.v._lookup_session_cache('b',prefix,'persona','m'),'')
        self.assertEqual(self.v._lookup_session_cache('a',changed,'persona','m'),'')
        changed=[dict(x) for x in prefix]; changed[0]['role']='assistant'
        self.assertEqual(self.v._lookup_session_cache('a',changed,'persona','m'),'')
        self.assertEqual(self.v._lookup_session_cache('a',prefix,'other','m'),'')


class ProviderPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        package=types.ModuleType('pool_test_chat'); package.__path__=[]
        guard=types.ModuleType('pool_test_chat.guard'); guard.SECURITY_BLOCK='test'
        sys.modules[package.__name__]=package; sys.modules[guard.__name__]=guard
        spec=importlib.util.spec_from_file_location('pool_test_chat.llm',ROOT/'plugins/chat_bot/llm.py')
        self.llm=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=self.llm
        spec.loader.exec_module(self.llm)

    async def asyncTearDown(self):
        await self.llm.close_clients()

    async def test_relay_is_direct_and_provider_uses_fixed_proxy_with_reuse(self):
        direct=self.llm._chat_client('http://127.0.0.1:8317/v1/chat/completions')
        proxied=self.llm._chat_client('https://example.invalid')
        self.assertIs(direct,self.llm._chat_client('http://127.0.0.1:8317/v1'))
        self.assertIsNot(direct,proxied)
        self.assertIs(self.llm._CHAT_CLIENTS[asyncio.get_running_loop()][None],direct)
        self.assertFalse(direct._trust_env)
        await self.llm.close_clients()
        self.assertTrue(direct.is_closed); self.assertTrue(proxied.is_closed)

    async def test_gemini_only_starts_immediately_without_hedge_delay(self):
        reply=self.llm.ChatResult(reply='test',ok=True)
        with patch.object(self.llm,'_chat_once',AsyncMock(return_value=reply)), patch.object(self.llm,'HEDGE_SECONDARY_DELAY',60.0):
            result=await asyncio.wait_for(self.llm.chat([], 'persona', '', gemini_key='fake',gemini_model='m',gemini_base='http://127.0.0.1:8317'), timeout=0.5)
            self.assertEqual(result.via,'gemini')


class WebUITests(unittest.IsolatedAsyncioTestCase):
    async def test_local_auth_and_login_contract(self):
        api=qr.WebUI({'port':6099,'token':'offline-test-secret'})
        await api.client.aclose()
        calls=[]
        def handle(req):
            calls.append(req)
            if req.url.path.endswith('auth/login'):
                self.assertEqual(json.loads(req.content)['hash'], qr.hashlib.sha256(b'offline-test-secret.napcat').hexdigest())
                return httpx.Response(200,json={'code':0,'data':{'Credential':'fake'}})
            self.assertEqual(req.headers['authorization'],'Bearer fake')
            return httpx.Response(200,json={'code':0,'data':{'isLogin':True}})
        api.client=httpx.AsyncClient(base_url='http://127.0.0.1:6099/api/',transport=httpx.MockTransport(handle),trust_env=False)
        try:
            await api.call('CheckLoginStatus'); await api.call('CheckLoginStatus')
            self.assertEqual(len(calls),3)
        finally: await api.close()

    async def test_webui_2fa_is_not_bypassed(self):
        api=qr.WebUI({'port':6099,'token':'test'})
        await api.client.aclose()
        api.client=httpx.AsyncClient(base_url='http://127.0.0.1/api/',transport=httpx.MockTransport(lambda _: httpx.Response(200,json={'code':0,'data':{'require2FA':True}})))
        try:
            with self.assertRaises(PermissionError): await api.call('CheckLoginStatus')
        finally: await api.close()


if __name__=='__main__': unittest.main()

