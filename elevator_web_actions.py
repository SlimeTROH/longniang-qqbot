"""Validate structured browser inputs and adapt them to the shared elevator engine."""
from __future__ import annotations

import json
import re


_EMPTY_COMMANDS = frozenset({
    "down", "fight", "flee", "extract", "rob", "rescue", "leave", "flare", "autoequip", "blessing", "wish_cancel",
})
_PARAMETERS = {
    **dict.fromkeys(_EMPTY_COMMANDS, ()),
    "start": ("map_id", "supplies", "nickname","focus"),
    "prophecy_start": ("nickname",),
    "prophecy_pick": ("floor", "id"),
    "pursue": ("focus",),
    "equip_blessing": ("slot","piece"),
    "wish_confirm": ("token",),
    "simulate": ("map_id", "nickname"),
    "rush": ("floors",),
    "rush_settings": ("floors", "skip_valuable", "event_mode", "auto_elite"),
    "event": ("option",),
    "save_box": ("index",),
    "swap_box": ("index", "safe_index"),
    "take_box": ("safe_index",),
    "use": ("index",),
    "field_use": ("id", "slot"),
    "field_pick": ("id", "floor", "slot"),
    "field_remove": ("id", "slot"),
    "support": ("owner_id",),
    "buy": ("id", "quantity"),
    "craft": ("id", "quantity"),
    "sell": ("stock_id", "equip_id", "quantity"),
    "wear": ("equip_id",),
    "reroll": ("equip_id",),
    "decode": ("stock_id", "quantity"),
    "decode_all": (),
    "rename": ("nickname",),
    "exchange_list": ("scope", "target_user_id", "mode", "asset_type", "stock_id", "equip_id", "piece", "quantity", "price_gold", "want_item_id"),
    "exchange_buy": ("listing_id", "offer_stock_id", "offer_equip_id", "offer_piece", "pay_mode"),
    "exchange_cancel": ("listing_id",),
    "exchange_reject": ("listing_id",),
}
# A revision must still match the transaction's current state. Web navigation has no
# QQ chat-flood penalty, whereas summons and commercial actions keep their cooldowns.
QUICK_COMMANDS = frozenset({"down", "rush", "fight", "flee", "extract", "event", "leave", "save_box", "swap_box", "take_box", "use", "field_use", "field_pick", "field_remove"})
_ID = re.compile(r"[a-z][a-z0-9_]{0,47}")
_RUSH_EVENT_COMMANDS = {"pause": "遇奇遇暂停", "random": "随机奇遇", "ignore": "无视奇遇"}


def check_command(name: str, params: dict) -> None:
    if not isinstance(name, str) or name not in _PARAMETERS:
        raise ValueError("该网页操作不可用。")
    if not isinstance(params, dict) or params.keys() - set(_PARAMETERS[name]):
        raise ValueError("操作参数无效。")


def _count(params: dict, key: str, limit: int, default: int | None = None) -> int:
    count = params.get(key, default)
    if type(count) is not int or not 1 <= count <= limit:
        raise ValueError("操作数量或物品位置无效。")
    return count


def _key(params: dict, name: str = "id") -> str:
    key = params.get(name)
    if not isinstance(key, str) or not _ID.fullmatch(key):
        raise ValueError("物品或地图编号无效。")
    return key


def _nickname(value) -> str:
    if not isinstance(value, str):
        raise ValueError("昵称须为 1 至 16 字。")
    name = " ".join(value.replace("[CQ:", "［CQ:").split())
    if not 1 <= len(name) <= 16 or name.startswith("__keep_"):
        raise ValueError("昵称须为 1 至 16 字。")
    return name


def _stack(turn, params):
    stock_id = _count(params, "stock_id", 2**53 - 1)
    return next((row for row in turn._stock() if row["stock_id"] == stock_id), None)


def _gear(turn, params):
    equip_id = _count(params, "equip_id", 2**53 - 1)
    return next((row.item for row in turn._equipment_options() if row.item["equip_id"] == equip_id), None)


def _run_item(turn, params, error, *, storage="bag", key="index"):
    items = turn._check_run()[storage]
    index = _count(params, key, 2**53 - 1)
    if index > len(items):
        raise error("物品位置已变动，请重新选择。")
    return index, items[index - 1]


def _prepare_start(turn, name, params, error):
    turn._check_idle()
    key = _key(params, "map_id")
    if key not in turn.catalog.maps:
        raise error("地图已失效，请重新选择。")
    supplied = params.get("supplies", [])
    if not isinstance(supplied, list) or len(supplied) > 32:
        raise ValueError("携带补给列表无效。")
    carry = []
    keys = set()
    for row in supplied:
        if not isinstance(row, dict) or row.keys() != {"id", "quantity"}:
            raise ValueError("携带补给参数无效。")
        item = _key(row)
        quantity = _count(row, "quantity", 99)
        if item in keys or turn.catalog.items.get(item, {}).get("kind") != "supply":
            raise ValueError("携带补给编号重复或无效。")
        keys.add(item)
        carry.append(f"{item} {quantity}")
    if "nickname" in params or not turn.player["name_confirmed"]:
        nickname = _nickname(params.get("nickname", turn.group_nick[:16]))
        if not turn.player["name_confirmed"]:
            # Persist the profile together with the first successful expedition.
            # If the real engine rejects the start, its savepoint leaves both unchanged.
            turn.player.update(nickname=nickname, name_confirmed=1)
    argument=" | ".join([key, *carry])
    if name=="start":
        if "focus" in params:
            argument+=" ;追寻 "+_focus(params)
        elif not turn.heirloom_focus:
            raise error("首次下潜请明确选定两套不同的追寻祝福。")
    return name, argument


