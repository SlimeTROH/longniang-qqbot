"""Bounded recovery for Full Desktop NTQQ + LiteLoaderQQNT + NapCat.Framework (`D:\\dshex\\qqnt_bot`).

Isolated from personal `D:\\TCQQ\\QQ.exe`; strips proxy env vars so QQNT uses direct domestic network.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import httpx

from napcat_webui import WebUI

HERE = Path(__file__).resolve().parent
BOT_QQ_ROOT = HERE.parent / 'qqnt_bot'
NAPCAT = BOT_QQ_ROOT / 'LiteLoader' / 'plugins' / 'napcat_framework'
QQ_EXE = BOT_QQ_ROOT / 'QQ.exe'
QQ_DATA_DIR = BOT_QQ_ROOT / 'userdata'
CONFIG = NAPCAT / 'config'
QQ_APP = BOT_QQ_ROOT / 'versions' / '9.9.37-54395' / 'resources' / 'app'
BOOTSTRAP_TEMPLATE = HERE / 'scripts' / 'qq_framework_entry.js'
# QQ 54395 x64, using the upstream QQNTFileVerifyPatch five-byte replacement.
# Pin the result so a client update must be reviewed before it can be launched.
QQ_NATIVE_SHA256 = 'bf8c1aa8ce80cdc84afe081e9f20d14c5d97430f0a6b9243e4903b95ea6e9c8c'

log = logging.getLogger('qq_recovery')
PASSWORD_STATE_FILE = HERE / 'logs' / 'qq-password-state.json'
_JSON_WRITE_LOCK = threading.Lock()
_PROXY_ENV_KEYS = (
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy',
    'NO_PROXY', 'no_proxy',
    'NAPCAT_PROXY_PORT', 'NAPCAT_PROXY_ADDRESS',
)
_SESSION_REVOKED_MARKERS = ('已失效', 'kickedoffline', 'kicked', '重新登录', '账号安全', '风控', '冻结')


def _is_session_revoked(message: object) -> bool:
    text = str(message or '').strip().lower()
    return bool(text) and any(marker in text for marker in _SESSION_REVOKED_MARKERS)


def _napcat_child_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a proxy-free, path-aligned environment for this bot's Desktop QQ + NapCat.Framework."""
    env = dict(os.environ if base_env is None else base_env)
    env.pop('NAPCAT_QUICK_PASSWORD', None)
    for key in _PROXY_ENV_KEYS:
        env.pop(key, None)
    env['NAPCAT_PROXY_CLOSE'] = '1'
    env['NAPCAT_WORKDIR'] = str(NAPCAT)
    env['NAPCAT_QQ_DATA_DIR'] = str(QQ_DATA_DIR)
    env['LITELOADERQQNT_PROFILE'] = str(BOT_QQ_ROOT / 'LiteLoader')
    patch_pkg = NAPCAT / 'qqnt.json'
    load_js = NAPCAT / 'loadNapCat.js'
    main_mjs = NAPCAT / 'napcat.mjs'
    if patch_pkg.is_file() and load_js.is_file() and main_mjs.is_file():
        env['NAPCAT_PATCH_PACKAGE'] = str(patch_pkg)
        env['NAPCAT_LOAD_PATH'] = str(load_js)
        env['NAPCAT_MAIN_PATH'] = str(main_mjs)
    return env


def atomic_json(path: Path, payload: dict) -> None:
    """Separate temporary files prevent CLI/monitor writers colliding."""
    with _JSON_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.' + path.stem + '-', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(payload, out, ensure_ascii=False)
            for attempt in range(5):
                try:
                    os.replace(name, path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01 * (2 ** attempt))
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


