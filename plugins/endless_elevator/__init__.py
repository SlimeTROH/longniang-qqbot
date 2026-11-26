"""NoneBot entry point for the cross-group Endless Elevator game."""
from __future__ import annotations

import asyncio
import base64
import re
import sqlite3
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from nonebot import get_driver, get_plugin, logger, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment, PrivateMessageEvent
from nonebot.rule import Rule
from nonebot.typing import T_State

from . import expedition, heirlooms
from .cards import close_renderer, prewarm_text_cards, render_card, render_text_card
from .game import RUN_TIMEOUT, Choice, ElevatorGame, Reply
from .storage import GameMaintenance, MAINTENANCE_MESSAGE

ROOT = Path(__file__).resolve().parents[2]
game = ElevatorGame(ROOT / "state" / "endless_elevator.sqlite3")

_ACTIONS = {
    "电梯": "menu", "电梯帮助": "help", "电梯菜单": "help", "电梯开局": "start", "怎么玩": "help",
    "下潜": "start", "进图": "start", "坐电梯": "start", "开玩": "start", "出发": "start",
    "深入": "down", "下一层": "down", "继续": "down", "走": "down", "冲": "down", "进": "down", "贪": "down",
    "连战": "rush", "连战设置": "rush_settings", "连进设置": "rush_settings",
    "连进": "rush", "自动深入": "rush", "连推": "rush", "速推": "rush", "自动探索": "rush",
    "模拟战": "simulate", "模拟": "simulate", "演习": "simulate", "试玩": "simulate",
    "战报": "battle_log", "详细战报": "battle_log", "战斗记录": "battle_log",
    "硬刚": "fight", "打": "fight", "干": "fight", "砍": "fight", "a": "fight", "刚": "fight",
    "溜走": "flee", "溜": "flee", "跑": "flee", "逃": "flee", "润": "flee",
    "撤离": "extract", "跑路": "extract", "回家": "extract", "撤": "extract", "溜回家": "extract", "收手": "extract",
    "存箱": "save_box", "放箱子": "save_box", "存": "save_box", "塞箱子": "save_box",
    "锁箱": "save_box", "保底": "save_box", "换箱": "swap_box", "换": "swap_box",
    "摇人": "flare", "救命": "flare", "sos": "flare", "呼救": "flare", "喊人": "flare", "帮帮我": "flare",
    "支援": "support", "救": "support", "帮打": "support", "来救": "support", "来了": "support", "空降": "support",
    "舔包": "rob", "舔": "rob", "捞尸": "rescue", "捞": "rescue", "离开": "leave", "选": "event",
    "背包": "bag", "用药": "use", "使用": "use", "吃药": "use", "喝药": "use",
    "带药下潜": "start",
    "商店": "shop", "市场": "shop", "补给站": "shop", "购买": "buy", "买": "buy", "扩容安全箱": "buy",
    "补票": "buy", "买药": "buy", "一键买药": "buy",
    "抽绿箱": "buy", "新手箱": "buy", "抽蓝箱": "buy", "进阶箱": "buy", "抽金箱": "buy", "金装匣": "buy", "尊享箱": "buy",
    "黑市": "black_market", "兑换": "craft", "合成": "craft",
    "解码": "decode", "开箱": "decode", "开盲盒": "decode", "拆箱": "decode", "开箱子": "decode", "全开盲盒": "decode", "全开": "decode", "全开箱": "decode",
    "仓库": "warehouse", "分类仓库": "warehouse", "出售": "sell", "卖": "sell", "卖货": "sell",
    "卖垃圾": "sell", "一键卖": "sell", "一键出售": "sell", "卖非黑市蓝": "sell",
    "一键清仓": "sell", "清仓": "sell",
    "卖绿装": "sell", "卖闲置绿装": "sell", "卖蓝装": "sell", "卖闲置蓝装": "sell",
    "卖紫装": "sell", "卖闲置紫装": "sell", "卖金装": "sell", "卖红装": "sell", "卖闲置红装": "sell", "卖闲置金装": "sell",
    "卖蓝绿装": "sell", "卖绿蓝装": "sell", "卖低级装备": "sell",
    "卖闲置装备": "sell", "卖全部闲置装备": "sell", "清装备": "sell", "一键卖装备": "sell", "卖装备": "sell",
    "装备": "equipment", "穿": "wear", "穿戴": "wear", "一键配装": "autoequip", "配装": "autoequip", "穿最强": "autoequip", "重铸": "reroll",
    "确认派单": "confirm_order", "执行派单": "confirm_order", "确认单号": "confirm_order", "执行单号": "confirm_order",
    "确认执行": "confirm_order", "同意派单": "confirm_order",
    "取消派单": "cancel_order", "取消单号": "cancel_order",
    "我的派单": "orders", "派单": "orders",
    "陈列室": "gallery", "我的": "gallery", "状态": "help", "菜单": "help",
    "改名": "rename", "修改昵称": "rename", "设置昵称": "rename", "电梯改名": "rename", "游戏昵称": "rename", "昵称": "rename",
    "排行": "rank", "全服排行": "all_rank", "榜单": "rank", "探索榜": "rank", "全服探索榜": "all_rank",
    "金雨祈福": "blessing",
    "祝福":"heirlooms", "祝福库":"heirlooms", "闪耀祝福":"heirlooms", "追寻":"pursue",
    "沿用上次":"pursue", "祝福装配":"equip_blessing", "确认清空并击杀":"wish_confirm",
    "悬赏": "quests", "每日悬赏": "quests", "每日任务": "quests", "电梯任务": "quests", "任务": "quests",
    "帮助": "help", "查": "lookup",
    "攻略": "help", "电梯攻略": "help", "搜打撤攻略": "help", "新手攻略": "help", "玩法攻略": "help",
    "物品介绍": "help", "道具介绍": "help", "物品大全": "help", "词条介绍": "help", "装备介绍": "help",
    "概率规则": "help", "爆率介绍": "help", "电梯概率": "help", "电梯爆率": "help",
    "首领攻略": "help", "boss攻略": "help", "首领房": "help",
    "局内道具":"field", "临装":"field", "使用临装":"field_use", "地图道具":"field_guide", "临装图鉴":"field_guide",
    "图鉴进度":"atlas", "背包技能":"atlas", "虹彩图鉴":"atlas", "电梯图鉴":"atlas",
    "交易所": "exchange", "交易行": "exchange", "我的挂单": "exchange",
    "挂单": "exchange_list", "上架": "exchange_list", "发起交易": "exchange_list",
    "交易": "exchange_buy", "买单": "exchange_buy", "成交": "exchange_buy", "接受交易": "exchange_buy",
    "撤单": "exchange_cancel", "下架": "exchange_cancel",
    "拒单": "exchange_reject", "拒绝交易": "exchange_reject",
    "嵌入星器": "prophecy_pick", "群星寓言集": "prophecy",
    "寓言集": "prophecy", "群星寓言": "prophecy", "来自群星的寓言集": "prophecy", "星兆": "prophecy",
    "预言集": "prophecy", "群星预言": "prophecy", "来自群星的预言集": "prophecy",
    "进入寓言集": "prophecy_start", "挑战寓言集": "prophecy_start", "开启寓言集": "prophecy_start", "踏入星兆": "prophecy_start",
    "进入预言集": "prophecy_start", "挑战预言集": "prophecy_start", "开启预言集": "prophecy_start",
    "寓言榜": "prophecy_rank", "星兆榜": "prophecy_rank", "上周寓言榜": "prophecy_rank",
    "预言榜": "prophecy_rank", "上周预言榜": "prophecy_rank",
}
_REQUIRES_RUN = frozenset({
    "继续", "走", "冲", "进", "贪", "连战", "连进", "自动深入", "连推", "速推", "自动探索",
    "打", "干", "砍", "a", "刚", "溜", "跑", "逃", "润",
    "撤", "回家", "溜回家", "收手", "存", "塞箱子", "锁箱", "保底", "换", "救命", "呼救", "喊人",
    "帮帮我", "舔", "捞", "离开", "使用", "选",
})
_PREFIX_ONLY = frozenset({"状态", "我的", "菜单", "攻略", "任务", "昵称"})
_SUPPORT_ALIAS = frozenset({"救", "来了", "空降"})
_HELP_TOPICS = {
    "地图", "道具", "词条", "合成", "黑市", "兑换", "难度", "药品", "流派",
    "攻略", "新手", "玩法", "指南", "教程", "物品", "装备", "底材", "盲盒", "介绍", "大全",
    "概率", "爆率", "规则", "公式", "掉率",
    "首领", "boss", "首领房", "背包技能", "图鉴", "图鉴进度", "局内道具", "地图道具", "彩级", "虹彩",
}
_EVENT_WORDS = {
    option["label"] for event in game.catalog.events.values() for option in event["choices"]
}
_DEFAULT_ARGUMENTS = {
    "沿用上次":"沿用上次",
    "带药下潜": "推荐 | 带药",
    "探索榜": "深度", "全服探索榜": "深度",
    "扩容安全箱": "扩容", "补票": "补票", "买药": "买药", "一键买药": "买药",
    "抽绿箱": "gear_box", "新手箱": "gear_box", "抽蓝箱": "gear_box_mid", "进阶箱": "gear_box_mid",
    "抽金箱": "gear_box_high", "金装匣": "gear_box_high", "尊享箱": "gear_box_high",
    "卖垃圾": "杂物", "一键卖": "杂物", "一键出售": "杂物", "卖非黑市蓝": "非黑市蓝",
    "一键清仓": "清仓", "清仓": "清仓",
    "卖绿装": "绿装", "卖闲置绿装": "绿装", "卖蓝装": "蓝装", "卖闲置蓝装": "蓝装",
    "卖紫装": "紫装", "卖闲置紫装": "紫装", "卖金装": "金装", "卖红装": "红装", "卖闲置红装": "红装", "卖闲置金装": "金装",
    "卖蓝绿装": "蓝绿装", "卖绿蓝装": "蓝绿装", "卖低级装备": "蓝绿装",
    "卖闲置装备": "闲置装备", "卖全部闲置装备": "闲置装备", "清装备": "闲置装备", "一键卖装备": "闲置装备",
    "卖装备": "装备",
    "全开盲盒": "全部", "全开": "全部", "全开箱": "全部", "分类仓库": "分类",
    "怎么玩": "攻略", "攻略": "攻略", "电梯攻略": "攻略", "搜打撤攻略": "攻略", "新手攻略": "攻略", "玩法攻略": "攻略",
    "物品介绍": "道具", "道具介绍": "道具", "物品大全": "道具", "词条介绍": "词条", "装备介绍": "装备",
    "概率规则": "概率", "爆率介绍": "概率", "电梯概率": "概率", "电梯爆率": "概率",
    "首领攻略":"首领", "boss攻略":"首领", "首领房":"首领",
    "我的挂单": "我的", "上周寓言榜": "上周", "上周预言榜": "上周",
}


