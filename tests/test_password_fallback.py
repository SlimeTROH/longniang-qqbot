"""Offline regression tests; only synthetic credentials and mock QQ APIs."""
import asyncio
import hashlib
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import qq_credentials as credentials
import qq_recovery as recovery


class CredentialTests(unittest.TestCase):
    def test_windows_encryption_roundtrip_and_account_binding(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(credentials,'CREDENTIAL_FILE',Path(folder)/'login.dpapi'):
            password='offline-secret-测试'
            credentials.save_password('12345',password)
            raw=credentials.CREDENTIAL_FILE.read_bytes()
            digest=hashlib.md5(password.encode()).hexdigest()
            self.assertNotIn(password.encode(),raw)
            self.assertNotIn(digest.encode(),raw)
            self.assertEqual(credentials.password_digest('12345'),digest)
            self.assertEqual(credentials.password_digest('99999'),'')

    def test_missing_credentials_are_not_inferred_from_another_account(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(credentials,'CREDENTIAL_FILE',Path(folder)/'missing'):
            self.assertEqual(credentials.password_digest('12345'),'')


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.state_patch=patch.object(recovery,'PASSWORD_STATE_FILE',Path(self.temp.name)/'password-state.json')
        self.state_patch.start()
        self.m=recovery.RecoveryMonitor()
        self.m.policy=recovery.RecoveryPolicy(0)
        self.m.policy.failures=3
        self.m.account='12345'
        self.m.api=Mock()
        self.m.api.call=AsyncMock()
        self.m.report=Mock()

    def tearDown(self):
        self.state_patch.stop()
        self.temp.cleanup()

    async def test_verification_pause_survives_restart_and_online_clears_it(self):
        with patch.object(recovery,'HERE',Path(self.temp.name)):
            (Path(self.temp.name)/'logs').mkdir()
            self.m.report=recovery.RecoveryMonitor.report.__get__(self.m)
            self.m.password_attempted=True;self.m.manual_required=True
            self.m.report('waiting for manual verification')
            restarted=recovery.RecoveryMonitor();restarted.account='12345'
            restarted._restore_password_state()
            self.assertTrue(restarted.password_attempted)
            self.assertTrue(restarted.manual_required)
            self.m.api.call.side_effect=[{'isLogin':True,'coreReady':True},{'uin':'12345'}]
            await self.m.tick(1000)
            restored=recovery.RecoveryMonitor();restored.account='12345';restored._restore_password_state()
            self.assertFalse(restored.manual_required)
            self.assertFalse(restored.password_attempted)

    async def test_quick_login_failure_uses_saved_password_once(self):
        self.m.api.call.side_effect=[{'isLogin':False,'loginPhase':'waiting_qrcode'},{},RuntimeError('failed'),None]
        with patch.object(credentials,'password_digest',return_value='a'*32):
            await self.m.tick(121)
        self.m.api.call.assert_any_await('PasswordLogin',uin='12345',passwordMd5='a'*32)
        self.assertTrue(self.m.password_attempted)
        self.assertFalse(self.m.manual_required)
        self.assertNotIn('a'*32,str(self.m.report.call_args_list))

    async def test_successful_quick_login_does_not_read_password(self):
        self.m.api.call.side_effect=[{'isLogin':False,'loginPhase':'waiting_qrcode'},{},None]
        with patch.object(credentials,'password_digest') as read:
            await self.m.tick(121)
            read.assert_not_called()

    async def test_captcha_or_device_check_stops_all_login_retries(self):
        for result in ({'needCaptcha':True,'proofWaterUrl':'secret-url'}, {'needNewDevice':True,'jumpUrl':'secret-url'}):
            self.m.password_attempted=False;self.m.manual_required=False
            self.m.api.call=AsyncMock(return_value=result)
            with patch.object(credentials,'password_digest',return_value='a'*32):
                await self.m._password_fallback()
            self.assertTrue(self.m.manual_required)
            self.m.api.call=AsyncMock(side_effect=[{'isLogin':False,'loginPhase':'waiting_qrcode'},{}])
            await self.m.tick(1000)
            self.assertEqual(self.m.api.call.await_count,2)
            self.assertNotIn('secret-url',str(self.m.report.call_args_list))

    async def test_wrong_password_or_timeout_is_never_repeated(self):
        for exc in (RuntimeError('wrong password'),TimeoutError('outcome unknown')):
            self.m.password_attempted=False;self.m.manual_required=False
            self.m.api.call=AsyncMock(side_effect=exc)
            with patch.object(credentials,'password_digest',return_value='a'*32):
                await self.m._password_fallback()
                await self.m._password_fallback()
            self.assertTrue(self.m.manual_required)
            self.assertEqual(self.m.api.call.await_count,1)

    async def test_no_secret_available_falls_back_to_existing_bounded_policy(self):
        with patch.object(credentials,'password_digest',return_value=''):
            await self.m._password_fallback()
        self.m.api.call.assert_not_awaited()
        self.assertFalse(self.m.password_attempted)

    async def test_online_resets_password_and_verification_state(self):
        self.m.password_attempted=True;self.m.manual_required=True
        self.m.api.call.side_effect=[{'isLogin':True,'coreReady':True},{'uin':'12345'}]
        await self.m.tick(1000)
        self.assertFalse(self.m.password_attempted)
        self.assertFalse(self.m.manual_required)

    async def test_wrong_account_is_never_sent_the_password(self):
        self.m.api.call.side_effect=[{'isLogin':False,'loginPhase':'waiting_qrcode'},{'uin':'99999'}]
        with patch.object(credentials,'password_digest') as read:
            await self.m.tick(121)
            read.assert_not_called()

    async def test_revoked_quick_login_skips_password_fallback(self):
        self.m.api.call.side_effect=[
            {'isLogin':False,'loginPhase':'waiting_qrcode'},
            {},
            RuntimeError('NapCat API rejected SetQuickLogin: 登录态已失效，请重新登录。'),
        ]
        with patch.object(credentials,'password_digest') as read:
            await self.m.tick(121)
            read.assert_not_called()
        self.assertTrue(self.m.manual_required)
        self.assertFalse(self.m.password_attempted)


if __name__=='__main__': unittest.main()
