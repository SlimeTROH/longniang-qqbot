"""qqctl —— QQ 机器人命令行控制台

不需要打开 QQ，直接在命令行里操控机器人 / 查看状态。

用法（在 qqbot 目录下）：
    ..\\venv\\Scripts\\python.exe qqctl.py info              查看状态
    ..\\venv\\Scripts\\python.exe qqctl.py groups            群列表
    ..\\venv\\Scripts\\python.exe qqctl.py friends           好友列表
    ..\\venv\\Scripts\\python.exe qqctl.py send-group 123456 你好
    ..\\venv\\Scripts\\python.exe qqctl.py send-private 123456789 你好
    ..\\venv\\Scripts\\python.exe qqctl.py poke 123456789    戳一戳
    ..\\venv\\Scripts\\python.exe qqctl.py help

原理：直接连接配置中的 OneBot V11 WebSocket，调用它的 API。
      不依赖 NoneBot 是否在运行。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import websockets  # noqa: E402

HERE = Path(__file__).resolve().parent


def load_env() -> dict:
    """从 .env 读配置（不依赖 NoneBot）。

    2026-10-20：这台机器的 .env 是 GBK(GB18030)，原来只按 utf-8 读会直接抛异常，
    所以依次尝试 utf-8 → gb18030 → latin-1。
    """
    conf: dict[str, str] = {}
    env_file = HERE / ".env"
    if env_file.exists():
        raw = env_file.read_bytes()
        text = ""
        for enc in ("utf-8-sig", "gb18030", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            conf[key.strip()] = value.strip().strip('"').strip("'")
    return conf


CONF = load_env()
TOKEN = CONF.get("ONEBOT_ACCESS_TOKEN", "")
RAW_URLS = CONF.get("ONEBOT_WS_URLS", '["ws://127.0.0.1:3001"]')
try:
    WS_URL = json.loads(RAW_URLS)[0]
except Exception:
    WS_URL = "ws://127.0.0.1:3001"


def expected_account() -> str:
    return (os.environ.get("QQBOT_ACCOUNT") or CONF.get("QQBOT_ACCOUNT") or "").strip()


class OneBot:
    """极简 OneBot 客户端。"""

    def __init__(self) -> None:
        self.ws = None
        self._seq = 0

    async def __aenter__(self):
        headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
        self.ws = await websockets.connect(WS_URL, additional_headers=headers, open_timeout=10, proxy=None)
        return self

    async def __aexit__(self, *exc):
        if self.ws:
            await self.ws.close()

    async def call(self, action: str, **params):
        account = expected_account()
        if account and action not in ("get_status", "get_login_info"):
            login = await self._request("get_login_info")
            if not ok(login) or str((data_of(login) or {}).get("user_id") or "") != account:
                raise PermissionError("OneBot 登录账号与 QQBOT_ACCOUNT 不符")
        return await self._request(action, **params)

    async def _request(self, action: str, **params):
        self._seq += 1
        echo = f"{action}-{self._seq}"
        await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
        deadline = asyncio.get_running_loop().time() + 20
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise asyncio.TimeoutError("OneBot API timed out")
            raw = await asyncio.wait_for(self.ws.recv(), timeout=left)
            msg = json.loads(raw)
            if msg.get("echo") == echo:
                return msg
            # 忽略推送的事件


def ok(resp: dict) -> bool:
    return resp.get("status") == "ok" or resp.get("retcode") == 0


def data_of(resp: dict):
    return resp.get("data")


async def connection_status() -> dict:
    """Check the configured OneBot without sending messages or listing contacts."""
    result = {"connected": False, "online": False, "account_matches": None}
    account = expected_account()

    async def query() -> None:
        async with OneBot() as bot:
            result["connected"] = True
            status = await bot.call("get_status")
            result["online"] = ok(status) and (data_of(status) or {}).get("online") is True
            if result["online"] and account:
                login = await bot.call("get_login_info")
                result["account_matches"] = (
                    ok(login) and str((data_of(login) or {}).get("user_id", "")) == account
                )

    try:
        await asyncio.wait_for(query(), timeout=4)
    except Exception as exc:
        result["error"] = type(exc).__name__
        result["online"] = False
    return result


HELP = """
qqctl —— QQ 机器人命令行控制台

  -- 操控 QQ（连接 OneBot，机器人程序可以独立运行）--
  info                      机器人与连接状态
  status                    OneBot 连接状态（JSON）
  groups                    群列表
  friends                   好友列表
  send-group <群号> <内容>   发群消息
  send-private <QQ> <内容>   发私聊
  poke <QQ>                 戳一戳（私聊）
  raw <api> [json参数]       直接调用任意 OneBot API

  -- 管理插件（走机器人本地端点，需要机器人运行中）--
  plugins                   列出所有插件
  reload [插件名]            热重载（不写名字 = 重载全部）
  unload <插件名>            热卸载（文件保留，重启后恢复）

  help                      显示帮助
  quit                      退出

示例：
  info
  plugins
  reload random_tools
  unload echo
  send-private 123456789 来自命令行的消息