@dataclass(frozen=True)
class PendingMenu:
    group_id: str
    message_id: str
    expires_at: float
    options: tuple[Choice, ...]
    plain: bool


_menus: dict[str, PendingMenu] = {}
_menu_history: dict[str, dict[str, PendingMenu]] = {}
_menu_refs: dict[tuple[str, str, str], tuple[str, float]] = {}
_REPEATABLE_MENU_ACTIONS = frozenset({
    "shop", "buy", "warehouse", "black_market", "craft", "decode",
    "equipment", "wear", "autoequip", "reroll", "help", "lookup",
    "gallery", "rank", "all_rank", "quests", "start", "simulate", "menu", "rename", "battle_log",
    "confirm_order", "cancel_order", "orders", "sell",
    "exchange", "exchange_list", "exchange_buy", "exchange_cancel", "exchange_reject",
    "prophecy", "prophecy_start", "prophecy_rank",
})
_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_sweeper: asyncio.Task | None = None
_prewarmer: asyncio.Task | None = None
_MUTATING_COMMANDS = frozenset({
    "buy", "sell", "craft", "decode", "extract", "fight", "rob", "rescue", "support",
    "wear", "autoequip", "reroll", "confirm_order",
    "exchange_list", "exchange_buy", "exchange_cancel", "exchange_reject", "prophecy_start", "prophecy_pick",
})


