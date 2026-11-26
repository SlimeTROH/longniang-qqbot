"""Player-to-player and public exchange for Endless Elevator.

Supports:
- Public listings (`scope='public'`) and 1-to-1 directed trades (`scope='direct'`)
- Gold sales (`mode='gold'`) with high per-category/per-rarity minimum price floors
- Same-(category, rarity) item-for-item barter (`mode='barter'`), optionally targeting a specific item
- Duplicate-only rainbow trading: players must always keep at least 1 copy of each rainbow item
"""
from __future__ import annotations

import json
import sqlite3

from . import heirlooms
from .storage import encode

PUBLIC_TTL = 72 * 3600
DIRECT_TTL = 24 * 3600
MAX_OPEN_LISTINGS = 12
MAX_GOLD_PRICE = 100_000_000

CATEGORY_LABELS = {
    "material": "材料",
    "supply": "补给",
    "crate": "盲盒",
    "collection": "藏品",
    "equipment": "装备",
    "shining": "闪耀祝福",
    "rainbow": "虹彩",
}

RARITY_LABELS = {
    "white": "白色",
    "green": "绿色",
    "blue": "蓝色",
    "purple": "紫色",
    "gold": "大金",
    "red": "传世红",
    "shining": "闪耀",
    "rainbow": "虹彩",
}

# High minimum unit gold floors to discourage alt-dumping and preserve rarity value.
_EQUIPMENT_MIN = {
    "green": 2_000,
    "blue": 6_000,
    "purple": 25_000,
    "gold": 80_000,
    "red": 350_000,
}
_ORDINARY_MIN = {
    "white": 500,
    "green": 1_200,
    "blue": 3_500,
    "purple": 10_000,
    "gold": 30_000,
}
_COLLECTION_MIN = {
    "gold": (25_000, 1.8),
    "red": (120_000, 2.0),
}
SHINING_MIN_GOLD = 200_000
RAINBOW_MIN_GOLD = 1_500_000


class ExchangeError(ValueError):
    """Domain validation error surfaced as a GameError by Turn."""


def min_gold_price(category: str, rarity: str, unit_value: int = 0, quantity: int = 1) -> int:
    """Return the minimum total gold price required for a listing."""
    qty = max(1, int(quantity))
    val = max(0, int(unit_value))
    if category == "rainbow":
        unit = RAINBOW_MIN_GOLD
    elif category == "shining":
        unit = SHINING_MIN_GOLD
    elif category == "equipment":
        unit = _EQUIPMENT_MIN.get(rarity, 25_000)
    elif category == "collection":
        floor_val, mult = _COLLECTION_MIN.get(rarity, (25_000, 1.8))
        unit = max(floor_val, round(val * mult))
    else:
        floor_val = _ORDINARY_MIN.get(rarity, 3_500)
        unit = max(floor_val, val * 2)
    return unit * qty


def classify_inventory_item(catalog, item_id: str) -> tuple[str, str]:
    if item_id in catalog.rainbow:
        return "rainbow", "rainbow"
    spec = catalog.items.get(item_id)
    if spec is None or spec.get("temporary_key"):
        raise ExchangeError("该物品不支持在交易所上架。")
    return spec["kind"], spec["rarity"]


def classify_want_target(catalog, asset_type: str, category: str, rarity: str, want_id: str) -> tuple[str, str]:
    """Validate an optional specific barter target and return (canonical_id, display_name)."""
    if not want_id:
        return "", f"任意{RARITY_LABELS.get(rarity, rarity)}{CATEGORY_LABELS.get(category, category)}"
    cleaned = want_id.strip()
    if asset_type == "shining":
        key = heirlooms.piece_key(cleaned)
        if not key or key not in heirlooms.PIECES:
            raise ExchangeError("指定的目标闪耀祝福不存在。")
        return key, heirlooms.PIECES[key]["name"]
    if asset_type == "equipment":
        key = _resolve_equipment_base(catalog, cleaned)
        if not key:
            raise ExchangeError("指定的目标装备底材不存在。")
        is_red_base = bool(catalog.equipment[key].get("gear_set"))
        if (rarity == "red") != is_red_base:
            raise ExchangeError("同品质分类以物换物：红装只能换传世红装，常规装备只能换同品质常规装备。")
        return key, f"{RARITY_LABELS.get(rarity, rarity)}·{catalog.equipment[key]['name']}"
    # Inventory items (material, supply, crate, collection, rainbow)
    key = _resolve_item_key(catalog, cleaned)
    if not key:
        raise ExchangeError("指定的目标物品不存在。")
    target_cat, target_rarity = classify_inventory_item(catalog, key)
    if (target_cat, target_rarity) != (category, rarity):
        raise ExchangeError(
            f"以物换物必须同品质同分类：当前上架为「{RARITY_LABELS.get(rarity, rarity)}{CATEGORY_LABELS.get(category, category)}」，"
            f"不能指定换取「{RARITY_LABELS.get(target_rarity, target_rarity)}{CATEGORY_LABELS.get(target_cat, target_cat)}」。"
        )
    return key, catalog.items[key]["name"]