def runtime_paths() -> dict:
    port = 6099
    try:
        web = json.loads((CONFIG / 'webui.json').read_text(encoding='utf-8-sig'))
        port = int(web.get('port', 6099))
    except (OSError, ValueError, TypeError):
        pass
    return {
        'napcat_dir': str(NAPCAT),
        'qq_exe': str(QQ_EXE),
        'launcher': str(QQ_EXE),
        'hook': str(BOT_QQ_ROOT / 'dbghelp.dll'),
        'qq_native': str(QQ_APP.parent.parent / 'QQNT.dll'),
        'entry': str(QQ_APP / 'app_launcher' / 'LiteLoader.js'),
        'app_package': str(QQ_APP / 'package.json'),
        'liteloader_main': str(BOT_QQ_ROOT / 'LiteLoader' / 'src' / 'main.js'),
        'framework_main': str(NAPCAT / 'napcat.cjs'),
        'framework_module': str(NAPCAT / 'napcat.mjs'),
        'bootstrap_template': str(BOOTSTRAP_TEMPLATE),
        'webui_config': str(CONFIG / 'webui.json'),
        'webui_url': f'http://127.0.0.1:{port}',
    }


def installation_status() -> dict:
    """Check the actual entry point, including isolation, before launching QQ."""
    paths = runtime_paths()
    required = ('qq_exe', 'qq_native', 'entry', 'app_package', 'liteloader_main',
                'framework_main', 'framework_module', 'bootstrap_template', 'webui_config')
    missing = [paths[key] for key in required if not Path(paths[key]).is_file()]
    errors = []
    try:
        package = json.loads(Path(paths['app_package']).read_text(encoding='utf-8-sig'))
        if package.get('main') != './app_launcher/LiteLoader.js':
            errors.append('QQ package does not load the isolated Framework entry')
    except (OSError, ValueError, AttributeError):
        errors.append('QQ package is unavailable or invalid')
    try:
        installed = Path(paths['entry']).read_text(encoding='utf-8-sig')
        expected = Path(paths['bootstrap_template']).read_text(encoding='utf-8-sig')
        if installed != expected:
            errors.append('Framework entry is incomplete; install scripts/qq_framework_entry.js')
    except (OSError, UnicodeError):
        errors.append('Framework entry or its template is unavailable')
    try:
        digest = hashlib.sha256()
        with Path(paths['qq_native']).open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != QQ_NATIVE_SHA256:
            errors.append('QQ native library does not match the verified Framework installation')
    except OSError:
        errors.append('QQ native library is unavailable')
    return {
        'ok': not missing and not errors, 'missing': missing, 'errors': errors,
        'qq_exe': paths['qq_exe'], 'webui': paths['webui_url'],
    }


async def status_snapshot() -> dict:
    result = {
        'phase': 'unreachable', 'webui_online': False,
        'account_matches': False, 'onebot_online': False,
        'manual_required': False,
    }
    api = None
    try:
        account, web = configuration()
        api = WebUI(web)
        async with asyncio.timeout(4.0):
            status = await api.call('CheckLoginStatus') or {}
            info = await api.call('GetQQLoginInfo') or {}
        result.update(
            phase=status.get('loginPhase') or 'unknown',
            webui_online=bool(status.get('isLogin') and status.get('coreReady')),
            account_matches=str(info.get('uin') or '') == account,
        )
        try:
            saved = json.loads(PASSWORD_STATE_FILE.read_text(encoding='utf-8'))
            if saved.get('account_hash') == hashlib.sha256(account.encode()).hexdigest():
                result['manual_required'] = bool(saved.get('manual_required')) and not result['webui_online']
        except (OSError, ValueError):
            pass
    except Exception as exc:
        result['webui_error'] = type(exc).__name__
    finally:
        if api:
            await api.close()
    try:
        from qqctl import OneBot, ok
        async with asyncio.timeout(3.0):
            async with OneBot() as bot:
                reply = await bot.call('get_status')
                result['onebot_online'] = bool(ok(reply) and (reply.get('data') or {}).get('online'))
    except Exception as exc:
        result['onebot_error'] = type(exc).__name__
    return result


def configuration() -> tuple[str, dict]:
    from qqctl import load_env
    conf = load_env()
    web = json.loads((CONFIG / 'webui.json').read_text(encoding='utf-8-sig'))
    account = conf.get('QQBOT_ACCOUNT') or str(web.get('autoLoginAccount') or '')
    if not account.isdecimal():
        raise ValueError('QQBOT_ACCOUNT must be set to the bot account')
    if web.get('disableWebUI') or not 1 <= int(web.get('port', 6099)) <= 65535:
        raise ValueError('NapCat WebUI is disabled or invalid')
    return account, web


