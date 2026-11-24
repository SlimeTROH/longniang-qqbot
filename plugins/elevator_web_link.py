"""Verify web accounts using the real sender of a group message and deliver game announcements."""
from __future__ import annotations

import asyncio
import re
from importlib import reload
from pathlib import Path

from nonebot import get_bots, get_driver, get_plugin, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.rule import Rule

from elevator_web_auth import AuthError, AuthStore
import elevator_web_feed

_VERIFY = re.compile(r"/?电梯验证(?:\s|$)")
_STATE = Path(__file__).resolve().parents[1] / "state"
_auth = AuthStore(_STATE / "elevator_web_auth.sqlite3")
# The root-level helper otherwise stays cached when this plugin is hot-reloaded.
_feed = reload(elevator_web_feed).GameFeed(_STATE / "endless_elevator.sqlite3")
_task = None


async def _is_web_verification(event: GroupMessageEvent) -> bool:
    return isinstance(event, GroupMessageEvent) and event.user_id != event.self_id and bool(
        _VERIFY.match(event.get_plaintext().strip())
    )


verification = on_message(rule=Rule(_is_web_verification), priority=0, block=True)


@verification.handle()
async def _verify(event: GroupMessageEvent):
    chat = get_plugin("chat_bot")
    if not chat or not chat.module._group_allowed(event):
        return
    args = event.get_plaintext().strip().split()
    if len(args) != 2:
        await verification.finish("在网页生成密钥后，在这里发送「/电梯验证 密钥」。")
    try:
        result = await asyncio.to_thread(
            _auth.confirm, args[1], str(event.user_id), source=str(event.user_id),
            group_id=str(event.group_id), nickname=event.sender.card or event.sender.nickname or "乘客",
        )
    except AuthError as exc:
        message = "验证暂时过于频繁，请稍后再试。" if exc.status_code == 429 else "验证未完成。请检查密钥是否有效，并用对应 QQ 发送。"
        await verification.finish(message)
    response = {
        "bind": "无尽电梯账号已绑定。返回网页即可开始游戏。",
        "change": "密码已修改，请返回网页重新登录。",
        "reset": "密码已重置，请返回网页用新密码登录。",
    }
    await verification.finish(response[result["purpose"]])


def _allowed(group_id: str) -> bool:
    chat = get_plugin("chat_bot")
    if not chat:
        return False
    return chat.module.group_store.load().allows(group_id, chat.module._configured_groups())


async def _announcements():
    while True:
        await asyncio.sleep(3)
        bots = [bot for bot in get_bots().values() if isinstance(bot, Bot)]
        if len(bots) != 1:
            continue
        try:
            row = await asyncio.to_thread(_feed.claim)
            if row is None:
                continue
            entry_id, group_id, body, _ = row
            if _allowed(group_id):
                message = Message(MessageSegment.text(body))
                await asyncio.wait_for(bots[0].send_group_msg(group_id=int(group_id), message=message), timeout=20)
            await asyncio.to_thread(_feed.acknowledge, entry_id)
        except Exception as exc:
            logger.bind(error=type(exc).__name__).warning("无尽电梯网页版群播报暂未发送")


def _start():
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_announcements())


@get_driver().on_startup
async def startup():
    _start()


async def shutdown():
    if _task and not _task.done():
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)


@get_driver().on_shutdown
async def stop():
    await shutdown()


try:
    asyncio.get_running_loop()
except RuntimeError:
    pass
else:
    _start()