def _resolve_item_key(catalog, query: str) -> str:
    q = query.strip().casefold()
    if not q:
        return ""
    if query in catalog.items:
        return query
    for key, spec in catalog.items.items():
        if q == key.casefold() or q == spec["name"].casefold() or q in {a.casefold() for a in spec.get("aliases", ())}:
            return key
    return ""


def _resolve_equipment_base(catalog, query: str) -> str:
    q = query.strip().casefold()
    if not q:
        return ""
    if query in catalog.equipment:
        return query
    for key, spec in catalog.equipment.items():
        if q == key.casefold() or q == spec["name"].casefold():
            return key
    return ""


def _rainbow_total_quantity(db: sqlite3.Connection, uid: str, item_id: str) -> int:
    row = db.execute(
        "SELECT coalesce(sum(quantity), 0) FROM inventory WHERE user_id=? AND item_id=?",
        (uid, item_id),
    ).fetchone()
    return int(row[0]) if row else 0


def _deliver_asset(
    db: sqlite3.Connection, catalog, uid: str, asset_type: str, payload: dict, quantity: int, now: int, month: str,
    *, credit_collection: bool = True,
) -> None:
    if asset_type == "equipment":
        db.execute(
            "INSERT INTO equipment(user_id,base_id,rarity,stats,affixes,rerolls,rules_version) VALUES(?,?,?,?,?,?,?)",
            (
                uid, payload["base_id"], payload["rarity"], encode(payload["stats"]),
                encode(payload["affixes"]), int(payload.get("rerolls", 0)), int(payload.get("rules_version", 2)),
            ),
        )
        return
    if asset_type == "shining":
        piece = payload["piece"]
        db.execute(
            "INSERT INTO heirloom_inventory(user_id,piece,quantity) VALUES(?,?,?) "
            "ON CONFLICT(user_id,piece) DO UPDATE SET quantity=quantity+excluded.quantity",
            (uid, piece, quantity),
        )
        return
    item_id = payload["id"]
    quality = payload.get("quality", "")
    value = int(payload.get("value", 0))
    db.execute(
        "INSERT INTO inventory(user_id,item_id,quality,value,quantity) VALUES(?,?,?,?,?) "
        "ON CONFLICT(user_id,item_id,quality,value) DO UPDATE SET quantity=quantity+excluded.quantity",
        (uid, item_id, quality, value, quantity),
    )
    if credit_collection and item_id in catalog.collections:
        db.execute(
            "INSERT INTO collections VALUES(?,?,?,?) ON CONFLICT(user_id,item_id) DO UPDATE SET count=count+excluded.count",
            (uid, item_id, quantity, now),
        )
        db.execute("INSERT OR IGNORE INTO season_stats(month,user_id) VALUES(?,?)", (month, uid))
        db.execute(
            "UPDATE season_stats SET collection_count=(SELECT count(*) FROM collections WHERE user_id=?) "
            "WHERE month=? AND user_id=?",
            (uid, month, uid),
        )


def _notice(db: sqlite3.Connection, uid: str, body: str) -> None:
    db.execute("INSERT INTO notices(user_id,body) VALUES(?,?)", (uid, body))