def open_webui() -> bool:
    """Open this installation's local login page without printing its token."""
    import webbrowser
    from urllib.parse import urlencode

    _, web = configuration()
    url = f"http://127.0.0.1:{int(web.get('port', 6099))}/webui/web_login"
    if web.get('token'):
        url += '?' + urlencode({'token': str(web['token'])})
    return webbrowser.open(url, new=2)


def owned_process_count() -> int:
    """Only recognize executables physically inside D:\\dshex\\qqnt_bot\\ (never touches D:\\TCQQ)."""
    script = (
        "$r=[IO.Path]::GetFullPath($env:QQBOT_NAPCAT_ROOT).TrimEnd('\\')+'\\'; "
        "$q=[IO.Path]::GetFullPath($env:QQBOT_QQ_ROOT).TrimEnd('\\')+'\\'; "
        "@((Get-CimInstance Win32_Process -ErrorAction Stop) | "
        "Where-Object {$_.ExecutablePath -and ($_.ExecutablePath.StartsWith($r,[StringComparison]::OrdinalIgnoreCase) "
        "-or $_.ExecutablePath.StartsWith($q,[StringComparison]::OrdinalIgnoreCase)) "
        "-and $_.Name -in @('QQ.exe','NapCatWinBootMain.exe')}).Count"
    )
    env = dict(os.environ, QQBOT_NAPCAT_ROOT=str(NAPCAT), QQBOT_QQ_ROOT=str(QQ_EXE.parent))
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, timeout=20, check=True, env=env,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    return int(result.stdout.strip())


def _launch_napcat_unlocked() -> bool:
    """Launch isolated Full Desktop NTQQ (`D:\\dshex\\qqnt_bot\\QQ.exe`) with LiteLoader + NapCat.Framework."""
    configuration()
    if owned_process_count():
        return False
    if not QQ_EXE.is_file():
        raise FileNotFoundError('QQ.exe missing: ' + str(QQ_EXE))
    check = installation_status()
    if not check['ok']:
        raise RuntimeError('Framework installation incomplete: ' + '; '.join(check['errors'] + check['missing']))
    QQ_DATA_DIR.mkdir(parents=True, exist_ok=True)
    env = _napcat_child_env()
    (HERE / 'logs').mkdir(exist_ok=True)
    with (HERE / 'logs' / 'napcat-launch.log').open('ab') as out:
        subprocess.Popen(
            [str(QQ_EXE), '--user-data-dir=' + str(QQ_DATA_DIR)],
            cwd=str(QQ_EXE.parent),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
        )
    return True


def launch_napcat() -> bool:
    import msvcrt
    (HERE / 'logs').mkdir(exist_ok=True)
    with (HERE / 'logs' / 'napcat-launch.lock').open('a+b') as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        try:
            return _launch_napcat_unlocked()
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


class RecoveryPolicy:
    """Pure policy: debounce, startup grace, three retries, reset only when online."""
    def __init__(self, now: float):
        self.unhealthy_since = now
        self.next_attempt = now + 120.0
        self.attempts = 0
        self.failures = 0

    def observe(self, online: bool, now: float) -> bool:
        if online:
            self.failures = 0
            self.attempts = 0
            self.unhealthy_since = now
            self.next_attempt = now + 120.0
            return False
        self.failures += 1
        return self.failures >= 3 and self.attempts < 3 and now >= self.next_attempt

    def attempted(self, now: float) -> None:
        self.attempts += 1
        self.next_attempt = now + (60.0, 120.0, 300.0)[min(self.attempts - 1, 2)]