def _focus(params):
    pair=params.get("focus")
    if (not isinstance(pair,list) or len(pair)!=2 or not all(isinstance(k,str) and re.fullmatch("B0[1-8]",k) for k in pair)
            or pair[0]==pair[1]):
        raise ValueError("请选择两套不同的追寻祝福。")
    return " ".join(pair)


def prepare(turn, name: str, params: dict, error) -> tuple[str, str]:
    """Return a single canonical action understood by the existing gameplay rules.

    Only exact, validated IDs are translated. Browsers never submit chat syntax.
    """
    if name in _EMPTY_COMMANDS:
        return name, ""
    if name in ("start", "simulate"):
        return _prepare_start(turn, name, params, error)
    if name == "prophecy_pick":
        return name, f"{_count(params, 'floor', 2000)} {_key(params)}"
    if name == "prophecy_start":
        turn._check_idle()
        if "nickname" in params or not turn.player["name_confirmed"]:
            nickname = _nickname(params.get("nickname", turn.group_nick[:16]))
            if not turn.player["name_confirmed"]:
                turn.player.update(nickname=nickname, name_confirmed=1)
        return name, ""
    if name == "exchange_cancel" or name == "exchange_reject":
        turn._check_idle()
        return name, str(_count(params, "listing_id", 2**53 - 1))
    if name == "exchange_list":
        turn._check_idle()
        scope = params.get("scope", "public")
        mode = params.get("mode", "gold")
        asset_type = params.get("asset_type", "")
        if scope not in ("public", "direct") or mode not in ("gold", "barter", "both") or asset_type not in ("inventory", "equipment", "shining"):
            raise ValueError("交易所挂单参数无效。")
        target_uid = params.get("target_user_id", "")
        if not isinstance(target_uid, str) or (scope == "direct" and not re.fullmatch(r"[0-9]{1,20}", target_uid.strip())):
            raise ValueError("1 对 1 交易请填写有效的对方 QQ 号。")
        payload = {
            "scope": scope, "target_user_id": target_uid.strip() if scope == "direct" else "",
            "mode": mode, "asset_type": asset_type,
        }
        if asset_type == "inventory":
            payload["stock_id"] = _count(params, "stock_id", 2**53 - 1)
            payload["quantity"] = _count(params, "quantity", 9999, 1)
        elif asset_type == "equipment":
            payload["equip_id"] = _count(params, "equip_id", 2**53 - 1)
            payload["quantity"] = 1
        else:
            piece = params.get("piece")
            if not isinstance(piece, str) or not re.fullmatch(r"B0[1-8]-[1-4]", piece):
                raise ValueError("无效的闪耀祝福编号。")
            payload["piece"] = piece
            payload["quantity"] = 1
        if mode in ("gold", "both"):
            payload["price_gold"] = _count(params, "price_gold", 100_000_000)
        else:
            payload["price_gold"] = 0
        if mode in ("barter", "both"):
            want = params.get("want_item_id", "")
            if not isinstance(want, str) or len(want) > 64:
                raise ValueError("目标物品编号无效。")
            payload["want_item_id"] = want.strip()
        else:
            payload["want_item_id"] = ""
        return name, "json:" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if name == "exchange_buy":
        turn._check_idle()
        payload = {"listing_id": _count(params, "listing_id", 2**53 - 1)}
        if params.get("offer_stock_id") is not None:
            payload["offer_stock_id"] = _count(params, "offer_stock_id", 2**53 - 1)
        if params.get("offer_equip_id") is not None:
            payload["offer_equip_id"] = _count(params, "offer_equip_id", 2**53 - 1)
        if params.get("offer_piece") is not None:
            piece = params["offer_piece"]
            if not isinstance(piece, str) or not re.fullmatch(r"B0[1-8]-[1-4]", piece):
                raise ValueError("无效的闪耀祝福编号。")
            payload["offer_piece"] = piece
        if params.get("pay_mode") is not None:
            pm = params["pay_mode"]
            if pm not in ("gold", "barter"):
                raise ValueError("无效的支付方式。")
            payload["pay_mode"] = pm
        return name, "json:" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if name=="pursue":
        return name,_focus(params)
    if name=="equip_blessing":
        slot=_count(params,"slot",4)
        piece=params.get("piece")
        if piece is not None and (not isinstance(piece,str) or not re.fullmatch("B0[1-8]-[1-4]",piece)):
            raise ValueError("无效的祝福编号。")
        if "piece" not in params: raise ValueError("缺少祝福编号。")
        return name,f"{slot} {piece or '空'}"
    if name=="wish_confirm":
        token=params.get("token")
        if not isinstance(token,str) or not re.fullmatch("[0-9a-f]{16}",token): raise ValueError("请重新预览清单。")
        return name,token
    if name == "rush":
        return name, str(_count(params, "floors", 10, turn.rush_floors))
    if name == "rush_settings":
        settings = []
        if "floors" in params:
            floors = _count(params, "floors", 10)
            if floors not in (3, 5, 10):
                raise ValueError("请选择 3、5 或 10 层。")
            settings.append(str(floors))
        if "skip_valuable" in params:
            skip = params["skip_valuable"]
            if type(skip) is not bool:
                raise ValueError("请指定是否跳过金红暂停。")
            settings.append("跳过金红" if skip else "遇金红暂停")
        if "event_mode" in params:
            mode = params["event_mode"]
            if not isinstance(mode, str) or mode not in _RUSH_EVENT_COMMANDS:
                raise ValueError("请选择遇到奇遇时停下、随机选择或无视。")
            settings.append(_RUSH_EVENT_COMMANDS[mode])
        if "auto_elite" in params:
            auto_elite = params["auto_elite"]
            if type(auto_elite) is not bool:
                raise ValueError("请指定是否自动打精英怪。")
            settings.append("自动打精英" if auto_elite else "遇精英暂停")
        return name, " ".join(settings)
    if name == "event":
        return name, str(_count(params, "option", 100))
    if name == "rename":
        return name, _nickname(params.get("nickname"))
    if name == "support":
        owner = params.get("owner_id")
        if not isinstance(owner, str) or not re.fullmatch(r"[0-9]{1,20}", owner):
            raise ValueError("求援对象无效。")
        return name, owner
    if name in ("field_use", "field_remove", "field_pick"):
        key = _key(params)
        turn._check_run()
        if key not in turn.catalog.field_items:
            raise error("局内道具已失效，请重新选择。")
        if name == "field_remove":
            position = f" {_count(params, 'slot', 2)}" if "slot" in params else ""
            return "field", f"解除 {key}{position}"
        if name == "field_pick":
            floor = _count(params, "floor", 999_999_999_999)
            slot = params.get("slot")
            if type(slot) is not int or not 0 <= slot <= 2:
                raise ValueError("请选择放弃或有效临装槽位。")
            return name, f"{floor}:{key}:{slot}"
        if "slot" in params:
            return name, f"{key} {_count(params, 'slot', 2)}"
        return name, key
    if name == "take_box":
        safe_index, _ = _run_item(turn, params, error, storage="safe", key="safe_index")
        return name, str(safe_index)
    if name in ("save_box", "swap_box", "use"):
        run = turn._check_run()
        if name != "use" and not params:
            if name == "save_box" and len(run["safe"]) >= turn._capacity():
                raise error("安全箱已满，请先选择要替换的格子。")
            return name, ""
        index, item = _run_item(turn, params, error)
        if name == "use":
            if turn.catalog.definition(item["id"]).get("kind") != "supply":
                raise error("请选择背包里的可用补给。")
            return name, str(index)
        if not turn._can_protect(item):
            raise error("这件物品无法放进安全箱。")
        if name == "save_box":
            if len(run["safe"]) >= turn._capacity():
                raise error("安全箱已满，请先选择要替换的格子。")
            return name, str(index)
        safe_index, _ = _run_item(turn, params, error, storage="safe", key="safe_index")
        return name, f"{index} | {safe_index}"
    turn._check_idle()
    if name in ("wear", "reroll") or name == "sell" and "equip_id" in params:
        if name == "sell" and len(params) != 1:
            raise ValueError("只能出售一种装备或一组同类物品。")
        item = _gear(turn, params)
        if item is None:
            raise error("这件装备已不在仓库中。")
        if name == "reroll" and item["rarity"] == "green":
            raise error("蓝色及以上装备可以重铸。")
        ref = f"#{item['equip_id']}"
        return name, f"确认 装备 {ref}" if name == "sell" else ref
    if name == "decode_all" or name == "decode" and not params:
        return "decode", "全部"
    if name in ("sell", "decode"):
        item = _stack(turn, params)
        if item is None:
            raise error("该物品已不在仓库中。")
        count = _count(params, "quantity", 9999, 1)
        if count > item["quantity"]:
            raise error("仓库中的物品数量不足。")
        if name == "decode" and turn.catalog.definition(item["id"]).get("kind") != "crate":
            raise error("请选择加密盲盒。")
        argument = f"#{item['stock_id']} {count}"
        return name, f"确认 {argument}" if name == "sell" else argument
    key = _key(params)
    count = _count(params, "quantity", 20 if name == "craft" else 99, 1)
    if name == "buy":
        if key not in {item.item["id"] for item in turn._shop_options()}:
            raise error("这件商品已不在商店中。")
    elif key not in {recipe["output"] for recipe in turn.catalog.weekly_recipes(turn.week)}:
        raise error("这条兑换已失效，请重新选择。")
    return name, f"{key} {count}"