def expire_listings(db: sqlite3.Connection, catalog, now: int, month: str) -> int:
    rows = db.execute(
        "SELECT * FROM exchange_listings WHERE status='open' AND expires_at<=? ORDER BY listing_id",
        (now,),
    ).fetchall()
    for row in rows:
        updated = db.execute(
            "UPDATE exchange_listings SET status='expired',resolved_at=? WHERE listing_id=? AND status='open'",
            (now, row["listing_id"]),
        ).rowcount
        if updated != 1:
            continue
        payload = json.loads(row["payload"])
        _deliver_asset(
            db, catalog, row["seller_id"], row["asset_type"], payload, row["quantity"], now, month,
            credit_collection=False,
        )
        _notice(db, row["seller_id"], f"交易所挂单 #{row['listing_id']}（{row['item_name']} ×{row['quantity']}）已过期退回仓库。")
    return len(rows)


def _escrow_inventory(
    db: sqlite3.Connection, catalog, uid: str, stock_id: int, quantity: int,
) -> tuple[str, str, str, str, int, dict]:
    row = db.execute(
        "SELECT * FROM inventory WHERE user_id=? AND stock_id=?", (uid, stock_id),
    ).fetchone()
    if row is None:
        raise ExchangeError("仓库中找不到这批物品。")
    if not 0 < quantity <= row["quantity"]:
        raise ExchangeError(f"这批物品当前只有 {row['quantity']} 件。")
    item_id = row["item_id"]
    category, rarity = classify_inventory_item(catalog, item_id)
    if category == "rainbow":
        if quantity != 1:
            raise ExchangeError("虹彩每次只能上架或交换 1 件重复副本。")
        total = _rainbow_total_quantity(db, uid, item_id)
        if total <= 1:
            raise ExchangeError("虹彩仅允许出售或交换重复获得的副本，仓库必须至少保留 1 件。")
    if quantity == row["quantity"]:
        db.execute("DELETE FROM inventory WHERE user_id=? AND stock_id=?", (uid, stock_id))
    else:
        db.execute("UPDATE inventory SET quantity=quantity-? WHERE user_id=? AND stock_id=?", (quantity, uid, stock_id))
    loot = {"id": item_id, "quality": row["quality"], "value": row["value"]}
    display = catalog.name(loot, symbol=False)
    return category, rarity, item_id, display, int(row["value"]), loot


def _escrow_equipment(
    db: sqlite3.Connection, catalog, uid: str, loadout: dict, equip_id: int,
) -> tuple[str, str, str, str, int, dict]:
    row = db.execute(
        "SELECT * FROM equipment WHERE user_id=? AND equip_id=?", (uid, equip_id),
    ).fetchone()
    if row is None:
        raise ExchangeError("装备库中找不到这件装备。")
    if equip_id in set(loadout.values()):
        raise ExchangeError("该装备正在穿戴中，请先换下后再挂单或交易。")
    stats = json.loads(row["stats"])
    affixes = json.loads(row["affixes"])
    base_id, rarity = row["base_id"], row["rarity"]
    spec = catalog.equipment[base_id]
    db.execute("DELETE FROM equipment WHERE user_id=? AND equip_id=?", (uid, equip_id))
    payload = {
        "base_id": base_id, "rarity": rarity, "stats": stats,
        "affixes": affixes, "rerolls": row["rerolls"], "rules_version": row["rules_version"],
    }
    display = f"{RARITY_LABELS.get(rarity, rarity)}·{spec['name']}"
    return "equipment", rarity, base_id, display, int(catalog.gear_sell.get(rarity, 260)), payload


def _escrow_shining(
    db: sqlite3.Connection, uid: str, slots: list[str | None], piece: str,
) -> tuple[str, str, str, str, int, dict]:
    key = heirlooms.piece_key(piece)
    if not key or key not in heirlooms.PIECES:
        raise ExchangeError("找不到这件闪耀祝福。")
    row = db.execute(
        "SELECT quantity FROM heirloom_inventory WHERE user_id=? AND piece=?", (uid, key),
    ).fetchone()
    qty = int(row["quantity"]) if row else 0
    if qty < 1:
        raise ExchangeError("祝福库中没有这件闪耀祝福。")
    if key in slots and qty <= 1:
        raise ExchangeError("该闪耀祝福正在祝福槽中装配，请先卸下或仅交易多余副本。")
    if qty == 1:
        db.execute("DELETE FROM heirloom_inventory WHERE user_id=? AND piece=?", (uid, key))
    else:
        db.execute("UPDATE heirloom_inventory SET quantity=quantity-1 WHERE user_id=? AND piece=?", (uid, key))
    info = heirlooms.PIECES[key]
    display = f"{heirlooms.SETS[info['set']]['name']}·{info['name']}"
    return "shining", "shining", key, display, 0, {"piece": key}