@dataclass(frozen=True)
class Route:
    command: str
    argument: str
    target: str = ""
    menu: PendingMenu | None = None


_ORDER_CMD_RE = re.compile(r"^(确认|执行|同意|通过|取消|作废)\s*([Ee][0-9A-Za-z]{4})$")
_MENU_NUM_RE = re.compile(r"^([0-9]{1,2})(?:(?:\s+|[*xX×])([0-9]{1,2}))?$")
_MENU_EXTRA_RE = re.compile(r"^([0-9]{1,2})(?:\s*[|｜]\s*|\s+)(.+)$")
_LOADOUT_KEYWORDS = frozenset({"带药", "自动", "自动带药", "全带", "推荐", "商场", "地铁", "实验室", "拍卖行", "动力站", "天文台"})


def _looks_like_loadout(extra: str) -> bool:
    cleaned = extra.replace("｜", "|").replace("，", ",").replace("、", ",").strip()
    if not cleaned:
        return False
    first = re.split(r"[|,+\s]+", cleaned, maxsplit=1)[0]
    first_base = re.split(r"[*xX×]", first, maxsplit=1)[0].casefold()
    if not first_base:
        return False
    if first_base in _LOADOUT_KEYWORDS:
        return True
    if any(first_base == mk or first_base in info["name"].casefold() for mk, info in game.catalog.maps.items()):
        return True
    return any(
        first_base == alias.casefold() or (len(first_base) >= 2 and first_base in alias.casefold())
        for key, item in game.catalog.items.items()
        if item["kind"] == "supply"
        for alias in (key, item["name"], *item.get("aliases", ()))
    )


