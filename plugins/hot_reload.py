"""热重载插件 —— 不重启进程，直接重新加载插件代码。

用法（在 QQ 里发）：
    /reload              重载所有插件
    /reload random_tools 只重载指定插件

开发流程：
    1. 改插件代码并保存（编辑器随便改）
    2. QQ 里发 /reload
    3. 立刻生效 —— NapCat 连接不断、其它插件的状态不丢

原理（四步缺一不可）：
    1. Matcher.destroy()       销毁该插件的所有事件响应器
    2. _revert_plugin()        从全局插件表移除
    3. 清 PluginManager 缓存   否则重新加载报 "Plugin already exists"
    4. 删 .pyc + 清 sys.modules 否则会继续用旧字节码（最隐蔽的坑）
"""
import asyncio
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from nonebot import get_driver, logger, on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.exception import NoneBotException
from nonebot.params import CommandArg

# 本插件自己的标识，重载时特殊处理（不能边执行边卸载自己）
SELF_ID = "hot_reload"

# 本地控制端点（让电脑上不打开 QQ 也能触发热重载）
CTL_HOST = "127.0.0.1"
CTL_PORT = 8091

reload_cmd = on_command("reload", aliases={"重载"}, force_whitespace=True, priority=1, block=True)
unload_cmd = on_command("unload", aliases={"卸载"}, force_whitespace=True, priority=1, block=True)
plugins_cmd = on_command("plugins", aliases={"插件"}, force_whitespace=True, priority=1, block=True)


def _env(name: str, default: str = "") -> str:
    """读配置：先看 NoneBot2 的 .env，再回退到进程环境变量。"""
    import os

    raw = getattr(get_driver().config, name.lower(), None)
    if raw is None or raw == "":
        raw = os.environ.get(name, None)
    return (str(raw).strip() if raw is not None else "") or default


def _allowlist() -> set[str]:
    raw = _env("CHAT_ADMIN_LIST") or _env("DSH_BRIDGE_ALLOWLIST")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _can_manage(event: MessageEvent) -> bool:
    return event.message_type == "private" and str(event.user_id) in _allowlist()


def _plugin_file(plugin) -> Path | None:
    """拿到插件主文件路径（用于定位 .pyc）。"""
    module = sys.modules.get(plugin.module_name)
    if module is None:
        return None
    path = getattr(module, "__file__", None)
    return Path(path) if path else None


def unload_plugin(plugin_id: str) -> tuple[bool, str]:
    """卸载一个插件：销毁响应器 → 移出注册表 → 清缓存。"""
    from nonebot.plugin import _plugins, _revert_plugin
    from nonebot.plugin.manager import _managers

    plugin = _plugins.get(plugin_id)
    if plugin is None:
        return False, f"{plugin_id} 未加载"

    # ① 销毁该插件的所有事件响应器
    destroyed = 0
    for matcher_cls in list(plugin.matcher):
        try:
            matcher_cls.destroy()
            destroyed += 1
        except (ValueError, KeyError):
            pass

    module_name = plugin.module_name
    source_file = _plugin_file(plugin)

    # ② 从全局插件表移除
    _revert_plugin(plugin)

    # ③ 清 PluginManager 的缓存记录（不做会报 Plugin already exists）
    for mgr in _managers:
        mgr._third_party_plugin_ids.pop(plugin_id, None)
        mgr._searched_plugin_ids.pop(plugin_id, None)
        mgr.plugins.discard(module_name)
        mgr.plugins.discard(plugin_id)

    # ④ 清 sys.modules（含子模块），并异步回收可能挂着的 HTTP 连接池/后台任务
    import asyncio as _asyncio

    for name in list(sys.modules):
        if name == module_name or name.startswith(module_name + "."):
            mod = sys.modules.pop(name, None)
            for closer_name in ("close_clients", "close_image_clients", "shutdown"):
                closer = getattr(mod, closer_name, None)
                if callable(closer):
                    try:
                        loop = _asyncio.get_running_loop()
                        res = closer()
                        if _asyncio.iscoroutine(res):
                            loop.create_task(res)
                    except Exception:  # noqa: BLE001
                        pass

    # ④ 删 .pyc，否则同一秒内改的代码会继续用旧字节码
    if source_file is not None and source_file.exists():
        try:
            pyc = Path(importlib.util.cache_from_source(str(source_file)))
            if pyc.exists():
                pyc.unlink()
        except (OSError, ValueError):
            pass

    return True, f"卸载 {plugin_id}（销毁 {destroyed} 个响应器）"