class RecoveryMonitor:
    def __init__(self):
        self.task = None
        self.state = ''
        self.policy = RecoveryPolicy(time.monotonic())
        self.api = None
        self.last_launch = 0.0
        self.password_attempted = False
        self.manual_required = False
        self._password_signature = None

    def start(self):
        from qqctl import load_env
        if load_env().get('QQBOT_AUTO_RECOVER', '1').lower() in ('0', 'false', 'off'):
            return
        if self.task and not self.task.done():
            return
        (HERE / 'logs').mkdir(exist_ok=True)
        if not log.handlers:
            handler = RotatingFileHandler(
                HERE / 'logs' / 'qq-recovery.log',
                maxBytes=1024 * 1024, backupCount=2, encoding='utf-8',
            )
            handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
            log.addHandler(handler)
            log.setLevel(logging.INFO)
        self.task = asyncio.create_task(self.run())

    def _restore_password_state(self):
        try:
            saved = json.loads(PASSWORD_STATE_FILE.read_text(encoding='utf-8'))
            if saved.get('account_hash') == hashlib.sha256(self.account.encode()).hexdigest():
                self.password_attempted = self.password_attempted or bool(saved.get('password_attempted'))
                self.manual_required = self.manual_required or bool(saved.get('manual_required'))
        except (OSError, ValueError):
            pass

    def report(self, state: str):
        if state != self.state:
            log.info('%s (attempts=%d)', state, self.policy.attempts)
            self.state = state
        atomic_json(HERE / 'logs' / 'qq-recovery.json', {
            'state': state, 'attempts': self.policy.attempts,
            'checked_at': time.time(), 'monitor_pid': os.getpid()})
        if getattr(self, 'account', ''):
            payload = {
                'account_hash': hashlib.sha256(self.account.encode()).hexdigest(),
                'password_attempted': self.password_attempted,
                'manual_required': self.manual_required,
            }
            try:
                info = PASSWORD_STATE_FILE.stat()
                signature = (payload, info.st_mtime_ns, info.st_size)
            except OSError:
                signature = None
            if signature is None or signature != self._password_signature:
                atomic_json(PASSWORD_STATE_FILE, payload)
                info = PASSWORD_STATE_FILE.stat()
                self._password_signature = (payload, info.st_mtime_ns, info.st_size)

    async def tick(self, now: float):
        if self.api is None:
            self.account, web = configuration()
            self._restore_password_state()
            self.api = WebUI(web)
        try:
            status = await self.api.call('CheckLoginStatus') or {}
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if self.policy.observe(False, now):
                self.policy.attempted(now)
                if await asyncio.to_thread(launch_napcat):
                    self.report('桌面版机器人 QQ 已退出，已重新启动')
                else:
                    self.report('桌面版机器人 QQ 仍在运行，等待就绪')
            else:
                self.report('等待桌面版机器人 QQ 连接' if self.policy.attempts < 3 else '需要检查机器人 QQ 窗口')
            return
        info = await self.api.call('GetQQLoginInfo') or {}
        actual_account = str(info.get('uin') or '')
        if actual_account and actual_account != self.account:
            self.report('当前登录账号与 QQBOT_ACCOUNT 不同，已停止自动操作')
            return
        online = status.get('isLogin') is True and status.get('coreReady') is True
        if online:
            if actual_account != self.account:
                self.report('当前登录账号与 QQBOT_ACCOUNT 不同，已停止自动操作')
                return
            self.policy.observe(True, now)
            self.password_attempted = False
            self.manual_required = False
            self.report('QQ 在线（完整桌面 Framework 模式），监控中')
            return
        ready = self.policy.observe(False, now)
        phase = str(status.get('loginPhase') or '')
        if self.manual_required:
            self.report('请在桌面 QQ 登录窗口或 WebUI 完成验证/扫码')
            return
        if _is_session_revoked(status.get('loginError')):
            self.manual_required = True
            self.report('QQ 登录态已失效，已停止自动重登以防触发风控，请在 QQ 窗口或 WebUI 扫码')
            return
        if self.policy.attempts >= 3:
            self.report('自动重登已尝试三次，需要扫码或手动验证')
            return
        if not ready:
            self.report('QQ 离线，等待客户端内置恢复')
            return
        if phase in ('initializing', 'qrcode_scanned', 'reconnecting') and now - self.policy.unhealthy_since < 300:
            self.report('客户端正在恢复或等待登录确认')
            return
        self.policy.attempted(now)
        if status.get('isOffline') or phase == 'reconnecting':
            await self.api.call('RestartNapCat')
            self.api.credential = ''
            self.report('QQ 离线，已请求重建登录服务')
        else:
            try:
                await self.api.call('SetQuickLogin', uin=self.account)
                self.report('已请求缓存账号自动重登')
            except RuntimeError as exc:
                if _is_session_revoked(exc):
                    self.manual_required = True
                    self.report('QQ 缓存登录态已失效，已停止密码重试以防触发风控，请在 QQ 窗口扫码')
                else:
                    await self._password_fallback()

    async def _password_fallback(self):
        from qq_credentials import password_digest
        self._restore_password_state()
        if self.password_attempted:
            self.manual_required = True
            self.report('本次掉线已尝试密码登录，请在 QQ 窗口检查或扫码')
            return
        try:
            digest = password_digest(self.account)
        except Exception:
            self.manual_required = True
            self.report('无法读取当前用户加密的登录凭据，请检查凭据或扫码')
            return
        if not digest:
            self.report('缓存登录失败，未找到该账号的密码凭据，可在 QQ 窗口扫码')
            return
        self.password_attempted = True
        self.report('正在尝试已保存账号的密码登录')
        try:
            result = await self.api.call('PasswordLogin', uin=self.account, passwordMd5=digest)
        except Exception:
            self.manual_required = True
            self.report('密码登录未成功或结果未确认，已停止重试；请在 QQ 窗口检查或扫码')
            return
        finally:
            digest = ''
        if isinstance(result, dict) and (result.get('needCaptcha') or result.get('needNewDevice')):
            self.manual_required = True
            self.report('密码登录需要验证码或设备验证，请在桌面 QQ 窗口或 WebUI 完成验证')
            return
        self.report('已提交密码登录，等待 QQ 确认在线')

    async def run(self):
        try:
            while True:
                try:
                    async with asyncio.timeout(45.0):
                        await self.tick(time.monotonic())
                except Exception as exc:
                    self.report('恢复检查暂不可用：' + type(exc).__name__)
                await asyncio.sleep(15.0)
        finally:
            if self.api:
                await self.api.close()

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None