def _parse_menu_input(text: str) -> tuple[int, int | None, str] | None:
    stripped = text.strip()
    m = _MENU_NUM_RE.match(stripped)
    if m:
        index = int(m.group(1))
        qty = int(m.group(2)) if m.group(2) is not None else None
        if index <= 0:
            return None
        return index, qty, ""
    m2 = _MENU_EXTRA_RE.match(stripped)
    if m2:
        index = int(m2.group(1))
        extra = m2.group(2).strip()
        if index > 0 and _looks_like_loadout(extra):
            return index, None, extra
    return None


def menu_choice(
    text: str, group_id: str, quoted: str | None, pending: PendingMenu | None, now: float,
    *, allow_quoted_any_time: bool = True,
) -> Route | None:
    parsed = _parse_menu_input(text)
    if parsed is None or pending is None:
        return None
    index, qty, extra = parsed
    if pending.group_id != group_id or not 1 <= index <= len(pending.options):
        return None
    if quoted:
        if quoted != pending.message_id:
            return None
        if not allow_quoted_any_time and now > pending.expires_at:
            return None
    elif not pending.plain or now > pending.expires_at:
        return None
    item = pending.options[index - 1]
    argument = item.argument
    if qty is not None:
        if item.action not in {"buy", "sell"} or not argument:
            return None
        argument = f"{argument} {qty}"
    elif extra:
        if item.action != "start":
            return None
        norm_extra = extra.replace("｜", "|").strip()
        argument = f"{argument} | {norm_extra}" if argument else norm_extra
    return Route(item.action, argument, menu=pending)


def _quote_id(event: MessageEvent) -> str | None:
    original = event.original_message or event.get_message()
    for segment in original:
        if segment.type == "reply":
            return str(segment.data.get("id") or "")
    return None


def _scope(event: MessageEvent) -> tuple[str, str]:
    """Return the game location and a menu key independent of private/group switches."""
    group = getattr(event, "group_id", None)
    uid = str(event.user_id)
    if getattr(event, "message_type", None) == "private" or group is None:
        private = f"private:{uid}"
        return private, private
    return str(group), uid


def _menu_ref(event: MessageEvent, message_id: str) -> tuple[str, str, str]:
    return str(event.self_id), _scope(event)[0], message_id


def _is_old_game_menu(event: GroupMessageEvent) -> bool:
    """Recognize older numbered game replies after losing the menu cache."""
    reply = event.reply
    if reply is None or reply.message_type != "group" or str(reply.sender.user_id) != str(event.self_id):
        return False
    group = getattr(reply, "group_id", None)
    if group is not None and str(group) != str(event.group_id):
        return False
    lines = reply.message.extract_plain_text().splitlines()
    return ("👉 引用这条消息回序号" in lines or "👉 直接回序号即可（引用回复也行）" in lines or "👉 直接回序号即可（引用也行）" in lines or (
        any(line.startswith("🛗 ") and "层 · HP " in line for line in lines)
        and any(line.startswith("👉 1.") for line in lines)
    ))


