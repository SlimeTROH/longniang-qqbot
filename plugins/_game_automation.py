"""农场与钓鱼自动化派单管理命令插件（确认派单 / 取消派单 / 查单 / 直接发起托管）。

关键安全设计：
  · 玩家确认必须携带唯一标识符（如 `确认 ORD-7K9M2X`），不拦截裸「确认 / 是」，
    因此与好感度重置、开垦土地、升级土地等二次确认 waiter 完全互不干扰；
  · 严格校验派单归属人 UID，群友无法冒名确认或取消他人的派单；
  · 随 NoneBot 生命周期自动恢复未完结托管、优雅关闭后台协程。
"""
from __future__ import annotations

import re

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.rule import Rule

from plugins.chat_bot.automation_planner import (
    AutomationSpec,
    compute_and_create_order,
    extract_order_id,
    format_order_card,
    format_order_status,
    normalize_order_id,
    order_store,
    parse_automation_from_text,
    runner,
)
from plugins.chat_bot.env_config import is_admin
from plugins.chat_bot.message_io import reply_to

_CONFIRM_RE = re.compile(
    r"^[/／!！]?\s*(?:确认派单|执行派单|启动派单|确认托管|确认)\s*(.*)$",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(
    r"^[/／!！]?\s*(?:取消派单|取消托管|停止托管|终止派单|取消)\s*(.*)$",
    re.IGNORECASE,
)
_QUERY_RE = re.compile(
    r"^[/／!！]?\s*(?:我的派单|派单列表|托管状态|查单|查询派单)(?:\s+(.*))?$",
    re.IGNORECASE,
)
_DISPATCH_CMD_RE = re.compile(
    r"^[/／!！]?\s*(?:派单|自动托管|自动化管理|托管农场|托管钓鱼)(?:\s*(.*))?$",
    re.IGNORECASE,
)


def _match_confirm(event: MessageEvent) -> bool:
    text = (event.get_plaintext() or "").strip()
    m = _CONFIRM_RE.match(text)
    if not m:
        return False
    tail = (m.group(1) or "").strip()
    # 裸「确认」不拦截，留给 waiter；「确认派单」或带合法唯一标识码的「确认 ORD-XXXXXX」才接管
    if text.lstrip("/／!！ ").startswith(("确认派单", "执行派单", "启动派单", "确认托管")):
        return True
    return bool(normalize_order_id(tail) or extract_order_id(tail, allow_bare_code=True))


def _match_cancel(event: MessageEvent) -> bool:
    text = (event.get_plaintext() or "").strip()
    m = _CANCEL_RE.match(text)
    if not m:
        return False
    tail = (m.group(1) or "").strip()
    if text.lstrip("/／!！ ").startswith(("取消派单", "取消托管", "停止托管", "终止派单")):
        return True
    return bool(normalize_order_id(tail) or extract_order_id(tail, allow_bare_code=True))


def _match_query(event: MessageEvent) -> bool:
    return bool(_QUERY_RE.match((event.get_plaintext() or "").strip()))


def _match_dispatch(event: MessageEvent) -> bool:
    return bool(_DISPATCH_CMD_RE.match((event.get_plaintext() or "").strip()))


confirm_matcher = on_message(rule=Rule(_match_confirm), priority=2, block=True)
cancel_matcher = on_message(rule=Rule(_match_cancel), priority=2, block=True)
query_matcher = on_message(rule=Rule(_match_query), priority=2, block=True)
dispatch_matcher = on_message(rule=Rule(_match_dispatch), priority=2, block=True)


@get_driver().on_startup
async def _recover_orders() -> None:
    runner.recover_running_orders()


@get_driver().on_shutdown
async def _shutdown_orders() -> None:
    await runner.shutdown()


@confirm_matcher.handle()
async def _handle_confirm(event: MessageEvent) -> None:
    uid = str(event.user_id)
    text = (event.get_plaintext() or "").strip()
    m = _CONFIRM_RE.match(text)
    tail = (m.group(1) or "").strip() if m else ""
    oid = normalize_order_id(tail) or extract_order_id(tail, allow_bare_code=True)
    if not oid:
        await confirm_matcher.finish(
            reply_to(
                event,
                "⚠️ 确认自动化派单必须提供唯一标识符，例如：\n"
                "确认 ORD-7K9M2X\n"
                "（发送「我的派单」可查看待确认单号）",
            )
        )
    _ok, msg = await runner.confirm_and_start(uid, oid, is_admin=is_admin(uid))
    await confirm_matcher.finish(reply_to(event, msg))


@cancel_matcher.handle()
async def _handle_cancel(event: MessageEvent) -> None:
    uid = str(event.user_id)
    text = (event.get_plaintext() or "").strip()
    m = _CANCEL_RE.match(text)
    tail = (m.group(1) or "").strip() if m else ""
    oid = normalize_order_id(tail) or extract_order_id(tail, allow_bare_code=True)
    _ok, msg = runner.cancel_order(uid, oid, is_admin=is_admin(uid))
    await cancel_matcher.finish(reply_to(event, msg))


@query_matcher.handle()
async def _handle_query(event: MessageEvent) -> None:
    uid = str(event.user_id)
    text = (event.get_plaintext() or "").strip()
    m = _QUERY_RE.match(text)
    tail = (m.group(1) or "").strip() if m else ""
    oid = normalize_order_id(tail) or extract_order_id(tail, allow_bare_code=True)
    if oid:
        order = order_store.get(oid)
        if not order:
            await query_matcher.finish(reply_to(event, f"找不到派单 {oid}。"))
        if order.uid != uid and not is_admin(uid):
            await query_matcher.finish(reply_to(event, f"派单 {oid} 不属于你。"))
        await query_matcher.finish(reply_to(event, format_order_status(order)))

    orders = order_store.list_by_user(uid, limit=5)
    if not orders:
        await query_matcher.finish(
            reply_to(
                event,
                "你目前还没有自动化派单记录。\n"
                "在群里可以 @ 我说「帮我自动托管农场/钓鱼」，也可以发「派单 自动管理农场2小时」。",
            )
        )
    status_icon = {
        "pending_confirm": "🕓待确认",
        "running": "⚙️运行中",
        "completed": "✅已完成",
        "cancelled": "🛑已取消",
        "expired": "⌛已过期",
        "failed": "⚠️中断",
    }
    lines = [f"📋 你的最近派单（共 {len(orders)} 单）："]
    for o in orders:
        st = status_icon.get(o.status, o.status)
        lines.append(f"· {o.order_id} [{st}] {o.goal_summary[:28]}（{o.current_step}/{len(o.steps)}步）")
    lines.append("\n发「查单 单号」查看详情，「确认 单号」启动待确认派单，「取消 单号」中止运行。")
    await query_matcher.finish(reply_to(event, "\n".join(lines)))


@dispatch_matcher.handle()
async def _handle_direct_dispatch(event: MessageEvent) -> None:
    uid = str(event.user_id)
    raw = (event.get_plaintext() or "").strip()
    spec = parse_automation_from_text(raw)
    if not spec.is_automation:
        low = raw.lstrip("/／!！ ")
        domain = "fishing" if low.startswith("托管钓鱼") else ("farm" if low.startswith("托管农场") else "both")
        spec = AutomationSpec(is_automation=True, domain=domain, goal=raw[:120])
    gid = str(event.group_id) if isinstance(event, GroupMessageEvent) else ""
    order = await compute_and_create_order(uid, gid, spec, raw_request=raw)
    await dispatch_matcher.finish(reply_to(event, format_order_card(order)))
