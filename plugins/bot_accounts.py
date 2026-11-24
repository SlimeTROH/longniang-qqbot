"""Ignore configured QQ bot accounts before dispatching their events."""

from __future__ import annotations

import json
import re
from pathlib import Path

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Event, Message, MessageEvent, NoticeEvent
from nonebot.exception import IgnoredException
from nonebot.message import _event_preprocessors, event_preprocessor
from nonebot.params import CommandArg
from nonebot.permission import Permission

from shared_history import atomic_json

DATA_FILE = Path(__file__).resolve().parents[1] / "state" / "bot_accounts.json"
_QQ_NUMBER = re.compile(r"[1-9][0-9]*")


def _load_accounts() -> set[str]:
    try:
        saved = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    entries = saved.get("accounts") if isinstance(saved, dict) else None
    if not isinstance(entries, list) or any(not _QQ_NUMBER.fullmatch(str(item)) for item in entries):
        raise ValueError("机器人名单格式无效")
    return {str(item) for item in entries}


_accounts = _load_accounts()


def _save_accounts(accounts: set[str]) -> None:
    global _accounts
    atomic_json(DATA_FILE, {"accounts": sorted(accounts)})
    _accounts = accounts


@event_preprocessor
async def ignore_bot_account(event: Event) -> None:
    if _accounts:
        actor = getattr(event, "operator_id", None) if isinstance(event, NoticeEvent) else None
        if str(actor or getattr(event, "user_id", None)) in _accounts:
            raise IgnoredException("configured bot account")


def shutdown() -> None:
    # The plugin reloader calls this before loading the replacement.
    for preprocessor in tuple(_event_preprocessors):
        if preprocessor.call is ignore_bot_account:
            _event_preprocessors.discard(preprocessor)


async def _is_admin(event: MessageEvent) -> bool:
    from plugins.chat_bot.env_config import admin_set

    return str(event.user_id) in admin_set()


_admin = Permission(_is_admin)
add_cmd = on_command(
    "加机器人", aliases={"添加机器人", "机器人 添加"}, permission=_admin,
    force_whitespace=True, priority=1, block=True,
)
remove_cmd = on_command(
    "删机器人", aliases={"删除机器人", "移除机器人", "机器人 移除"}, permission=_admin,
    force_whitespace=True, priority=1, block=True,
)
list_cmd = on_command(
    "机器人列表", aliases={"机器人 列表"}, permission=_admin, force_whitespace=True, priority=1, block=True,
)


def _change_account(event: MessageEvent, arg: Message, *, add: bool) -> str:
    account = arg.extract_plain_text().strip()
    if not _QQ_NUMBER.fullmatch(account) or any(not segment.is_text() for segment in arg):
        return f"请填写 QQ 号，例如「{'加机器人' if add else '删机器人'} 123456789」"
    if add and account == str(event.self_id):
        return "请填写其他机器人的 QQ 号"
    if add == (account in _accounts):
        return "这个账号已在名单中" if add else "名单里没有这个账号"
    updated = _accounts | {account} if add else _accounts - {account}
    try:
        _save_accounts(updated)
    except OSError as exc:
        logger.warning(f"bot_accounts: 保存名单失败（{type(exc).__name__}）")
        return "机器人名单暂时保存失败"
    return "已加入机器人名单" if add else "已从机器人名单移除"


@add_cmd.handle()
async def handle_add(event: MessageEvent, arg: Message = CommandArg()) -> None:
    await add_cmd.finish(_change_account(event, arg, add=True))


@remove_cmd.handle()
async def handle_remove(event: MessageEvent, arg: Message = CommandArg()) -> None:
    await remove_cmd.finish(_change_account(event, arg, add=False))


@list_cmd.handle()
async def handle_list() -> None:
    if not _accounts:
        await list_cmd.finish("机器人名单为空")
    await list_cmd.finish("已忽略的机器人账号\n" + "\n".join(sorted(_accounts)))