def _number_route(event: MessageEvent, text: str, quoted: str | None) -> Route | None:
    uid = str(event.user_id)
    gid, menu_key = _scope(event)
    now = time.monotonic()
    selected = menu_choice(text, gid, quoted, _menus.get(menu_key), now)
    if selected is not None:
        return selected
    if quoted:
        older = _menu_history.get(menu_key, {}).get(quoted)
        if older is not None:
            hist_selected = menu_choice(text, gid, quoted, older, now)
            if hist_selected is not None and hist_selected.command in _REPEATABLE_MENU_ACTIONS:
                return hist_selected
    parsed = _parse_menu_input(text)
    if not quoted or parsed is None:
        return None
    reference = _menu_refs.get(_menu_ref(event, quoted))
    if reference is not None and reference[1] > now:
        return Route("refresh_menu" if reference[0] == uid else "foreign_menu", "")
    if not gid.startswith("private:") and _is_old_game_menu(event):
        return Route("refresh_menu", "")
    return None


def _group_allowed(event: GroupMessageEvent) -> bool:
    plugin = get_plugin("chat_bot")
    return bool(plugin and plugin.module._group_allowed(event))


def _known_lookup(name: str) -> bool:
    query = name.casefold()
    if not query:
        return False
    if heirlooms.piece_key(query) or heirlooms.set_key(query):
        return True
    if any(query in alias.casefold() for alias in ("通行证", "车票", "新手装备箱", "进阶装备箱", "尊享装备箱", "尊享金装匣", "绿箱", "蓝箱", "金箱", "金装匣", "装备盲盒", "装备箱", "安全箱", "安全箱扩容", "扩容")):
        return True
    return any(
        query in alias.casefold()
        for table in (game.catalog.items, game.catalog.affixes, game.catalog.equipment, game.catalog.field_items, game.catalog.gear_sets)
        for item in table.values()
        for alias in (item["name"], *item.get("aliases", ()))
    )


def _command_text(event: MessageEvent) -> str:
    return event.get_plaintext().strip().removeprefix("/").strip().replace("　", " ")


def private_candidate(event: PrivateMessageEvent) -> bool:
    if event.user_id == event.self_id:
        return False
    text = _command_text(event)
    if not text:
        return False
    if _parse_menu_input(text) is not None:
        return _number_route(event, text, _quote_id(event)) is not None
    if _ORDER_CMD_RE.fullmatch(text) or text in _EVENT_WORDS:
        return True
    explicit = text.startswith("电梯 ")
    command = text[3:].lstrip() if explicit else text
    if not command:
        return explicit
    word = command.split(maxsplit=1)[0].casefold()
    if word in _PREFIX_ONLY and not explicit:
        return False
    return (word in _ACTIONS or word == "开局"
            or re.fullmatch(r"(?:连战|连进)\s*(?:10|[1-9])层?", command) is not None)