async def login_once() -> bool:
    monitor = RecoveryMonitor()
    monitor.policy = RecoveryPolicy(time.monotonic() - 121.0)
    monitor.policy.failures = 3
    try:
        await monitor.tick(time.monotonic())
        if monitor.manual_required:
            print(monitor.state)
            return False
        deadline = time.monotonic() + 45.0
        while time.monotonic() < deadline:
            status = await monitor.api.call('CheckLoginStatus') or {}
            if status.get('isLogin') and status.get('coreReady'):
                info = await monitor.api.call('GetQQLoginInfo') or {}
                if str(info.get('uin') or '') == monitor.account:
                    monitor.policy.observe(True, time.monotonic())
                    monitor.report('QQ 在线，密码兜底已安装')
                    print('QQ online; account matches')
                    return True
            await asyncio.sleep(3.0)
        print('QQ login is pending; check QQ window or WebUI')
        return False
    except Exception as exc:
        print('QQ login unavailable: ' + type(exc).__name__)
        return False
    finally:
        if monitor.api:
            await monitor.api.close()


if __name__ == '__main__':
    if '--check' in sys.argv:
        check = installation_status()
        print(json.dumps(check, ensure_ascii=False))
        sys.exit(0 if check['ok'] else 1)
    if '--paths' in sys.argv:
        print(json.dumps(runtime_paths(), ensure_ascii=False))
        sys.exit(0)
    if '--status' in sys.argv:
        print(json.dumps(asyncio.run(status_snapshot()), ensure_ascii=False))
        sys.exit(0)
    if '--open-webui' in sys.argv:
        try:
            opened = open_webui()
            print('NapCat Framework login page opened' if opened else 'Could not open the browser')
            sys.exit(0 if opened else 1)
        except Exception as exc:
            print('NapCat Framework login page unavailable: ' + type(exc).__name__)
            sys.exit(1)
    if '--login' in sys.argv:
        sys.exit(0 if asyncio.run(login_once()) else 2)
    if '--launch' in sys.argv:
        try:
            print('Desktop QQ (Framework) started' if launch_napcat() else 'Desktop QQ (Framework) already running')
        except Exception as exc:
            print('Desktop QQ launch unavailable: ' + type(exc).__name__)
            sys.exit(1)
