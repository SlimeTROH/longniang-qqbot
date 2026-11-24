"""农场插件离线兜底与修复版签到命令。

修复内容：
  1. 启动时若 `nonebot-plugin-farm` 离线拉取签到配置失败，注入内置里程碑表避免 KeyError 崩溃；
  2. 接管 `/签到`（`/农场签到`）：修复原版插件吞异常不加币及旧版补丁 `monthSignDays` 恒写死为 1、
     里程碑种子/点券未发放的问题，底层与 `game_controller.do_farm_sign_sqlite` 共用同一事务逻辑。
"""
from __future__ import annotations

from nonebot import get_driver, logger, on_command

from plugins.chat_bot.game_controller import DEFAULT_SIGN_MILESTONES, do_farm_sign_sqlite

DEFAULT_SIGN = {"continuou": DEFAULT_SIGN_MILESTONES}


def fix_sign_config() -> bool:
    """检查并补全农场插件内存中的签到里程碑配置。"""
    try:
        from nonebot_plugin_farm.json import g_pJsonManager
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"farm_offline_fix: 找不到农场插件（{exc!r}），跳过")
        return False

    sign = getattr(g_pJsonManager, "m_pSign", None)
    if not sign:
        g_pJsonManager.m_pSign = dict(DEFAULT_SIGN)
        logger.success("farm_offline_fix: 签到配置为空（离线），已注入内置默认值")
        return True
    if "continuou" not in sign:
        sign["continuou"] = DEFAULT_SIGN_MILESTONES
        logger.success("farm_offline_fix: 签到配置缺少 continuou，已补全")
        return True
    return False


@get_driver().on_startup
async def _patch_on_startup() -> None:
    fix_sign_config()


sign_cmd = on_command("签到", aliases={"农场签到"}, priority=3, block=True)


def _db_path() -> str:
    from nonebot_plugin_farm.config import g_sDBFilePath

    return str(g_sDBFilePath)


def _do_sign(uid: str) -> tuple[bool, str]:
    return do_farm_sign_sqlite(_db_path(), str(uid))


@sign_cmd.handle()
async def _handle_sign(event) -> None:
    uid = str(event.user_id)
    try:
        _ok, text = _do_sign(uid)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"farm_offline_fix: 签到失败 {exc!r}")
        await sign_cmd.finish(f"签到出错了：{type(exc).__name__}")

    if getattr(event, "message_type", "") == "group":
        try:
            from nonebot.adapters.onebot.v11 import MessageSegment

            _msg = MessageSegment.reply(int(event.message_id)) + text
        except Exception:  # noqa: BLE001
            _msg = text
        await sign_cmd.finish(_msg)
    await sign_cmd.finish(text)