async def _route(event: MessageEvent) -> Route | None:
    text = _command_text(event)
    if not text:
        return None
    uid = str(event.user_id)
    gid, menu_key = _scope(event)
    private = gid.startswith("private:")
    pending = _menus.get(menu_key)
    om = _ORDER_CMD_RE.match(text)
    if om:
        verb, oid = om.group(1), om.group(2).upper()
        return Route("cancel_order" if verb in ("取消", "作废") else "confirm_order", oid)
    if _parse_menu_input(text) is not None:
        return _number_route(event, text, _quote_id(event))
    explicit = text.startswith("电梯 ")
    if explicit:
        text = text[3:].lstrip()
    short_rush = re.fullmatch(r"(连战|连进)\s*(10|[1-9])层?", text)
    if short_rush:
        text = " ".join(short_rush.groups())
    parts = text.split(maxsplit=1)
    if not parts:
        return Route("menu", "")
    word = parts[0].casefold()
    argument = parts[1].strip() if len(parts) > 1 else ""
    if word in _PREFIX_ONLY and not explicit:
        return None
    if word in {"出发", "开玩"} and not explicit and (pending is None or pending.group_id != gid):
        return None
    if word == "帮助" and not explicit and argument not in _HELP_TOPICS:
        return None
    if word == "查" and not explicit and not _known_lookup(argument):
        if pending is None or pending.group_id != gid or time.monotonic() > pending.expires_at:
            return None
    if word == "卖" and not explicit:
        first_arg = argument.split(maxsplit=1)[0] if argument else ""
        sell_tokens = {
            "杂物", "垃圾", "清仓", "一键清仓", "白", "白色", "绿", "绿色", "蓝", "蓝色", "紫", "紫色", "非黑市蓝",
            "绿装", "闲置绿装", "蓝装", "闲置蓝装", "紫装", "闲置紫装", "金装", "闲置金装", "红装", "闲置红装",
            "蓝绿装", "绿蓝装", "低级装备", "闲置装备", "全部闲置装备", "清装备", "一键卖装备", "全部装备",
            "装备", "确认",
        }
        if not first_arg or not (
            first_arg.startswith("#") or first_arg in sell_tokens
            or _known_lookup(first_arg.split("*")[0].split("x")[0].split("×")[0])
        ):
            return None
    command = "start" if private and word == "开局" else _ACTIONS.get(word)
    if not command:
        label = text
        if label in _EVENT_WORDS and await asyncio.to_thread(game.run_kind, uid) == "choice":
            return Route("event", label)
        return None
    if word in _REQUIRES_RUN and not explicit and not await asyncio.to_thread(game.has_run, uid):
        return None
    if word in _SUPPORT_ALIAS and not explicit and not await asyncio.to_thread(game.has_signal, gid, uid):
        return None
    # Most shorthand actions are exact standalone inputs in an active game.
    if word in _REQUIRES_RUN and argument and command not in {"save_box", "swap_box", "use", "event", "rush"}:
        return None
    if word in _DEFAULT_ARGUMENTS:
        if not argument:
            argument = _DEFAULT_ARGUMENTS[word]
        elif word in ("抽绿箱", "新手箱", "抽蓝箱", "进阶箱", "抽金箱", "金装匣", "尊享箱", "卖装备", "卖蓝装", "卖紫装", "卖金装", "卖红装", "卖闲置红装", "卖蓝绿装", "卖绿蓝装", "卖低级装备", "卖闲置装备", "卖全部闲置装备", "清装备", "一键卖装备"):
            argument = f"{_DEFAULT_ARGUMENTS[word]} {argument}"
    target = ""
    if command in ("support", "exchange_list"):
        target = next((
            str(segment.data.get("qq", "")) for segment in event.get_message()
            if segment.type == "at" and str(segment.data.get("qq", "")) != str(event.self_id)
        ), "")
    return Route(command, argument, target)


async def _matches(event: GroupMessageEvent, state: T_State) -> bool:
    if event.user_id == event.self_id or not _group_allowed(event):
        return False
    try:
        route = await _route(event)
    except (sqlite3.Error, OSError) as exc:
        logger.bind(error=type(exc).__name__).warning("无尽电梯暂时无法判断当前游戏状态")
        return False
    if route is None:
        return False
    state["elevator_route"] = route
    return True


matcher = on_message(rule=Rule(_matches), priority=6, block=True)


def _matches_quoted_menu(event: GroupMessageEvent, state: T_State) -> bool:
    text = _command_text(event)
    if event.user_id == event.self_id or _parse_menu_input(text) is None:
        return False
    quoted = _quote_id(event)
    if not quoted or not _group_allowed(event):
        return False
    route = _number_route(event, text, quoted)
    if route is None:
        return False
    state["elevator_route"] = route
    return True


# A reply tied to a game menu has priority over generic numeric choices.
quoted_menu_matcher = on_message(rule=Rule(_matches_quoted_menu), priority=1, block=True)