def _skip_ids() -> set[str]:
    """不可热重载的插件（默认排除依赖启动钩子的重插件）。

    原因：@driver.on_startup 这类钩子只在进程启动时触发一次，
    热重载后不会重跑，插件会处于「加载了但没初始化」的半残状态。
    """
    raw = _env("QQBOT_RELOAD_SKIP", "dsh_bridge")
    return {x.strip() for x in raw.split(",") if x.strip()}


def load_plugin_fresh(module_name: str) -> tuple[bool, str]:
    """重新加载插件（必须用模块名，如 plugins.random_tools）。"""
    import nonebot

    importlib.invalidate_caches()
    try:
        nonebot.load_plugin(module_name)
        return True, module_name
    except Exception as exc:  # noqa: BLE001
        return False, f"{module_name} 失败：{exc}"


def reload_plugin(plugin_id: str) -> str:
    """完整重载一个插件，返回给用户看的说明。"""
    from nonebot.plugin import _plugins

    if plugin_id == SELF_ID:
        return "（跳过自己）"
    if plugin_id in _skip_ids():
        return f"⏭️ {plugin_id}（有状态，需重启进程）"

    plugin = _plugins.get(plugin_id)
    # 卸载前先记住模块名 —— load_plugin 需要的是它，不是 plugin_id
    if plugin is not None:
        module_name = plugin.module_name
    elif plugin_id.startswith("plugins."):
        module_name = plugin_id
    elif (Path(__file__).resolve().parent / plugin_id).exists() or (
        Path(__file__).resolve().parent / f"{plugin_id}.py"
    ).exists():
        module_name = f"plugins.{plugin_id}"
    else:
        module_name = plugin_id

    unloaded, _ = unload_plugin(plugin_id)
    if not unloaded:
        # 清理此前启动失败残留的半加载子模块与 PluginManager 记录，确保全新重读
        from nonebot.plugin.manager import _managers

        short_id = module_name.rsplit(".", 1)[-1]
        for mgr in _managers:
            mgr._third_party_plugin_ids.pop(short_id, None)
            mgr._searched_plugin_ids.pop(short_id, None)
            mgr.plugins.discard(module_name)
            mgr.plugins.discard(short_id)
        for mod_key in list(sys.modules):
            if mod_key == module_name or mod_key.startswith(module_name + "."):
                sys.modules.pop(mod_key, None)
    ok, _msg = load_plugin_fresh(module_name)

    if not unloaded and not ok:
        return f"❌ {plugin_id}（不存在或加载失败）"
    return f"{'✅' if ok else '❌'} {plugin_id}"


def _available_plugin_ids() -> list[str]:
    """当前已加载的插件（排除自己和跳过列表）。"""
    from nonebot.plugin import get_loaded_plugins

    skip = _skip_ids() | {SELF_ID}
    return sorted(p.id_ for p in get_loaded_plugins() if p.id_ not in skip)


@reload_cmd.handle()
async def handle_reload(event: MessageEvent, arg: Message = CommandArg()):
    if not _can_manage(event):
        await reload_cmd.finish()
        return

    target = arg.extract_plain_text().strip()

    if target:
        # 指定插件
        results = [reload_plugin(target)]
    else:
        # 全部插件：先卸载再加载，避免"边遍历边改字典"
        ids = _available_plugin_ids()
        if not ids:
            await reload_cmd.finish("ℹ️ 没有可重载的插件。")
        results = [reload_plugin(pid) for pid in ids]

    lines = "\n".join(f"  {r}" for r in results if r != "（跳过自己）")
    await reload_cmd.finish(f"插件重载结果\n{lines or '没有插件被重载。'}")


def _all_plugin_ids() -> list[str]:
    """列出当前已加载的所有插件（含被跳过的）。"""
    from nonebot.plugin import get_loaded_plugins

    return sorted(p.id_ for p in get_loaded_plugins())


