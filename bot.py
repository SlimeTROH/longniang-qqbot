import asyncio
import os
import sys
from pathlib import Path

from network_config import configure_process_proxy

configure_process_proxy()

import nonebot
from nonebot.adapters.onebot.v11 import Bot, PrivateMessageEvent
from nonebot.log import default_format, logger
from nonebot.matcher import Matcher
from nonebot.rule import Rule
from onebot_guard import Adapter as ONEBOT_V11Adapter

# 初始化 NoneBot
nonebot.init()

# 额外把日志写入文件（UTF-8），方便随时回看运行状态。
# 注意：写不了时（权限/占用）不能让机器人起不来，降级为只输出控制台。
try:
    os.makedirs("logs", exist_ok=True)
    logger.add(
        "logs/bot.log",
        rotation="10 MB",
        retention="7 days",
        encoding="utf-8",
        format=default_format,
    )
except Exception as _exc:  # noqa: BLE001
    logger.warning(f"日志文件不可写，仅输出到控制台：{_exc!r}")

# 连接独立运行的 OneBot V11 服务。
driver = nonebot.get_driver()
driver.config.command_start.add("/")


def _private_admins() -> set[str]:
    from plugins.chat_bot.env_config import admin_set

    return admin_set()


driver.register_adapter(ONEBOT_V11Adapter, private_admins=_private_admins)


def _is_private_elevator(bot: Bot, event: PrivateMessageEvent) -> bool:
    adapter = bot.adapter
    return isinstance(adapter, ONEBOT_V11Adapter) and adapter.uses_private_elevator(event)


_private_elevator = nonebot.on_message(rule=Rule(_is_private_elevator), priority=-1, block=False)


@_private_elevator.handle()
async def _route_private_elevator(bot: Bot, event: PrivateMessageEvent, matcher: Matcher) -> None:
    adapter = bot.adapter
    if isinstance(adapter, ONEBOT_V11Adapter):
        if not adapter._allows_private_peer(event.user_id):
            matcher.stop_propagation()
        if await adapter.dispatch_private_elevator(bot, event):
            matcher.stop_propagation()


@driver.on_shutdown
async def _close_network_clients() -> None:
    for name, module in list(sys.modules.items()):
        if name.endswith(("chat_bot.vertex", "chat_bot.llm")):
            close = getattr(module, "close_clients", None)
            if close is not None:
                await close()


_chat_warmup_started = False


@driver.on_bot_connect
async def _warm_chat() -> None:
    global _chat_warmup_started
    if _chat_warmup_started:
        return
    _chat_warmup_started = True
    for name, module in list(sys.modules.items()):
        if name.endswith("chat_bot") and getattr(module, "persona", None):
            vertex_module = sys.modules.get(name + ".vertex")
            if vertex_module:
                task = asyncio.create_task(vertex_module.warmup(
                    module.VERTEX_AUTH, module.GEMINI_CHAT_MODEL,
                    module.llm.build_static_system(
                        module.persona.base,
                        module.persona.name,
                        games_enabled=module.FARM_ENABLED or module.FISHING_ENABLED,
                    )))
                vertex_module._BACKGROUND_TASKS.add(task)
                task.add_done_callback(vertex_module._BACKGROUND_TASKS.discard)

# ------------------------------------------------------------------ 加载插件
# 用 pyproject.toml 统一加载，这样「手写插件」和「nb plugin install 装的商店插件」
# 都会被加载：
#     [tool.nonebot] plugin_dirs        → 扫描目录，装手写插件
#     [tool.nonebot.plugins]            → 模块名，装商店插件
_TOML = Path(__file__).resolve().parent / "pyproject.toml"
if _TOML.is_file():
    nonebot.load_from_toml(str(_TOML))
else:
    # 没有 pyproject.toml 时回退，保证机器人仍能起来
    logger.warning("未找到 pyproject.toml，回退为只加载 plugins 目录")
    nonebot.load_plugins("plugins")


# ---------------------------------------------------------------- 跳过农场插件的启动联网
# nonebot_plugin_farm 的 on_startup 会去 http://diuse.work 拉签到表和查作物库版本，
# 那个域名现在 502，两次请求各超时 5~6 秒 —— 启动时间大半耗在这里。
# 本地资源文件插件本来就自带，不联网也能正常种地。
#
# 时机很关键：必须等 load_from_toml 把插件都加载完（此时还没 run，startup 未执行），
# 再从 sys.modules 拿到模块打补丁。手动 import 会破坏 NoneBot 的插件加载。
def _skip_farm_network() -> None:
    try:
        req = sys.modules.get("nonebot_plugin_farm.request")
        js = sys.modules.get("nonebot_plugin_farm.json")
        if req is None or js is None:
            return

        async def _skip(*_a, **_k) -> bool:
            return True

        # initPlantDBFile 是 classmethod；initSignInFile 是实例方法
        req.CRequestManager.initPlantDBFile = classmethod(_skip)
        js.CJsonManager.initSignInFile = _skip
        logger.info("farm: 已跳过启动联网检查（用本地资源）")
    except Exception as _exc:  # noqa: BLE001
        logger.warning(f"farm 联网跳过失败（不影响运行）：{_exc!r}")


_skip_farm_network()

if __name__ == "__main__":
    nonebot.run()