async def _send_forward_reply(
    bot: Bot, event: GroupMessageEvent, reply: Reply, card: bytes | None
) -> dict | int | None:
    nick = event.sender.card or event.sender.nickname or "群友"
    title = reply.forward_title or "无尽电梯"
    bot_uin = str(bot.self_id)
    nodes: list[dict] = []

    def _node(content: list[dict], name: str = f"龙娘 · {title}") -> dict:
        return {"type": "node", "data": {"name": name, "uin": bot_uin, "content": content}}

    if card:
        preview_head = reply.image_caption or (reply.text.splitlines()[0] if reply.text else title)
        hint = "（点开看图，关闭后直接回序号即可）" if reply.choices else "（点开查看完整内容）"
        nodes.append(_node([{"type": "text", "data": {"text": f"@{nick} {preview_head}\n{hint}"}}]))
        b64 = base64.b64encode(card).decode("ascii")
        nodes.append(_node([{"type": "image", "data": {"file": f"base64://{b64}"}}]))
        if reply.choices:
            menu_lines = [f"{i}.{c.label}" for i, c in enumerate(reply.choices, 1)]
            nodes.append(_node([{"type": "text", "data": {"text": "👉 快捷操作（关闭卡片后直接发序号）：\n" + "\n".join(menu_lines)}}]))
    else:
        lines = reply.message().splitlines()
        preview = f"@{nick} " + (lines[0] if lines else title)
        nodes.append(_node([{"type": "text", "data": {"text": preview}}]))
        body = "\n".join(lines[1:]) if len(lines) > 1 else reply.message()
        if body:
            nodes.append(_node([{"type": "text", "data": {"text": body}}]))

    try:
        return await bot.call_api("send_forward_msg", group_id=event.group_id, messages=nodes)
    except Exception as exc:
        logger.warning(f"无尽电梯合并转发失败（{exc!r}），退回普通消息")
        return None


def _format_reply(event: MessageEvent, reply: Reply, card: bytes | None) -> Message:
    outgoing = Message(MessageSegment.reply(event.message_id))
    if getattr(event, "message_type", None) != "private" and reply.mention.isascii() and reply.mention.isdigit():
        outgoing.extend(MessageSegment.at(reply.mention) + MessageSegment.text(" "))
    if card and reply.as_image:
        if reply.image_caption:
            outgoing.append(MessageSegment.text(reply.image_caption + "\n"))
        outgoing.append(MessageSegment.image(card))
    elif card:
        if reply.notice_ids:
            outgoing.append(MessageSegment.text(reply.text.splitlines()[0] + "\n"))
        outgoing.append(MessageSegment.image(card))
        if reply.choices:
            footer = Reply("", reply.choices, reply.plain_numbers, multiline_menu=reply.multiline_menu).message()
            outgoing.append(MessageSegment.text(footer))
    else:
        outgoing.append(MessageSegment.text(reply.message()))
    return outgoing


@matcher.handle()
@quoted_menu_matcher.handle()
async def handle(bot: Bot, event: GroupMessageEvent, state: T_State) -> None:
    async def send(message: Message):
        return await bot.send(event, message)

    await _handle_route(bot, event, state["elevator_route"], send)


async def handle_private_event(
    bot: Bot, event: PrivateMessageEvent, send: Callable[[Message], Awaitable[dict | int | None]],
) -> bool:
    if not private_candidate(event):
        return False
    try:
        route = await _route(event)
    except (sqlite3.Error, OSError) as exc:
        logger.bind(error=type(exc).__name__).warning("无尽电梯暂时无法判断私聊游戏状态")
        await send(_format_reply(event, Reply("电梯的存档暂时没接上，等一会儿再试。"), None))
        return True
    if route is not None:
        await _handle_route(bot, event, route, send)
    return True


