import concurrent.futures
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import qq_recovery as qr


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)

    def tearDown(self):self.temp.cleanup()

    def test_atomic_writers_do_not_share_a_temporary_file(self):
        path=self.root/'status.json'
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda i: qr.atomic_json(path,{'writer':i}),range(24)))
        self.assertIn(json.loads(path.read_text())['writer'],range(24))
        self.assertEqual(list(self.root.glob('*.tmp')),[])

    def test_failed_atomic_replace_keeps_existing_record_and_removes_temp(self):
        path=self.root/'status.json';path.write_text('{"original":true}')
        with patch.object(qr.os,'replace',side_effect=OSError('fake disk error')):
            with self.assertRaises(OSError):qr.atomic_json(path,{'original':False})
        self.assertTrue(json.loads(path.read_text())['original'])
        self.assertEqual(list(self.root.glob('*.tmp')),[])

    def test_runtime_paths_return_only_public_launcher_metadata(self):
        config=self.root/'config';config.mkdir()
        (config/'webui.json').write_text(json.dumps({'port':6098,'token':'must-stay-private','autoLoginAccount':'1234567'}))
        with patch.object(qr,'CONFIG',config), patch.object(qr,'NAPCAT',self.root), patch.object(qr,'QQ_EXE',self.root/'QQ.exe'):
            data=qr.runtime_paths()
        self.assertEqual(data['webui_url'],'http://127.0.0.1:6098')
        self.assertNotIn('must-stay-private',json.dumps(data))
        self.assertNotIn('1234567',json.dumps(data))

    def framework_fixture(self):
        paths = {key: str(self.root / (key + '.txt')) for key in (
            'qq_exe', 'qq_native', 'entry', 'app_package', 'liteloader_main',
            'framework_main', 'framework_module', 'bootstrap_template', 'webui_config')}
        for filename in paths.values():
            Path(filename).write_text('fixture', encoding='utf-8')
        Path(paths['app_package']).write_text(json.dumps({'main': './app_launcher/LiteLoader.js'}))
        paths['webui_url'] = 'http://127.0.0.1:6099'
        native_hash = patch.object(qr, 'QQ_NATIVE_SHA256', hashlib.sha256(b'fixture').hexdigest())
        native_hash.start()
        self.addCleanup(native_hash.stop)
        return paths

    def test_installation_rejects_broken_entry_even_when_all_files_exist(self):
        paths = self.framework_fixture()
        Path(paths['entry']).write_text('require(String.rawD:\\bot\\LiteLoader);')
        with patch.object(qr, 'runtime_paths', return_value=paths):
            status = qr.installation_status()
        self.assertFalse(status['ok'])
        self.assertEqual(status['missing'], [])
        self.assertTrue(any('entry is incomplete' in error for error in status['errors']))

    def test_installation_requires_qq_to_use_isolated_entry(self):
        paths = self.framework_fixture()
        with patch.object(qr, 'runtime_paths', return_value=paths):
            self.assertTrue(qr.installation_status()['ok'])
            Path(paths['app_package']).write_text(json.dumps({'main': './application.asar/app_launcher/index.js'}))
            self.assertFalse(qr.installation_status()['ok'])
            Path(paths['app_package']).write_text('[]')
            self.assertFalse(qr.installation_status()['ok'])

    def test_installation_reports_missing_framework_before_launch(self):
        paths = self.framework_fixture()
        Path(paths['framework_module']).unlink()
        with patch.object(qr, 'runtime_paths', return_value=paths):
            status = qr.installation_status()
        self.assertFalse(status['ok'])
        self.assertEqual(status['missing'], [paths['framework_module']])

    def test_installation_rejects_an_unreviewed_native_library(self):
        paths = self.framework_fixture()
        Path(paths['qq_native']).write_bytes(b'different QQ version')
        with patch.object(qr, 'runtime_paths', return_value=paths):
            status = qr.installation_status()
        self.assertFalse(status['ok'])
        self.assertTrue(any('native library does not match' in error for error in status['errors']))

    def test_launch_checks_entry_and_passes_isolated_directory_to_qq(self):
        exe = self.root / 'QQ.exe'
        exe.touch()
        (self.root / 'dbghelp.dll').touch()
        data = self.root / 'userdata'
        with patch.object(qr, 'QQ_EXE', exe), patch.object(qr, 'QQ_DATA_DIR', data), \
                patch.object(qr, 'HERE', self.root), patch.object(qr, 'configuration'), \
                patch.object(qr, 'owned_process_count', return_value=0), \
                patch.object(qr, 'installation_status', return_value={'ok': False, 'errors': ['bad entry'], 'missing': []}) as check, \
                patch.object(qr, '_napcat_child_env', return_value={}), patch.object(qr.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'bad entry'):
                qr._launch_napcat_unlocked()
            spawn.assert_not_called()
            self.assertFalse(data.exists())
            check.return_value = {'ok': True}
            self.assertTrue(qr._launch_napcat_unlocked())
        self.assertEqual(spawn.call_args.args[0], [str(exe), '--user-data-dir=' + str(data)])
        self.assertTrue(data.is_dir())

    def test_open_webui_passes_token_to_login_page(self):
        web={'port':6099,'token':'test token+value'}
        with patch.object(qr,'configuration',return_value=('1234567',web)):
            with patch('webbrowser.open',return_value=True) as open_browser:
                self.assertTrue(qr.open_webui())
        url=urlparse(open_browser.call_args.args[0])
        self.assertEqual(url.hostname,'127.0.0.1')
        self.assertEqual(url.port,6099)
        self.assertEqual(url.path,'/webui/web_login')
        self.assertEqual(parse_qs(url.query),{'token':[web['token']]})

    def test_password_state_is_not_rewritten_until_changed_or_replaced(self):
        state=self.root/'logs'/'pw.json'
        with patch.object(qr,'HERE',self.root), patch.object(qr,'PASSWORD_STATE_FILE',state):
            monitor=qr.RecoveryMonitor();monitor.account='12345'
            monitor.report('online')
            with patch.object(qr,'atomic_json',wraps=qr.atomic_json) as write:
                monitor.report('online')
                self.assertEqual([c.args[0] for c in write.call_args_list],[self.root/'logs'/'qq-recovery.json'])
            qr.atomic_json(state,{'changed_by_other_process':True})
            monitor.report('online')
            self.assertIn('account_hash',json.loads(state.read_text()))


if __name__=='__main__':unittest.main()