def _scan_plugin_files() -> list[str]:
    """扫描 plugins 目录，返回**存在**的插件名（含被 `_` 前缀禁用的）。"""
    here = Path(__file__).resolve().parent
    found: list[str] = []
    for entry in here.iterdir():
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        if entry.suffix == ".py":
            found.append(entry.stem)
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            found.append(entry.name)
    return sorted(found)


def _read_project_config() -> dict:
    """读 pyproject.toml 里的 [tool.nonebot] 配置。"""
    toml_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not toml_path.is_file():
        return {}
    try:
        import tomllib

        with open(toml_path, "rb") as fh:
            conf = tomllib.load(fh)
        return conf.get("tool", {}).get("nonebot", {}) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"hot_reload: 读 pyproject.toml 失败 {exc!r}")
        return {}


def _disabled_files() -> list[str]:
    """目录里被 `_` 前缀禁用（NoneBot 不会加载）的插件名。

    返回的名字与 `_scan_plugin_files()` 保持一致（不带 .py 后缀），
    否则同一个人会在「已禁用」和「未加载」里重复出现。
    """
    here = Path(__file__).resolve().parent
    names: list[str] = []
    for entry in here.iterdir():
        if not entry.name.startswith("_"):
            continue
        if entry.suffix == ".py" and entry.stem != "__init__":
            names.append(entry.stem)
        elif entry.is_dir() and (entry / "__init__.py").is_file():
            names.append(entry.name)
    return sorted(names)


def build_plugins_report() -> str:
    """生成插件总览文本（纯函数，便于测试和复用）。"""
    loaded = _all_plugin_ids()
    skip = _skip_ids()
    on_disk = _scan_plugin_files()
    disabled = _disabled_files()
    conf = _read_project_config()

    lines: list[str] = []
    lines.append(f"📦 已加载（{len(loaded)} 个）")
    for pid in loaded:
        lines.append(f"  {'⏭️' if pid in skip else '✅'} {pid}")

    # 在目录里存在、但没加载（被 /unload 卸了，或加载失败）
    loaded_set = set(loaded)
    missing = [n for n in on_disk if n not in loaded_set and n not in disabled]
    if missing:
        lines.append("")
        lines.append(f"📁 目录存在但未加载（{len(missing)} 个）")
        for name in missing:
            lines.append(f"  ⚠️ {name}（发 /reload {name} 可加载）")

    if disabled:
        lines.append("")
        lines.append(f"🚫 已禁用（`_` 前缀，{len(disabled)} 个）")
        for name in disabled:
            lines.append(f"  {name}")

    lines.append("")
    lines.append("⚙️ 配置（pyproject.toml）")
    dirs = conf.get("plugin_dirs") or []
    lines.append(f"  扫描目录：{', '.join(dirs) if dirs else '（无）'}")
    store = conf.get("plugins") or {}
    store_names = [m for mods in store.values() for m in mods]
    lines.append(f"  商店插件：{', '.join(store_names) if store_names else '（无）'}")
    builtin = conf.get("builtin_plugins") or []
    lines.append(f"  内置插件：{', '.join(builtin) if builtin else '（无）'}")

    lines.append("")
    lines.append("⏭️ = 有状态，不参与热重载")
    return "\n".join(lines)


@plugins_cmd.handle()
async def handle_plugins(event: MessageEvent):
    """插件总览：已加载 / 目录里存在的 / 配置声明的。"""
    if not _can_manage(event):
        await plugins_cmd.finish()
        return
    await plugins_cmd.finish(build_plugins_report())


@unload_cmd.handle()
async def handle_unload(event: MessageEvent, arg: Message = CommandArg()):
    """卸载一个插件：立即失效，但文件保留，重启机器人后会恢复。"""
    if not _can_manage(event):
        await unload_cmd.finish()
        return

    target = arg.extract_plain_text().strip().split()
    if not target:
        ids = _available_plugin_ids()
        await unload_cmd.finish(
            "发「卸载 插件名」停用本次运行中的插件。\n"
            + ("可卸载　" + "、".join(ids) if ids else "当前没有可卸载的插件。")
        )

    name = target[0]
    if name == SELF_ID:
        await unload_cmd.finish("热重载插件正在处理指令，无法在此卸载。")
    if name in _skip_ids():
        await unload_cmd.finish(f"{name} 需要随进程启停。")

    ok, msg = unload_plugin(name)
    if ok:
        await unload_cmd.finish(f"已卸载 {name}。重启后会重新加载。")
    await unload_cmd.finish(msg)