def create_listing(
    db: sqlite3.Connection, catalog, *,
    seller_id: str, seller_name: str, loadout: dict, slots: list[str | None],
    scope: str, target_user_id: str, mode: str,
    asset_type: str, stock_id: int | None = None, equip_id: int | None = None, piece: str = "",
    quantity: int = 1, price_gold: int = 0, want_item_id: str = "", now: int, month: str,
) -> dict:
    expire_listings(db, catalog, now, month)
    if scope not in ("public", "direct"):
        raise ExchangeError("上架方式仅支持公开（public）或 1 对 1（direct）。")
    if mode not in ("gold", "barter", "both"):
        raise ExchangeError("交易模式仅支持金币（gold）、以物换物（barter）或两者兼可（both）。")
    target = target_user_id.strip() if scope == "direct" else ""
    if scope == "direct":
        if not target or not target.isascii() or not target.isdigit():
            raise ExchangeError("1 对 1 交易请指定对方有效的 QQ 号。")
        if target == seller_id:
            raise ExchangeError("不能向自己发起 1 对 1 交易。")
        exists = db.execute("SELECT nickname FROM players WHERE user_id=?", (target,)).fetchone()
        if exists is None:
            raise ExchangeError(f"找不到乘客 {target} 的电梯档案，对方需先进入过无尽电梯。")
    open_count = db.execute(
        "SELECT count(*) FROM exchange_listings WHERE seller_id=? AND status='open'", (seller_id,),
    ).fetchone()[0]
    if open_count >= MAX_OPEN_LISTINGS:
        raise ExchangeError(f"每人最多同时持有 {MAX_OPEN_LISTINGS} 笔进行中的交易所挂单。")

    if asset_type == "inventory":
        if stock_id is None:
            raise ExchangeError("请选择要上架的仓库物品。")
        category, rarity, item_id, item_name, unit_value, payload = _escrow_inventory(
            db, catalog, seller_id, stock_id, quantity,
        )
    elif asset_type == "equipment":
        if equip_id is None:
            raise ExchangeError("请选择要上架的装备。")
        quantity = 1
        category, rarity, item_id, item_name, unit_value, payload = _escrow_equipment(
            db, catalog, seller_id, loadout, equip_id,
        )
    elif asset_type == "shining":
        quantity = 1
        category, rarity, item_id, item_name, unit_value, payload = _escrow_shining(
            db, seller_id, slots, piece,
        )
    else:
        raise ExchangeError("不支持的交易所物品类型。")

    want_id, want_name = "", ""
    if mode in ("gold", "both"):
        minimum = min_gold_price(category, rarity, unit_value, quantity)
        if type(price_gold) is not int or price_gold < minimum or price_gold > MAX_GOLD_PRICE:
            raise ExchangeError(
                f"「{item_name}」×{quantity} 的金币底价为 {minimum:,} 金（上限 {MAX_GOLD_PRICE:,} 金），请提高标价。"
            )
    else:
        price_gold = 0
    if mode in ("barter", "both"):
        want_id, want_name = classify_want_target(catalog, asset_type, category, rarity, want_item_id)
        if want_id and category in ("rainbow", "shining") and want_id == item_id:
            raise ExchangeError("不能指定用同款虹彩或闪耀换取自身。")

    ttl = DIRECT_TTL if scope == "direct" else PUBLIC_TTL
    cursor = db.execute(
        """INSERT INTO exchange_listings(
            seller_id,seller_name,scope,target_user_id,mode,asset_type,category,rarity,
            item_id,item_name,quantity,unit_value,payload,price_gold,want_item_id,want_item_name,
            status,created_at,expires_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
        (
            seller_id, seller_name, scope, target, mode, asset_type, category, rarity,
            item_id, item_name, quantity, unit_value, encode(payload), price_gold, want_id, want_name,
            now, now + ttl,
        ),
    )
    listing_id = int(cursor.lastrowid)
    if scope == "direct":
        if mode == "both":
            terms = f"{price_gold:,} 金 或 换 {want_name}"
        elif mode == "gold":
            terms = f"{price_gold:,} 金"
        else:
            terms = f"换 {want_name}"
        _notice(db, target, f"{seller_name} 向你发起 1 对 1 交易 #{listing_id}：{item_name} ×{quantity}（{terms}）。")
    return dict(db.execute("SELECT * FROM exchange_listings WHERE listing_id=?", (listing_id,)).fetchone())


def fulfill_listing(
    db: sqlite3.Connection, catalog, *,
    listing_id: int, buyer_id: str, buyer_name: str, buyer_gold: int,
    buyer_loadout: dict, buyer_slots: list[str | None],
    offer_stock_id: int | None = None, offer_equip_id: int | None = None, offer_piece: str = "",
    pay_mode: str = "", now: int, month: str,
) -> tuple[dict, int]:
    """Execute purchase or barter. Returns (listing_dict, gold_spent)."""
    expire_listings(db, catalog, now, month)
    row = db.execute("SELECT * FROM exchange_listings WHERE listing_id=?", (listing_id,)).fetchone()
    if row is None or row["status"] != "open":
        raise ExchangeError(f"挂单 #{listing_id} 不存在或已结束。")
    if row["seller_id"] == buyer_id:
        raise ExchangeError("不能购买或交换自己的挂单。")
    if row["scope"] == "direct" and row["target_user_id"] != buyer_id:
        raise ExchangeError("这是一笔 1 对 1 定向交易，仅指定对象可以成交。")

    has_offer = (offer_stock_id is not None) or (offer_equip_id is not None) or bool(offer_piece)
    if row["mode"] == "gold":
        effective_mode = "gold"
    elif row["mode"] == "barter":
        effective_mode = "barter"
    else:
        if pay_mode in ("gold", "barter"):
            effective_mode = pay_mode
        else:
            effective_mode = "barter" if has_offer else "gold"
    gold_spent = 0
    if effective_mode == "gold":
        price = int(row["price_gold"])
        if buyer_gold < price:
            raise ExchangeError(f"金币不足，需要 {price:,} 金（当前 {buyer_gold:,} 金）。")
        db.execute("UPDATE players SET gold=gold+? WHERE user_id=?", (price, row["seller_id"]))
        gold_spent = price
        buyer_offer_text = f"{price:,} 金"
    else:
        asset_type, category, rarity, qty = row["asset_type"], row["category"], row["rarity"], int(row["quantity"])
        if asset_type == "inventory":
            if offer_stock_id is None:
                raise ExchangeError("以物换物请选择你用于交换的仓库物品。")
            o_cat, o_rarity, o_id, o_name, _, o_payload = _escrow_inventory(
                db, catalog, buyer_id, offer_stock_id, qty,
            )
        elif asset_type == "equipment":
            if offer_equip_id is None:
                raise ExchangeError("以物换物请选择你用于交换的闲置装备。")
            o_cat, o_rarity, o_id, o_name, _, o_payload = _escrow_equipment(
                db, catalog, buyer_id, buyer_loadout, offer_equip_id,
            )
        else:
            if not offer_piece:
                raise ExchangeError("以物换物请选择你用于交换的闪耀祝福。")
            o_cat, o_rarity, o_id, o_name, _, o_payload = _escrow_shining(
                db, buyer_id, buyer_slots, offer_piece,
            )
        if (o_cat, o_rarity) != (category, rarity):
            raise ExchangeError(
                f"品质或分类不匹配：对方要求「{RARITY_LABELS.get(rarity, rarity)}{CATEGORY_LABELS.get(category, category)}」，"
                f"你提供的是「{RARITY_LABELS.get(o_rarity, o_rarity)}{CATEGORY_LABELS.get(o_cat, o_cat)}」。"
            )
        if row["want_item_id"] and o_id != row["want_item_id"]:
            raise ExchangeError(f"对方指定换取「{row['want_item_name']}」，与你提供的物品不一致。")
        if category in ("rainbow", "shining") and o_id == row["item_id"]:
            raise ExchangeError("不能用同一款虹彩或闪耀祝福互相交换。")
        _deliver_asset(db, catalog, row["seller_id"], asset_type, o_payload, qty, now, month)
        buyer_offer_text = f"{o_name} ×{qty}"

    updated = db.execute(
        "UPDATE exchange_listings SET status='completed',buyer_id=?,resolved_at=? WHERE listing_id=? AND status='open'",
        (buyer_id, now, listing_id),
    ).rowcount
    if updated != 1:
        raise ExchangeError("挂单状态已变动，请刷新重试。")
    _deliver_asset(db, catalog, buyer_id, row["asset_type"], json.loads(row["payload"]), int(row["quantity"]), now, month)
    _notice(
        db, row["seller_id"],
        f"交易所成交 #{listing_id}：{buyer_name} 以 {buyer_offer_text} 换走了你的 {row['item_name']} ×{row['quantity']}。",
    )
    return dict(row), gold_spent


def cancel_listing(db: sqlite3.Connection, catalog, *, listing_id: int, seller_id: str, now: int, month: str) -> dict:
    expire_listings(db, catalog, now, month)
    row = db.execute("SELECT * FROM exchange_listings WHERE listing_id=?", (listing_id,)).fetchone()
    if row is None or row["status"] != "open":
        raise ExchangeError(f"挂单 #{listing_id} 不存在或已结束。")
    if row["seller_id"] != seller_id:
        raise ExchangeError("只能撤销自己发布的挂单。")
    db.execute(
        "UPDATE exchange_listings SET status='cancelled',resolved_at=? WHERE listing_id=? AND status='open'",
        (now, listing_id),
    )
    _deliver_asset(
        db, catalog, seller_id, row["asset_type"], json.loads(row["payload"]), int(row["quantity"]), now, month,
        credit_collection=False,
    )
    return dict(row)


def reject_listing(db: sqlite3.Connection, catalog, *, listing_id: int, user_id: str, user_name: str, now: int, month: str) -> dict:
    expire_listings(db, catalog, now, month)
    row = db.execute("SELECT * FROM exchange_listings WHERE listing_id=?", (listing_id,)).fetchone()
    if row is None or row["status"] != "open":
        raise ExchangeError(f"挂单 #{listing_id} 不存在或已结束。")
    if row["scope"] != "direct" or row["target_user_id"] != user_id:
        raise ExchangeError("只有 1 对 1 交易的指定接收方可以拒绝。")
    db.execute(
        "UPDATE exchange_listings SET status='rejected',resolved_at=? WHERE listing_id=? AND status='open'",
        (now, listing_id),
    )
    _deliver_asset(
        db, catalog, row["seller_id"], row["asset_type"], json.loads(row["payload"]), int(row["quantity"]), now, month,
        credit_collection=False,
    )
    _notice(db, row["seller_id"], f"{user_name} 拒绝了你的 1 对 1 交易 #{listing_id}（{row['item_name']}），物品已退回。")
    return dict(row)


def visible_listings(db: sqlite3.Connection, catalog, uid: str, now: int, month: str) -> list[dict]:
    rows = db.execute(
        """SELECT * FROM exchange_listings
           WHERE status='open' AND expires_at>? AND (scope='public' OR seller_id=? OR target_user_id=?)
           ORDER BY CASE WHEN scope='direct' AND target_user_id=? THEN 0 WHEN seller_id=? THEN 1 ELSE 2 END,
                    listing_id DESC LIMIT 120""",
        (now, uid, uid, uid, uid),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        payload = json.loads(item["payload"])
        detail = {
            "listing_id": item["listing_id"],
            "seller_id": item["seller_id"],
            "seller_name": item["seller_name"],
            "scope": item["scope"],
            "target_user_id": item["target_user_id"],
            "mode": item["mode"],
            "asset_type": item["asset_type"],
            "category": item["category"],
            "category_label": CATEGORY_LABELS.get(item["category"], item["category"]),
            "rarity": item["rarity"],
            "rarity_label": RARITY_LABELS.get(item["rarity"], item["rarity"]),
            "item_id": item["item_id"],
            "item_name": item["item_name"],
            "quantity": item["quantity"],
            "unit_value": item["unit_value"],
            "price_gold": item["price_gold"],
            "want_item_id": item["want_item_id"],
            "want_item_name": item["want_item_name"],
            "created_at": item["created_at"],
            "expires_at": item["expires_at"],
            "is_mine": item["seller_id"] == uid,
            "is_for_me": item["scope"] == "direct" and item["target_user_id"] == uid,
        }
        if item["asset_type"] == "equipment":
            detail["stats"] = payload.get("stats", {})
            detail["affixes"] = [
                {"id": k, "name": catalog.affixes[k]["name"], "description": catalog.affixes[k]["description"]}
                for k in payload.get("affixes", ()) if k in catalog.affixes
            ]
        result.append(detail)
    return result