async def _handle_route(
    bot: Bot, event: MessageEvent, route: Route, send: Callable[[Message], Awaitable[dict | int | None]],
) -> None:
    uid = str(event.user_id)
    gid, menu_key = _scope(event)
    private = gid.startswith("private:")
    lock = _locks.setdefault(uid, asyncio.Lock())
    async with lock:
        if route.menu is not None and _menus.get(menu_key) is not route.menu:
            quoted_id = _quote_id(event)
            if not quoted_id:
                return
            if not (route.command in _REPEATABLE_MENU_ACTIONS and _menu_history.get(menu_key, {}).get(quoted_id) is route.menu):
                route = Route("refresh_menu", "")
        if not private and not _group_allowed(event):
            return
        refresh = route.command == "refresh_menu"
        try:
            foreign = route.command == "foreign_menu"
            if foreign or (refresh and not await asyncio.to_thread(game.has_run, uid)):
                cause = "这是另一位乘客的菜单。" if foreign else "这条菜单已失效。"
                reply = Reply(cause + "发「电梯帮助」查看你当前的选项。")
                await send(_format_reply(event, reply, None))
                return
            command = "help" if refresh else route.command
            result = await asyncio.to_thread(
                game.execute, uid, getattr(event.sender, "card", None) or event.sender.nickname or "群友",
                gid, command, route.argument, target=route.target,
            )
        except GameMaintenance:
            await send(_format_reply(event, Reply(MAINTENANCE_MESSAGE), None))
            return
        except (sqlite3.Error, OSError) as exc:
            logger.bind(error=type(exc).__name__).warning("无尽电梯的存档暂不可用")
            await send(_format_reply(event, Reply("电梯的存档暂时没接上，等一会儿再试。"), None))
            return
        if not result.text:
            return
        if refresh:
            result.text = "这条菜单已经更新，请选择当前操作。\n" + result.text
        _menus.pop(menu_key, None)
        card = None
        if result.as_image or result.card:
            try:
                if result.as_image:
                    card = await asyncio.to_thread(render_text_card, result.message())
                else:
                    card = await asyncio.to_thread(render_card, result.card, result.card_lines)
            except (OSError, ValueError):
                logger.warning("无尽电梯的卡片未生成，改用文字")
        sent = None
        if not private and result.forward_title:
            sent = await _send_forward_reply(bot, event, result, card)
        if sent is None:
            sent = await send(_format_reply(event, result, card))
        if result.choices:
            if isinstance(sent, dict):
                message_id = sent.get("message_id", "")
            elif isinstance(sent, int):
                message_id = str(sent)
            else:
                message_id = ""
            now = time.monotonic()
            message_id = str(message_id) if message_id is not None else ""
            ttl = 180 if result.plain_numbers else 120
            pending_entry = PendingMenu(gid, message_id, now + ttl, result.choices, True)
            _menus[menu_key] = pending_entry
            if message_id:
                _menu_refs[_menu_ref(event, message_id)] = (uid, now + RUN_TIMEOUT)
                hist = _menu_history.setdefault(menu_key, {})
                hist[message_id] = pending_entry
                while len(hist) > 6:
                    hist.pop(next(iter(hist)))
        await asyncio.to_thread(game.acknowledge_notices, uid, result.notice_ids)
        if command in _MUTATING_COMMANDS:
            asyncio.create_task(_refresh_user_cards(uid))


async def _maintenance() -> None:
    while True:
        now = time.monotonic()
        for key, (_, expires) in tuple(_menu_refs.items()):
            if expires <= now:
                _menu_refs.pop(key, None)
        try:
            await asyncio.to_thread(game.sweep)
        except GameMaintenance:
            pass
        except (sqlite3.Error, OSError) as exc:
            logger.bind(error=type(exc).__name__).warning("无尽电梯后台结算暂未完成")
        await asyncio.sleep(30)


async def _refresh_user_cards(uid: str) -> None:
    try:
        texts = await asyncio.to_thread(game.prewarm_user_texts, uid)
        if texts:
            await asyncio.to_thread(prewarm_text_cards, texts)
    except Exception:
        pass


async def _prewarm_all_cards() -> None:
    try:
        texts = await asyncio.to_thread(game.prewarm_texts)
        if texts:
            count = await asyncio.to_thread(prewarm_text_cards, texts)
            logger.info(f"无尽电梯卡片预热完成：共检查 {len(texts)} 张，新渲染 {count} 张")
    except Exception as exc:
        logger.bind(error=type(exc).__name__).warning("无尽电梯卡片预热未完成")


def _start_maintenance() -> None:
    global _sweeper, _prewarmer
    loop = asyncio.get_running_loop()
    if _sweeper is None or _sweeper.done():
        _sweeper = loop.create_task(_maintenance())
    if _prewarmer is None or _prewarmer.done():
        _prewarmer = loop.create_task(_prewarm_all_cards())


@get_driver().on_startup
async def startup() -> None:
    _start_maintenance()


async def shutdown() -> None:
    tasks = [t for t in (_sweeper, _prewarmer) if t]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.to_thread(close_renderer)


try:
    asyncio.get_running_loop()
except RuntimeError:
    pass
else:
    _start_maintenance()