# ======================================================================
# 本地控制端点：让电脑上（不打开 QQ）也能触发热重载
#
#   GET  /ping              健康检查
#   GET  /plugins           插件总览（纯文本）
#   POST /reload            重载全部
#   POST /reload?plugin=xx  只重载 xx
#   POST /unload?plugin=xx  卸载 xx
#
# 只监听 127.0.0.1，不对外网开放。
# ======================================================================
_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}


class CtlHttpServer:
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(self._handle, self._host, self._port)

    async def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin1").split()
            method = parts[0] if parts else ""
            path = parts[1] if len(parts) > 1 else "/"

            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("latin1").partition(":")
                headers[key.strip().lower()] = value.strip()

            length = int(headers.get("content-length", "0") or 0)
            body = await reader.readexactly(length) if length > 0 else b""

            await self._route(writer, method, path, body)
        except Exception as exc:  # noqa: BLE001
            try:
                await self._text(writer, 500, f"error: {exc}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _route(
        self, writer: asyncio.StreamWriter, method: str, path: str, body: bytes
    ) -> None:
        parsed = urlparse(path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        payload: dict = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:  # noqa: BLE001
                payload = {}

        name = (
            (query.get("plugin") or [None])[0] or payload.get("plugin") or ""
        ).strip()

        if route == "/ping":
            await self._text(writer, 200, "pong")
            return

        if route == "/plugins":
            await self._text(writer, 200, build_plugins_report())
            return

        if route == "/reload":
            targets = [name] if name else _available_plugin_ids()
            if not targets:
                await self._text(writer, 200, "（没有可重载的插件）")
                return
            results = [reload_plugin(t) for t in targets]
            await self._text(writer, 200, "\n".join(results))
            return

        if route == "/unload":
            if not name:
                await self._text(writer, 400, "缺少 plugin 参数，例如 /unload?plugin=echo")
                return
            if name == SELF_ID:
                await self._text(writer, 400, "不能卸载自己")
                return
            if name in _skip_ids():
                await self._text(writer, 400, f"{name} 有状态，热卸载会让它半残")
                return
            ok, msg = unload_plugin(name)
            await self._text(writer, 200 if ok else 404, f"{'✅' if ok else '❌'} {msg}")
            return

        await self._text(
            writer,
            404,
            "可用端点：\n"
            "  GET  /ping\n"
            "  GET  /plugins\n"
            "  POST /reload              （重载全部）\n"
            "  POST /reload?plugin=名字\n"
            "  POST /unload?plugin=名字\n",
        )

    async def _text(self, writer: asyncio.StreamWriter, status: int, text: str) -> None:
        body = text.encode("utf-8")
        reason = _REASONS.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("utf-8")
        writer.write(head + body)
        await writer.drain()


_ctl_server: CtlHttpServer | None = None


@get_driver().on_startup
async def _start_ctl_server() -> None:
    global _ctl_server
    if _env("QQBOT_CTL_ENABLED", "1").lower() in ("0", "false", "no"):
        logger.info("hot_reload: 本地控制端点已关闭（QQBOT_CTL_ENABLED=0）")
        return
    port = int(_env("QQBOT_CTL_PORT", str(CTL_PORT)) or CTL_PORT)
    server = CtlHttpServer(CTL_HOST, port)
    try:
        await server.start()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"hot_reload: 控制端点启动失败（端口 {port}）：{exc!r}")
        return
    _ctl_server = server
    logger.success(f"hot_reload: 本地控制端点已启动 http://{CTL_HOST}:{port}")


@get_driver().on_shutdown
async def _stop_ctl_server() -> None:
    global _ctl_server
    if _ctl_server is not None:
        await _ctl_server.stop()
        _ctl_server = None