"""


async def cmd_info() -> None:
    async with OneBot() as bot:
        login = data_of(await bot.call("get_login_info")) or {}
        status = data_of(await bot.call("get_status")) or {}
        friends = data_of(await bot.call("get_friend_list")) or []
        groups = data_of(await bot.call("get_group_list")) or []

        print("=" * 46)
        print("  QQ 机器人状态")
        print("=" * 46)
        print(f"  连接地址   : {WS_URL}")
        print(f"  机器人 QQ  : {login.get('user_id')}  ({login.get('nickname')})")
        print(f"  在线状态   : {'✅ 在线' if status.get('online') else '❌ 离线'}")
        print(f"  好友数量   : {len(friends)}")
        print(f"  群数量     : {len(groups)}")
        print("=" * 46)


async def cmd_groups() -> None:
    async with OneBot() as bot:
        groups = data_of(await bot.call("get_group_list")) or []
        print(f"共 {len(groups)} 个群：\n")
        for g in groups:
            print(f"  {g.get('group_id'):>12}  {g.get('group_name')}  (人数 {g.get('member_count')})")


async def cmd_friends() -> None:
    async with OneBot() as bot:
        friends = data_of(await bot.call("get_friend_list")) or []
        print(f"共 {len(friends)} 个好友：\n")
        for f in friends:
            print(f"  {f.get('user_id'):>12}  {f.get('nickname')}")


async def cmd_send_group(group_id: str, text: str) -> None:
    async with OneBot() as bot:
        resp = await bot.call("send_group_msg", group_id=int(group_id), message=text)
        print(f"  {'✅ 已发送' if ok(resp) else '❌ 失败'}  message_id={data_of(resp) and data_of(resp).get('message_id')}")


async def cmd_send_private(user_id: str, text: str) -> None:
    async with OneBot() as bot:
        resp = await bot.call("send_private_msg", user_id=int(user_id), message=text)
        print(f"  {'✅ 已发送' if ok(resp) else '❌ 失败'}  message_id={data_of(resp) and data_of(resp).get('message_id')}")


async def cmd_poke(user_id: str) -> None:
    async with OneBot() as bot:
        resp = await bot.call("friend_poke", user_id=int(user_id))
        print(f"  {'✅ 已戳' if ok(resp) else '❌ 失败'}  {resp.get('message') or ''}")


async def cmd_raw(action: str, params_json: str) -> None:
    params = json.loads(params_json) if params_json else {}
    async with OneBot() as bot:
        resp = await bot.call(action, **params)
        print(json.dumps(resp, ensure_ascii=False, indent=2))


# ----------------------------------------------------------------------
# 插件管理：通过机器人自建的本地控制端点（HTTP 127.0.0.1:8091）
# 这条路径不经过 QQ，所以在电脑上就能热重载/卸载插件。
# ----------------------------------------------------------------------
CTL_URL = "http://127.0.0.1:8091"


def _ctl(method: str, path: str) -> str:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"{CTL_URL}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return f"❌ HTTP {exc.code}\n{exc.read().decode('utf-8')}"
    except Exception as exc:  # noqa: BLE001
        return (
            f"❌ 连不上机器人的控制端点（{CTL_URL}）\n"
            f"   原因：{type(exc).__name__}: {exc}\n"
            "   请确认：\n"
            "     ① 机器人正在运行\n"
            "     ② .env 里 QQBOT_CTL_ENABLED 没有被设成 0"
        )


def cmd_plugins() -> None:
    """列出所有插件（不需要走 QQ）。"""
    print(_ctl("GET", "/plugins"))


def cmd_reload(name: str) -> None:
    """热重载插件。"""
    path = f"/reload?plugin={name}" if name else "/reload"
    print(_ctl("POST", path))


def cmd_unload(name: str) -> None:
    """热卸载插件。"""
    print(_ctl("POST", f"/unload?plugin={name}"))


def run_command(line: str) -> bool:
    """执行一条命令。返回 False 表示要退出。"""
    argv = line.split()
    if not argv:
        return True

    cmd, *rest = argv
    if cmd in ("quit", "exit", "q"):
        return False

    try:
        if cmd in ("help", "-h", "--help", "?"):
            print(HELP)
        elif cmd == "info":
            asyncio.run(cmd_info())
        elif cmd == "status":
            print(json.dumps(asyncio.run(connection_status())))
        elif cmd == "groups":
            asyncio.run(cmd_groups())
        elif cmd == "friends":
            asyncio.run(cmd_friends())
        elif cmd == "send-group" and len(rest) >= 2:
            asyncio.run(cmd_send_group(rest[0], " ".join(rest[1:])))
        elif cmd == "send-private" and len(rest) >= 2:
            asyncio.run(cmd_send_private(rest[0], " ".join(rest[1:])))
        elif cmd == "poke" and len(rest) >= 1:
            asyncio.run(cmd_poke(rest[0]))
        elif cmd == "raw" and len(rest) >= 1:
            asyncio.run(cmd_raw(rest[0], rest[1] if len(rest) > 1 else ""))
        elif cmd == "plugins":
            cmd_plugins()
        elif cmd == "reload":
            cmd_reload(" ".join(rest).strip())
        elif cmd == "unload" and len(rest) >= 1:
            cmd_unload(rest[0])
        else:
            print(f"未知命令或不完整：{cmd}（输入 help 查看用法）")
    except ConnectionRefusedError:
        print("❌ 连不上 OneBot，请确认配置中的服务正在运行")
    except asyncio.TimeoutError:
        print("❌ 请求超时，OneBot 服务可能未响应")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 出错了：{type(exc).__name__}: {exc}")
    return True


def interactive() -> None:
    """交互式控制台（双击 bat 进入）。"""
    print("=" * 46)
    print("  QQ 机器人控制台")
    print("=" * 46)
    print("  输入命令后回车。help 看帮助，quit 退出。")
    print("=" * 46)
    print()
    while True:
        try:
            line = input("qq> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not run_command(line):
            break
    print("已退出。")


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        interactive()
        return
    run_command(" ".join(argv))


if __name__ == "__main__":
    main()

