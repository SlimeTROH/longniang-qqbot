"""Game rules and transactional commands for Endless Elevator."""
from __future__ import annotations

import copy
import difflib
import json
import math
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from random import Random

from . import combat, exchange, expedition, heirlooms, prophecy
from .catalog import Catalog, RARITIES, SLOTS, STAT_NAMES
from .storage import GameMaintenance, Store, encode

SHANGHAI = timezone(timedelta(hours=8))
RUN_TIMEOUT = 30 * 60
RELIC_LIFETIME = 24 * 60 * 60
SIGNAL_LIFETIME = 3 * 60
ADRENALINE_ATK_CAP = .50
EVENT_ATK_CAP = .60
BOSS_GRENADE_CAP = 2
TIP_SAFEBOX, TIP_COMBAT, TIP_EXTRACT, TIP_LOBBY, TIP_RELIC = (1 << bit for bit in range(5))

BOARD = {
    "深度": ("max_floor", "深渊开拓王", "s.max_floor DESC, s.floor_value DESC"),
    "收藏": ("collection_count", "大金收藏家", "s.collection_count DESC"),
    "老六": ("rob_value", "头号舔包老六", "s.rob_value DESC"),
    "雷锋": ("rescues", "感动全服活雷锋", "s.rescues DESC"),
    "倒霉": ("death_value", "悲情倒霉蛋", "s.death_value DESC"),
}
BOARD_ALIASES = {"大金": "收藏", "捞尸": "雷锋", "翻车": "倒霉", "舔包": "老六"}
BOARD_UNITS = {"收藏": "件", "老六": "金", "雷锋": "次", "倒霉": "金"}
MAP_SHORT = {"mall": "商场", "subway": "地铁", "abyss": "实验室", "auction": "拍卖行", "tide":"动力站", "zenith":"天文台"}
ITEM_BRIEFS = {
    "coffee": "回 40% 最大生命（局内发「用药」）",
    "ice_tea": "回 25% 血，本层+下层敏 +15（15% 打嗝 -8 血）",
    "smoke": "无伤脱战，也可直接破撤离伏击",
    "flare": "额外发强效信号弹，最多喊 3 位群友补刀",
    "adrenaline": "本局攻击 +25%（最多叠 2 层），代价扣 10% 当前血",
    "grenade": "削普通怪或精英 30% 当前生命。首领每场最多中两枚，每枚削当前生命 12%，最低留 1 点。",
    "lockpick": "下一个摸金宝箱保底品质升 1 档",
    "radar": "提前偷看下一层内容（箱/怪/奇遇/遗物）",
    "shield": "展开 50 点临时护盾，优先替你吸收伤害",
    "warp_card": "非战斗刷卡直降 2 层，跳过风险并侦察下层",
    "magnet": "本局后续新摸物资估值 +25%，并立刻白摸 1 箱",
    "pouch": "本局安全箱临时 +1 格",
    "doll": "遭遇致命伤害时自动消耗，带回物资，按战败结算",
    "vip_card": "刷卡直通下一层无怪隐藏金库，连开 3 个高爆箱",
    "reroll_stone": "重铸装备词条时优先消耗，可替代 1 件蓝色材料",
    "crate_blue": "带回大厅发「解码」开奖，出大金/装备/替身娃娃",
    "crate_purple": "带回大厅发「解码」开奖，更高概率出大金/高阶装",
    "crate_gold": "带回大厅发「解码」开奖，有机会直出传世红",
    "ticket": (f"下潜入场券，每日补足到 {expedition.TICKET_DAILY_REFILL} 张，"
               f"每日限买 {expedition.TICKET_DAILY_PURCHASE_LIMIT} 张，"
               f"未用购买票最多持有 {expedition.TICKET_PAID_CAP} 张（先用免费票）"),
    "safebox": "永久多锁 1 格好货，翻车绝不掉落",
}
SHOP_VIRTUAL_LOOKUP = {
    "ticket": ("通行证", ("ticket", "通行证", "车票", "票"), ITEM_BRIEFS["ticket"] + "。集齐「脑洞深渊」票夹容量 +1。"),
    "gear_box": ("新手装备箱", ("gear_box", "新手装备箱", "绿箱", "初级箱", "装备盲盒", "装备箱"), ""),
    "gear_box_mid": ("进阶装备箱", ("gear_box_mid", "进阶装备箱", "蓝箱", "中级箱", "精良箱"), ""),
    "gear_box_high": ("尊享装备箱", ("gear_box_high", "尊享装备箱", "尊享箱", "尊享金装匣", "金箱", "金装匣", "高级箱", "紫箱"), ""),
    "safebox": ("安全箱扩容", ("safebox", "安全箱", "扩容", "扩容安全箱"), "永久扩大安全箱格数（初始 1 格，2 格 6,000 金 / 3 格 30,000 金 / 4 格 88,000 金）。锁进安全箱的货即使翻车也绝对带回仓库。"),
}
QUEST_POOL = (
    {"id": "floors", "title": "深入禁区累计 12 层", "target": 12, "gold": 650},
    {"id": "kills", "title": "放倒 3 只禁区怪物", "target": 3, "item": "crate_blue", "qty": 1},
    {"id": "extracts", "title": "带着物资安全撤离 2 次", "target": 2, "gold": 900},
    {"id": "events", "title": "触发并处理 3 次奇遇", "target": 3, "item": "coffee", "qty": 2},
    {"id": "chests", "title": "摸开 5 个禁区宝箱", "target": 5, "gold": 750},
    {"id": "trade", "title": "仓库卖货或黑市兑换 1 次", "target": 1, "item": "ice_tea", "qty": 2},
)
HIGH_VALUE_SELL_THRESHOLD = 1500
PROTECTED_SUPPLIES = frozenset({"doll", "vip_card", "magnet", "pouch", "grenade", "radar", "lockpick", "shield", "warp_card"})


COOLDOWNS = {
    "start": 3, "simulate": 2, "down": 2, "rush": 2, "fight": 2, "flee": 2, "extract": 2,
    "save_box": 1, "swap_box": 1, "battle_log": 1,
    "flare": 10, "support": 3, "rob": 2, "rescue": 2, "use": 2, "buy": 2, "shop": 2,
    "craft": 2, "decode": 2, "warehouse": 2, "sell": 2, "equipment": 2, "reroll": 2,
    "gallery": 5, "rank": 10, "all_rank": 10, "help": 5, "menu": 5,
    "exchange": 2, "exchange_list": 2, "exchange_buy": 2, "exchange_cancel": 1, "exchange_reject": 1,
    "prophecy": 2, "prophecy_start": 3, "prophecy_rank": 5,
}

COMMANDS = {
    "menu": "lobby", "help": "help", "lookup": "lookup",
    "start": "start", "simulate": "simulate", "down": "down", "rush": "rush", "rush_settings": "rush_settings",
    "fight": "fight", "flee": "flee", "extract": "extract", "battle_log": "battle_log",
    "save_box": "save_box", "swap_box": "swap_box", "take_box": "take_box", "bag": "bag", "use": "use",
    "event": "event_choice", "rob": "rob", "rescue": "rescue", "leave": "leave",
    "flare": "flare", "support": "support",
    "shop": "shop", "buy": "buy", "warehouse": "warehouse", "sell": "sell",
    "curate_collectibles": "curate_collectibles",
    "equipment": "equipment", "wear": "wear", "autoequip": "autoequip", "reroll": "reroll",
    "black_market": "black_market", "craft": "craft", "decode": "decode", "blessing": "blessing",
    "gallery": "gallery", "rank": "rank", "all_rank": "all_rank", "quests": "quests", "rename": "rename",
    "confirm_order": "confirm_order", "cancel_order": "cancel_order", "orders": "list_orders",
    "heirlooms": "heirloom_menu", "pursue": "pursue", "equip_blessing":"equip_blessing",
    "wish_confirm":"wish_confirm", "wish_cancel":"wish_cancel",
    "field": "field_bag", "field_use": "use_field", "field_pick": "pick_field",
    "field_guide": "field_guide", "atlas": "atlas",
    "exchange": "exchange_board", "exchange_list": "exchange_list", "exchange_buy": "exchange_buy",
    "exchange_cancel": "exchange_cancel", "exchange_reject": "exchange_reject",
    "prophecy": "prophecy_menu", "prophecy_start": "prophecy_start", "prophecy_rank": "prophecy_rank",
    "prophecy_pick": "prophecy_pick",
}

OPEN_AI_COMMANDS = frozenset(k for k in COMMANDS if k not in {
    "confirm_order", "cancel_order", "wish_confirm",
    "exchange_list", "exchange_buy", "exchange_cancel", "exchange_reject",
})
_GROUP_ONLY_ACTIONS = frozenset({"flare", "support", "blessing"})
FIELD_CHOICE_FIRST = frozenset({"down", "rush", "fight", "flee", "extract", "event", "rob", "rescue", "leave", "flare"})
ORDER_TTL = 600
ORDER_STEP_LIMIT = 60
_ORDER_COMPACT_AFTER = 15
_ORDER_RECEIPT_PREVIEW = 4
_ORDER_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


@dataclass(frozen=True)
class Choice:
    label: str
    action: str
    argument: str = ""


@dataclass(frozen=True)
class ItemChoice:
    reference: str
    label: str
    names: tuple[str, ...]
    item: dict


@dataclass(frozen=True)
class _ItemPage:
    number: int
    total: int
    rows: slice

    @classmethod
    def select(cls, text: str, item_count: int, page_size: int = 5) -> _ItemPage:
        total = max(1, (item_count + page_size - 1) // page_size)
        requested = int(text) if text.isascii() and text.isdigit() else 1
        number = min(max(1, requested), total)
        start = (number - 1) * page_size
        return cls(number, total, slice(start, start + page_size))

    def heading(self, title: str) -> str:
        if self.total == 1:
            return title
        return f"{title}\n第 {self.number}/{self.total} 页"

    def navigation(self, action: str, prefix: str = "", *, next_label: str = "下一页") -> tuple[Choice, ...]:
        choices = []
        if self.number > 1:
            choices.append(Choice("上一页", action, f"{prefix}{self.number - 1}"))
        if self.number < self.total:
            choices.append(Choice(next_label, action, f"{prefix}{self.number + 1}"))
        return tuple(choices)


@dataclass
class Reply:
    text: str
    choices: tuple[Choice, ...] = ()
    plain_numbers: bool = False
    mention: str = ""
    card: str = ""
    card_lines: tuple[str, ...] = ()
    notice_ids: tuple[int, ...] = ()
    count_cooldown: bool = True
    multiline_menu: bool = False
    as_image: bool = False
    image_caption: str = ""
    forward_title: str = ""

    def message(self) -> str:
        if not self.choices:
            return self.text
        menu = [f"{index}.{choice.label}" for index, choice in enumerate(self.choices, 1)]
        if self.multiline_menu:
            return self.text + "\n" + "\n".join(menu) + "\n👉 直接回序号即可（引用也行）"
        return self.text + "\n👉 " + " ｜ ".join(menu)


class GameError(ValueError):
    pass


def _clock(now: float) -> datetime:
    return datetime.fromtimestamp(now, tz=SHANGHAI)


def _quantity(text: str) -> tuple[str, int]:
    cleaned = text.strip()
    if cleaned.startswith("#") and cleaned[1:].isdigit():
        return cleaned, 1
    for sep_char in ("*", "×", "x", "X", " "):
        if sep_char in cleaned:
            name, _, candidate = cleaned.rpartition(sep_char)
            name, candidate = name.strip(), candidate.strip()
            if name and candidate.isascii() and candidate.isdigit():
                return name, int(candidate)
    return cleaned, 1


def _nickname(text: str) -> str:
    return " ".join(text.replace("[CQ:", "［CQ:").split())[:26] or "群友"


def _player_stock(db: sqlite3.Connection, user_id: str) -> list[dict]:
    return [
        {"id": row["item_id"], "quality": row["quality"], "value": row["value"],
         "quantity": row["quantity"], "stock_id": row["stock_id"]}
        for row in db.execute("SELECT * FROM inventory WHERE user_id=? ORDER BY stock_id", (user_id,))
    ]


class ElevatorGame:
    def __init__(self, path: Path, *, catalog: Catalog | None = None, rng: Random | None = None):
        self.catalog = catalog or Catalog()
        self.store = Store(path, self.catalog)
        self.rng = rng or Random()
        self._battle_logs: dict[str, str] = {}

    def save_battle_log(self, user_id: str, text: str) -> None:
        self._battle_logs[user_id] = text
        if len(self._battle_logs) > 200:
            self._battle_logs.pop(next(iter(self._battle_logs)))

    def get_battle_log(self, user_id: str) -> str:
        return self._battle_logs.get(user_id, "")

    def _collection_cleanup(self, db: sqlite3.Connection, user_id: str) -> tuple[dict, list[tuple[dict, int]], str]:
        groups: dict[tuple[str, str], list[dict]] = {}
        for stock in _player_stock(db, user_id):
            item = self.catalog.items.get(stock["id"])
            if (not item or item["kind"] != "collection" or item["rarity"] not in {"gold", "red"}
                    or stock["id"] in self.catalog.rainbow):
                continue
            name = item["name"]
            groups.setdefault((item["rarity"], name), []).append(stock)

        collections = []
        sales = []
        snapshot = []
        for rarity, name in sorted(groups, key=lambda group: (group[0] != "red", group[1])):
            stocks = groups[rarity, name]
            keep = min(stocks, key=lambda stock: (-stock["value"], stock["stock_id"]))
            group_sales = [(stock, stock["quantity"] - (stock is keep)) for stock in stocks]
            sales.extend((stock, count) for stock, count in group_sales if count > 0)
            collections.append({"name": name, "rarity": rarity, "keep_value": keep["value"],
                                "sale_count": sum(count for _, count in group_sales),
                                "gold": sum(stock["value"] * count for stock, count in group_sales)})
            snapshot.append([rarity, name, stocks, keep["stock_id"]])

        plan = {"collections": collections, "sale_count": sum(row["sale_count"] for row in collections),
                "gold": sum(row["gold"] for row in collections)}
        fingerprint = sha256(encode([user_id, snapshot, plan]).encode("utf-8")).hexdigest()
        return plan, sales, fingerprint

    def plan_collection_cleanup(
        self, user_id: str, nickname: str, group_id: str, *, create_order: bool = False, now: float | None = None,
    ) -> dict:
        """Quote a warehouse cleanup, optionally issuing one order for the quoted inventory."""
        with self.store.read() as db:
            if create_order and db.execute("SELECT 1 FROM active_runs WHERE user_id=?", (user_id,)).fetchone():
                raise GameError("请先「撤离」回大厅，再生成藏品清理派单。")
            plan, _, fingerprint = self._collection_cleanup(db, user_id)
        if create_order and plan["sale_count"]:
            order = self.create_order(
                user_id, nickname, group_id, "同名红、金藏品各保留最高单价的一件",
                [{"action": "curate_collectibles", "argument": fingerprint,
                  "note": f"出售 {plan['sale_count']:,} 件重复藏品，预计 {plan['gold']:,} 金"}],
                now=now,
            )
            plan["order_id"] = order["order_id"]
        return plan

    def create_order(
        self,
        user_id: str,
        nickname: str,
        group_id: str,
        summary: str,
        steps: list[dict],
        *,
        now: float | None = None,
    ) -> dict:
        if not steps or len(steps) > ORDER_STEP_LIMIT:
            raise GameError(f"派单须包含 1 至 {ORDER_STEP_LIMIT} 步。")
        normalized: list[dict] = []
        for idx, step in enumerate(steps, 1):
            if not isinstance(step, dict):
                raise GameError(f"第 {idx} 条操作格式错误。")
            act = str(step.get("action", "")).strip()
            arg = str(step.get("argument", "")).strip()
            note = str(step.get("note", "")).strip() or f"{act} {arg}".strip()
            if act not in OPEN_AI_COMMANDS:
                raise GameError(f"第 {idx} 条操作使用了不支持的接口「{act}」。")
            normalized.append({"action": act, "argument": arg, "note": note[:120]})
        at = int(time.time() if now is None else now)
        clean_summary = " ".join(str(summary or "无尽电梯代操作派单").split())[:100]
        with self.store.transaction() as db:
            turn = Turn(self, db, user_id, _nickname(nickname), group_id, at)
            if any(step["action"] == "curate_collectibles" for step in normalized):
                turn._check_idle()
            turn.persist(touch=False)
            order_id = ""
            for _ in range(32):
                cand = "E" + "".join(self.rng.choice(_ORDER_ALPHABET) for _ in range(4))
                if db.execute("SELECT 1 FROM ai_orders WHERE order_id=?", (cand,)).fetchone() is None:
                    order_id = cand
                    break
            if not order_id:
                raise GameError("生成唯一派单号失败，请重试。")
            db.execute(
                "INSERT INTO ai_orders(order_id,user_id,group_id,summary,steps_json,status,created_at,expires_at) "
                "VALUES(?,?,?,?,?,'pending',?,?)",
                (order_id, user_id, group_id, clean_summary, encode(normalized), at, at + ORDER_TTL),
            )
        return {
            "order_id": order_id,
            "summary": clean_summary,
            "steps": normalized,
            "expires_in": ORDER_TTL,
        }

    def has_run(self, user_id: str) -> bool:
        with self.store.read() as db:
            return db.execute("SELECT 1 FROM active_runs WHERE user_id=?", (user_id,)).fetchone() is not None

    def run_kind(self, user_id: str) -> str:
        with self.store.read() as db:
            row = db.execute("SELECT payload FROM active_runs WHERE user_id=?", (user_id,)).fetchone()
            return json.loads(row["payload"])["event"]["kind"] if row else ""

    def has_signal(self, group_id: str, user_id: str, now: float | None = None) -> bool:
        at = int(time.time() if now is None else now)
        with self.store.read() as db:
            return db.execute(
                "SELECT 1 FROM signals WHERE group_id=? AND owner_id<>? AND expires_at>?",
                (group_id, user_id, at),
            ).fetchone() is not None

    def execute(
        self, user_id: str, nickname: str, group_id: str, action: str,
        argument: str = "", *, target: str = "", now: float | None = None,
    ) -> Reply:
        at = int(time.time() if now is None else now)
        with self.store.transaction() as db:
            _settle_months(db, _clock(at).strftime("%Y-%m"))
            turn = Turn(self, db, user_id, _nickname(nickname), group_id, at)
            turn.expire()
            turn, reply, _ = self.apply_turn(turn, action, argument, target)
            return turn.with_notices(reply)

    def apply_turn(
        self, turn: Turn, action: str, argument: str = "", target: str = "", *,
        touch: bool = True, cooldown: bool = True,
    ) -> tuple[Turn, Reply, str | None]:
        """Apply one action within the caller's game transaction, rolling back invalid moves."""
        turn.limit_notices.clear()
        if action not in COMMANDS:
            return turn, turn.reply("发「电梯」看看现在能做什么。", count_cooldown=False), "invalid"
        db, user_id, at = turn.db, turn.uid, turn.at
        cooldown_key = action
        if action in {"warehouse", "equipment", "gallery", "rank", "all_rank", "help"}:
            cooldown_key += ":" + argument.strip().casefold()
        interval = COOLDOWNS.get(action, 0) if cooldown else 0
        if interval:
            previous = db.execute(
                "SELECT used_at FROM command_cooldowns WHERE user_id=? AND command=?", (user_id, cooldown_key),
            ).fetchone()
            if previous and at - previous["used_at"] < interval:
                return turn, Reply(""), "cooldown"
        db.execute("SAVEPOINT elevator_action")
        try:
            reply = turn.execute_action(action, argument, target)
            if turn.limit_notices:
                reply.text = "\n".join([reply.text, *(f"⚠️ {notice}" for notice in turn.limit_notices)])
            turn.persist(touch=touch)
            if reply.count_cooldown and interval:
                db.execute(
                    "INSERT INTO command_cooldowns VALUES(?,?,?) ON CONFLICT(user_id,command) "
                    "DO UPDATE SET used_at=excluded.used_at", (user_id, cooldown_key, at),
                )
            return turn, reply, None
        except GameError as error:
            db.execute("ROLLBACK TO elevator_action")
            turn = Turn(self, db, user_id, turn.group_nick, turn.gid, at)
            return turn, turn.reply(str(error), count_cooldown=False), "invalid"
        finally:
            db.execute("RELEASE elevator_action")

    def sweep(self, now: float | None = None) -> None:
        at = int(time.time() if now is None else now)
        month = _clock(at).strftime("%Y-%m")
        with self.store.transaction() as db:
            _settle_months(db, month)
            exchange.expire_listings(db, self.catalog, at, month)
            prophecy.settle_completed_weeks(db, prophecy.week_key(at), self.catalog, at)
            stale = db.execute(
                "SELECT a.user_id,a.group_id,p.nickname FROM active_runs a "
                "JOIN players p USING(user_id) WHERE a.updated_at <= ?", (at - RUN_TIMEOUT,),
            ).fetchall()
            for row in stale:
                Turn(self, db, row["user_id"], row["nickname"], row["group_id"], at).expire()
            db.execute("DELETE FROM relics WHERE expires_at <= ? OR claimed=1", (at,))
            db.execute("DELETE FROM signals WHERE expires_at <= ?", (at,))
            db.execute("DELETE FROM group_buffs WHERE expires_at <= ?", (at,))
            db.execute("DELETE FROM rescue_pairs WHERE day < ?", (_clock(at).date().isoformat(),))
            db.execute("DELETE FROM command_cooldowns WHERE used_at < ?", (at - 120,))
            db.execute("DELETE FROM ai_orders WHERE expires_at < ?", (at - 86400,))

    def acknowledge_notices(self, user_id: str, notice_ids: tuple[int, ...]) -> None:
        if notice_ids:
            with self.store.transaction() as db:
                db.executemany("DELETE FROM notices WHERE user_id=? AND notice_id=?",
                               [(user_id, notice_id) for notice_id in notice_ids])

    def prewarm_texts(self, *, now: float | None = None) -> list[str]:
        """Build all static help/shop/black-market cards + active players' live black-market/lobby cards."""
        at = int(time.time() if now is None else now)
        texts: list[str] = []
        with self.store.read() as db:
            sample_row = db.execute("SELECT user_id, nickname FROM players LIMIT 1").fetchone()
            if sample_row is not None:
                turn = Turn(self, db, sample_row["user_id"], sample_row["nickname"], "", at)
                turn.run = None
                for topic in ("攻略", "地图", "概率", "道具", "道具 页2", "道具 页3", "词条", "词条 页2", "词条 页3", "装备", "装备 页2"):
                    try:
                        r = turn.help(topic, "")
                        if r.as_image:
                            texts.append(r.message())
                    except Exception:
                        pass
                try:
                    r = turn.lookup("", "")
                    if r.as_image:
                        texts.append(r.message())
                except Exception:
                    pass
                orig_sb = turn.player["safebox"]
                for sb in (1, 2, 3, 4):
                    turn.player["safebox"] = sb
                    try:
                        r = turn.shop("", "")
                        if r.as_image:
                            texts.append(r.message())
                    except Exception:
                        pass
                turn.player["safebox"] = orig_sb

            for row in db.execute("SELECT user_id, nickname FROM players ORDER BY runs DESC LIMIT 20"):
                try:
                    t = Turn(self, db, row["user_id"], row["nickname"], "", at)
                    if t.run is None:
                        for bm_arg in ("", "2"):
                            r = t.black_market(bm_arg, "")
                            if r.as_image:
                                texts.append(r.message())
                        r_lobby = t._lobby()
                        if r_lobby.as_image:
                            texts.append(r_lobby.message())
                except Exception:
                    continue
        return texts

    def prewarm_user_texts(self, user_id: str, *, now: float | None = None) -> list[str]:
        """Refresh a single player's dynamic black_market/lobby/quests cards after state updates."""
        at = int(time.time() if now is None else now)
        texts: list[str] = []
        with self.store.read() as db:
            row = db.execute("SELECT user_id, nickname FROM players WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                return texts
            t = Turn(self, db, row["user_id"], row["nickname"], "", at)
            if t.run is None:
                for fn, arg in (
                    (t.black_market, ""), (t.black_market, "2"),
                    (t.shop, ""), (t._lobby, None), (t.quests, ""),
                ):
                    try:
                        r = fn() if arg is None else fn(arg, "")
                        if r.as_image:
                            texts.append(r.message())
                    except Exception:
                        pass
        return texts


def _settle_months(db: sqlite3.Connection, month: str) -> None:
    months = db.execute(
        "SELECT DISTINCT month FROM season_stats WHERE month < ? AND month NOT IN (SELECT month FROM settled_months)",
        (month,),
    ).fetchall()
    for row in months:
        previous = row["month"]
        for category, (metric, title, order) in BOARD.items():
            winner = db.execute(
                f"SELECT s.user_id FROM season_stats s WHERE s.month=? AND s.{metric}>0 "
                f"ORDER BY {order}, s.user_id LIMIT 1", (previous,),
            ).fetchone()
            if winner:
                db.execute("INSERT OR IGNORE INTO season_titles VALUES(?,?,?,?)",
                           (previous, category, winner["user_id"], title))
                db.execute("INSERT INTO notices(user_id,body) VALUES(?,?)",
                           (winner["user_id"], f"{previous} 赛季称号「{title}」已收入陈列室。"))
        db.execute("INSERT INTO settled_months VALUES(?)", (previous,))


class Turn:
    def __init__(self, game: ElevatorGame, db: sqlite3.Connection, uid: str, name: str, gid: str, now: int):
        self.db, self.uid, self.gid, self.at = db, uid, gid, now
        self.game, self.catalog, self.rng = game, game.catalog, game.rng
        self.limit_notices: list[str] = []
        clock = _clock(now)
        self.day, self.month = clock.date().isoformat(), clock.strftime("%Y-%m")
        self.week = f"{clock.isocalendar().year}-{clock.isocalendar().week}"
        self.prophecy_week = prophecy.week_key(now)
        self.group_nick = name
        db.execute(
            "INSERT OR IGNORE INTO players(user_id,nickname) VALUES(?,?)",
            (uid, name),
        )
        self.player = dict(db.execute("SELECT * FROM players WHERE user_id=?", (uid,)).fetchone())
        self.heirloom_focus, self.heirloom_slots, self.heirloom_owned = heirlooms.load(db,uid)
        pref = db.execute("SELECT floors,skip_valuable,event_mode,auto_elite FROM rush_preferences WHERE user_id=?", (uid,)).fetchone()
        self.rush_floors, self.rush_skip_valuable = (pref[0], bool(pref[1])) if pref else (3, False)
        self.rush_event_mode, self.rush_auto_elite = (pref[2], bool(pref[3])) if pref else ("pause", False)
        self.archives = {
            row["item_id"]: row["count"] for row in db.execute("SELECT item_id,count FROM collections WHERE user_id=?", (uid,))
        }
        members = {name: {key for key, definition in self.catalog.collections.items() if definition["set"] == name}
                   for name in self.catalog.sets}
        self.completed_sets = [name for name, keys in members.items() if keys and keys <= self.archives.keys()]
        self.ticket_cap = expedition.ticket_capacity(self.completed_sets, self.catalog.sets)
        tickets, bought, paid = expedition.ticket_status(self.player, self.day, self.ticket_cap)
        if self.player["ticket_day"] != self.day:
            self.player["support_rewards"] = 0
        self.player.update(ticket_day=self.day, tickets=tickets, bought_tickets=bought,
                           daily_ticket_refill=expedition.TICKET_DAILY_REFILL, paid_tickets=paid)
        self._quest_notes: list[str] = []
        row = db.execute("SELECT * FROM active_runs WHERE user_id=?", (uid,)).fetchone()
        self.run = json.loads(row["payload"]) if row else None
        if self.run and not self.run.get("prophecy"):
            repaired=expedition.repair_legacy_ring_shield(self.run)
            if not repaired and self.run.get("archive_version",0)<expedition.ARCHIVE_VERSION:
                expedition.migrate_linear_ring(self.run)
        if self.run is not None and not self.run.get("prophecy") and self.run.get("rules_version",1)<2:
            old_max = max(1,self.run["stats"]["hp"])
            self.run["stats"],self.run["perks"] = self._stats()
            self.run["hp"] = min(self.run["stats"]["hp"],max(1,round(self.run["hp"]*self.run["stats"]["hp"]/old_max)))
            for item in self.run["bag"]+self.run["safe"]:
                self.catalog.migrate_loose_gear(item)
            self._init_field()
        if self.run and not self.run.get("prophecy") and self.run.get("archive_version",0) < expedition.ARCHIVE_VERSION:
            old_max = max(1, self.run["stats"]["hp"])
            self.run["stats"], self.run["perks"] = self._stats()
            if self.run["hp"] > 0:
                self.run["hp"] = max(1, min(self.run["stats"]["hp"], round(self.run["hp"]*self.run["stats"]["hp"]/old_max)))
            self.run["archive_version"] = expedition.ARCHIVE_VERSION
        if self.run and self.run.get("prophecy") and self.run.get("prophecy_week") != self.prophecy_week:
            self.finish_run(True, "本周星兆已轮换，上期试炼自动结算")
        elif self.run and self.run.get("prophecy") and "star_ranks" not in self.run:
            prophecy.migrate_stars(self.run, self.catalog)
        self.last_action = row["updated_at"] if row else now
        self.last_group = row["group_id"] if row else gid
        db.execute("INSERT OR IGNORE INTO season_stats(month,user_id) VALUES(?,?)", (self.month, uid))
        db.execute("INSERT OR IGNORE INTO group_active_users VALUES(?,?,?)", (gid, self.month, uid))

    def persist(self, *, touch: bool = True) -> None:
        keys = ("nickname", "name_confirmed", "gold", "tickets", "ticket_day", "bought_tickets", "daily_ticket_refill", "paid_tickets",
                "support_rewards", "safebox", "runs", "extracts", "loadout", "tutorial_flags")
        self.db.execute(
            "UPDATE players SET " + ", ".join(f"{key}=?" for key in keys) + " WHERE user_id=?",
            tuple(self.player[key] for key in keys) + (self.uid,),
        )
        self.db.execute("UPDATE season_stats SET collection_count=? WHERE month=? AND user_id=?",
                        (len(self.archives), self.month, self.uid))
        if self.run is None:
            self.db.execute("DELETE FROM active_runs WHERE user_id=?", (self.uid,))
        else:
            self.db.execute(
                "INSERT INTO active_runs(user_id,group_id,updated_at,payload) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET group_id=excluded.group_id, updated_at=excluded.updated_at,payload=excluded.payload",
                (self.uid, self.gid if touch else self.last_group, self.at if touch else self.last_action, encode(self.run)),
            )

    def expire(self) -> None:
        if self.run is not None and self.at - self.last_action >= RUN_TIMEOUT:
            is_trial = bool(self.run.get("sim") or self.run.get("prophecy"))
            text = self.finish_run(True, "久等没动静，电梯自动把你拉回了地面")
            if not is_trial:
                self.notice(self.uid, "\n".join(text.splitlines()[:2]))
        self.persist(touch=False)

    def notice(self, user_id: str, body: str) -> None:
        self.db.execute("INSERT INTO notices(user_id,body) VALUES(?,?)", (user_id, body))

    def with_notices(self, reply: Reply) -> Reply:
        rows = self.db.execute(
            "SELECT notice_id,body FROM notices WHERE user_id=? ORDER BY notice_id LIMIT 2", (self.uid,),
        ).fetchall()
        if rows and reply.text:
            reply.text = "；".join("📮 " + row["body"] for row in rows) + "\n" + reply.text
            reply.notice_ids = tuple(row["notice_id"] for row in rows)
        return reply

    def _load_quests(self) -> list[dict]:
        row = self.db.execute(
            "SELECT payload FROM daily_quests WHERE day=? AND user_id=?", (self.day, self.uid),
        ).fetchone()
        if row is not None:
            return json.loads(row["payload"])
        picks = Random(f"{self.day}:{self.uid}").sample(QUEST_POOL, 3)
        quests = [{**q, "progress": 0, "done": False} for q in picks]
        self.db.execute(
            "INSERT INTO daily_quests(day,user_id,payload) VALUES(?,?,?)",
            (self.day, self.uid, encode(quests)),
        )
        return quests

    def _advance_quest(self, kind: str, amount: int = 1) -> None:
        if self.run and (self.run.get("sim") or self.run.get("prophecy")):
            return
        quests = self._load_quests()
        changed = False
        for q in quests:
            if q["id"] == kind and not q["done"]:
                q["progress"] = min(q["target"], q["progress"] + amount)
                changed = True
                if q["progress"] >= q["target"]:
                    q["done"] = True
                    if q.get("gold"):
                        self.player["gold"] += q["gold"]
                        reward_txt = f"{q['gold']:,} 金"
                    else:
                        item_key = q["item"]
                        qty = q.get("qty", 1)
                        self.deposit(self.catalog.ordinary_item(item_key, self.rng), quantity=qty)
                        reward_txt = f"{self.catalog.items[item_key]['name']} ×{qty}"
                    self._quest_notes.append(f"🎯 每日悬赏完成【{q['title']}】→ 自动入账 {reward_txt}！")
        if changed:
            self.db.execute(
                "UPDATE daily_quests SET payload=? WHERE day=? AND user_id=?",
                (encode(quests), self.day, self.uid),
            )

    def quests(self, _text: str, _target: str) -> Reply:
        quests = self._load_quests()
        done_count = sum(1 for q in quests if q["done"])
        lines = [f"🎯 今日悬赏委托（{done_count}/{len(quests)}｜游玩时自动累计并即时发奖）"]
        for q in quests:
            mark = "✅" if q["done"] else "⬜"
            reward = f"{q['gold']:,} 金" if q.get("gold") else f"{self.catalog.items[q['item']]['name']} ×{q.get('qty', 1)}"
            lines.append(f"{mark} {q['title']}（{q['progress']}/{q['target']}） · 奖励：{reward}")
        return self.reply(
            "\n".join(lines),
            (Choice("下潜", "start"), Choice("大厅", "menu"), Choice("商店", "shop"), Choice("黑市", "black_market")),
            plain=False, count_cooldown=False, as_image=True, forward_title="每日悬赏",
        )

    def reply(
        self, text: str, options: tuple[Choice, ...] | None = None, *,
        plain: bool | None = None, **kwargs,
    ) -> Reply:
        if self._quest_notes and text:
            text = text + "\n" + "\n".join(self._quest_notes)
            self._quest_notes.clear()
        if (
            self.run is not None
            and self.run["event"]["kind"] == "ready"
            and not self.run.get("field_offer")
            and text
            and options is None
            and not kwargs.get("as_image")
            and not kwargs.get("card")
        ):
            summary = self._next_floor_summary()
            if summary and summary not in text:
                text = text + "\n" + summary
        if options is None:
            options = self.actions()
            if self.run and prophecy.star_offer(self.run):
                options = tuple(
                    Choice(f"嵌入{row['name']}（{row['label']} +{row['gain']}）", "prophecy_pick",
                           f"{self.run['floor']} {row['id']}")
                    for row in prophecy.star_rows(self.run) if row["offered"]
                ) + options
        if self.gid.startswith("private:"):
            options = tuple(choice for choice in options if choice.action not in _GROUP_ONLY_ACTIONS)
        return Reply(text, options, bool(self.run) if plain is None else plain, **kwargs)

    def execute_action(self, action: str, argument: str, target: str, *, confirmed_order: bool = False) -> Reply:
        if self.gid.startswith("private:"):
            if action in _GROUP_ONLY_ACTIONS:
                raise GameError("求援、支援和金雨祈福请在已开放电梯的群里使用。")
            if action == "rank":
                action = "all_rank"
        if action == "curate_collectibles" and not confirmed_order:
            raise GameError("请先生成藏品清理派单，再由本人确认执行。")
        if action in FIELD_CHOICE_FIRST and self.run and self.run.get("field_offer"):
            raise GameError(self._field_offer_text())
        return getattr(self, COMMANDS[action])(argument, target)

    def note_limit(self, text: str) -> None:
        if text not in self.limit_notices:
            self.limit_notices.append(text)

    def _tip(self, flag: int, message: str) -> str:
        if self.player["runs"] > 2 or self.player["tutorial_flags"] & flag:
            return ""
        self.player["tutorial_flags"] |= flag
        return "\n💡 " + message

    def _veteran_bonus(self) -> dict[str, int]:
        lv = min(25, int(self.player["extracts"]))
        return {"hp": lv * 4, "atk": lv, "def": lv // 2}

    def _worn_rows(self):
        gear=[]
        for slot, equipment_id in json.loads(self.player["loadout"]).items():
            item=self.db.execute("SELECT * FROM equipment WHERE user_id=? AND equip_id=?",(self.uid,equipment_id)).fetchone()
            if item is not None and self.catalog.equipment[item["base_id"]]["slot"]==slot:
                gear.append(item)
        return gear

    def _stats(self, *, sources=None) -> tuple[dict, dict]:
        return self._gear_panel(self._worn_rows(), sources=sources)

    def _gear_panel(self, gear, *, sources=None) -> tuple[dict,dict]:
        stats, perks = dict(self.catalog.base), {}
        if sources is not None:
            sources.update({k:[{"source":"角色基础","op":"base","value":v,"result":v}] for k,v in stats.items()})
        expedition.add_stats(stats,self._veteran_bonus(),sources,f"资深探员 · {min(25,self.player['extracts'])} 次撤离")
        for effect in self.completed_sets:
            expedition.add_stats(stats,self.catalog.sets[effect].get("stats",{}),sources,"收藏套装 · "+effect)
        for item in gear:
            label = ""
            if sources is not None:
                label = f"{self.catalog.equipment[item['base_id']]['name']} #{item['equip_id']}"
            expedition.add_stats(stats,json.loads(item["stats"]),sources,label)
            for perk in json.loads(item["affixes"]):
                power = self.catalog.affixes[perk]["power"]
                perks[perk] = max(power, perks.get(perk, 0))
        self.catalog.apply_gear_sets(stats, perks, (row["base_id"] for row in gear if "base_id" in row.keys()),sources=sources)
        heirlooms.apply(stats,perks,self.heirloom_slots,sources=sources)
        expedition.apply_archive(stats,perks,self.archives,self.catalog,sources=sources)
        return stats, perks

    def secondary_origins(self):
        origins={}
        if self.run and self.run.get("prophecy"):
            cfg = prophecy.weekly_config(self.run["prophecy_week"], self.catalog)
            affix_groups={}
            for item in cfg["gear"]:
                for k in item["affixes"]:
                    affix_groups.setdefault(k,[]).append(item["name"])
            for k,items in affix_groups.items():
                label=f"{self.catalog.affixes[k]['name']}（{'、'.join(items)}，同款取最高）"
                origins[k]=[(label,self.catalog.affixes[k]["power"])]
            for k,n in self.catalog.gear_set_counts(item["base_id"] for item in cfg["gear"]).items():
                suit=self.catalog.gear_sets[k]
                for threshold,effect in suit["bonuses"].items():
                    if n>=int(threshold):
                        for stat,value in effect.get("perks",{}).items():
                            origins.setdefault(stat,[]).append((f"{suit['name']} · {threshold}件",value))
            rainbow=self.catalog.rainbow
            effects=[(rainbow[k]["name"],rainbow[k]) for k in cfg["rainbows"] if k in rainbow]
            if len(effects)==len(rainbow):
                effects.append(("四彩共鸣",self.catalog.progression["resonance"]))
            for name,effect in effects:
                for stat,value in effect.get("perks",{}).items():
                    origins.setdefault(stat,[]).append((name,value))
        else:
            worn=self._worn_rows()
            affix_groups={}
            for item in worn:
                for k in json.loads(item["affixes"]):
                    affix_groups.setdefault(k,[]).append(self.catalog.equipment[item["base_id"]]["name"])
            for k,items in affix_groups.items():
                label=f"{self.catalog.affixes[k]['name']}（{'、'.join(items)}，同款取最高）"
                origins[k]=[(label,self.catalog.affixes[k]["power"])]
            for k,n in self.catalog.gear_set_counts(row["base_id"] for row in worn).items():
                suit=self.catalog.gear_sets[k]
                for threshold,effect in suit["bonuses"].items():
                    if n>=int(threshold):
                        for stat,value in effect.get("perks",{}).items():
                            origins.setdefault(stat,[]).append((f"{suit['name']} · {threshold}件",value))
            rainbow=self.catalog.rainbow
            effects=[(v["name"],v) for k,v in rainbow.items() if k in self.archives]
            if len(effects)==len(rainbow):
                effects.append(("四彩共鸣",self.catalog.progression["resonance"]))
            for name,effect in effects:
                for stat,value in effect.get("perks",{}).items():
                    origins.setdefault(stat,[]).append((name,value))
        if self.run:
            active=[k for k,end in self.run.get("field_buffs",{}).items() if end>=self.run["floor"]]
            active.extend(k for k in expedition.field_slots(self.run) if k)
            for k in active:
                for stat,value in self.catalog.field_items[k].get("mods",{}).items():
                    origins.setdefault("field:"+stat,[]).append((self.catalog.field_items[k]["name"],value))
        return origins

    def _init_field(self):
        self.run["rules_version"] = 2
        self.run["archive_version"] = expedition.ARCHIVE_VERSION
        self.run.setdefault("field_seed",f"{self.uid}:{self.at}:{self.player['runs']}:{self.run['map']}")
        self.run.setdefault("field_items",{})
        expedition.refresh(self.run,self.catalog.field_items)

    def _check_idle(self) -> None:
        if self.run:
            raise GameError("人还在电梯里呢，先「撤离」回大厅再折腾外面的东西。")

    def _check_run(self) -> dict:
        if not self.run:
            raise GameError(f"还没进电梯呢，手头还有 {self.player['tickets']} 张通行证，发「下潜」就能走。")
        return self.run

    def _charge(self, amount: int) -> None:
        if amount <= 0:
            return
        if self.run and (self.run.get("sim") or self.run.get("prophecy")):
            sim_gold = int(self.run.get("sim_gold", 0))
            if sim_gold < amount:
                label = "✦ 寓言试炼币" if self.run.get("prophecy") else "🧪 模拟测试币"
                raise GameError(f"{label}不足（需 {amount:,}，当前剩 {sim_gold:,}）。")
            self.run["sim_gold"] = sim_gold - amount
            return
        if self.player["gold"] < amount:
            raise GameError(f"差了点钱——需要 {amount:,} 金，钱包里只有 {self.player['gold']:,} 金。")
        self.player["gold"] -= amount

    def _map_options(self) -> list[tuple[str, dict]]:
        return [(key, info) for key, info in self.catalog.maps.items()
                if self.player["extracts"] >= info["extracts"] and self.player["gold"] >= info["entry"]]

    def _first_time_name_prompt(self) -> Reply:
        gname = self.group_nick
        return self.reply(
            "🛗 欢迎上车《无尽电梯：群友搜打撤》（全服跨群互通）！\n"
            f"🎁 新人起步带 500 金 + {expedition.TICKET_DAILY_REFILL} 张通行证，前 3 局自带防爆保险，翻车也不掉货。\n"
            f"🏷️ 你的全服游戏昵称将在所有群统一显示（排行、陈列室、支援、遗物盒通用）。\n"
            f"当前群昵称：「{gname}」\n"
            "要沿用当前群昵称，还是自定义一个全服昵称？\n"
            "• 想改名直接发：「改名 你的新昵称」（1~16 字，之后随时也能改）",
            (Choice(f"沿用「{gname}」直接出发", "rename", "__keep_start__"),
             Choice("沿用并先看大厅", "rename", "__keep_lobby__")),
            plain=True, count_cooldown=False,
        )

    def rename(self, text: str, _target: str) -> Reply:
        raw = text.strip()
        if raw == "__keep_start__":
            self.player["nickname"] = self.group_nick
            self.player["name_confirmed"] = 1
            return self.start("mall", "")
        if raw == "__keep_lobby__":
            self.player["nickname"] = self.group_nick
            self.player["name_confirmed"] = 1
            return self._lobby()
        if not raw:
            return self.reply(
                f"🏷️ 当前全服昵称：【{self.player['nickname']}】（全服跨群统一显示，不随换群改变）\n"
                f"• 修改昵称：发「改名 新昵称」（1~16 字）\n"
                f"• 同步本群昵称：可点下方沿用「{self.group_nick}」",
                (Choice(f"沿用本群「{self.group_nick}」", "rename", "同步群昵称"),
                 Choice("回大厅", "menu"), Choice("下潜", "start")),
                plain=False, count_cooldown=False,
            )
        if raw in ("同步群昵称", "沿用群昵称", "恢复群昵称", "同步"):
            new_name = self.group_nick
        else:
            cleaned = " ".join(raw.replace("[CQ:", "［CQ:").split())
            if not cleaned or len(cleaned) > 16:
                raise GameError("全服昵称长度需要 1~16 个字，请换一个再试。")
            new_name = cleaned
        first_setup = not self.player.get("name_confirmed")
        self.player["nickname"] = new_name
        self.player["name_confirmed"] = 1
        self.db.execute(
            "UPDATE relics SET owner_name=? WHERE owner_id=? AND claimed=0",
            (new_name, self.uid),
        )
        prefix = "🎉 全服昵称设置成功！" if first_setup else "🏷️ 全服昵称已修改！"
        return self.reply(
            f"{prefix}以后不管去哪个群，都会统一显示为【{new_name}】。（随时可发「改名 新昵称」修改）",
            (Choice("立刻下潜", "start"), Choice("回大厅", "menu"), Choice("陈列室", "gallery")),
            plain=False,
        )

    def _resolve_map_key(self, name: str) -> str:
        if not name:
            return ""
        if name == "推荐":
            accessible = self._map_options()
            return next(key for key, info in reversed(accessible) if not info["entry"])
        return next(
            (key for key, info in self.catalog.maps.items()
             if name == key or name == info["name"] or name == MAP_SHORT.get(key) or name in info["name"]),
            "",
        )

    def _warehouse_supplies(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        for item in self._stock():
            if self.catalog.items.get(item["id"], {}).get("kind") == "supply":
                counts[item["id"]] = counts.get(item["id"], 0) + item["quantity"]
        return list(counts.items())

    def _save_heirlooms(self):
        self.db.execute("""INSERT INTO heirloom_loadouts(user_id,focus,slots) VALUES(?,?,?)
                           ON CONFLICT(user_id) DO UPDATE SET focus=excluded.focus,slots=excluded.slots""",
                        (self.uid,encode(self.heirloom_focus),encode(self.heirloom_slots)))

    def _select_focus(self, tokens):
        pair = [heirlooms.set_key(name) for name in tokens]
        if len(pair)!=2 or None in pair or pair[0]==pair[1]:
            raise GameError("从 8 套中选两套不同的祝福，如「追寻 1 8」。")
        if self.run and (self.run.get("heirloom_focus") or self.run.get("sim")):
            raise GameError("本局追寻已经锁定，请在下次下潜前调整。")
        self.heirloom_focus=pair
        if self.run:
            self.run["heirloom_focus"]=pair.copy()
        self._save_heirlooms()

    def pursue(self,text: str,_target: str) -> Reply:
        tokens=text.replace("，"," ").replace(","," ").split()
        if tokens==["沿用上次"]:
            tokens=self.heirloom_focus
        if len(tokens)==2:
            self._select_focus(tokens)
            label="、".join(heirlooms.SETS[k]["name"] for k in self.heirloom_focus)
            return self.reply("本次追寻已选定："+label, (Choice("电梯大厅","menu"),) if self.run else
                              (Choice("选择地图","start"),Choice("祝福库","heirlooms")),plain=True,count_cooldown=False)
        if self.run and self.run.get("heirloom_focus"):
            return self.heirloom_menu("","")
        first=heirlooms.set_key(tokens[0]) if len(tokens)==1 else None
        if tokens and not first:
            raise GameError("发「追寻 1 8」或从菜单选两套不同祝福。")
        picks=tuple(Choice(f"{name}（{sum(bool(self.heirloom_owned.get(f'{code}-{i}')) for i in range(1,5))}/4）",
                           "pursue",f"{first} {code}" if first else code)
                    for code, name in ((k,v["name"]) for k,v in heirlooms.SETS.items()) if code!=first)
        return self.reply("请选择第二套追寻祝福。" if first else "正式下潜前先选两套不同的追寻祝福。也可直接发「追寻 1 8」。",
                          picks,plain=True,multiline_menu=True,count_cooldown=False)

    def heirloom_menu(self,text: str,_target: str) -> Reply:
        code=heirlooms.set_key(text)
        piece=heirlooms.piece_key(text)
        if piece: code=piece[:3]
        if text.strip() and not code: raise GameError("没有找到这个祝福编号或名称。")
        if code:
            spec=heirlooms.SETS[code]
            lines=[f"✨ {spec['name']} · {spec['short']}",spec["lore"],"两件　"+spec["two"],"四件　"+spec["four"]]
            choices=[]
            for key,item in heirlooms.PIECES.items():
                if item["set"]!=code or (piece and key!=piece): continue
                a,n=item["stats"]
                lines.extend([f"{key} {item['name']} · {STAT_NAMES[a]} +{n} · 持有 {self.heirloom_owned.get(key,0)}",item["lore"]])
                if self.heirloom_owned.get(key) and not self.run:
                    choices.append(Choice(f"装配 {item['name']}","equip_blessing",key))
            choices.append(Choice("祝福库","heirlooms"))
        else:
            focus=(self.run or {}).get("heirloom_focus",self.heirloom_focus)
            lines=["✨ 闻岑的闪耀祝福","追寻　"+("、".join(heirlooms.SETS[k]["name"] for k in focus) or "待选两套"),
                   "卡槽　"+" ｜ ".join(f"{i}. "+(heirlooms.PIECES[k]["name"] if k else "空") for i,k in enumerate(self.heirloom_slots,1))]
            choices=[Choice(s["name"],"heirlooms",k) for k,s in heirlooms.SETS.items()]
            if not self.run or not self.run.get("heirloom_focus"): choices.append(Choice("选追寻","pursue"))
            lines.append("看故事发「祝福 B06」，装卸发「祝福装配 1 B06-1」或「祝福装配 1 空」。")
        return self.reply("\n".join(lines),tuple(choices),plain=True,as_image=True,forward_title="闻岑的闪耀祝福",count_cooldown=False)

    def equip_blessing(self,text: str,_target: str) -> Reply:
        self._check_idle()
        words=text.split(maxsplit=1)
        if len(words)==1 and (key:=heirlooms.piece_key(words[0])) and self.heirloom_owned.get(key):
            return self.reply("选一个祝福槽位：",tuple(Choice(f"{i} · "+(heirlooms.PIECES[k]["name"] if k else "空"),
                              "equip_blessing",f"{i} {key}") for i,k in enumerate(self.heirloom_slots,1)),plain=True,count_cooldown=False)
        if len(words)!=2 or words[0] not in ("1","2","3","4"):
            raise GameError("用法：祝福装配 槽位 编号；卸下时编号写「空」。")
        slot=int(words[0])-1
        key=heirlooms.piece_key(words[1])
        if words[1]!="空" and (not key or not self.heirloom_owned.get(key)):
            raise GameError("祝福库中没有这件遗物。")
        if key and key in self.heirloom_slots and self.heirloom_slots[slot]!=key:
            raise GameError("同一件遗物只能装备一份，请先卸下原槽位。")
        self.heirloom_slots[slot]=key
        self._save_heirlooms()
        return self.reply(f"祝福槽 {slot+1}："+(heirlooms.PIECES[key]["name"] if key else "空"),count_cooldown=False)

    def start(self, text: str, _target: str) -> Reply:
        if not self.player.get("name_confirmed") and self.player["runs"] == 0 and self.run is None:
            return self._first_time_name_prompt()
        self._check_idle()
        text, sep, chosen = text.partition(" ;追寻 ")
        if sep: self._select_focus(chosen.split())
        if not self.heirloom_focus:
            return self.pursue("","")
        if not self.player["tickets"]:
            raise GameError("今天的通行证用完了！可以去「商店」加购、等明天零点刷新，或者发「模拟战」免费试玩。")
        normalized = text.replace("｜", "|").strip()
        tokens = [part.strip() for part in normalized.split("|") if part.strip()] if normalized else []
        name = tokens[0] if tokens else ""
        supplies = tokens[1:] if len(tokens) > 1 else []
        # Also allow space-separated "商场 冰美式 2 烟雾弹" or "冰美式 2" without "|"
        if name and not self._resolve_map_key(name):
            head, sep, tail = name.partition(" ")
            if sep and self._resolve_map_key(head):
                name = head
                supplies = [tail.strip(), *supplies]
            elif name in ("带药", "自动", "自动带药", "全带") or self._match_supply_key(name.split()[0].split("*")[0].split("x")[0].split("×")[0]):
                supplies = [name, *supplies]
                name = "推荐"

        accessible = self._map_options()
        wh_supplies = self._warehouse_supplies()
        if not name and (len(accessible) > 1 or wh_supplies):
            recommended = next(key for key, info in reversed(accessible) if not info["entry"])
            lines = [f"这趟准备下哪一站？手头剩 {self.player['tickets']} 张通行证。"]
            choices = [
                Choice(
                    info["name"] + ("（推荐）" if key == recommended and len(accessible) > 1 else f"（{info['entry']:,} 金）" if info["entry"] else ""),
                    "start", key,
                )
                for key, info in accessible
            ]
            if wh_supplies:
                preview = "、".join(f"{self.catalog.items[k]['name']}×{c}" for k, c in wh_supplies[:5])
                if len(wh_supplies) > 5:
                    preview += f" 等{len(wh_supplies)}种"
                lines.append(f"🎒 仓库可带道具：{preview}")
                lines.append("💡 直接回序号空手出发；回「序号 冰美式 2 烟雾弹」或「序号 带药」带药出发（引用也行）！")
                rec_name = self.catalog.maps[recommended]["name"]
                choices.append(Choice(f"{rec_name}（一键带药）", "start", f"{recommended} | 带药"))
            return self.reply("\n".join(lines), tuple(choices), plain=True, count_cooldown=False)

        key = self._resolve_map_key(name) if name else accessible[0][0]
        if not key:
            return self.reply("目前能停这几站，挑一个：",
                              tuple(Choice(info["name"], "start", key) for key, info in accessible),
                              plain=True, count_cooldown=False)
        info = self.catalog.maps[key]
        if self.player["extracts"] < info["extracts"]:
            raise GameError(f"{info['name']}得累计安全撤离 {info['extracts']} 次才放行，先去别的图练练手吧。")
        items = self._take_loadout(supplies)
        self._charge(info["entry"])
        if self.player["tickets"] == self.player["paid_tickets"]:
            self.player["paid_tickets"] -= 1
        self.player["tickets"] -= 1
        self.player["runs"] += 1
        stats, perks = self._stats()
        self.run = {
            "map": key, "floor": 1, "hp": stats["hp"], "stats": stats, "perks": perks,
            "heirloom_focus": self.heirloom_focus.copy(),
            "bag": items, "safe": [], "safe_extra": 0, "tea_until": 0, "sos_used": False,
            "last_stand": False, "lockpick": False, "peek": "loot" if self.player["runs"] == 1 else "",
            "gold_room": False, "guaranteed_loot": False,
            "shield": int(perks.get("shield_start", 0)),
            "atk_boost": 0.0,
            "combat_cleared_floor": 0,
            "last_killed_name": "",
            "value_bonus": sum(self.catalog.sets[name].get("value_bonus", 0) for name in self.completed_sets),
            "rare_bonus": sum(self.catalog.sets[name].get("rare_bonus", 0) for name in self.completed_sets),
            "event_bonus": sum(self.catalog.sets[name].get("event_bonus", 0) for name in self.completed_sets),
            "event": {"kind": "ready"},
        }
        intro = ""
        if self.player["runs"] == 1:
            intro = ("🛗 欢迎上车《无尽电梯》（全服跨群互通）！\n"
                     f"🎁 新人起步带 500 金 + {expedition.TICKET_DAILY_REFILL} 张通行证，前 3 局自带防爆保险，翻车也不掉货。\n")
        if items:
            counts: dict[str, int] = {}
            for it in items:
                counts[it["id"]] = counts.get(it["id"], 0) + 1
            carried_txt = "、".join(f"{self.catalog.items[k]['name']}×{c}" for k, c in counts.items())
            intro += f"🎒 已随身携带：{carried_txt}（局内发「使用」生效）\n"
        self._init_field()
        self.run["shield"] += int(perks.get("prism_shield",0))
        return self.reply(intro + self._enter_floor())

    def simulate(self, text: str, _target: str) -> Reply:
        if not self.player.get("name_confirmed") and self.player["runs"] == 0 and self.run is None:
            return self._first_time_name_prompt()
        self._check_idle()
        name = text.replace("｜", "|").split("|", 1)[0].strip()
        all_maps = list(self.catalog.maps.items())
        if not name:
            choices = tuple(
                Choice(f"{info['name']}（怪×{info['multiplier']:g}）", "simulate", mk)
                for mk, info in all_maps
            )
            return self.reply(
                "🧪 模拟战模式（全地图免费开放 · 不扣通行证/金币/道具 · 不带出战利品/不计榜）\n"
                "自带免费演习补给包（冰美式×2、烟雾弹、便携护盾、脉冲雷），套用你当前真实装备与属性！选一站开练：",
                choices, plain=True, count_cooldown=False,
            )
        key = self._resolve_map_key(name)
        if not key:
            choices = tuple(Choice(info["name"], "simulate", mk) for mk, info in all_maps)
            return self.reply("🧪 选择模拟区域：", choices, plain=True, count_cooldown=False)
        stats, perks = self._stats()
        sim_kit = []
        for item_id, qty in (("coffee", 2), ("smoke", 1), ("shield", 1), ("grenade", 1)):
            for _ in range(qty):
                it = self.catalog.ordinary_item(item_id, self.rng)
                it["sim_kit"] = True
                sim_kit.append(it)
        self.run = {
            "map": key, "floor": 1, "hp": stats["hp"], "stats": stats, "perks": perks,
            "bag": sim_kit, "safe": [], "safe_extra": 0, "tea_until": 0, "sos_used": False,
            "last_stand": False, "lockpick": False, "peek": "",
            "gold_room": False, "guaranteed_loot": False,
            "shield": int(perks.get("shield_start", 0)),
            "atk_boost": 0.0,
            "combat_cleared_floor": 0,
            "last_killed_name": "",
            "sim": True,
            "sim_gold": 10000,
            "value_bonus": sum(self.catalog.sets[n].get("value_bonus", 0) for n in self.completed_sets),
            "rare_bonus": sum(self.catalog.sets[n].get("rare_bonus", 0) for n in self.completed_sets),
            "event_bonus": sum(self.catalog.sets[n].get("event_bonus", 0) for n in self.completed_sets),
            "event": {"kind": "ready"},
        }
        intro = "🧪 【模拟战开启】0消耗/不带出/不计榜，已配发免费演习包（冰美式×2、烟雾弹、护盾、脉冲雷）+ 10,000 模拟币。\n"
        return self.reply(intro + self._enter_floor())

    def _is_prophecy(self) -> bool:
        return bool(self.run and self.run.get("prophecy"))

    def encounter_events(self):
        return self.catalog.prophecy_events if self._is_prophecy() else self.catalog.events

    def _world_rng(self, tag: str, floor: int | None = None) -> Random:
        if self.run and self.run.get("prophecy"):
            f = self.run["floor"] if floor is None else floor
            return prophecy.world_rng(self.run["prophecy_week"], f, tag)
        return self.rng

    def _map_title(self) -> str:
        if self.run and self.run.get("prophecy"):
            return f"✦ {prophecy.TITLE} F{self.run['floor']}/{prophecy.MAX_FLOOR}"
        prefix = "🧪 模拟 · " if self.run and self.run.get("sim") else ""
        return f"{prefix}{self.catalog.maps[self.run['map']]['name']} F{self.run['floor']}"

    def _eligible_relics(self, floor: int) -> list[sqlite3.Row]:
        if self.run and (self.run.get("sim") or self.run.get("prophecy")):
            return []
        return self.db.execute(
            "SELECT * FROM relics WHERE map_id=? AND floor BETWEEN ? AND ? AND owner_id<>? "
            "AND expires_at>? AND claimed=0 AND (reserved_by IS NULL OR reserved_until<=?) ORDER BY box_id",
            (self.run["map"], floor - 1, floor + 1, self.uid, self.at, self.at),
        ).fetchall()

    def _reserve_relic(self) -> sqlite3.Row | None:
        rows = self._eligible_relics(self.run["floor"])
        local = [row for row in rows if row["group_id"] == self.gid]
        remote = [row for row in rows if row["group_id"] != self.gid]
        use_local = local and (not remote or self.rng.random() < 0.8)
        pool = local if use_local else remote
        if not pool:
            return None
        box = self.rng.choice(pool)
        count = self.db.execute(
            "UPDATE relics SET reserved_by=?,reserved_until=? WHERE box_id=? AND claimed=0 "
            "AND (reserved_by IS NULL OR reserved_until<=?)",
            (self.uid, min(box["expires_at"], self.at + RUN_TIMEOUT), box["box_id"], self.at),
        ).rowcount
        return box if count == 1 else None

    def _event_weights(self, floor: int) -> dict[str, float]:
        weights = expedition.weights(self.catalog.weights,floor)
        perks = self.run["perks"]
        transferred = weights["normal"] * perks.get("sneak", 0)
        weights["normal"] -= transferred
        weights["loot"] += transferred
        if not self._eligible_relics(floor):
            weights["loot"] += weights.pop("relic")
        else:
            bonus = min(weights["loot"], weights["relic"] * perks.get("vulture", 0))
            weights["relic"] += bonus
            weights["loot"] -= bonus
        if self._is_prophecy():
            extra_elite = min(weights["loot"] * 0.5, weights["elite"] * 0.55)
            weights["elite"] += extra_elite
            weights["loot"] -= extra_elite
        return weights

    def _event_roll(self, floor: int) -> str:
        weights = self._event_weights(floor)
        return self._world_rng("room", floor).choices(list(weights), list(weights.values()))[0]

    def _rare_rate(self, floor: int, *, chest: bool = True, blessing: bool | None = None) -> float:
        run = self.run
        map_data, perks = self.catalog.maps[run["map"]], run["perks"]
        if blessing is None:
            blessing = self._golden_rain_active()
        return expedition.rate(map_data,floor,run["stats"]["luk"],run["rare_bonus"],
                               perks.get("treasure",0) if chest else 0,blessing)

    def _note_rare_limit(self, *, chest: bool, blessing: bool | None = None) -> None:
        if self._rare_rate(self.run["floor"], chest=chest, blessing=blessing) >= expedition.RARE_CAP:
            source = "宝箱" if chest else "打怪"
            self.note_limit(f"本层{source}战利品的基础金红率已达 {expedition.RARE_CAP:.0%} 上限，额外爆率加成不会再提高本层这项判定。升档另算。")

    def _golden_rain_active(self) -> bool:
        if self._is_prophecy():
            return False
        return self.db.execute("SELECT 1 FROM group_buffs WHERE group_id=? AND expires_at>?",
                               (self.gid, self.at)).fetchone() is not None

    def _next_floor_summary(self) -> str:
        run = self.run
        if run is None:
            return ""
        if run.get("prophecy") and run["floor"] >= prophecy.MAX_FLOOR:
            return f"✦ 已抵达【{prophecy.TITLE}】最高层 F{prophecy.MAX_FLOOR}！"
        next_f = run["floor"] + 1
        info = self.catalog.maps[run["map"]]
        if (prophecy.boss_number(next_f) if run.get("prophecy") else expedition.boss_number(next_f)):
            return f"📊 F{next_f} 必遇首领。可以先整备局内道具，击败后回满生命。"
        ambush_note = ""
        if not run.get("prophecy") and info.get("ambush_rate") and run["floor"] >= info.get("ambush_floor", 0):
            if run.get("combat_cleared_floor") == run["floor"]:
                ambush_note = "｜本层已清怪，撤离无伏击"
            else:
                amb = info["ambush_rate"] * (1 - run["perks"].get("veteran", 0))
                ambush_note = f"｜撤离伏击{amb:.0%}"
        if run["gold_room"]:
            return f"📊 F{next_f}：🔓 100% 隐藏金库(3高爆箱){ambush_note}"
        if run["peek"]:
            names = {"loot": "宝箱", "normal": "普怪", "elite": "精英", "choice": "奇遇", "relic": "遗物盒"}
            return f"📊 F{next_f}：📡 100%「{names.get(run['peek'], run['peek'])}」{ambush_note}"
        weights = self._event_weights(next_f)
        total = sum(weights.values()) or 1.0
        labels = [("loot", "箱"), ("normal", "怪"), ("elite", "精"), ("choice", "奇"), ("relic", "遗")]
        parts = [f"{cn}{weights[k] / total:.0%}" for k, cn in labels if weights.get(k, 0) > 0]
        rar = self.catalog.rarities
        r_share = rar["red"]["weight"] / (rar["gold"]["weight"] + rar["red"]["weight"])
        high_rate = self._rare_rate(next_f, chest=True)
        red_rate = high_rate * r_share
        return f"📊 F{next_f}：{' '.join(parts)}｜金红{high_rate:.1%}(红{red_rate:.1%}){ambush_note}"

    def _describe_event_option(self, option: dict, pending: dict) -> str:
        run = self.run
        costs: list[str] = []
        if option.get("cost"):
            costs.append(f"花 {option['cost']:,} 金")
        if option.get("hp_current_cost"):
            hp_loss = max(1, int(run["hp"] * option["hp_current_cost"]))
            costs.append(f"耗 {option['hp_current_cost']:.0%} 当前血(约{hp_loss})")
        if option.get("buy_offer") and pending.get("price"):
            costs.append(f"花 {pending['price']:,} 金")

        # Determine success probability
        prob: float | None = None
        prob_tag = ""
        if "guess" in option:
            prob = 1 / 3
            prob_tag = "33%猜中"
        elif option.get("agi_check") or "chance" in option:
            base = option.get("chance", 0.48 + combat.agility(run) * 0.012 - run["floor"] * 0.018)
            prob = combat.clamp(base + run["event_bonus"], 0.10, 0.94)
            prob_tag = f"{'敏捷检定' if option.get('agi_check') else ''}{prob:.0%}成功"

        # Build success outcome description
        gains: list[str] = []
        if option.get("buy_offer") and pending.get("offer"):
            gains.append(f"买下{self.catalog.name(pending['offer'])}")
        if option.get("loot"):
            min_r = option.get("min_rarity")
            r_cn = self.catalog.rarities[min_r]["name"] + "底起跳" if min_r and min_r != "white" else "随机"
            gains.append(f"得{option['loot']}件{r_cn}物资")
        if option.get("random_supply"):
            s_names = "/".join(self.catalog.items[k]["name"] for k in option["random_supply"][:3])
            suffix = "等道具" if len(option["random_supply"]) > 3 else ""
            gains.append(f"随机得{s_names}{suffix}")
        if option.get("items"):
            i_names = "+".join(self.catalog.items[k]["name"] for k in option["items"])
            gains.append(f"得{i_names}")
        if option.get("heal"):
            gains.append(f"回{option['heal']:.0%}血")
        if option.get("grant_shield"):
            gains.append(f"加{option['grant_shield']}护盾")
        if option.get("atk_boost"):
            gains.append(f"本局攻+{option['atk_boost']:.0%}")
        if option.get("rare_boost"):
            gains.append(f"本局金/红率+{option['rare_boost']:.0%}")
        if option.get("hint"):
            gains.append("偷看下层")
        if option.get("buff_loot"):
            gains.append("下箱升1档")
        if option.get("skip_floors"):
            destination = expedition.landing(run["floor"], option["skip_floors"])
            gains.append(f"跳至 F{destination}" + (" 首领房" if expedition.boss_number(destination) else ""))

        if not costs and not gains and prob is None:
            return "无消耗安全离开"

        # Failure outcome description
        fail_str = ""
        if prob is not None:
            fail_p = 1 - prob
            if option.get("failure_loot"):
                fail_str = f"；失败({fail_p:.0%})保底得{option['failure_loot']}件安慰货"
            else:
                f_hp = option.get("failure_hp") or max(1, int(run["stats"]["hp"] * option.get("failure_fraction", 0)))
                fail_str = f"；失败({fail_p:.0%})扣约{f_hp}HP"

        cost_prefix = ("、".join(costs) + "，") if costs else ""
        gain_text = "+".join(gains) if gains else "无事发生"
        if prob_tag:
            return f"{cost_prefix}{prob_tag}→{gain_text}{fail_str}"
        return f"{cost_prefix}100%→{gain_text}"

    def _known_next_room(self):
        next_floor = self.run["floor"] + 1
        if (prophecy.boss_number(next_floor) if self._is_prophecy() else expedition.boss_number(next_floor)):
            return "首领房"
        if self.run["gold_room"]:
            return "隐藏金库"
        return {"loot": "摸金宝箱", "normal": "普通怪物", "elite": "精英遭遇",
                "choice": "随机奇遇", "relic": "遗物盒"}.get(self.run["peek"])

    def _peek(self) -> str:
        known = self._known_next_room()
        if known:
            return known
        self.run["peek"] = self._event_roll(self.run["floor"] + 1)
        return self._known_next_room()

    def radar_scan(self):
        if self.run and self.run.get("radar_floor") == self.run["floor"] + 1:
            return {"floor":self.run["radar_floor"], "room":self._known_next_room()}
        return None

    def _items_text(self, items: list[dict], limit: int = 3) -> str:
        labels = [f"{self.catalog.name(item)}（{item['value']:,} 金）" for item in items[:limit]]
        if len(items) > limit:
            labels.append(f"另有 {len(items) - limit} 件")
        return "、".join(labels) or "空"

    def _value(self, items: list[dict]) -> int:
        return sum(item["value"] for item in items)

    def _floor_result(self) -> dict:
        result = self.run.get("floor_result")
        if result is None or result["floor"] != self.run["floor"]:
            result = {"floor": self.run["floor"], "title": "本层搜获", "loot": [], "field": {}}
            self.run["floor_result"] = result
        return result

    def _give(self, items: list[dict]) -> str:
        for item in items:
            item["run_pickup"]=True
        self.run["bag"].extend(items)
        self._floor_result()["loot"].extend(
            {key: item[key] for key in ("id", "value", "quality", "rarity") if key in item}
            for item in items
        )
        reds = [self.catalog.name(item) for item in items if self.catalog.rarity(item) == "red"]
        if not reds:
            return ""
        if self.run.get("prophecy"):
            return "\n✦ [星兆] 闪出红光——试炼摸到传世红【" + "、".join(reds) + "】（寓言集零带出，仅计估值）"
        if self.run.get("sim"):
            return "\n🧪 [模拟] 闪出红光——模拟摸到传世红【" + "、".join(reds) + "】（模拟战不带出）"
        return "\n🟥 警报！电梯里闪出刺眼红光——摸到传世红【" + "、".join(reds) + "】！赶紧「存箱」防翻车！"

    def _loot(self, count: int = 1, *, chest: bool, boost: int = 0, minimum: str = "white") -> list[dict]:
        has_blessing = self._golden_rain_active()
        if count:
            self._note_rare_limit(chest=chest, blessing=has_blessing)
        loot_rng = self._world_rng(f"loot:{'chest' if chest else 'drop'}:{boost}:{minimum}")
        loot = []
        for index in range(count):
            bonus = 0
            if chest and index == 0 and self.run["lockpick"]:
                bonus = 1
                self.run["lockpick"] = False
            loot.append(self.catalog.loot(self.run, loot_rng, chest=chest, blessing=has_blessing,
                                          boost=boost + bonus, minimum=minimum))
        self._first_drop_bonus(loot)
        return loot

    def _first_drop_bonus(self, loot):
        if self._is_prophecy():
            return
        info = self.catalog.maps[self.run["map"]]
        if info.get("guaranteed_equipment") and not self.run["guaranteed_loot"]:
            self.run["guaranteed_loot"] = True
            loot.append(self.catalog.roll_gear(self.rng, info["guaranteed_equipment"], info["value_multiplier"]))

    def _enter_floor(self) -> str:
        run = self.run
        if run.get("prophecy"):
            prophecy.refresh_stars(run)
        if run.get("radar_floor", run["floor"]+1) <= run["floor"]:
            run.pop("radar_floor",None)
        adventure = run.get("adventure")
        if adventure and adventure["destination"] != run["floor"]:
            run.pop("adventure")
        if "field_seed" not in run:
            self._init_field()
            run["shield"] = run.get("shield",0) + int(run["perks"].get("prism_shield",0))
        expedition.refill_ring_shield(run)
        result = self._floor_result()
        treatment = run.get("med_regen")
        treatment_note = ""
        if treatment and treatment["last_floor"] != run["floor"]:
            healed = self._heal(treatment["fraction"], report_limit=True)
            treatment["last_floor"] = run["floor"]
            treatment["left"] -= 1
            treatment_note = f"\n💊 缓释再生液回复 {healed} HP，剩余 {treatment['left']} 次。"
            if not treatment["left"]:
                del run["med_regen"]
        before = run.get("field_items", {}).copy()
        field_pool = prophecy.filter_field_items(self.catalog) if run.get("prophecy") else self.catalog.field_items
        field_note = expedition.enter_field(run, field_pool)
        for key, quantity in run.get("field_items", {}).items():
            gained = quantity - before.get(key, 0)
            if gained > 0:
                result["field"][key] = result["field"].get(key, 0) + gained
        body = self._floor_encounter() + treatment_note
        if options := prophecy.star_offer(run):
            names = "、".join(prophecy.STAR_RELICS[k][0] for k in options)
            body += f"\n✦ 本层星匣：{names}。可嵌入一件，离层即错过。"
        if run.get("field_offer"):
            return body + "\n" + self._field_offer_text()
        return body + ("\n"+field_note if field_note else "")

    def _floor_encounter(self) -> str:
        run = self.run
        is_prophecy = bool(run.get("prophecy"))
        if not run.get("sim") and not is_prophecy:
            self.db.execute(
                "UPDATE season_stats SET max_floor=?,floor_value=0 WHERE month=? AND user_id=? AND max_floor<?",
                (run["floor"], self.month, self.uid, run["floor"]),
            )
            self._record_map_progress(run)
        run["event"] = {"kind": "ready"}
        self._advance_quest("floors", 1)
        if expedition.boss_number(run["floor"]) or (is_prophecy and run["floor"] == prophecy.MAX_FLOOR):
            boss_rng = self._world_rng("boss")
            if is_prophecy:
                enemy = prophecy.make_monster(self.catalog, run["map"], run["floor"], boss_rng, boss=True)
            else:
                enemy = combat.monster(self.catalog.maps[run["map"]],run["floor"],boss_rng,boss=True)
            run["event"] = {"kind":"combat","enemy":enemy}
            boss_intros = (
                "整条井道的灯光骤然熄灭，应急红灯缓缓亮起——",
                "轿厢猛地一沉，钢缆发出不堪重负的呻吟——",
                "铁门刚滑开半掌，一股沉重的压迫感扑面而来——",
            )
            first_note = "你先手" if combat.agility(run) >= enemy["agi"] else "首领先手"
            if is_prophecy:
                return (f"👑 {boss_rng.choice(boss_intros)}\n"
                        f"{self._map_title()}星渊霸主【{enemy['name']}】降临！"
                        f"（{enemy['hp']:,} HP · 攻 {enemy['atk']:,} · 防 {enemy['def']:,} · 敏 {enemy['agi']}，{first_note}）\n"
                        f"每 4 回合重击 ×{expedition.BOSS_HEAVY:g} 后可整备。击败回满血（寓言集不产出虹彩、闪耀与可带出物资）。")
            return (f"👑 {self.rng.choice(boss_intros)}\n"
                    f"{self._map_title()}守门霸主【{enemy['name']}】横身挡路！"
                    f"（{enemy['hp']} HP · 攻 {enemy['atk']} · 防 {enemy['def']} · 敏 {enemy['agi']}，{first_note}）\n"
                    f"每 4 回合重击 ×{expedition.BOSS_HEAVY:g} 后可整备。击败回满血，本场正式局红装率 "
                    f"{expedition.boss_red_gear_rate(enemy['boss'],self.catalog.maps[run['map']]['multiplier']):.2%}，虹彩独立 0.5%。")
        if run["gold_room"]:
            run["gold_room"], run["peek"] = False, ""
            self._floor_result()["title"] = "打开隐藏金库"
            items = self._loot(3, chest=True, boost=1, minimum="blue")
            self._advance_quest("chests", 3)
            text = "🔓 金库大门缓缓滑开，里头安静得只剩财宝的反光——一口气连开三箱！\n" + self._items_text(items) + self._give(items)
        else:
            kind = run["peek"] or self._event_roll(run["floor"])
            run["peek"] = ""
            box = self._reserve_relic() if kind == "relic" else None
            if box is not None:
                run["event"] = {"kind": "relic", "box_id": box["box_id"]}
                source = f"{box['owner_name']} " if box["group_id"] == self.gid else f"隔壁群的 {box['owner_name']} "
                bounty = math.floor(box["value"] * run["perks"].get("rescue", 0.35))
                scav = "（触发趁火打劫回20%血）" if run["perks"].get("scavenge") else ""
                text = (
                    f"💀 角落里躺着 {source}翻车留下的遗物盒，掂量着值 {box['value']:,} 金。\n"
                    f"• 舔包：全装进临时背包{scav}，活着撤离才归你\n"
                    f"• 捞尸：寄还失主，100% 立刻入账 {bounty:,} 金赏金"
                )
                text += self._tip(TIP_RELIC, "「舔包」全塞临时背包（得活着带出）；「捞尸」直接寄还失主并拿现金赏金。")
            elif kind in ("loot", "relic"):
                self._floor_result()["title"] = "打开宝箱"
                flavor_rng = self._world_rng("flavor")
                items = self._loot(chest=True)
                top_rarity = max((RARITIES.index(self.catalog.rarity(item)) for item in items), default=0)
                if top_rarity >= 5:
                    lead = "🚨 门缝炸出刺眼红光！手别抖，摸到："
                elif top_rarity == 4:
                    lead = flavor_rng.choice(("✨ 盒缝漏出一道金光——出金了！摸到：", "✨ 手感沉得不对劲，掀开居然是大金！摸到："))
                else:
                    scenes = {
                        "mall": ("📦 翻开落灰货架，顺手摸出：", "📦 撬开半掩的收银台，翻出：", "📦 掀开花车盖布，底下压着："),
                        "subway": ("📦 掰开松动的候车座椅，摸出：", "📦 撬开检票机检修盖，里头藏着：", "📦 翻开角落的无主行李箱，捡到："),
                        "abyss": ("📦 拉开未上锁的恒温柜，取出：", "📦 撬开防爆样本箱，里头躺着：", "📦 翻开凌乱的实验台抽屉，摸出："),
                        "auction": ("📦 掀开丝绒展台幕布，摸走：", "📦 撬开贵宾包厢暗格，翻出：", "📦 打开落拍流拍箱，里头竟躺着："),
                    }.get(run["map"], ("📦 撬开角落的箱子，摸出：",))
                    lead = flavor_rng.choice(scenes)
                self._advance_quest("chests", 1)
                text = lead + self._items_text(items) + self._give(items)
                if top_rarity >= 2 and not is_prophecy:
                    text += self._tip(TIP_SAFEBOX, "好货怕翻车？发「存箱」就能把最贵的锁进安全箱。")
            elif kind in ("elite", "normal"):
                mon_rng = self._world_rng("monster")
                if is_prophecy:
                    enemy = prophecy.make_monster(self.catalog, run["map"], run["floor"], mon_rng, elite=kind == "elite")
                else:
                    enemy = combat.monster(self.catalog.maps[run["map"]], run["floor"], mon_rng, elite=kind == "elite")
                run["event"] = {"kind": "combat", "enemy": enemy}
                first_note = "你先手" if combat.agility(run) >= enemy["agi"] else "怪先手"
                stat_tag = f"{enemy['hp']:,} HP · 攻 {enemy['atk']:,} · 防 {enemy['def']:,} · 敏 {enemy['agi']}({first_note})"
                if enemy["elite"]:
                    text = mon_rng.choice((
                        f"👹 电梯顶灯骤然变红！守层狠角色【{enemy['name']}】（{stat_tag}）横在了正前方。",
                        f"👹 地面微微发震，【{enemy['name']}】（{stat_tag}）盯着电梯口缓缓逼近。",
                    ))
                else:
                    text = mon_rng.choice((
                        f"👾 电梯门刚滑开，【{enemy['name']}】（{stat_tag}）迎面撞了过来。",
                        f"👾 走廊尽头闪出黑影，【{enemy['name']}】（{stat_tag}）死死堵住去路。",
                        f"👾 脚底踩响了空罐头，惊动了角落的【{enemy['name']}】（{stat_tag}）。",
                    ))
                if not is_prophecy:
                    text += self._tip(TIP_COMBAT, "回「打」一次出战报，打不过就回「跑」，遇精英还能「摇人」白嫖群友帮打。")
            else:
                ev_rng = self._world_rng("choice")
                config = ev_rng.choice(list(self.encounter_events().values()))
                run["event"] = {"kind": "choice", "id": config["id"]}
                if config.get("guess"):
                    run["event"]["secret"] = ev_rng.randint(1, 3)
                text = f"🎲 {config['name']}\n{config['text']}"
                if config.get("offer"):
                    offer = (self.catalog.roll_gear(ev_rng, ev_rng.choice(("green", "blue", "purple")))
                             if ev_rng.random() < 0.45 else self.catalog.ordinary_item(
                                 ev_rng.choice(("coffee", "ice_tea", "pouch") if is_prophecy else
                                               ("coffee", "smoke", "ice_tea", "pouch")), ev_rng))
                    run["event"].update(offer=offer, price=max(1, int(offer["value"] * 0.70)))
                    brief = ITEM_BRIEFS.get(offer["id"], "")
                    brief_note = f"（{brief}）" if brief else ""
                    text += f"\n他掏出一件货：{self.catalog.name(offer)}{brief_note}，开价 {run['event']['price']:,} 金"
                opt_lines = [
                    f"• {idx}.{opt['label']}：{self._describe_event_option(opt, run['event'])}"
                    for idx, opt in enumerate(config["choices"], 1)
                ]
                text += "\n" + "\n".join(opt_lines)
        if run["floor"] >= 3 or run["hp"] * 2 < run["stats"]["hp"]:
            text += self._tip(TIP_EXTRACT, "见好就收的话随时发「撤离」，能把全身收获稳稳带回家。")
        shield_str = f" · 盾 {run['shield']}" if run.get("shield", 0) > 0 else ""
        return f"🛗 {self._map_title()} · HP {run['hp']}/{run['stats']['hp']}{shield_str}\n{text}"

    def _release_relic(self) -> None:
        self.db.execute("UPDATE relics SET reserved_by=NULL,reserved_until=0 WHERE reserved_by=? AND claimed=0", (self.uid,))

    def _clear_event(self) -> None:
        self._release_relic()
        self.db.execute("DELETE FROM signals WHERE owner_id=?", (self.uid,))
        self.run["event"] = {"kind": "ready"}

    def down(self, _text: str, _target: str) -> Reply:
        run = self._check_run()
        if run["event"]["kind"] != "ready":
            raise GameError("眼前这摊事还没解决呢，先处理完再按下一层。")
        if run.get("prophecy") and run["floor"] >= prophecy.MAX_FLOOR:
            return self.reply(self.finish_run(True, f"登顶【{prophecy.TITLE}】F{prophecy.MAX_FLOOR} 终层"))
        run["floor"] += 1
        return self.reply(self._enter_floor())

    def rush_settings(self, text: str, _target: str) -> Reply:
        event_modes = {"遇奇遇暂停": "pause", "随机奇遇": "random", "无视奇遇": "ignore"}
        previous = (self.rush_floors, self.rush_skip_valuable, self.rush_event_mode, self.rush_auto_elite)
        floors, skip, event_mode, auto_elite = previous
        for part in text.strip().split():
            if part in ("3","5","10"):
                floors = int(part)
            elif part in ("跳过金红","遇金红暂停"):
                skip = part == "跳过金红"
            elif part in event_modes:
                event_mode = event_modes[part]
            elif part in ("自动打精英", "遇精英暂停"):
                auto_elite = part == "自动打精英"
            else:
                raise GameError("发「连战设置」查看层数、金红、奇遇和精英选项。")
        settings = floors, skip, event_mode, auto_elite
        if settings != previous:
            self.db.execute("""
                INSERT INTO rush_preferences(user_id,floors,skip_valuable,event_mode,auto_elite) VALUES(?,?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET floors=excluded.floors,skip_valuable=excluded.skip_valuable,
                    event_mode=excluded.event_mode,auto_elite=excluded.auto_elite
            """, (self.uid, *settings))
            self.rush_floors, self.rush_skip_valuable, self.rush_event_mode, self.rush_auto_elite = settings
        mode = "跳过金红" if skip else "遇金红暂停"
        toggle = "遇金红暂停" if skip else "跳过金红"
        event_label = next(label for label, value in event_modes.items() if value == event_mode)
        elite_label = "自动打精英" if auto_elite else "遇精英暂停"
        elite_toggle = "遇精英暂停" if auto_elite else "自动打精英"
        return self.reply(f"连战 {floors} 层 · {mode} · {event_label} · {elite_label}。遇首领暂停。\n"
                          "随机奇遇可能花金币或损血。", (
            *(Choice(f"{n} 层","rush_settings",str(n)) for n in (3,5,10)),
            Choice(toggle,"rush_settings",toggle),
            *(Choice(label, "rush_settings", label) for label in event_modes if label != event_label),
            Choice(elite_toggle, "rush_settings", elite_toggle),
        ), plain=True, multiline_menu=True, count_cooldown=False)

    def _rush_adventure(self, floor_limit: int, row: dict) -> str:
        run = self.run
        pending = run["event"]
        event = self.encounter_events()[pending["id"]]
        floor = run["floor"]
        if self.rush_event_mode == "random":
            budget = run["sim_gold"] if run.get("sim") else self.player["gold"]
            eligible = []
            for i, option in enumerate(event["choices"], 1):
                cost = option.get("cost", 0) + (pending["price"] if option.get("buy_offer") else 0)
                distance = option.get("skip_floors", 0)
                if cost <= budget and (not distance or expedition.landing(floor, distance) <= floor_limit):
                    eligible.append(i)
            if eligible:
                chosen = self.rng.choice(eligible)
                report = self.event_choice(str(chosen), "", enter_landing=False)
                row["adventure"] = self.last_adventure
                option = event["choices"][chosen-1]["label"]
                return f"F{floor} · 随机选择「{option}」\n{report.text}"
        reason = "按连战设置无视。" if self.rush_event_mode == "ignore" else "可选项余额不足或会超过本次连战层数。"
        self.last_adventure = {
            "name": event["name"], "option": "无视奇遇", "floor": floor, "destination": floor,
            "outcome": "ignored", "lines": [reason], "loot": [],
        }
        run["adventure"] = row["adventure"] = self.last_adventure
        self._clear_event()
        return f"F{floor} · 略过「{event['name']}」 · {reason}"

    def rush(self, text: str, _target: str) -> Reply:
        run = self._check_run()
        if run["event"]["kind"] != "ready":
            raise GameError("先处理眼前的遭遇，再连进。")
        if text and (not text.isascii() or not text.isdigit() or len(text)>2 or not 1<=int(text)<=10):
            raise GameError("连战最多 10 层，发「连战设置」可选 3、5、10 层。")
        steps = int(text) if text else self.rush_floors
        first = run["floor"]+1
        floor_limit = run["floor"] + steps
        if run.get("prophecy"):
            if run["floor"] >= prophecy.MAX_FLOOR:
                return self.reply(self.finish_run(True, f"登顶【{prophecy.TITLE}】F{prophecy.MAX_FLOOR} 终层"))
            floor_limit = min(floor_limit, prophecy.MAX_FLOOR)
        lines = []
        self.last_rush = {"floors": [], "reason": "达到所选层数"}
        jumped = stop_after_landing = False
        while jumped or run["floor"] < floor_limit:
            low_health = run["hp"]*2 <= run["stats"]["hp"]
            if not jumped:
                if self.last_rush["floors"] and low_health:
                    self.last_rush["reason"] = "生命不超过 50%"
                    lines.append("⚠️ 生命 ≤50%，已暂停。")
                    break
                run["floor"] += 1
            floor = run["floor"]
            jumped = False
            before=len(run["bag"])
            body=self._enter_floor()
            row = self._floor_result()
            self.last_rush["floors"].append(row)
            if prophecy.star_offer(run):
                self.last_rush["reason"] = "发现试炼星匣"
                lines.append(body)
                break
            if run.get("field_offer"):
                enemy = run["event"].get("enemy", {})
                encounter = "遇到首领，" if enemy.get("boss") else "遇到精英，" if enemy.get("elite") else ""
                self.last_rush["reason"] = encounter + "待选择临装"
                lines.append(body)
                break
            if stop_after_landing:
                lines.append(body)
                break
            kind=run["event"]["kind"]
            if kind=="combat":
                enemy=run["event"]["enemy"]
                stop_elite = enemy["elite"] and not self.rush_auto_elite
                if enemy.get("boss") or stop_elite or run["hp"]*2<=run["stats"]["hp"]:
                    self.last_rush["reason"] = "遇到首领" if enemy.get("boss") else "遇到精英" if stop_elite else "生命不超过 50%"
                    lines.append(body)
                    break
                note=self._fight(enemy).text
                lines.append(f"F{run['floor']} · {note.splitlines()[0]}")
                lines.extend(line for line in body.splitlines() if "拾到局内道具" in line)
                if not self.run:
                    self.last_rush["reason"] = "本局已结算"
                    lines.extend(note.splitlines()[1:])
                    break
                if enemy["hp"]:
                    mid=enemy.get("elite") and enemy.get("rounds_fought")==expedition.ELITE_INTERVAL
                    self.last_rush["reason"] = "精英中场" if mid else "战斗平手"
                    lines.append("精英战进入第 8 回合整备窗口。" if mid else "本场平手，已暂停。")
                    break
            elif kind == "choice" and self.rush_event_mode != "pause":
                if run["hp"]*2 <= run["stats"]["hp"]:
                    self.last_rush["reason"] = "生命不超过 50%"
                    lines.append(body)
                    break
                lines.append(self._rush_adventure(floor_limit, row))
                lines.extend(line for line in body.splitlines() if "拾到局内道具" in line)
                if not self.run:
                    self.last_rush["reason"] = "本局已结算"
                    break
                jumped = run["floor"] != floor
            else:
                lines.append(body)
                if kind!="ready":
                    self.last_rush["reason"] = "遇到奇遇" if kind == "choice" else "遇到遗物"
                    break
            if not self.rush_skip_valuable and any(self.catalog.rarity(it) in ("gold","red") for it in run["bag"][before:]):
                self.last_rush["reason"] = "拾到金红物资"
                lines.append("✨ 获得高价值物资，已暂停，可先存箱。")
                if not jumped:
                    break
                stop_after_landing = True
            info=self.catalog.maps[run["map"]]
            if (not run.get("prophecy") and not stop_after_landing and info["ambush_rate"] and floor==info["ambush_floor"]
                    and run["combat_cleared_floor"]!=floor):
                self.last_rush["reason"] = "抵达伏击起始层"
                lines.append("⚠️ 已到撤离伏击起始层，先决定去留。")
                if not jumped:
                    break
                stop_after_landing = True
        return self.reply(f"⚡ 连进 F{first} → F{run['floor']}\n"+"\n".join(lines))

    def _signal_active(self) -> bool:
        return self.db.execute("SELECT 1 FROM signals WHERE owner_id=? AND expires_at>?",
                               (self.uid, self.at)).fetchone() is not None

    def _check_enemy(self) -> dict:
        run = self._check_run()
        if run["event"]["kind"] != "combat":
            raise GameError("这层挺安静的，附近没怪可打。")
        return run["event"]["enemy"]

    def _enemy_win(self, enemy: dict) -> str:
        lines: list[str] = []
        floor_result = self._floor_result()
        old_count = len(floor_result["loot"])
        boss = enemy.get("boss",0)
        if boss:
            if self.run["event"].get("enemy") is not enemy or enemy["hp"] or self.run.get("paid_boss_floor")==self.run["floor"]:
                raise GameError("该首领的胜利已经结算。")
            self.run["paid_boss_floor"] = self.run["floor"]
            if not self.run.get("sim") and not self.run.get("prophecy"):
                focus=self.run.get("heirloom_focus")
                if not focus:
                    raise GameError("本旧局尚未登记追寻池，请先发「追寻 1 8」或在网页选择两套，再挑战首领。")
                reward=heirlooms.roll(self.run["floor"],focus,self.heirloom_owned,self.rng)
                if reward:
                    first=reward not in self.heirloom_owned
                    self.db.execute("""INSERT INTO heirloom_inventory(user_id,piece,quantity) VALUES(?,?,1)
                                   ON CONFLICT(user_id,piece) DO UPDATE SET quantity=quantity+1""",(self.uid,reward))
                    self.heirloom_owned[reward]=self.heirloom_owned.get(reward,0)+1
                    floor_result["loot"].append({"id":reward,"value":0,"rarity":"shining","banked":True})
                    piece=heirlooms.PIECES[reward]
                    lines.append(f"✨【闪耀掉落】{heirlooms.SETS[piece['set']]['name']}·{piece['name']}（"+
                                 ("首次点亮，已入祝福库，下局整备可装" if first else "重复件，已入祝福库")+"）")
            items = self.catalog.boss_rewards(self.run,self._world_rng("boss_loot"),boss)
            self._first_drop_bonus(items)
            if not self.run.get("sim") and not self.run.get("prophecy") and self.rng.random()<expedition.RAINBOW_CHANCE:
                key = self.rng.choice(tuple(self.catalog.rainbow))
                skill = self.catalog.rainbow[key]
                first = key not in self.archives
                item = {"id": key, "value": self.catalog.items[key]["value"], "quality": ""}
                self.deposit(item, boss_reward=True)
                floor_result["loot"].append({**item, "banked": True})
                if not first:
                    lines.append(f"🌈 又获得了【{skill['name']}】！已存入仓库，同款技能仍生效一次。")
                else:
                    lines.append(f"🌈 奇迹降临！【{skill['name']}】已存入仓库，永久技能当场点亮！\n   ✨ {skill['description']}")
                    if skill.get("lore"):
                        lines.append(f"   📜 {skill['lore']}")
                    if set(self.catalog.rainbow)<=self.archives.keys():
                        self.completed_sets.append("虹彩共鸣")
                        res = self.catalog.progression.get("resonance", {})
                        lines.append(f"🌈✨ 四彩合流！「虹彩共鸣」永久觉醒！{res.get('lore', '')}")
                    self.run["stats"], self.run["perks"] = self._stats()
                    if key == "prism_sky":
                        expedition.refill_ring_shield(self.run)
            if self.run.get("burst",{}).get("blood"): self.run.pop("burst")
            self.run["hp"] = self.run["stats"]["hp"]
            lines.append(f"👑 漂亮！首领轰然倒地，紧绷的神经瞬间放松——生命全额回满至 {self.run['hp']} HP！")
        else:
            boost_rng = self._world_rng("elite_boost")
            items = self._loot(2 if enemy["elite"] else 1, chest=False,
                               boost=(1 if boost_rng.random()<.72 else 2) if enemy["elite"] else 0)
        if not boss and self.run["perks"].get("vampire"):
            healed = self._heal(self.run["perks"]["vampire"])
            if healed:
                lines.append(f"🩸 嗜血回血 +{healed} HP（当前 {self.run['hp']}/{self.run['stats']['hp']} HP）")
        lines.append("💰 顺手摸走：" + self._items_text(items) + self._give(items))
        battle = floor_result.get("battle")
        if battle is not None and battle is getattr(self, "last_battle", None):
            battle["loot"] = floor_result["loot"][old_count:]
        self._advance_quest("kills", 1)
        self.run["combat_cleared_floor"] = self.run["floor"]
        self.run["last_killed_name"] = enemy["name"].removeprefix("电梯守门怪 · ")
        self._clear_event()
        if enemy["ambush"]:
            lines.append(self.finish_run(True, "放倒守门怪、冲出伏击"))
        elif self.run.get("prophecy") and self.run["floor"] >= prophecy.MAX_FLOOR:
            lines.append(self.finish_run(True, f"斩落 F{prophecy.MAX_FLOOR} 星渊终主，登顶群星尽头"))
        return "\n".join(lines)

    def _record_battle_log(self, enemy: dict, report: combat.FightResult) -> str:
        self.last_battle = {
            "floor": self.run["floor"], "enemy": enemy["name"], "rounds": report.rounds,
            "dealt": report.total_dealt, "taken": report.total_taken,
            "crits": report.crits, "dodges": report.dodges,
            "hp": self.run["hp"], "enemy_hp": enemy["hp"],
            "outcome": "defeat" if not self.run["hp"] else "victory" if not enemy["hp"] else "draw",
            "log": list(report.log), "loot": [],
        }
        self._floor_result()["battle"] = self.last_battle
        player_agi = combat.agility(self.run)
        opening = (
            f"⚔️ {self._map_title()} · 详细战报 vs【{enemy['name']}】\n"
            f"开战：你 {report.player_start_hp}/{self.run['stats']['hp']} HP（敏 {player_agi}"
            f"{'，先手' if report.player_first else ''}） vs "
            f"怪 {report.enemy_start_hp}/{enemy['max_hp']} HP（敏 {enemy['agi']}"
            f"{'' if report.player_first else '，先手'}）"
        )
        highlights = [f"造成 {report.total_dealt}", f"承伤 {report.total_taken}"]
        if report.peak:
            highlights.append(f"最高一击 {report.peak}")
        if report.crits:
            highlights.append(f"暴击 {report.crits} 次")
        if report.dodges:
            highlights.append(f"闪避 {report.dodges} 次")
        hp_tail = (
            f"你剩 {self.run['hp']}/{self.run['stats']['hp']} HP · 怪剩 {enemy['hp']}/{enemy['max_hp']} HP"
            if enemy["hp"]
            else f"剩 {self.run['hp']}/{self.run['stats']['hp']} HP"
        )
        full = "\n".join([opening, *report.log, f"汇总：{' · '.join(highlights)} ｜ {hp_tail}"])
        self.game.save_battle_log(self.uid, full)
        return full

    def battle_log(self, _text: str, _target: str) -> Reply:
        log_text = self.game.get_battle_log(self.uid)
        if not log_text:
            raise GameError("最近还没有战斗记录，打一架后再发「战报」查看逐回合详情。")
        return self.reply(log_text, (), count_cooldown=False, forward_title="详细战报")

    def _fight(self, enemy: dict, prefix: str = "") -> Reply:
        report = combat.fight(self.run, enemy, self._world_rng(f"combat:{enemy.get('rounds_fought', 0)}"))
        self._record_battle_log(enemy, report)
        if not self.run["hp"]:
            headline = f"⚔️ 死撑 {report.rounds} 回合倒在了【{enemy['name']}】面前"
        elif not enemy["hp"]:
            if self.run["hp"] * 10 <= self.run["stats"]["hp"] * 3:
                headline = f"⚔️ 险胜！{report.rounds} 回合硬扛放倒【{enemy['name']}】"
            else:
                headline = f"⚔️ {report.rounds} 回合放倒【{enemy['name']}】"
        else:
            headline = f"⚔️ 与【{enemy['name']}】缠斗 {report.rounds} 回合平手"
        highlights = [f"伤 {report.total_dealt}", f"承 {report.total_taken}"]
        if report.crits:
            highlights.append(f"暴击×{report.crits}")
        if report.dodges:
            highlights.append(f"闪避×{report.dodges}")
        hp_tail = (
            f"你剩 {self.run['hp']}/{self.run['stats']['hp']} · 怪剩 {enemy['hp']}/{enemy['max_hp']}"
            if enemy["hp"]
            else f"剩 {self.run['hp']}/{self.run['stats']['hp']} HP"
        )
        log_tip = "（发「战报」看逐回合）" if (report.rounds >= 3 or not self.run["hp"]) else ""
        text = f"{prefix}{headline}（{' · '.join(highlights)} ｜ {hp_tail}）{log_tip}"
        if not self.run["hp"]:
            return self.reply(text + "\n" + self.fallen())
        if not enemy["hp"]:
            text += "\n" + self._enemy_win(enemy)
        elif enemy.get("boss"):
            text += "\n👑 首领喘着粗气重新架起架势，双方保留剩余血量！赶紧磕药或拍局内道具再来一轮！"
        elif enemy.get("elite") and enemy.get("rounds_fought",0)<expedition.ELITE_LIMIT:
            text += f"\n【精英中场】与{enemy['name']}激战满 8 回合，可用药、启用临装或支援。准备就绪后从第 9 回合续战。"
        elif enemy.get("elite"):
            self._clear_event()
            text += f"\n【平手退场】与{enemy['name']}战满 16 回合。留在 F{self.run['floor']}，无击杀奖励。"
            if enemy["ambush"]: text += "本次撤离未完成。"
        elif enemy["ambush"]:
            text += "\n🚪 守门怪死死卡着电梯门不肯松手，双方保留剩余血量！补口状态再干它！"
        else:
            self._clear_event()
            text += "\n怪物骂骂咧咧退回暗处了，可惜没掉东西。"
        return self.reply(text)

    def fight(self, _text: str, _target: str) -> Reply:
        enemy = self._check_enemy()
        if self._signal_active():
            raise GameError("求救信号弹还在天上亮着呢，先等群友空降补刀。")
        return self._fight(enemy)

    def flee(self, _text: str, _target: str) -> Reply:
        enemy = self._check_enemy()
        if self._signal_active():
            raise GameError("信号弹还亮着，先等群友支援。")
        if enemy["ambush"]:
            raise GameError("守门怪死死扒着电梯门，只能硬刚放倒它，或者甩颗烟雾弹脱身。")
        if enemy.get("boss"):
            raise GameError("首领把向下的路堵得水泄不通，硬溜可溜不过去！想保命可以直接「撤离」或甩颗烟雾弹，要不就磕药整备再干！")
        if self._world_rng(f"flee:{enemy.get('rounds_fought', 0)}").random() < combat.flee_rate(self.run):
            self._clear_event()
            return self.reply("💨 脚底抹油无伤脱身！还在这一层，趁现在决定走还是留。")
        had_last_stand = bool(self.run["last_stand"])
        effects = []
        lost = combat.hurt(self.run, combat.damage(enemy["atk"], combat.defence(self.run)),
                           strike=True,enemy=enemy,log=effects)
        saved = (not had_last_stand) and bool(self.run["last_stand"])
        if not self.run["hp"]:
            return self.reply(f"⚠️ 没跑掉！背后挨了一记狠的（-{lost} HP），直接交代在这儿了……\n" + self.fallen())
        save_note = ("【不屈锁1血！】" if saved else "")+"".join(effects)
        return self._fight(enemy, prefix=f"⚠️ 没溜成，回头先挨了一巴掌（-{lost} HP{save_note}），只能硬上了！\n")

    def extract(self, _text: str, _target: str) -> Reply:
        run = self._check_run()
        if run["event"]["kind"] == "combat" and run["event"]["enemy"]["ambush"]:
            raise GameError("电梯门被守门怪扒住了！打赢它或者扔颗烟雾弹就能撤。")
        if run.get("prophecy"):
            return self.reply(self.finish_run(True))
        info = self.catalog.maps[run["map"]]
        if run.get("combat_cleared_floor") == run["floor"]:
            return self.reply(self.finish_run(True))
        chance = info["ambush_rate"] * (1 - run["perks"].get("veteran", 0))
        if chance and run["floor"] >= info["ambush_floor"] and self.rng.random() < chance:
            if run["event"]["kind"] == "combat":
                enemy = run["event"]["enemy"]
                enemy["ambush"] = True
                if not enemy["name"].startswith("电梯守门怪 · "):
                    enemy["name"] = f"电梯守门怪 · {enemy['name']}"
                first_tag = "你先手" if combat.agility(run) >= enemy["agi"] else "怪先手"
                return self.reply(
                    f"⚠️ 电梯门刚合到一半，眼前的【{enemy['name']}】（剩 {enemy['hp']}/{enemy['max_hp']} HP · 攻 {enemy['atk']} · 敏 {enemy['agi']}，{first_tag}）硬生生扒开了门缝！\n"
                    "打赢它或丢颗烟雾弹才能撤离。"
                )
            self._clear_event()
            enemy = combat.monster(
                info, run["floor"], self.rng, elite=True, ambush=True,
                exclude_name=run.get("last_killed_name", ""),
            )
            run["event"] = {"kind": "combat", "enemy": enemy}
            first_tag = "你先手" if combat.agility(run) >= enemy["agi"] else "怪先手"
            return self.reply(
                f"⚠️ 电梯门刚合到一半，暗处冲出的【{enemy['name']}】（{enemy['hp']} HP · 攻 {enemy['atk']} · 敏 {enemy['agi']}，{first_tag}）硬生生扒开了门缝！\n"
                "打赢它或丢颗烟雾弹才能撤离。"
            )
        return self.reply(self.finish_run(True))

    def _heal(self, fraction: float, *, report_limit: bool = False) -> int:
        if self.run.get("burst",{}).get("blood"):
            return 0
        stats = self.run["stats"]
        before = self.run["hp"]
        self.run["hp"] = min(stats["hp"], before + max(1, round(stats["hp"] * fraction)))
        if report_limit and self.run["hp"] == stats["hp"]:
            self.note_limit(expedition.health_limit_note(stats["hp"]))
        return self.run["hp"] - before

    def fallen(self) -> str:
        for inventory in (self.run["bag"], self.run["safe"]):
            index = next((index for index, item in enumerate(inventory) if item["id"] == "doll"), None)
            if index is not None:
                inventory.pop(index)
                return self.finish_run(False, protected_by_doll=True)
        return self.finish_run(False)

    def finish_run(self, extracted: bool, reason: str = "", *,
                   forfeit_safe: bool = False, protected_by_doll: bool = False) -> str:
        run = self.run
        if extracted and run["hp"] <= 0:
            return self.fallen()
        if run.get("prophecy"):
            self._release_relic()
            self.db.execute("DELETE FROM signals WHERE owner_id=?", (self.uid,))
            week = run["prophecy_week"]
            floor = run["floor"]
            cleared = (
                floor if (run["event"]["kind"] == "ready" or run.get("combat_cleared_floor") == floor
                          or (extracted and run["event"]["kind"] != "combat"))
                else max(0, floor - 1)
            )
            if prophecy.boss_number(floor) and run.get("combat_cleared_floor") != floor:
                cleared = floor - 1
            trial_items = [] if forfeit_safe else [it for it in (*run["safe"], *run["bag"]) if not it.get("sim_kit")]
            val = self._value(trial_items)
            improved = prophecy.record_score(
                self.db, week, self.uid, self.player["nickname"],
                max_floor=floor, cleared_floor=cleared, value=val, extracted=extracted, now=self.at,
            )
            record = prophecy.player_record(self.db, week, self.uid)
            self.run = None
            self.last_departure = {
                "floor": floor, "cleared_floor": cleared, "extracted": extracted,
                "simulation": True, "prophecy": True, "week": week,
                "units": len(trial_items), "value": val, "protected_by_doll": protected_by_doll,
            }
            if cleared >= prophecy.MAX_FLOOR and extracted:
                head = f"👑✦ 【{prophecy.TITLE} · 登顶 F{prophecy.MAX_FLOOR}】星海为你加冕！"
            elif extracted:
                lead = f"{reason}，" if reason else "星门缓缓闭合，"
                head = f"✦ 【{prophecy.TITLE}结束】{lead}止步 F{floor}（已通过 F{cleared} · 剩 {run['hp']}/{run['stats']['hp']} HP）"
            else:
                head = f"✦ 【{prophecy.TITLE}阵亡】止步 F{floor}（已通过 F{cleared}）！"
            rank_line = f"本周全服排名：第 {record['rank']} 名" if record else ""
            pb_note = " · 🎉 刷新个人本周最佳纪录！" if improved else ""
            return (
                f"{head}\n"
                f"本期星兆：{week} · {run.get('prophecy_name', '')}\n"
                f"{rank_line}{pb_note}\n"
                f"本局搜获估值 {val:,} 金（0 消耗 · 零带出，不产出虹彩/闪耀/物资 · 发「寓言榜」查看周榜）"
            )
        if run.get("sim"):
            self._release_relic()
            self.db.execute("DELETE FROM signals WHERE owner_id=?", (self.uid,))
            self.run = None
            sim_items = [] if forfeit_safe else [it for it in (*run["safe"], *run["bag"]) if not it.get("sim_kit")]
            val = self._value(sim_items)
            self.last_departure = {"floor": run["floor"], "extracted": extracted, "simulation": True,
                                   "units": len(sim_items), "value": val, "protected_by_doll": protected_by_doll}
            if extracted:
                lead = f"{reason}，" if reason else "模拟舱门合上，"
                head = f"🧪 【模拟战结束】{lead}止步第 {run['floor']} 层（剩 {run['hp']}/{run['stats']['hp']} HP）"
            else:
                head = f"🧪 【模拟战阵亡】第 {run['floor']} 层倒下了！"
            return f"{head}\n模拟战利品 {len(sim_items)} 件（估值约 {val:,} 金，0消耗 · 不带出 · 不计榜）"
        bag, safe = run["bag"], run["safe"]
        insured = self.player["runs"] <= 3 and not forfeit_safe
        recovered = [] if forfeit_safe else safe
        if extracted or insured or protected_by_doll:
            recovered = [*safe, *bag]
        fold = run["perks"].get("fold", 0) if extracted or protected_by_doll else 0
        for item in recovered:
            if fold and self.catalog.rarity(item) in RARITIES[:3] and self.catalog.definition(item["id"]).get("kind") == "material":
                item["value"] = round(item["value"] * (1 + fold))
            self.deposit(item)
        value = self._value(recovered)
        self.last_departure = {"floor": run["floor"], "extracted": extracted, "simulation": False,
                               "units": len(recovered), "value": value, "protected_by_doll": protected_by_doll}
        if extracted:
            self.player["extracts"] += 1
            self._advance_quest("extracts", 1)
            floor = run["floor"]
            self.db.execute(
                "UPDATE season_stats SET best_extract_value=CASE WHEN best_extract_floor<? THEN ? "
                "WHEN best_extract_floor=? THEN max(best_extract_value,?) ELSE best_extract_value END, "
                "best_extract_floor=max(best_extract_floor,?) WHERE month=? AND user_id=?",
                (floor, value, floor, value, floor, self.month, self.uid),
            )
            lead = f"{reason}，" if reason else "电梯门哐地合上，"
            prefix = f"🚪 {lead}稳稳撤回地面！（第 {run['floor']} 层安全撤离）"
        elif protected_by_doll:
            prefix = f"🎭 第 {run['floor']} 层战败。替身娃娃已消耗，本局物资全部保全。"
        else:
            if forfeit_safe:
                prefix = f"💀 第 {run['floor']} 层裁决阵亡，背包与安全箱物品均未带回。"
            elif insured:
                prefix = f"💀 第 {run['floor']} 层翻车了！好在萌新防爆保险兜底，身上家当全保住了。"
            elif recovered:
                prefix = f"💀 贪心翻车在第 {run['floor']} 层！好在安全箱替你保住了最贵的那几件。"
            else:
                prefix = f"💀 贪心翻车在第 {run['floor']} 层，安全箱空空如也，这趟白跑了……"
            loss = self._value(bag) if not insured else 0
            if loss >= 300:
                self.db.execute(
                    "INSERT INTO relics(group_id,owner_id,owner_name,map_id,floor,items,value,expires_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (self.gid, self.uid, self.player["nickname"], run["map"], run["floor"], encode(bag), loss,
                     self.at + RELIC_LIFETIME),
                )
                prefix += f"\n📦 原地掉下一个估值 {loss:,} 金的遗物盒（24 小时内群友路过可舔可捞）"
            self._stat("death_value", loss + (self._value(safe) if forfeit_safe else 0))
        self.db.execute(
            "UPDATE season_stats SET floor_value=CASE WHEN max_floor<? THEN ? WHEN max_floor=? "
            "THEN max(floor_value,?) ELSE floor_value END, max_floor=max(max_floor,?) WHERE month=? AND user_id=?",
            (run["floor"], value if extracted else 0, run["floor"], value if extracted else 0,
             run["floor"], self.month, self.uid),
        )
        self._record_map_progress(run, value if extracted else None)
        self._clear_event()
        self.run = None
        summary = f"带回 {len(recovered)} 件入库，合计 {value:,} 金。" if recovered else "这趟空手回来了，好歹人还在。"
        if forfeit_safe:
            summary = "本局失去的物品未入库。"
        text = f"{prefix}\n{summary}"
        if extracted:
            text += self._tip(TIP_LOBBY, "发「电梯」回大厅逛商店/仓库，发「陈列室」看大金墙。")
        return text

    def _record_map_progress(self, run: dict, extracted_value: int | None = None) -> None:
        floor = run["floor"]
        exit_floor = floor if extracted_value is not None else 0
        value = extracted_value if extracted_value is not None else 0
        self.db.execute(
            """INSERT INTO season_map_stats(month,user_id,map_id,max_floor,floor_value,best_extract_floor,best_extract_value)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(month,user_id,map_id) DO UPDATE SET
                 floor_value=CASE WHEN excluded.max_floor>max_floor THEN excluded.floor_value
                                  WHEN excluded.max_floor=max_floor THEN max(floor_value,excluded.floor_value)
                                  ELSE floor_value END,
                 max_floor=max(max_floor,excluded.max_floor),
                 best_extract_value=CASE WHEN excluded.best_extract_floor>best_extract_floor THEN excluded.best_extract_value
                                         WHEN excluded.best_extract_floor=best_extract_floor THEN max(best_extract_value,excluded.best_extract_value)
                                         ELSE best_extract_value END,
                 best_extract_floor=max(best_extract_floor,excluded.best_extract_floor)""",
            (self.month, self.uid, run["map"], floor, value, exit_floor, value),
        )

    def _stat(self, column: str, value: int) -> None:
        if column not in {"rob_value", "rescues", "death_value"}:
            raise ValueError("unknown season stat")
        self.db.execute(f"UPDATE season_stats SET {column}={column}+? WHERE month=? AND user_id=?",
                        (value, self.month, self.uid))

    def actions(self) -> tuple[Choice, ...]:
        run = self.run
        if run is None:
            return (Choice(f"下潜({self.player['tickets']}票)", "start"), Choice("模拟战(免费)", "simulate"),
                    Choice("仓库", "warehouse"), Choice("商店", "shop"))
        if run.get("field_offer"):
            return self._field_offer_choices()
        pending = run["event"]
        withdraw = Choice("撤离", "extract")
        if pending["kind"] == "combat":
            enemy = pending["enemy"]
            if enemy.get("boss"):
                choices = [Choice("挑战首领","fight"),Choice("局内道具","field"),Choice("用药","use")]
                if not run["sos_used"] and not run.get("sim") and not run.get("prophecy") and not self._signal_active():
                    choices.append(Choice("摇人","flare"))
                if not enemy["ambush"]: choices.append(withdraw)
                return tuple(choices)
            if self._signal_active():
                waiting = (Choice("背包", "bag"), Choice("存箱", "save_box"), Choice("用药", "use"))
                return waiting if enemy["ambush"] else (*waiting, withdraw)
            result = [Choice("硬刚（打）", "fight")]
            if (enemy["elite"] or enemy["ambush"]) and not run["sos_used"] and not run.get("prophecy"):
                result.append(Choice("摇人", "flare"))
            if not enemy["ambush"]:
                result.append(Choice(f"溜走（{combat.flee_rate(run):.0%}）", "flee"))
            if len(result) < 3 or enemy["ambush"]:
                result.append(Choice("用药", "use"))
            if not enemy["ambush"]:
                result.append(withdraw)
            return tuple(result)
        if pending["kind"] == "relic":
            return (Choice("舔包（装入临时背包）", "rob"), Choice("捞尸（寄还并领取赏金）", "rescue"),
                    Choice("离开", "leave"), withdraw)
        if pending["kind"] == "choice":
            event = self.encounter_events()[pending["id"]]
            options = tuple(Choice(option["label"], "event", str(index))
                            for index, option in enumerate(event["choices"], 1))
            return (*options, withdraw) if len(options) < 4 else options
        total = self._value(run["bag"] + run["safe"])
        return (Choice("深入", "down"), Choice(f"撤离({total:,}金)", "extract"),
                Choice(f"连战{self.rush_floors}层", "rush"), Choice("存箱", "save_box"), Choice("用药", "use"))

    def _loot_selection(self, items: list[dict]) -> list[ItemChoice]:
        return [
            ItemChoice(str(index), f"{self.catalog.name(item)}（{item['value']:,} 金）",
                       self._item_names(item["id"]), item)
            for index, item in enumerate(items, 1)
        ]

    def _item_names(self, key: str) -> tuple[str, ...]:
        definition = self.catalog.definition(key)
        return (key, definition["name"], *definition.get("aliases", ()))

    def _pick(self, query: str, options: list[ItemChoice], action: str, heading: str) -> ItemChoice | Reply:
        if not options:
            return self.reply("暂时没有可选的物品。", count_cooldown=False)
        query = query.strip().casefold()
        keys = [option for option in options if query and query == option.reference.casefold()]
        if keys:
            return keys[0]
        exact = [option for option in options if query in (name.casefold() for name in option.names)]
        matches = exact or [option for option in options if query and any(query in name.casefold() for name in option.names)]
        if len(matches) == 1:
            return matches[0]
        if not matches and query:
            for option in options:
                display = option.names[1] if len(option.names) > 1 else option.label
                windows = [display[i:i + len(query)] for i in range(max(1, len(display) - len(query) + 1))]
                if difflib.get_close_matches(query, [*windows, *option.names], n=1, cutoff=0.5 if len(query) > 1 else 0.75):
                    matches.append(option)
            if matches:
                heading = f"没对上「{query}」，是不是下面这些？"
            else:
                heading = f"没对上「{query}」，可以从这里选。"
        visible = (matches or options)[:5]
        return self.reply(
            heading, tuple(Choice(option.label, action, option.reference) for option in visible),
            plain=False, multiline_menu=True, count_cooldown=False,as_image=True,forward_title="物品选择",
        )

    def _capacity(self) -> int:
        return self.player["safebox"] + self.run["safe_extra"]

    def _can_protect(self, item: dict) -> bool:
        return not self.catalog.definition(item["id"]).get("temporary_key")

    def atlas(self, _text: str, _target: str) -> Reply:
        owned=set(self.archives).intersection(self.catalog.collections)
        lines=expedition.atlas_lines(self.catalog.progression,owned,len(self.catalog.collections))
        return self.reply("\n".join(lines),(Choice("陈列室","gallery"),Choice("首领规则","help","首领")),
                          plain=True,as_image=True,forward_title="图鉴进度与虹彩")

    def field_guide(self, text: str, _target: str) -> Reply:
        scopes={"通用":"all",**{m["name"]:k for k,m in self.catalog.maps.items()}}
        name,_,page_no=text.strip().partition(" ")
        area=scopes.get(name) or (self._resolve_map_key(name) if name else "")
        if not area:
            shared = sum(item["map"] == "all" for item in self.catalog.field_items.values())
            return self.reply("🎒 井道拾荒 · 局内道具手册"
                              f"\n全部地图共享 {shared} 种道具，各图另有专属道具。它们独立掉落，不占宝箱名额。"
                              f"\n首层、首领前一层、首领房必定摸到 1 件，其余楼层 {expedition.FIELD_CHANCE:.0%} 顺手捡到。"
                              f"回血、吸血和进层回血道具的抽取权重 ×{expedition.FIELD_HEAL_WEIGHT:g}，"
                              f"已佩戴的同款临装权重 ×{expedition.FIELD_EQUIPPED_WEIGHT:g}。离开电梯后，局内道具会清空。",
                              tuple(Choice(label,"field_guide",label) for label in scopes),
                              plain=True,as_image=True,forward_title="局内道具手册")
        label=next(s for s,k in scopes.items() if k==area)
        entries=[item for item in self.catalog.field_items.values() if item["map"]==area]
        page=_ItemPage.select(page_no,len(entries))
        lines=[page.heading(f"🎒 {label} · 局内道具"),
               *[f"• {i['name']} · {expedition.field_description(i)}" for i in entries[page.rows]]]
        return self.reply("\n".join(lines),page.navigation("field_guide",label+" ")+(Choice("返回分类","field_guide"),),
                          plain=True,as_image=True,forward_title="局内道具图鉴")

    def field_bag(self, text: str, _target: str) -> Reply:
        if not self.run: return self.field_guide(text,"")
        run=self.run
        items=self.catalog.field_items
        if text.startswith("解除 "):
            key, slot = self._field_target(text[3:])
            if key is None:
                raise GameError("找不到正在佩戴或生效的这件道具。")
            try:
                message = expedition.remove_field(run, key, items, slot=slot, note_limit=self.note_limit)
            except ValueError as exc:
                raise GameError(str(exc)) from exc
            return self.reply(message)
        if run.get("field_offer"):
            return self.reply(self._field_offer_text())
        keys=[k for k,n in run.get("field_items",{}).items() if n and k in items]
        page=_ItemPage.select(text,len(keys))
        lines=[page.heading(f"🎒 {self._map_title()} · 本局临时道具"),"离开电梯即清空，该拍就拍别舍不得！"]
        lines.extend(self._field_slot_lines())
        if (scale := run.get("field_damage_multiplier", 1)) > 1:
            lines.append(f"当前伤害结算 · 对怪物 ×{scale:g} · 承受怪物伤害 ×{scale:g}")
        lines.extend(f"生效中 · {items[k]['name']} 至 F{end}（含）" for k,end in run.get("field_buffs",{}).items())
        for mode in ("guard", "burst"):
            if charge := run.get(mode):
                name, _ = expedition.field_charge_info(items[charge["id"]], charge)
                lines.append(f"{name} · 剩余 {charge['left']} 次")
        choices=[]
        for k in keys[page.rows]:
            item=items[k]
            lines.append(f"• {item['name']} ×{run['field_items'][k]} · {expedition.field_description(item)}")
            verb = "装备" if item["mode"] == "wear" else "使用"
            label = "发动公正裁决" if item.get("judgement") else f"{verb} {item['name']}"
            choices.append(Choice(label, "field_use", k))
        if not keys: lines.append("临时兜里空空如也，继续往深处摸几层看看吧。")
        return self.reply("\n".join(lines),tuple(choices)+page.navigation("field")+(Choice("道具手册","field_guide"),),
                          plain=True,as_image=True,forward_title="本局道具")

    def use_field(self, text: str, _target: str) -> Reply:
        run=self._check_run()
        if not text.strip(): return self.field_bag("","")
        items=self.catalog.field_items
        key, slot = self._field_target(text)
        if key is None:
            raise GameError("发「局内道具」查看已拾取的名字和使用选项。")
        if key == expedition.WISH:
            return self._preview_wish()
        if items[key].get("judgement"):
            return self._use_judgement(key)
        if items[key]["mode"] == "wear" and run.get("field_items", {}).get(key, 0) and slot is None:
            choices = self.field_equip_options(key)
            if len(choices) > 1:
                return self.reply(f"选一个槽位换上【{items[key]['name']}】，也可以保留在临时兜里。\n"
                                  + "\n".join(self._field_slot_lines()), choices, count_cooldown=False)
        try:
            message = expedition.activate(run, key, items, slot=slot, note_limit=self.note_limit)
        except ValueError as exc:
            raise GameError(str(exc)) from exc
        return self.reply(message)

    def _wish_cost(self):
        enemy=self._check_enemy()
        run=self.run
        if run.get("field_offer"): raise GameError(self._field_offer_text())
        if enemy["hp"]<=0 or not run.get("field_items",{}).get(expedition.WISH):
            raise GameError("需持有临装并面对存活的敌人。")
        if enemy.get("boss") and not run.get("sim") and not run.get("heirloom_focus"):
            raise GameError("本局需先选定两套追寻祝福。")
        bag=[i for i in run["bag"] if i.get("run_pickup")]
        safe=[i for i in run["safe"] if i.get("run_pickup")]
        fingerprint=sha256(encode([run["field_seed"],run["floor"],enemy,bag,safe]).encode()).hexdigest()
        return fingerprint, {"enemy":enemy["name"],"bag":len(bag),"safe":len(safe),"value":self._value(bag+safe)}

    def _preview_wish(self) -> Reply:
        fingerprint,cost=self._wish_cost()
        pending=self.run.get("wish_pending",{})
        if pending.get("hash")!=fingerprint:
            pending={"hash":fingerprint,"token":secrets.token_hex(8)}
            self.run["wish_pending"]=pending
        self.last_wish_preview={**cost,"token":pending["token"]}
        items = (f"清空背包 {cost['bag']} 件、安全箱 {cost['safe']} 件本局拾取，估值 {cost['value']:,} 金。"
                 if cost["bag"] or cost["safe"] else "背包和安全箱没有本局拾取。")
        return self.reply(f"确认发动【为了我所期待的】？\n目标　{cost['enemy']}\n{items}"
                          "消耗 1 件临装并立即击杀目标，之后正常结算奖励。",
                          (Choice("确认清空并击杀","wish_confirm",pending["token"]),Choice("取消","wish_cancel")),
                          plain=True,count_cooldown=False)

    def wish_cancel(self,text: str,_target: str) -> Reply:
        self._check_run().pop("wish_pending",None)
        return self.reply("已取消发动。",count_cooldown=False)

    def wish_confirm(self,text: str,_target: str) -> Reply:
        fingerprint,cost=self._wish_cost()
        pending=self.run.get("wish_pending",{})
        if not text or pending.get("token")!=text or pending.get("hash")!=fingerprint:
            reply=self._preview_wish()
            reply.text="目标或清单已变化，请重新核对。\n"+reply.text
            return reply
        run=self.run
        run.pop("wish_pending")
        for container in ("bag","safe"):
            run[container]=[item for item in run[container] if not item.get("run_pickup")]
        stash=run["field_items"]
        stash[expedition.WISH]-=1
        if not stash[expedition.WISH]: del stash[expedition.WISH]
        enemy=run["event"]["enemy"]
        report=combat.FightResult(player_start_hp=run["hp"],enemy_start_hp=enemy["hp"],total_dealt=enemy["hp"])
        enemy["hp"]=0
        line=(f"【为了我所期待的】已消耗临装 ×1，清空背包 {cost['bag']} 件、安全箱 {cost['safe']} 件本局拾取，"
              f"估值 {cost['value']:,} 金，立即击杀 {cost['enemy']}。")
        report.log.append(line)
        self._record_battle_log(enemy,report)
        return self.reply(line+"\n【本次击杀奖励】\n"+self._enemy_win(enemy))

    def _use_judgement(self, key: str) -> Reply:
        run = self.run
        if run.get("field_offer"):
            raise GameError(self._field_offer_text())
        stash = run.get("field_items", {})
        if not stash.get(key, 0):
            raise GameError("这局还没有拾到概念集合体-公正。")
        enemy = self._check_enemy()
        if enemy["hp"] <= 0:
            raise GameError("当前怪物已经倒下，无法发动公正裁决。")
        stash[key] -= 1
        if not stash[key]:
            del stash[key]

        report = combat.FightResult(player_start_hp=run["hp"], enemy_start_hp=enemy["hp"])
        if self.rng.random() < expedition.JUDGEMENT_DEATH_CHANCE:
            report.total_taken = run["hp"]
            run["hp"] = 0
            text = "⚖️ 公正裁决落在你身上。你当场阵亡，替身娃娃和新手保险未能救场。"
            report.log.append(text)
            self._record_battle_log(enemy, report)
            return self.reply(text + "\n" + self.finish_run(False, forfeit_safe=True))
        report.total_dealt = enemy["hp"]
        enemy["hp"] = 0
        text = f"⚖️ 公正裁决落在【{enemy['name']}】身上。怪物当场倒下。"
        report.log.append(text)
        self._record_battle_log(enemy, report)
        return self.reply(text + "\n" + self._enemy_win(enemy))

    def _field_target(self, text: str) -> tuple[str | None, int | None]:
        text = text.strip()
        name, _, suffix = text.rpartition(" ")
        slot = int(suffix) if name and suffix in ("1", "2") else None
        query = name if slot is not None else text
        key = next((k for k, item in self.catalog.field_items.items() if query in (k, item["name"])), None)
        return key, slot

    def _field_slot_lines(self) -> list[str]:
        lines = []
        for slot, key in enumerate(expedition.field_slots(self.run), 1):
            item = self.catalog.field_items.get(key)
            detail = f"{item['name']}（{expedition.field_description(item)}）" if item else "空"
            lines.append(f"临装槽 {slot} · {detail}")
        return lines

    def field_equip_options(self, key: str, *, floor: int | None = None) -> tuple[Choice, ...]:
        worn = expedition.field_slots(self.run)
        items = self.catalog.field_items
        choices = []
        for slot in expedition.field_choices(self.run, key):
            existing = items.get(worn[slot - 1])
            label = f"替换槽 {slot} · {existing['name']}" if existing else f"装备到槽 {slot}"
            choice = Choice(label, "field_pick", f"{floor}:{key}:{slot}") if floor is not None else (
                Choice(label, "field_use", f"{key} {slot}"))
            choices.append(choice)
        return tuple(choices)

    def _field_offer_choices(self) -> tuple[Choice, ...]:
        offer = self.run["field_offer"]
        floor, key = offer["floor"], offer["id"]
        return (*self.field_equip_options(key, floor=floor), Choice("放弃新装备", "field_pick", f"{floor}:{key}:0"))

    def _field_offer_text(self) -> str:
        item = self.catalog.field_items[self.run["field_offer"]["id"]]
        return "\n".join([f"🎽 发现临装【{item['name']}】。请先选择装备或放弃。",
                          expedition.field_description(item), *self._field_slot_lines()])

    def pick_field(self, text: str, _target: str) -> Reply:
        run = self._check_run()
        floor, _, remainder = text.partition(":")
        key, _, slot = remainder.partition(":")
        if not floor.isascii() or not floor.isdecimal() or len(floor) > 12 or slot not in ("0", "1", "2"):
            raise GameError("请选择当前临装菜单里的选项。")
        try:
            message = expedition.choose_field(run, key, int(floor), int(slot), self.catalog.field_items,
                                              note_limit=self.note_limit)
        except ValueError as exc:
            raise GameError(str(exc)) from exc
        return self.reply(message)

    def _best_item(self) -> dict:
        eligible = [item for item in self.run["bag"] if self._can_protect(item)]
        if not eligible:
            raise GameError("背包空空如也，先摸点货再锁安全箱吧。")
        return max(eligible, key=lambda item: item["value"])

    def bag(self, text: str, _target: str) -> Reply:
        if self.run is None:
            return self.warehouse(text, "")
        items, safe = self.run["bag"], self.run["safe"]
        page = _ItemPage.select(text, len(items))
        label = f"🎒 {self._map_title()} · 背包 {len(items)} 件"
        rows = [f"{index}. {self.catalog.name(item)} · {item['value']:,} 金"
                for index, item in enumerate(items[page.rows], page.rows.start + 1)]
        rows += [f"🔒 安全箱 {len(safe)}/{self._capacity()} · {self._items_text(safe, 2)}"]
        rows += [f"临时道具 {sum(self.run.get('field_items',{}).values())} 件 · 发「局内道具」查看"]
        opts = tuple(c for c in self.actions() if c.action != "bag")
        if page.number < page.total:
            opts = opts[:3]
        return self.reply(
            "\n".join([page.heading(label), *rows]),
            page.navigation("bag", next_label="背包下一页") + (Choice("局内道具","field"),) + opts, plain=True,
            count_cooldown=False,as_image=True,forward_title="局内背包",
        )

    def save_box(self, text: str, _target: str) -> Reply:
        run = self._check_run()
        if text:
            options = [item for item in self._loot_selection(run["bag"]) if self._can_protect(item.item)]
            if not options:
                raise GameError("背包里还没有能放进安全箱的东西。")
            chosen = self._pick(text, options, "save_box", "想锁住哪一件？")
            if isinstance(chosen, Reply):
                return chosen
            item = chosen.item
        else:
            item = self._best_item()
        if len(run["safe"]) >= self._capacity():
            self._note_safebox_full()
            lowest = min(run["safe"], key=lambda thing: thing["value"])
            body = f"🔒 安全箱已满，当前最低估值的是 {self.catalog.name(lowest)}（{lowest['value']:,} 金）。"
            if item["value"] > lowest["value"]:
                body += f"\n{self.catalog.name(item)}值 {item['value']:,} 金，回「换」可保护手头最贵的一件。"
                return self.reply(body, (Choice("换最贵的", "swap_box"), *self.actions()[:2]), count_cooldown=False)
            return self.reply(body, count_cooldown=False)
        run["bag"].remove(item)
        run["safe"].append(item)
        if len(run["safe"]) >= self._capacity():
            self._note_safebox_full()
        return self.reply(f"🔒 咔哒一声锁进安全箱：{self.catalog.name(item)}（{item['value']:,} 金），这把翻车也不亏了。")

    def _note_safebox_full(self) -> None:
        self.note_limit(f"本局安全箱 {self._capacity()} 格已用满，取出或替换一件后才能放入其他货物。")

    def swap_box(self, text: str, _target: str) -> Reply:
        run = self._check_run()
        if not run["safe"]:
            return self.save_box(text, "")
        if not text:
            item = self._best_item()
            previous = min(run["safe"], key=lambda thing: thing["value"])
            if item["value"] <= previous["value"]:
                raise GameError("箱子里已经是值钱的那几件。也能发「换箱 新物品 旧物品」指定替换。")
        else:
            parts = text.split("|") if "|" in text else text.split(maxsplit=1)
            if len(parts) != 2:
                raise GameError("可发「换」自动换入最贵物品，或发「换箱 新物品 | 旧物品」指定。")
            new_query, old_query = parts
            new = self._pick(new_query, self._loot_selection(run["bag"]), "save_box", "背包里选哪件？")
            old = self._pick(old_query, self._loot_selection(run["safe"]), "bag", "安全箱里选哪件？")
            if isinstance(new, Reply) or isinstance(old, Reply):
                raise GameError("请用「背包」核对新旧物品，再输入「换箱 新物品 | 旧物品」。")
            item, previous = new.item, old.item
            if not self._can_protect(item):
                raise GameError("临时任务钥匙只能放在背包。")
        run["safe"][run["safe"].index(previous)] = item
        run["bag"][run["bag"].index(item)] = previous
        return self.reply(f"🔒 已保护 {self.catalog.name(item)}，并取回 {self.catalog.name(previous)}。")

    def take_box(self, text: str, _target: str) -> Reply:
        run = self._check_run()
        if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= len(run["safe"]):
            raise GameError("请重新选择安全箱里的物品。")
        item = run["safe"].pop(int(text) - 1)
        run["bag"].append(item)
        return self.reply(f"已从安全箱取回 {self.catalog.name(item)}，物品已放入背包。")

    def _box(self) -> sqlite3.Row | None:
        run = self._check_run()
        if run["event"]["kind"] != "relic":
            raise GameError("附近没有遗物盒。")
        box = self.db.execute(
            "SELECT * FROM relics WHERE box_id=? AND claimed=0 AND expires_at>? AND reserved_by=? AND owner_id<>?",
            (run["event"]["box_id"], self.at, self.uid, self.uid),
        ).fetchone()
        if box is None:
            return None
        return box

    def _claim_box(self, *, rescuing: bool) -> Reply:
        box = self._box()
        if not box or self.db.execute(
            "UPDATE relics SET claimed=1 WHERE box_id=? AND claimed=0 AND expires_at>? AND reserved_by=?",
            (box["box_id"], self.at, self.uid),
        ).rowcount != 1:
            self._clear_event()
            return self.reply("💨 扑了个空，盒里只剩张字条：「来晚了兄弟，已打包带走」")
        items = json.loads(box["items"])
        local = box["group_id"] == self.gid
        mention = ""
        if rescuing:
            for item in items:
                self.deposit(item, owner=box["owner_id"])
            pair = tuple(sorted((self.uid, box["owner_id"])))
            row = self.db.execute("SELECT rewards FROM rescue_pairs WHERE day=? AND first_id=? AND second_id=?",
                                  (self.day, *pair)).fetchone()
            if row and row["rewards"] >= 2:
                reward = 0
            else:
                self.db.execute("INSERT INTO rescue_pairs VALUES(?,?,?,1) ON CONFLICT(day,first_id,second_id) "
                                "DO UPDATE SET rewards=rewards+1", (self.day, *pair))
                reward = math.floor(box["value"] * self.run["perks"].get("rescue", 0.35))
                self.player["gold"] += reward
                self._stat("rescues", 1)
            self.notice(box["owner_id"], f"😇 好人一生平安！有群友帮你把掉在禁区的 {len(items)} 件家当全捞回仓库了。")
            text = f"😇 良心发现，把这 {len(items)} 件物资原封不动寄还失主了！"
            text += f" 入账 {reward:,} 金见义勇为赏金（活雷锋 +1）。" if reward else " 今天给这位失主捞尸的赏金已领满，纯做好事不留名！"
        else:
            healed_line = ""
            if self.run["perks"].get("scavenge"):
                healed_line = f"（趁火打劫回血 +{self._heal(self.run['perks']['scavenge'])} HP）"
            text = f"🦊 毫不客气！把 {len(items)} 件遗物全塞进了临时背包{healed_line}，记得活着撤离才算真正落袋：\n" + self._items_text(items) + self._give(items)
            self._stat("rob_value", box["value"])
            if local:
                mention = box["owner_id"]
                text = f"你的遗物盒刚被同群老六顺走了！\n{text}"
            else:
                self.notice(box["owner_id"], "🦊 坏消息：你掉在禁区的遗物盒被路过的老六顺走了，下次记得先锁安全箱。")
        self._clear_event()
        return self.reply(text, mention=mention)

    def rob(self, _text: str, _target: str) -> Reply:
        return self._claim_box(rescuing=False)

    def rescue(self, _text: str, _target: str) -> Reply:
        return self._claim_box(rescuing=True)

    def leave(self, _text: str, _target: str) -> Reply:
        run = self._check_run()
        if run["event"]["kind"] not in ("choice", "relic"):
            raise GameError("现在可用「深入」或「撤离」决定去向。")
        self._clear_event()
        return self.reply("🚶 稳妥起见，绕开这摊事留在当前层。")

    def event_choice(self, text: str, _target: str, *, enter_landing: bool = True) -> Reply:
        run = self._check_run()
        pending = run["event"]
        if pending["kind"] != "choice":
            raise GameError("现在没有待选择的奇遇。")
        config = self.encounter_events()[pending["id"]]
        options = config["choices"]
        if text.isascii() and text.isdigit() and 1 <= int(text) <= len(options):
            option = options[int(text) - 1]
        else:
            option = next((option for option in options if option["label"] == text), None)
        if option is None:
            raise GameError(f"正在处理「{config['name']}」，选一个底部动作吧。")
        origin = run["floor"]
        spent = option.get("cost", 0)

        def record(outcome, notes, loot=()):
            if spent:
                notes = [f"支付 {spent:,} 金。", *notes]
            self.last_adventure = {
                "name": config["name"], "option": option["label"], "floor": origin,
                "destination": run["floor"], "outcome": outcome, "lines": notes,
                "loot": [{key: item[key] for key in ("id", "value", "quality", "rarity") if key in item}
                         for item in loot],
            }
            run["adventure"] = self.last_adventure

        self._charge(option.get("cost", 0))
        hp_cost = option.get("hp_current_cost")
        if hp_cost:
            combat.hurt(run, max(1, int(run["hp"] * hp_cost)))
            if not run["hp"]:
                record("failure", ["支付奇遇的生命代价后，生命耗尽。"])
                return self.reply(self.fallen())
        ev_rng = self._world_rng(f"event:{option['label']}")
        if "guess" in option:
            success = pending["secret"] == option["guess"]
        elif option.get("agi_check") or "chance" in option:
            base = option.get("chance", 0.48 + combat.agility(run) * 0.012 - run["floor"] * 0.018)
            success = ev_rng.random() < combat.clamp(base + run["event_bonus"], 0.10, 0.94)
        else:
            success = True
        self._advance_quest("events", 1)
        if not success:
            if option.get("failure_loot"):
                consolation = self._loot(option["failure_loot"], chest=False)
                record("failure", ["没有拿到大奖，获得了安慰奖。"], consolation)
                self._clear_event()
                return self.reply(f"🎲 {config['name']} · 爪子一抖没抓稳大奖，好歹掉了个安慰奖：" + self._items_text(consolation) + self._give(consolation))
            nominal = option.get("failure_hp") or max(1, int(run["stats"]["hp"] * option.get("failure_fraction", 0)))
            lost = combat.hurt(run, nominal)
            record("failure", [f"尝试失败，损失 {lost} HP，剩余 {run['hp']}/{run['stats']['hp']} HP。"])
            if not run["hp"]:
                return self.reply(f"💥 没赌成，反倒结结实实挨了一记（-{lost} HP）……\n" + self.fallen())
            self._clear_event()
            return self.reply(f"💥 手慢半拍扑了个空，还蹭掉 {lost} HP（剩 {run['hp']}/{run['stats']['hp']} HP）。")

        lines = [f"🎲 {config['name']} · {option['label']}"]
        items = []
        if option.get("buy_offer"):
            self._charge(pending["price"])
            spent += pending["price"]
            items.append(pending["offer"])
        if option.get("loot"):
            items.extend(self._loot(option["loot"], chest=True, minimum=option.get("min_rarity", "white")))
        if option.get("random_supply"):
            picked = ev_rng.choice(option["random_supply"])
            items.append(self.catalog.ordinary_item(picked, ev_rng))
        items += [self.catalog.ordinary_item(key, ev_rng) for key in option.get("items", ())]
        if items:
            lines.append("🎁 到手：" + self._items_text(items) + self._give(items))
        notes = []
        if option.get("heal"):
            healed = self._heal(option["heal"], report_limit=True)
            notes.append(f"💚 缓过一口气，恢复 {healed} HP。")
        if option.get("grant_shield") and not run.get("burst",{}).get("blood"):
            run["shield"] = int(run.get("shield", 0)) + int(option["grant_shield"])
            notes.append(f"🛡️ 领到一层临时护盾 +{option['grant_shield']}（当前盾 {run['shield']}）。")
        if option.get("atk_boost"):
            run["atk_boost"] = min(EVENT_ATK_CAP, round(run.get("atk_boost", 0.0) + option["atk_boost"], 2))
            if run["atk_boost"] >= EVENT_ATK_CAP:
                self.note_limit(f"本局奇遇攻击加成已达 +{EVENT_ATK_CAP:.0%} 上限，再叠同类奇遇也不会增加。")
            notes.append(f"⚡ 改装成功！本局攻击累计 +{int(run['atk_boost'] * 100)}%。")
        if option.get("rare_boost"):
            run["rare_bonus"] = round(run.get("rare_bonus", 0.0) + option["rare_boost"], 2)
            notes.append(f"🙏 香火显灵！本局大金爆率额外 +{int(option['rare_boost'] * 100)}%。")
            self._note_rare_limit(chest=True)
        if option.get("hint"):
            hint = self._peek()
            notes.append(f"📡 顺手摸清了底细：下一层多半是「{hint}」。")
        if option.get("buff_loot"):
            run["lockpick"] = True
            notes.append("✨ 功德到位，下一个宝箱品质保底升一档！")
        if hp_cost:
            notes.append(f"当前 HP {run['hp']}/{run['stats']['hp']}。")
        lines.extend(notes)
        self._clear_event()
        if option.get("skip_floors"):
            target_floor = expedition.landing(run["floor"], int(option["skip_floors"]))
            if run.get("prophecy"):
                target_floor = min(prophecy.MAX_FLOOR, target_floor)
            run["floor"] = target_floor
            lines.append(f"🚀 直降梯轰鸣下坠，一口气跳到第 {run['floor']} 层！")
            if enter_landing:
                lines.append(self._enter_floor())
        if not notes and not items:
            notes.append("找到捷径，电梯继续下行。" if run["floor"] != origin else "奇遇结束，电梯门已开启。")
        record("success", notes, items)
        return self.reply("\n".join(lines))

    def _check_flare(self) -> dict:
        if self._is_prophecy():
            raise GameError(f"【{prophecy.TITLE}】为单人实力试炼，不可摇人支援。")
        if self.gid.startswith("private:"):
            raise GameError("可以在已开放电梯的群里使用信号弹求援。道具已保留。")
        enemy = self._check_enemy()
        if not enemy["elite"]:
            raise GameError("信号弹留给精英或守门怪，普通怪可以打或跑。")
        if self._signal_active():
            raise GameError("上一发信号弹还亮着，本群群友可以发「支援」。")
        return enemy

    def _send_flare(self, *, powerful: bool) -> Reply:
        enemy = self._check_flare()
        if self.run.get("sim"):
            raise GameError("🧪 模拟战为单人演练模式，无法发射真实求救信号弹。")
        self.db.execute(
            "INSERT INTO signals(owner_id,group_id,expires_at,capacity) VALUES(?,?,?,?) "
            "ON CONFLICT(owner_id) DO UPDATE SET group_id=excluded.group_id,expires_at=excluded.expires_at,"
            "capacity=excluded.capacity,helpers='[]'",
            (self.uid, self.gid, self.at + SIGNAL_LIFETIME, 3 if powerful else 1),
        )
        if not powerful:
            self.run["sos_used"] = True
        return self.reply(f"🚨 {self.player['nickname']} 在电梯里拉响了求救信号！面前是【{enemy['name']}】（剩 {enemy['hp']} HP）\n"
                          "3 分钟内谁发个「支援」或「救」，就能免费空降补一刀领盲盒！")

    def flare(self, _text: str, _target: str) -> Reply:
        self._check_flare()
        if self.run["sos_used"]:
            raise GameError("本局免费信号弹已经发过。有强效信号弹时，可以发「使用 信号弹」。")
        return self._send_flare(powerful=False)

    def support(self, text: str, target: str) -> Reply:
        user_id = target or (text if text.isascii() and text.isdigit() else "")
        signals = self.db.execute(
            "SELECT s.*,p.nickname FROM signals s JOIN players p ON p.user_id=s.owner_id "
            "WHERE s.group_id=? AND s.expires_at>? AND s.owner_id<>? ORDER BY s.expires_at DESC,s.rowid DESC",
            (self.gid, self.at, self.uid),
        ).fetchall()
        signal = next((row for row in signals if row["owner_id"] == user_id), None) if user_id else next(
            (row for row in signals if self.uid not in json.loads(row["helpers"])), None,
        )
        if signal is None:
            raise GameError("当前群没有可支援的信号。等群友发出「摇人」再来。")
        helpers = json.loads(signal["helpers"])
        if self.uid in helpers:
            raise GameError("你已经支援过这发信号弹了，其他群友还可以来帮忙。")
        owner = Turn(self.game, self.db, signal["owner_id"], signal["nickname"], signal["group_id"], self.at)
        enemy = owner._check_enemy()
        stats, perks = self._stats()
        damage = combat.support_damage(stats, perks, enemy, owner.run["perks"])
        enemy["hp"] = max(0, enemy["hp"] - damage)
        reward = ""
        if self.player["support_rewards"] < 5:
            self.player["support_rewards"] += 1
            self.deposit(self.catalog.ordinary_item("crate_purple" if perks.get("kindhearted") else "crate_blue", self.rng))
            reward = "\n🎁 辛苦费到手，支援盲盒已进仓库。"
            if self.player["support_rewards"] == 5:
                self.note_limit("今日 5 份支援盲盒已领满，仍可以继续支援队友。")
        else:
            reward = "\n今天的 5 份支援盲盒都领满啦，纯义气相挺！"
        helpers.append(self.uid)
        body = (
            f"🤝 空降神兵！帮 {owner.player['nickname']} 一击轰掉 {damage} 血，当场放倒【{enemy['name']}】！"
            if not enemy["hp"]
            else f"🤝 空降帮 {owner.player['nickname']} 补了一刀！轰掉 {damage} 血，【{enemy['name']}】还剩 {enemy['hp']} HP。"
        )
        if not enemy["hp"]:
            was_ambush = bool(enemy.get("ambush"))
            body += "\n" + owner._enemy_win(enemy)
            if was_ambush:
                owner.notice(owner.uid, f"🤝 群友空降帮你放倒了【{enemy['name']}】，已顺势安全撤离回大厅！")
            else:
                owner.notice(owner.uid, f"🤝 群友空降帮你放倒了【{enemy['name']}】！发「电梯」接着往下走吧。")
        elif len(helpers) >= signal["capacity"]:
            self.db.execute("DELETE FROM signals WHERE owner_id=?", (owner.uid,))
        else:
            self.db.execute("UPDATE signals SET helpers=? WHERE owner_id=?", (encode(helpers), owner.uid))
        owner.persist(touch=False)
        return self.reply(body + reward)

    def _record_discovery(self, key: str, quantity: int) -> None:
        first = key not in self.archives
        self.archives[key] = self.archives.get(key, 0) + quantity
        if first:
            count = len(self.archives.keys() & self.catalog.collections.keys())
            total = len(self.catalog.collections)
            for stage in self.catalog.progression["atlas"]:
                if count == expedition.atlas_required(stage,total):
                    self._quest_notes.append(f"📖 图鉴点亮 {count}/{total}，永久获得{expedition.stat_line(stage['stats'])}！")

    def deposit(self, item: dict, *, owner: str | None = None, quantity: int = 1, boss_reward: bool = False) -> None:
        uid = owner or self.uid
        key = item["id"]
        if (key in self.catalog.rainbow and not boss_reward) or key in self.catalog.field_items:
            raise GameError("该物品不能作为仓库货物入库。")
        if key in self.catalog.equipment:
            self.catalog.migrate_loose_gear(item)
            self.db.execute(
                "INSERT INTO equipment(user_id,base_id,rarity,stats,affixes,rules_version) VALUES(?,?,?,?,?,2)",
                (uid, key, item["rarity"], encode(item["stats"]), encode(item["affixes"])),
            )
        else:
            self.db.execute(
                "INSERT INTO inventory(user_id,item_id,quality,value,quantity) VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id,item_id,quality,value) DO UPDATE SET quantity=quantity+excluded.quantity",
                (uid, key, item["quality"], item["value"], quantity),
            )
        if key in self.catalog.collections:
            self.db.execute(
                "INSERT INTO collections VALUES(?,?,?,?) ON CONFLICT(user_id,item_id) DO UPDATE SET count=count+excluded.count",
                (uid, key, quantity, self.at),
            )
            if uid == self.uid:
                self._record_discovery(key, quantity)
            else:
                self.db.execute("INSERT OR IGNORE INTO season_stats(month,user_id) VALUES(?,?)", (self.month, uid))
                self.db.execute(
                    "UPDATE season_stats SET collection_count=(SELECT count(*) FROM collections WHERE user_id=?) "
                    "WHERE month=? AND user_id=?", (uid, self.month, uid),
                )

    def _stock(self) -> list[dict]:
        return _player_stock(self.db, self.uid)

    def _stock_options(self, items: list[dict]) -> list[ItemChoice]:
        return [ItemChoice(
            f"#{item['stock_id']}",
            f"{self.catalog.name(item)} ×{item['quantity']} · 单价 {item['value']:,} 金",
            (*self._item_names(item["id"]), str(index)), item,
        ) for index, item in enumerate(items, 1)]

    def _remove_stock(self, item: dict, quantity: int) -> None:
        if not 0 < quantity <= item["quantity"]:
            raise GameError(f"这批物品还有 {item['quantity']} 件，换个数量吧。")
        if quantity == item["quantity"]:
            self.db.execute("DELETE FROM inventory WHERE user_id=? AND stock_id=?", (self.uid, item["stock_id"]))
        else:
            self.db.execute("UPDATE inventory SET quantity=quantity-? WHERE user_id=? AND stock_id=?",
                            (quantity, self.uid, item["stock_id"]))

    def _matching_stock(self, key: str) -> list[dict]:
        if key in ("any_blue", "any_green"):
            grade = "blue" if key == "any_blue" else "green"
            return [item for item in self._stock() if self.catalog.rarity(item) == grade
                    and self.catalog.definition(item["id"])["kind"] == "material"]
        return [item for item in self._stock() if item["id"] == key]

    def _consume(self, key: str, quantity: int) -> list[dict]:
        rows = self._matching_stock(key)
        if not 0 < quantity <= sum(row["quantity"] for row in rows):
            name = "蓝色材料" if key == "any_blue" else ("绿色材料" if key == "any_green" else self.catalog.items[key]["name"])
            raise GameError(f"仓库里没有 {quantity} 件{name}。")
        remaining = quantity
        taken = []
        for row in rows:
            count = min(remaining, row["quantity"])
            if count:
                self._remove_stock(row, count)
                taken.extend({key: row[key] for key in ("id", "quality", "value")} for _ in range(count))
            remaining -= count
            if not remaining:
                break
        return taken

    def _match_supply_key(self, name: str) -> str:
        q = name.strip().casefold()
        if not q:
            return ""
        exact = next(
            (key for key, item in self.catalog.items.items() if item["kind"] == "supply"
             and q in (label.casefold() for label in self._item_names(key))),
            "",
        )
        if exact:
            return exact
        if len(q) >= 2:
            return next(
                (key for key, item in self.catalog.items.items() if item["kind"] == "supply"
                 and any(q in label.casefold() for label in self._item_names(key))),
                "",
            )
        return ""

    def _auto_loadout(self) -> list[dict]:
        carried: list[dict] = []
        heal_left = 2
        medicines = [k for k in self.catalog.shop if self.catalog.items.get(k,{}).get("medicine")]
        for key in ("coffee", "ice_tea", *medicines):
            avail = sum(r["quantity"] for r in self._matching_stock(key))
            take = min(avail, heal_left)
            if take > 0:
                carried.extend(self._consume(key, take))
                heal_left -= take
        for key, cap in (("smoke", 1), ("shield", 1), ("doll", 1)):
            avail = sum(r["quantity"] for r in self._matching_stock(key))
            take = min(avail, cap)
            if take > 0:
                carried.extend(self._consume(key, take))
        if not carried:
            # Fallback: take up to 2 of any other supply in stock
            for key, qty in self._warehouse_supplies()[:2]:
                carried.extend(self._consume(key, min(qty, 1)))
        if not carried:
            raise GameError("仓库里目前没有可携带的药品或道具，先去「商店」买点吧。")
        return carried

    def _take_loadout(self, arguments: list[str]) -> list[dict]:
        carried: list[dict] = []
        chunks: list[str] = []
        for raw in arguments:
            for part in raw.replace("｜", "|").replace("，", ",").replace("、", ",").replace("+", ",").split(","):
                for sub in part.split("|"):
                    if sub.strip():
                        chunks.append(sub.strip())
        for chunk in chunks:
            if chunk in ("带药", "自动", "自动带药", "全带"):
                carried.extend(self._auto_loadout())
                continue
            name, count = _quantity(chunk)
            key = self._match_supply_key(name)
            if key:
                if count <= 0:
                    raise GameError("携带数量需要正整数。")
                carried.extend(self._consume(key, count))
                continue
            # Support space-separated multiple items like "冰美式 2 烟雾弹 1" or "冰美式*2 烟雾弹"
            words = chunk.split()
            idx = 0
            while idx < len(words):
                tok = words[idx]
                if tok in ("带药", "自动", "自动带药", "全带"):
                    carried.extend(self._auto_loadout())
                    idx += 1
                    continue
                t_name, t_qty = _quantity(tok)
                t_key = self._match_supply_key(t_name)
                if not t_key:
                    raise GameError(f"没有找到可携带的「{tok}」。用「物品介绍」看道具名，或直接回复「序号 带药」。")
                if t_qty == 1 and idx + 1 < len(words) and words[idx + 1].isascii() and words[idx + 1].isdigit():
                    t_qty = int(words[idx + 1])
                    idx += 2
                else:
                    idx += 1
                if t_qty <= 0:
                    raise GameError("携带数量需要正整数。")
                carried.extend(self._consume(t_key, t_qty))
        return carried

    def use(self, text: str, _target: str) -> Reply:
        run = self._check_run()
        if text in self.catalog.field_items or any(text == v["name"] for v in self.catalog.field_items.values()):
            return self.use_field(text,_target)
        options = [option for option in self._loot_selection(run["bag"])
                   if self.catalog.definition(option.item["id"]).get("kind") == "supply"]
        if not options:
            if not text and run.get("field_items"):
                return self.field_bag("","")
            raise GameError("这次没带消耗品。可先撤离，到商店买药，再用「下潜 地图 | 冰美式 2」带进去。")
        if not text and run["hp"] < run["stats"]["hp"] * 0.7:
            medicines = [k for k, spec in self.catalog.items.items() if spec.get("medicine")
                         and (not spec["medicine"].get("combat_only") or run["event"]["kind"] == "combat")
                         and (not spec["medicine"].get("visits") or not run.get("med_regen"))]
            recovery = next((option for item_id in ("coffee", "ice_tea", *medicines) for option in options
                             if option.item["id"] == item_id), None)
        else:
            recovery = None
        chosen = recovery or self._pick(text, options, "use", "🎒 背包里可以用这些道具。")
        if isinstance(chosen, Reply):
            return chosen
        item = chosen.item
        key, label = item["id"], self.catalog.name(item, symbol=False)
        if run.get("prophecy") and key in prophecy.EXIT_ITEMS:
            raise GameError("星兆试炼内禁用直接撤离道具。")
        medicine = self.catalog.items[key].get("medicine")
        if medicine:
            return self._use_medicine(item, label, medicine)
        if run.get("field_offer") and key in {"warp_card", "smoke", "grenade", "flare"}:
            raise GameError(self._field_offer_text())
        if run.get("burst",{}).get("blood") and key in ("coffee","ice_tea","shield"):
            raise GameError("奇点蓄力中，完成下一次主攻击后再用回复或护盾。")
        if key == "doll":
            return self.reply("替身娃娃放在背包里就能护你一次。", count_cooldown=False)
        if key == "flare":
            response = self._send_flare(powerful=True)
            run["bag"].remove(item)
            return response
        run["bag"].remove(item)
        if key == "coffee":
            healed = self._heal(0.40, report_limit=True)
            return self.reply(f"☕ 一口{label}灌下去瞬间清醒，回血 {healed} HP！")
        if key == "ice_tea":
            healed = self._heal(0.25, report_limit=True)
            run["tea_until"] = max(run["tea_until"], run["floor"] + 1)
            extra = ""
            if self._world_rng("ice_tea").random() < 0.15:
                lost = combat.hurt(run, 8)
                if not run["hp"]:
                    return self.reply("红茶喝得太急，打了个要命的嗝。\n" + self.fallen())
                extra = f" 打嗝损失 {lost} HP。"
            return self.reply(f"🧃 恢复 {healed} HP，这层和下一层敏捷 +15。{extra}")
        if key == "smoke":
            enemy = self._check_enemy()
            self._clear_event()
            if enemy.get("boss"):
                return self.reply(self.finish_run(True,"甩出烟雾弹糊了首领一脸白雾，顺势拉闸溜出首领房"))
            if enemy["ambush"]:
                return self.reply(self.finish_run(True, "一颗烟雾弹糊了守门怪一脸，趁乱拉闸"))
            return self.reply("💨 啪地摔碎烟雾弹，趁白雾弥漫无伤脱身！还在当前层。")
        if key == "grenade":
            enemy = self._check_enemy()
            if enemy.get("boss"):
                if enemy.get("grenades",0) >= BOSS_GRENADE_CAP:
                    raise GameError(f"这只首领已达到 {BOSS_GRENADE_CAP} 枚脉冲雷上限，剩下的血量要靠攻击打掉。")
                if enemy["hp"] <= 1:
                    raise GameError("脉冲雷最多把首领削到 1 HP，剩下的血量要靠攻击打掉。")
                enemy["grenades"] = enemy.get("grenades",0)+1
                if enemy["grenades"] == BOSS_GRENADE_CAP:
                    self.note_limit(f"这只首领已用满 {BOSS_GRENADE_CAP} 枚脉冲雷，剩下的血量要靠攻击打掉。")
                cap = enemy["hp"] - 1
                damage = max(1, int(enemy["hp"] * .12))
            else:
                cap = enemy["hp"]
                damage = max(1, int(enemy["hp"] * .30))
            removed = min(cap, combat.scale_dealt_damage(run, damage))
            enemy["hp"] = max(0, enemy["hp"] - removed)
            result = f"💥 一发脉冲雷糊脸炸掉 {removed} HP，【{enemy['name']}】只剩 {enemy['hp']} HP！"
            if not enemy["hp"]:
                result += "\n" + self._enemy_win(enemy)
            return self.reply(result)
        if key == "lockpick":
            if run["lockpick"]:
                raise GameError("下一份宝箱已经有品质加成，这件撬锁器先留着吧。")
            run["lockpick"] = True
            return self.reply("🔓 撬锁器备好了，下一个摸金宝箱保底品质升一档！")
        if key == "radar":
            room = self._peek()
            run["radar_floor"] = run["floor"] + 1
            self.last_radar_scan = self.radar_scan()
            return self.reply(f"📡 热成像雷达 · F{run['radar_floor']} 侦测到「{room}」。")
        if key == "pouch":
            if run["safe_extra"]:
                raise GameError("本局临时箱格已经加过一次，扩容袋可以下次再用。")
            run["safe_extra"] = 1
            self.note_limit("每局只能用扩容袋加 1 格安全箱，本局已用完。")
            return self.reply(f"🔒 本局安全箱加到 {self._capacity()} 格。")
        if key == "adrenaline":
            if run.get("atk_boost", 0.0) >= ADRENALINE_ATK_CAP:
                raise GameError(f"肾上腺素最多把本局攻击加成叠到 +{ADRENALINE_ATK_CAP:.0%}，这针已保留。")
            cost = min(max(0, run["hp"] - 1), max(1, int(run["hp"] * 0.10)))
            run["hp"] -= cost
            run["atk_boost"] = min(ADRENALINE_ATK_CAP, round(run.get("atk_boost", 0.0) + 0.25, 2))
            if run["atk_boost"] >= ADRENALINE_ATK_CAP:
                self.note_limit(f"肾上腺素的攻击加成已达 +{ADRENALINE_ATK_CAP:.0%} 上限，继续使用不会增加。")
            return self.reply(f"💉 肾上腺素一针见效！掉 {cost} 血（剩 {run['hp']} HP），本局攻击已叠至 +{int(run['atk_boost'] * 100)}%！")
        if key == "shield":
            run["shield"] = int(run.get("shield", 0)) + 50
            return self.reply(f"🛡️ 纳米防爆护盾展开！当前临时护盾 {run['shield']} 点。")
        if key == "magnet":
            run["value_bonus"] = round(run.get("value_bonus", 0.0) + 0.25, 2)
            bonus_items = self._loot(1, chest=True)
            return self.reply("🧲 吸金磁铁嗡嗡启动！本局后续物资估值额外 +25%，顺手吸来：" + self._items_text(bonus_items) + self._give(bonus_items))
        if key == "warp_card":
            if run["event"]["kind"] != "ready":
                raise GameError("眼前还有没处理完的事，解决了再刷直降电梯卡。")
            target_floor = expedition.landing(run["floor"], 2)
            if run.get("prophecy"):
                target_floor = min(prophecy.MAX_FLOOR, target_floor)
            run["floor"] = target_floor
            body = self._enter_floor()
            peek = self._peek()
            return self.reply(f"🚀 滴——直降电梯卡生效，连跳两层！\n{body}\n📡 顺手扫了一眼更下面：下一层多半是「{peek}」。")
        if key == "vip_card":
            if run["gold_room"]:
                raise GameError("下一层金库已解锁，这张门禁卡留到下次。")
            run["gold_room"] = True
            return self.reply("🗝️ 滴——VIP 门禁卡刷卡成功！下一层直通隐藏金库，无怪连开三箱！")
        raise GameError("这玩意局内用不了，带回大厅仓库再折腾。")

    def _use_medicine(self, item, name, spec):
        run = self.run
        try:
            healed, shield = expedition.use_medicine(run, spec)
        except ValueError as exc:
            raise GameError(str(exc)) from exc
        run["bag"].remove(item)
        maximum = run["stats"]["hp"]
        notes = [f"回复 {healed} HP（{run['hp']}/{maximum}）"]
        if shield:
            notes.append(f"增加 {shield} 护盾")
        if spec.get("visits"):
            notes.append(f"接下来 {spec['visits']} 个新楼层继续治疗")
        if healed and run["hp"] == maximum:
            self.note_limit(expedition.health_limit_note(maximum))
        return self.reply(f"💊 {name} · " + "，".join(notes))

    def _bm_material_uses(self) -> dict[str, list[str]]:
        uses: dict[str, list[str]] = {}
        for recipe in self.catalog.recipes:
            out_name = self.catalog.items[recipe["output"]]["name"]
            for key in recipe["requires"]:
                if key not in ("any_blue", "any_green"):
                    uses.setdefault(key, []).append(out_name)
        return uses

    def _warehouse_tag(self, item: dict, bm_uses: dict[str, list[str]]) -> str:
        spec = self.catalog.items.get(item["id"], {})
        kind = spec.get("kind", "")
        if kind == "crate":
            return " [可开箱]"
        if kind == "collection":
            return " [珍藏]"
        if kind == "supply":
            return " [道具]"
        if item["id"] == "reroll_stone":
            return " [洗练]"
        if item["id"] in bm_uses:
            target = bm_uses[item["id"]][0]
            return f" [黑市→{target[:4]}]"
        return ""

    def _parse_warehouse_mode(self, text: str) -> tuple[str, str]:
        tokens = text.strip().split()
        mode_map = {
            "全部": "value", "价值": "value", "按价值": "value",
            "品质": "rarity", "按品质": "rarity", "稀有度": "rarity",
            "最新": "recent", "时间": "recent",
            "分类": "summary", "总览": "summary", "汇总": "summary",
            "盲盒": "crate", "箱子": "crate",
            "道具": "supply", "药品": "supply", "补给": "supply",
            "材料": "material", "杂物": "material",
            "珍藏": "collection", "大金": "collection", "收藏": "collection",
            "黑市": "bm", "黑市料": "bm", "配方": "bm",
        }
        mode, page_text = "value", ""
        for tok in tokens:
            if tok.isascii() and tok.isdigit():
                page_text = tok
            elif tok in mode_map:
                mode = mode_map[tok]
        return mode, page_text

    def warehouse(self, text: str, _target: str) -> Reply:
        items = self._stock()
        bm_uses = self._bm_material_uses()
        mode, page_text = self._parse_warehouse_mode(text)
        total_units = sum(item["quantity"] for item in items)
        total_val = sum(item["value"] * item["quantity"] for item in items)
        crate_cnt = sum(item["quantity"] for item in items if self.catalog.items[item["id"]]["kind"] == "crate")

        # Summary / category overview mode
        if mode == "summary":
            buckets = {
                "🎁 加密盲盒（可开箱）": [i for i in items if self.catalog.items[i["id"]]["kind"] == "crate"],
                "🧪 局内道具（可带进图）": [i for i in items if self.catalog.items[i["id"]]["kind"] == "supply"],
                "💎 珍藏": [i for i in items if self.catalog.items[i["id"]]["kind"] == "collection"
                          and i["id"] not in self.catalog.rainbow],
                "🌈 虹彩藏品": [i for i in items if i["id"] in self.catalog.rainbow],
                "🔧 黑市配方材料（留着换货）": [i for i in items if i["id"] in bm_uses],
                "📦 其他普通材料（可卖钱）": [
                    i for i in items
                    if self.catalog.items[i["id"]]["kind"] == "material" and i["id"] not in bm_uses
                ],
            }
            equipped = set(json.loads(self.player["loadout"]).values())
            spare_eq = [
                r for r in self.db.execute("SELECT equip_id,rarity FROM equipment WHERE user_id=?", (self.uid,))
                if r["equip_id"] not in equipped
            ]
            lines = [
                f"📦 全服仓库 · 分类总览（共 {total_units} 件 · 总估 {total_val:,} 金）",
                f"💰 当前钱包：{self.player['gold']:,} 金",
            ]
            for label, arr in buckets.items():
                cnt = sum(i["quantity"] for i in arr)
                val = sum(i["value"] * i["quantity"] for i in arr)
                lines.append(f"• {label}：{cnt} 件（约 {val:,} 金）")
            spare_val = sum(self.catalog.gear_sell.get(r["rarity"], 260) for r in spare_eq)
            lines.append(f"• 🧰 闲置未穿装备：{len(spare_eq)} 件（可卖约 {spare_val:,} 金）")
            lines.append("💡 切视图发「仓库 盲盒/道具/材料/珍藏/黑市/品质」；卖任意物品发「卖 #编号 [数量/全部]」。")
            choices: list[Choice] = []
            if crate_cnt:
                choices.append(Choice(f"开盲盒({crate_cnt}个)", "decode"))
            choices.extend((
                Choice("看全部(按价值)", "warehouse", "价值"),
                Choice("看道具", "warehouse", "道具"),
                Choice("看材料", "warehouse", "材料"),
                Choice("看黑市料", "warehouse", "黑市"),
                Choice("卖白绿杂物", "sell", "杂物"),
            ))
            return self.reply("\n".join(lines), tuple(choices), plain=False, as_image=True, forward_title="跨群仓库")

        # Filter & sort items according to view mode
        mode_titles = {
            "value": "按价值", "rarity": "按品质", "recent": "最新入库",
            "crate": "加密盲盒", "supply": "局内道具", "material": "材料",
            "collection": "收藏品", "bm": "黑市材料",
        }
        mode_prefix_arg = {
            "value": "", "rarity": "品质 ", "recent": "最新 ",
            "crate": "盲盒 ", "supply": "道具 ", "material": "材料 ",
            "collection": "珍藏 ", "bm": "黑市 ",
        }
        filtered = list(items)
        if mode == "crate":
            filtered = [i for i in items if self.catalog.items[i["id"]]["kind"] == "crate"]
        elif mode == "supply":
            filtered = [i for i in items if self.catalog.items[i["id"]]["kind"] == "supply"]
        elif mode == "material":
            filtered = [i for i in items if self.catalog.items[i["id"]]["kind"] == "material"]
        elif mode == "collection":
            filtered = [i for i in items if self.catalog.items[i["id"]]["kind"] == "collection"]
        elif mode == "bm":
            filtered = [i for i in items if i["id"] in bm_uses]

        stock_grades = (*RARITIES, "rainbow")
        if mode == "rarity":
            filtered.sort(key=lambda i: (stock_grades.index(self.catalog.rarity(i)), i["value"] * i["quantity"]), reverse=True)
        elif mode == "recent":
            filtered.sort(key=lambda i: i["stock_id"], reverse=True)
        else:
            filtered.sort(key=lambda i: (i["value"] * i["quantity"], stock_grades.index(self.catalog.rarity(i))), reverse=True)

        options = self._stock_options(filtered)
        page = _ItemPage.select(page_text, len(options), page_size=12)
        head = f"📦 全服仓库（{mode_titles[mode]}）· {total_units} 件（总估 {total_val:,} 金）· 钱包 {self.player['gold']:,} 金"
        rows: list[str] = []
        for entry in options[page.rows]:
            it = entry.item
            tag = self._warehouse_tag(it, bm_uses)
            if mode == "supply":
                brief = self._item_brief(it["id"])
                rows.append(f"{entry.reference} {self.catalog.name(it)} ×{it['quantity']} · {it['value']:,}金｜{brief}")
            else:
                total_item = it["value"] * it["quantity"]
                sub_val = f"单{it['value']:,}/共{total_item:,}金" if it["quantity"] > 1 else f"{it['value']:,}金"
                rows.append(f"{entry.reference} {self.catalog.name(it)} ×{it['quantity']} · {sub_val}{tag}")
        if not rows:
            rows = ["这个分类下暂时空空如也。"]

        tip = "💡 卖货：「卖 #编号 [数量/全部]」「卖 杂物 / 非黑市蓝 / 紫 / 绿装」；开箱：「开箱」或「开箱 全部」；切视图：「仓库 分类/品质/盲盒/道具/材料/黑市」。"
        nav_choices: list[Choice] = []
        if mode == "crate" and crate_cnt:
            nav_choices.append(Choice(f"一键全开({crate_cnt}个)", "decode", "全部"))
            nav_choices.append(Choice("单开 1 个", "decode"))
        elif crate_cnt:
            nav_choices.append(Choice(f"开盲盒({crate_cnt}个)", "decode"))
        nav_choices.append(Choice("卖白绿杂物", "sell", "杂物"))
        nav_choices.append(Choice("分类总览", "warehouse", "分类"))
        nav_choices.append(Choice("按品质排" if mode != "rarity" else "按价值排", "warehouse", "品质" if mode != "rarity" else "价值"))
        nav_choices.append(Choice("黑市兑换", "black_market"))
        return self.reply(
            "\n".join([page.heading(head), *rows, tip]),
            tuple(nav_choices) + page.navigation("warehouse", mode_prefix_arg[mode]),
            plain=False, as_image=True, forward_title="跨群仓库",
        )

    def _sell_reasons(self, item: dict, count: int, bm_uses: dict[str, list[str]]) -> list[str]:
        reasons: list[str] = []
        spec = self.catalog.items[item["id"]]
        rarity = self.catalog.rarity(item)
        total = item["value"] * count
        if spec["kind"] == "crate":
            reasons.append("这是可解码开奖的加密盲盒（发「开箱」能开装备/大金/红货，直接卖很亏）")
        if rarity in ("purple", "gold", "red"):
            reasons.append(f"属于【{self.catalog.rarities[rarity]['name']}】高稀有度物品")
        if item["id"] in bm_uses:
            reasons.append("属于黑市配方材料（可换【" + "】【".join(bm_uses[item["id"]]) + "】）")
        if item["id"] in PROTECTED_SUPPLIES:
            reasons.append("属于高价值稀有局内道具")
        if total >= HIGH_VALUE_SELL_THRESHOLD and not (spec["kind"] == "material" and rarity in ("white", "green")):
            reasons.append(f"本批估值达 {total:,} 金")
        return reasons

    def _sell_equipment(self, query: str, confirmed: bool) -> Reply:
        equipped = set(json.loads(self.player["loadout"]).values())
        all_eq = list(self.db.execute("SELECT * FROM equipment WHERE user_id=? ORDER BY equip_id", (self.uid,)))
        spare_all = [r for r in all_eq if r["equip_id"] not in equipped]

        batch_map = {
            "绿装": (("green",), "闲置绿装", False, "绿装"),
            "闲置绿装": (("green",), "闲置绿装", False, "绿装"),
            "绿": (("green",), "闲置绿装", False, "绿装"),
            "蓝装": (("blue",), "闲置蓝装", True, "蓝装"),
            "闲置蓝装": (("blue",), "闲置蓝装", True, "蓝装"),
            "紫装": (("purple",), "闲置紫装", True, "紫装"),
            "闲置紫装": (("purple",), "闲置紫装", True, "紫装"),
            "金装": (("gold",), "闲置金装", True, "金装"),
            "闲置金装": (("gold",), "闲置金装", True, "金装"),
            "红装": (("red",), "闲置红装", True, "红装"),
            "闲置红装": (("red",), "闲置红装", True, "红装"),
            "蓝绿装": (("green", "blue"), "闲置蓝绿装", True, "蓝绿装"),
            "绿蓝装": (("green", "blue"), "闲置蓝绿装", True, "蓝绿装"),
            "低级装备": (("green", "blue"), "闲置蓝绿装", True, "蓝绿装"),
            "闲置装备": (("green", "blue", "purple"), "全部闲置绿蓝紫装备", True, "闲置装备"),
            "全部闲置装备": (("green", "blue", "purple"), "全部闲置绿蓝紫装备", True, "闲置装备"),
            "清装备": (("green", "blue", "purple"), "全部闲置绿蓝紫装备", True, "闲置装备"),
            "一键卖装备": (("green", "blue", "purple"), "全部闲置绿蓝紫装备", True, "闲置装备"),
            "全部装备": (("green", "blue", "purple"), "全部闲置绿蓝紫装备", True, "闲置装备"),
        }
        if query in batch_map:
            rarities, title, need_confirm, canon_arg = batch_map[query]
            targets = [r for r in spare_all if r["rarity"] in rarities]
            if not targets:
                raise GameError(f"装备库里没有可卖的{title}（已穿戴装备受保护）。")
            gold = sum(self.catalog.gear_sell.get(r["rarity"], 260) for r in targets)
            by_r = {rk: sum(1 for r in targets if r["rarity"] == rk) for rk in ("red", "gold", "purple", "blue", "green")}
            breakdown = " + ".join(
                f"{self.catalog.rarities[rk]['symbol']}{self.catalog.rarities[rk]['name']}×{cnt}"
                for rk, cnt in by_r.items() if cnt
            )
            has_high = any(r["rarity"] != "green" for r in targets)
            if need_confirm and has_high and not confirmed:
                preview_lines = []
                high_count = by_r["red"] + by_r["gold"] + by_r["purple"]
                if high_count:
                    hi = [r for r in targets if r["rarity"] in ("red", "gold", "purple")][:6]
                    for r in hi:
                        spec = self.catalog.equipment[r["base_id"]]
                        afs = "·".join(self.catalog.affixes[a]["name"] for a in json.loads(r["affixes"]))
                        preview_lines.append(
                            f"  • #{r['equip_id']} {self.catalog.rarities[r['rarity']]['symbol']}{spec['name']}"
                            + (f"（{afs}）" if afs else "")
                        )
                    if high_count > 6:
                        preview_lines.append(f"  …等共 {high_count} 件高阶装备")
                extra_txt = ("\n含高阶装备：\n" + "\n".join(preview_lines)) if preview_lines else ""
                return self.reply(
                    f"⚠️ 一键出售【{title}】确认\n"
                    f"待售共 {len(targets)} 件（{breakdown}），预计落袋 {gold:,} 金（已穿戴装备绝不出售）。{extra_txt}",
                    (Choice(f"确认全卖(+{gold:,}金)", "sell", f"确认 {canon_arg}"),
                     Choice("一键配装先", "autoequip"), Choice("取消·装备库", "equipment")),
                    plain=True, count_cooldown=False,
                )
            self.db.executemany("DELETE FROM equipment WHERE user_id=? AND equip_id=?",
                                [(self.uid, r["equip_id"]) for r in targets])
            self.player["gold"] += gold
            self._advance_quest("trade", 1)
            return self.reply(
                f"💰 一键清理{title} {len(targets)} 件（{breakdown}），落袋 {gold:,} 金！钱包 {self.player['gold']:,} 金。",
                (Choice("逛商店", "shop"), Choice("查看装备", "equipment"), Choice("返回仓库", "warehouse"), Choice("带药下潜", "start", "推荐 | 带药")),
                plain=False,
            )

        target_str = query.removeprefix("装备").strip()
        id_tokens = [tok for tok in target_str.replace("，", " ").replace(",", " ").split() if tok.startswith("#") and tok[1:].isdigit()]
        if len(id_tokens) > 1:
            wanted = {int(tok[1:]) for tok in id_tokens}
            targets = [r for r in spare_all if r["equip_id"] in wanted]
            if not targets:
                raise GameError("这些编号里没有可出售的闲置装备（已穿戴的装备受保护）。")
            gold = sum(self.catalog.gear_sell.get(r["rarity"], 260) for r in targets)
            has_high = any(r["rarity"] != "green" for r in targets)
            ids_str = " ".join(f"#{r['equip_id']}" for r in targets)
            if has_high and not confirmed:
                return self.reply(
                    f"⚠️ 批量出售指定装备 {len(targets)} 件（{ids_str}），预计落袋 {gold:,} 金。确定出售吗？",
                    (Choice(f"确认出售(+{gold:,}金)", "sell", f"确认 装备 {ids_str}"),
                     Choice("取消·装备库", "equipment")),
                    plain=True, count_cooldown=False,
                )
            self.db.executemany("DELETE FROM equipment WHERE user_id=? AND equip_id=?",
                                [(self.uid, r["equip_id"]) for r in targets])
            self.player["gold"] += gold
            self._advance_quest("trade", 1)
            return self.reply(
                f"💰 批量出售 {len(targets)} 件装备（{ids_str}），落袋 {gold:,} 金！钱包 {self.player['gold']:,} 金。",
                (Choice("查看装备", "equipment"), Choice("逛商店", "shop"), Choice("返回仓库", "warehouse")),
                plain=False,
            )

        options = [e for e in self._equipment_options() if e.item["equip_id"] not in equipped]
        if not options:
            raise GameError("没有可出售的闲置装备（已穿戴的装备受保护，需先换下）。")
        selected = self._pick(target_str, options, "sell", "要出售哪一件闲置装备？")
        if isinstance(selected, Reply):
            return selected
        item = selected.item
        price = self.catalog.gear_sell.get(item["rarity"], 260)
        if item["rarity"] != "green" and not confirmed:
            return self.reply(
                f"⚠️ 高价值装备出售确认！\n"
                f"待售：{selected.label}（{self.catalog.rarities[item['rarity']]['name']}装备，预计落袋 {price:,} 金）\n"
                f"提醒：蓝装及以上带有词条，卖掉无法找回。确定出售吗？",
                (Choice(f"确认出售(+{price:,}金)", "sell", f"确认 装备 #{item['equip_id']}"),
                 Choice("取消 · 看装备库", "equipment"), Choice("返回仓库", "warehouse")),
                plain=True, count_cooldown=False,
            )
        self.db.execute("DELETE FROM equipment WHERE user_id=? AND equip_id=?", (self.uid, item["equip_id"]))
        self.player["gold"] += price
        self._advance_quest("trade", 1)
        return self.reply(
            f"💰 已出售闲置装备 {selected.label}，落袋 {price:,} 金！现在钱包 {self.player['gold']:,} 金。",
            (Choice("查看装备", "equipment"), Choice("返回仓库", "warehouse"), Choice("逛商店", "shop")),
            plain=False,
        )

    def curate_collectibles(self, fingerprint: str, _target: str) -> Reply:
        self._check_idle()
        plan, sales, current = self.game._collection_cleanup(self.db, self.uid)
        if fingerprint != current or not sales:
            raise GameError("红、金藏品库存与派单时不同，请重新核算并确认派单。")
        for stock, count in sales:
            self._remove_stock(stock, count)
        self.player["gold"] += plan["gold"]
        self._advance_quest("trade", 1)
        return self.reply(f"已卖出 {plan['sale_count']:,} 件重复藏品，到账 {plan['gold']:,} 金。"
                          "同名同品质藏品各保留了最高单价的一件。")

    def sell(self, text: str, _target: str) -> Reply:
        self._check_idle()
        raw = text.strip()
        confirmed = False
        if raw.startswith("确认 "):
            confirmed = True
            raw = raw[3:].strip()
        elif raw.endswith(" 确认"):
            confirmed = True
            raw = raw[:-3].strip()
        if not raw:
            return self.reply(
                "💰 快捷出售指令：\n"
                "• 一键清仓：「一键清仓」(白绿料+非黑市蓝+闲置绿装)｜「卖 杂物」「卖 非黑市蓝」\n"
                "• 一键卖装备：「卖绿装」「卖蓝装」「卖紫装」「卖蓝绿装」「清装备」(绿+蓝+紫，保护已穿戴、金装和红装)｜「卖装备 #1 #2」\n"
                "• 卖指定物品：「卖 #编号 数量/全部」或「卖 物品名 数量」（后面加「确认」可跳过二次确认）",
                (Choice("一键清仓", "sell", "清仓"), Choice("卖蓝绿装", "sell", "蓝绿装"),
                 Choice("清闲置装备", "sell", "闲置装备"), Choice("看仓库", "warehouse")),
                plain=True, count_cooldown=False,
            )
        if raw in ("清仓", "一键清仓"):
            bm_uses = self._bm_material_uses()
            mat_targets = [
                it for it in self._stock()
                if self.catalog.items[it["id"]]["kind"] == "material"
                and it["id"] not in bm_uses
                and self.catalog.rarity(it) in ("white", "green", "blue")
            ]
            equipped = set(json.loads(self.player["loadout"]).values())
            green_eq = [
                r for r in self.db.execute("SELECT * FROM equipment WHERE user_id=?", (self.uid,))
                if r["rarity"] == "green" and r["equip_id"] not in equipped
            ]
            if not mat_targets and not green_eq:
                raise GameError("已经很干净啦！没有可一键清仓的普通材料或闲置绿装（黑市料/盲盒/道具/蓝以上装备已自动保留）。")
            mat_gold = sum(it["value"] * it["quantity"] for it in mat_targets)
            mat_cnt = sum(it["quantity"] for it in mat_targets)
            eq_gold = sum(self.catalog.gear_sell["green"] for _ in green_eq)
            for it in mat_targets:
                self._remove_stock(it, it["quantity"])
            if green_eq:
                self.db.executemany("DELETE FROM equipment WHERE user_id=? AND equip_id=?",
                                    [(self.uid, r["equip_id"]) for r in green_eq])
            total_gold = mat_gold + eq_gold
            self.player["gold"] += total_gold
            self._advance_quest("trade", 1)
            parts = []
            if mat_cnt:
                parts.append(f"非黑市白/绿/蓝料 ×{mat_cnt}（+{mat_gold:,}金）")
            if green_eq:
                parts.append(f"闲置绿装 ×{len(green_eq)}（+{eq_gold:,}金）")
            return self.reply(
                f"🧹 一键清仓完成！卖出 {' + '.join(parts)}，共落袋 {total_gold:,} 金！钱包 {self.player['gold']:,} 金。",
                (Choice("清蓝绿装", "sell", "蓝绿装"), Choice("逛商店", "shop"),
                 Choice("查看仓库", "warehouse"), Choice("带药下潜", "start", "推荐 | 带药")),
                plain=False,
            )
        eq_batch_keys = {
            "绿装", "闲置绿装", "蓝装", "闲置蓝装", "紫装", "闲置紫装", "金装", "闲置金装", "红装", "闲置红装",
            "蓝绿装", "绿蓝装", "低级装备", "闲置装备", "全部闲置装备", "清装备", "一键卖装备", "全部装备",
        }
        if raw in eq_batch_keys or raw.startswith("装备"):
            return self._sell_equipment(raw, confirmed)

        sell_all = False
        name, quantity = raw, 1
        for suffix in (" 全部", " 全", "*全部", "*全"):
            if raw.endswith(suffix):
                name = raw[: -len(suffix)].strip()
                sell_all = True
                break
        if not sell_all:
            name, quantity = _quantity(raw)
        if not sell_all and quantity <= 0:
            raise GameError("出售数量需要正整数，或者直接写「全部」。")

        rows = self._stock()
        bm_uses = self._bm_material_uses()
        selected_grade = next((grade for grade, title in (("white", "白"), ("green", "绿"), ("blue", "蓝"), ("purple", "紫"))
                               if name in (grade, title, title + "色")), None)

        if name in ("杂物", "垃圾", "非黑市蓝") or selected_grade:
            if name == "非黑市蓝":
                scoped = [
                    item for item in rows
                    if self.catalog.rarity(item) == "blue"
                    and self.catalog.definition(item["id"])["kind"] == "material"
                    and item["id"] not in bm_uses and item["id"] != "reroll_stone"
                ]
            else:
                grades = {selected_grade} if selected_grade else set(RARITIES[:2])
                scoped = [
                    item for item in rows if self.catalog.rarity(item) in grades
                    and self.catalog.definition(item["id"])["kind"] == "material"
                ]
            chosen = [(item, item["quantity"]) for item in scoped]
            if not chosen:
                raise GameError(f"仓库里没有可卖的「{name}」材料。")
            # High-value warning for batch selling blue (with BM mats) or purple materials
            if not confirmed and selected_grade in ("blue", "purple"):
                bm_hits = [item for item, _ in chosen if item["id"] in bm_uses or item["id"] == "reroll_stone"]
                est_gold = sum(item["value"] * cnt for item, cnt in chosen)
                est_units = sum(cnt for _, cnt in chosen)
                if selected_grade == "purple" or bm_hits:
                    warn = "包含紫色高价材料" if selected_grade == "purple" else f"包含 {len(bm_hits)} 种黑市配方材料/洗练石"
                    opts = [Choice(f"确认全卖(+{est_gold:,}金)", "sell", f"确认 {name}")]
                    if selected_grade == "blue":
                        opts.append(Choice("改卖非黑市蓝", "sell", "非黑市蓝"))
                    opts.append(Choice("取消 · 返回仓库", "warehouse"))
                    return self.reply(
                        f"⚠️ 批量出售确认！\n待售：{est_units} 件{self.catalog.rarities[selected_grade]['name']}材料（约 {est_gold:,} 金）\n"
                        f"提醒：{warn}，卖掉就没法去黑市换神装道具啦。",
                        tuple(opts), plain=False, count_cooldown=False,
                    )
        else:
            entry = self._pick(name, self._stock_options(rows), "sell", "这次要卖哪一件？")
            if isinstance(entry, Reply):
                return entry
            item = entry.item
            if item["id"] in self.catalog.rainbow:
                raise GameError("虹彩藏品没有金币回收价，会保留在仓库里。")
            count = item["quantity"] if sell_all else quantity
            if count > item["quantity"]:
                raise GameError(f"这批【{self.catalog.name(item)}】仓库里只有 {item['quantity']} 件。")
            if not confirmed:
                reasons = self._sell_reasons(item, count, bm_uses)
                if reasons:
                    est_gold = item["value"] * count
                    spec = self.catalog.items[item["id"]]
                    opts = [Choice(f"确认出售(+{est_gold:,}金)", "sell", f"确认 #{item['stock_id']} {count}")]
                    if spec["kind"] == "crate":
                        opts.append(Choice("改去开箱解码", "decode", f"#{item['stock_id']}"))
                    elif item["id"] in bm_uses:
                        opts.append(Choice("去黑市看看", "black_market"))
                    opts.append(Choice("取消 · 返回仓库", "warehouse"))
                    return self.reply(
                        f"⚠️ 高价值/重要物品出售确认！\n"
                        f"待售：{self.catalog.name(item)} ×{count}（预计落袋 {est_gold:,} 金）\n"
                        f"提醒：{'；'.join(reasons)}\n确定要卖掉吗？",
                        tuple(opts), plain=False, count_cooldown=False,
                    )
            chosen = [(item, count)]

        gold, units = 0, 0
        for item, count in chosen:
            self._remove_stock(item, count)
            gold += item["value"] * count
            units += count
        self.player["gold"] += gold
        self._advance_quest("trade", 1)
        return self.reply(
            f"💰 打包出货 {units} 件，落袋 {gold:,} 金！现在钱包 {self.player['gold']:,} 金。",
            (Choice("返回仓库", "warehouse"), Choice("逛商店", "shop"), Choice("黑市兑换", "black_market")),
            plain=False,
        )

    def _item_brief(self, key: str) -> str:
        if key in self.catalog.gear_boxes:
            return self.catalog.box_description(key)
        code = self.catalog.equipment.get(key, {}).get("gear_set")
        if code:
            return self.catalog.gear_set_description(code)
        if key in ITEM_BRIEFS:
            return ITEM_BRIEFS[key]
        return self.catalog.items.get(key, {}).get("description", "")

    def _shop_options(self) -> list[ItemChoice]:
        options = []
        for index, (key, price) in enumerate(self.catalog.shop.items(), 1):
            if key == "ticket":
                names = ("ticket", "通行证", "车票", "票")
            elif key in self.catalog.gear_boxes:
                names = SHOP_VIRTUAL_LOOKUP[key][1]
            else:
                names = self._item_names(key)
            brief = self._item_brief(key)
            title = names[1]
            if key in self.catalog.gear_boxes:
                title = self.catalog.progression["gear_boxes"][key]["tier"] + " " + title
            label = f"{title} · {price:,} 金" + (f"｜{brief}" if brief else "")
            options.append(ItemChoice(key, label, (*names, title, str(index)), {"id": key, "price": price}))
        if str(self.player["safebox"] + 1) in self.catalog.safebox_prices:
            key = str(self.player["safebox"] + 1)
            price = self.catalog.safebox_prices[key]
            options.append(ItemChoice("扩容", f"安全箱扩至 {key} 格 · {price:,} 金｜{ITEM_BRIEFS['safebox']}",
                                      ("safebox", "扩容", "安全箱", str(len(options) + 1)),
                                      {"id": "safebox", "price": price}))
        return options

    def shop(self, text: str, _target: str) -> Reply:
        if self.run and self.run["event"].get("offer"):
            pending = self.run["event"]
            brief = self._item_brief(pending["offer"]["id"])
            note = f"\n作用：{brief}" if brief else ""
            return self.reply(f"🛒 走私贩 · {self.catalog.name(pending['offer'])}，七折 {pending['price']:,} 金{note}")
        self._check_idle()
        options = self._shop_options()
        title = "🛒 电梯补给站（市场 · 金币直购）"
        categories = ("装备箱","回复药剂","战术补给","通行与扩容")
        category, _, page_text = text.strip().partition(" ")
        if category not in categories:
            tiers = [f"{box['tier']} {box['name']} · {self.catalog.shop[key]:,} 金"
                     for key,box in self.catalog.progression["gear_boxes"].items()]
            return self.reply("\n".join([title,*tiers,"选择想逛的货架："]),
                              tuple(Choice(c, "shop", c) for c in categories),
                              plain=False, multiline_menu=True, as_image=True, forward_title="商店分类")
        options = [entry for entry in options if self.catalog.shop_category(entry.item["id"]) == category]
        page = _ItemPage.select(page_text, len(options), page_size=10)
        choices = tuple(Choice(item.label, "buy", item.reference) for item in options[page.rows])
        return self.reply(
            page.heading(title + " · " + category) + "\n"
            "买完进仓库，下潜带货例：「下潜 商场 | 冰美式 2 | 烟雾弹」；查详情发「查 名称」，换特供道具看「黑市」。",
            choices + page.navigation("shop", category) + (Choice("返回商店分类","shop"),),
            plain=False, multiline_menu=True, as_image=True, forward_title="商店价目表",
            image_caption=f"💰 钱包 {self.player['gold']:,} 金｜点开看价目，关闭后直接回「序号」或「序号 数量」（如 1 5）直买",
        )

    def remaining_ticket_purchases(self) -> int:
        return max(0, min(expedition.TICKET_DAILY_PURCHASE_LIMIT - self.player["bought_tickets"],
                          expedition.TICKET_PAID_CAP - self.player["paid_tickets"],
                          self.ticket_cap - self.player["tickets"]))

    def _note_ticket_limits(self) -> None:
        reasons = []
        if self.player["bought_tickets"] >= expedition.TICKET_DAILY_PURCHASE_LIMIT:
            reasons.append(f"今日已购 {expedition.TICKET_DAILY_PURCHASE_LIMIT} 张，购买资格零点刷新")
        if self.player["paid_tickets"] >= expedition.TICKET_PAID_CAP:
            reasons.append(f"未用购买票已存满 {expedition.TICKET_PAID_CAP} 张，消耗后腾出位置")
        if self.player["tickets"] >= self.ticket_cap:
            reasons.append(f"通行证总容量 {self.ticket_cap} 张已满")
        if reasons:
            self.note_limit("通行证已达购买上限：" + "；".join(reasons) + "。")

    def buy(self, text: str, _target: str) -> Reply:
        if not text.strip():
            reply = self.shop("", "")
            reply.count_cooldown = False
            return reply
        if self.run and self.run["event"].get("offer"):
            return self.event_choice("交易", "")
        self._check_idle()
        raw_buy = text.strip()
        if raw_buy in ("补票", "买满票", "补满票"):
            can_buy = self.remaining_ticket_purchases()
            if can_buy <= 0:
                raise GameError(f"现在不能加购。还有未用购买票 {self.player['paid_tickets']}/{expedition.TICKET_PAID_CAP} 张，"
                                f"今日已购 {self.player['bought_tickets']}/{expedition.TICKET_DAILY_PURCHASE_LIMIT} 张。")
            price = next(o.item["price"] for o in self._shop_options() if o.item["id"] == "ticket")
            afford = min(can_buy, self.player["gold"] // price)
            if afford <= 0:
                raise GameError(f"钱包只有 {self.player['gold']:,} 金，买 1 张票需要 {price:,} 金。")
            self._charge(price * afford)
            self.player["tickets"] += afford
            self.player["bought_tickets"] += afford
            self.player["paid_tickets"] += afford
            self._note_ticket_limits()
            return self.reply(
                f"🎟️ 一键补票 ×{afford}（花 {price * afford:,} 金）！现有 {self.player['tickets']}/{self.ticket_cap} 票｜钱包 {self.player['gold']:,} 金。",
                (Choice("带药下潜", "start", "推荐 | 带药"), Choice("逛商店", "shop"), Choice("一键买药", "buy", "买药")),
                plain=False,
            )
        if raw_buy in ("买药", "一键买药", "补药"):
            shop_prices = {o.item["id"]: o.item["price"] for o in self._shop_options()}
            c_price = shop_prices["coffee"]
            s_price = shop_prices["smoke"]
            bought_parts = []
            spent = 0
            c_qty = min(3, self.player["gold"] // c_price)
            if c_qty > 0:
                self._charge(c_price * c_qty)
                self.deposit(self.catalog.ordinary_item("coffee", self.rng), quantity=c_qty)
                spent += c_price * c_qty
                bought_parts.append(f"冰美式×{c_qty}")
            if self.player["gold"] >= s_price:
                self._charge(s_price)
                self.deposit(self.catalog.ordinary_item("smoke", self.rng), quantity=1)
                spent += s_price
                bought_parts.append("烟雾弹×1")
            if not bought_parts:
                raise GameError(f"钱包只有 {self.player['gold']:,} 金，至少需要 {c_price:,} 金买冰美式。")
            return self.reply(
                f"🛍️ 一键补药成功：{' + '.join(bought_parts)}（共花 {spent:,} 金｜钱包剩 {self.player['gold']:,} 金），已进仓库！",
                (Choice("带药下潜", "start", "推荐 | 带药"), Choice("再买一套", "buy", "买药"), Choice("逛商店", "shop")),
                plain=False,
            )
        name, count = _quantity(text)
        if not count:
            raise GameError("要买几件？数量用正整数。")
        max_safebox = max(map(int, self.catalog.safebox_prices), default=1)
        if (name.casefold() in {"safebox", "安全箱", "扩容", "安全箱扩容", "扩容安全箱"}
                and self.player["safebox"] >= max_safebox):
            raise GameError(f"永久安全箱已达到 {max_safebox} 格上限；局内还能用扩容袋临时加 1 格。")
        option = self._pick(name, self._shop_options(), "buy", "想补哪一种？")
        if isinstance(option, Reply):
            return option
        key, price = option.item["id"], option.item["price"]
        if count > 99:
            raise GameError("单次最多购买 99 件。")
        if key == "ticket":
            remaining = self.remaining_ticket_purchases()
            if count > remaining:
                raise GameError(f"每天限购 {expedition.TICKET_DAILY_PURCHASE_LIMIT} 张，未用购买票限持有 {expedition.TICKET_PAID_CAP} 张。"
                                f"你还有未用购买票 {self.player['paid_tickets']} 张，目前可买 {remaining} 张。")
            self._charge(price * count)
            self.player["tickets"] += count
            self.player["bought_tickets"] += count
            self.player["paid_tickets"] += count
            self._note_ticket_limits()
            opts = [Choice("返回商店", "shop")]
            if self.remaining_ticket_purchases():
                opts.append(Choice("再买 1 张", "buy", "ticket 1"))
            opts.extend((Choice("下潜", "start"), Choice("仓库", "warehouse")))
            return self.reply(
                f"🎟️ 已补 {count} 张（花 {price * count:,} 金），现有 {self.player['tickets']}/{self.ticket_cap} 张通行证｜钱包剩 {self.player['gold']:,} 金。",
                tuple(opts), plain=False,
            )
        if key == "safebox":
            if count != 1:
                raise GameError("安全箱逐级升级，每次扩一格。")
            self._charge(price)
            self.player["safebox"] += 1
            if self.player["safebox"] >= max_safebox:
                self.note_limit(f"永久安全箱已达到 {max_safebox} 格上限；局内还能用扩容袋临时加 1 格。")
            return self.reply(
                f"🔒 安全箱已长期扩容到 {self.player['safebox']} 格（花 {price:,} 金）｜钱包剩 {self.player['gold']:,} 金。",
                (Choice("返回商店", "shop"), Choice("下潜", "start"), Choice("仓库", "warehouse")),
                plain=False,
            )
        self._charge(price * count)
        if key in self.catalog.gear_boxes:
            box_title, grades, weights = self.catalog.gear_boxes[key]
            loot = []
            for _ in range(count):
                grade = self.rng.choices(grades, weights)[0]
                item = self.catalog.roll_gear(self.rng, grade)
                cur = self.db.execute(
                    "INSERT INTO equipment(user_id,base_id,rarity,stats,affixes,rules_version) VALUES(?,?,?,?,?,2)",
                    (self.uid, item["id"], item["rarity"], encode(item["stats"]), encode(item["affixes"])),
                )
                item["equip_id"] = cur.lastrowid
                loot.append(item)
            grade_counts = {g: sum(1 for i in loot if i["rarity"] == g) for g in ("red", "gold", "purple", "blue", "green")}
            summary_tags = [
                f"{self.catalog.rarities[g]['symbol']}{self.catalog.rarities[g]['name']}×{c}"
                for g, c in grade_counts.items() if c
            ]
            lines = [
                f"🎁 {box_title} ×{count} 开箱完成（花 {price * count:,} 金｜钱包剩 {self.player['gold']:,} 金）",
                "统计：" + " · ".join(summary_tags),
            ]
            shown = sorted(loot, key=lambda it: RARITIES.index(it["rarity"]), reverse=True)[:8]
            for it in shown:
                afs = "、".join("【" + self.catalog.affixes[a]["name"] + "】" for a in it["affixes"])
                st = " ".join(f"{STAT_NAMES[f]}{v:+d}" for f, v in it["stats"].items())
                eid_tag = f"#{it['equip_id']} " if it.get("equip_id") else ""
                lines.append(f"• {eid_tag}{self.catalog.name(it)}（{st}{(' · ' + afs) if afs else ''}）")
            if count > len(shown):
                lines.append(f"（其余 {count - len(shown)} 件已入装备库）")
            post_opts = [
                Choice("返回商店", "shop"),
                Choice(f"再抽 {count} 个", "buy", f"{key} {count}"),
                Choice("五连抽" if count != 5 else "十连抽", "buy", f"{key} {5 if count != 5 else 10}"),
                Choice("一键配装", "autoequip"),
            ]
            if grade_counts.get("green"):
                post_opts.append(Choice("卖闲置绿装", "sell", "绿装"))
            return self.reply("\n".join(lines), tuple(post_opts), plain=False)
        self.deposit(self.catalog.ordinary_item(key, self.rng), quantity=count)
        brief = self._item_brief(key)
        note = f"\n作用：{brief}\n提示：下潜时用「下潜 地图 | {self.catalog.items[key]['name']} {count}」带上，局内发「使用」生效。" if brief else ""
        return self.reply(
            f"🛒 {self.catalog.items[key]['name']} ×{count} 已入库（花 {price * count:,} 金｜钱包剩 {self.player['gold']:,} 金）。{note}",
            (Choice("返回商店", "shop"), Choice(f"再买 {count} 件", "buy", f"{key} {count}"),
             Choice("仓库", "warehouse"), Choice("下潜", "start")),
            plain=False,
        )

    def _equipment_options(self) -> list[ItemChoice]:
        equipped = set(json.loads(self.player["loadout"] or "{}").values())
        rarity_rank = {grade: rank for rank, grade in enumerate(RARITIES)}
        rows = list(self.db.execute("SELECT * FROM equipment WHERE user_id=?", (self.uid,)))
        rows.sort(
            key=lambda r: (
                0 if r["equip_id"] in equipped else 1,
                -rarity_rank.get(r["rarity"], 0),
                -int(r["equip_id"]),
            )
        )
        result = []
        for index, row in enumerate(rows, 1):
            item = dict(row)
            spec = self.catalog.equipment[item["base_id"]]
            grade = self.catalog.rarities[item["rarity"]]["symbol"]
            label = f"{grade} #{item['equip_id']} {spec['name']}"
            result.append(ItemChoice(f"#{item['equip_id']}", label, (str(index), spec["name"]), item))
        return result

    def equipment(self, text: str, _target: str) -> Reply:
        first, _, rest = text.partition(" ")
        if first in ("穿戴", "穿"):
            return self.wear(rest, "")
        if first in ("一键", "一键配装"):
            return self.autoequip("", "")
        options = self._equipment_options()
        equipped = set(json.loads(self.player["loadout"]).values())
        if text and not text.startswith("页"):
            selected = self._pick(text, options, "equipment", "看哪一件装备？")
            if isinstance(selected, Reply):
                return selected
            item = selected.item
            spec = self.catalog.equipment[item["base_id"]]
            stats = " · ".join(f"{STAT_NAMES[key]} {value:+d}" for key, value in json.loads(item["stats"]).items())
            perks = [self.catalog.affixes[key] for key in json.loads(item["affixes"])]
            lines = [selected.label, f"{spec['slot']} · {'已穿戴' if item['equip_id'] in equipped else '装备库'}", stats]
            if spec.get("gear_set"):
                lines.extend(self.catalog.gear_set_description(spec["gear_set"]).splitlines())
            lines.extend(f"【{perk['name']}】{perk['description']}" for perk in perks)
            eq_choices = [Choice("穿戴", "wear", selected.reference), Choice("重铸", "reroll", selected.reference)]
            if item["equip_id"] not in equipped:
                eq_price = self.catalog.gear_sell.get(item["rarity"], 260)
                eq_choices.append(Choice(f"出售(+{eq_price:,}金)", "sell", f"装备 #{item['equip_id']}"))
            eq_choices.append(Choice("装备库", "equipment"))
            return self.reply("\n".join(lines), tuple(eq_choices), plain=False, card="装备详情", card_lines=tuple(lines), forward_title="装备详情")
        page = _ItemPage.select(text.removeprefix("页"), len(options), page_size=10)
        by_slot: dict[str, list[str]] = {slot: [] for slot in SLOTS}
        for index, entry in enumerate(options[page.rows], page.rows.start + 1):
            slot = self.catalog.equipment[entry.item["base_id"]]["slot"]
            worn = " · 穿戴中" if entry.item["equip_id"] in equipped else ""
            by_slot.setdefault(slot, []).append(f"{index}. {entry.label}{worn}")
        rows = [line for slot, entries in by_slot.items() if entries for line in (f"【{slot}】", *entries)]
        spare_green = sum(1 for e in options if e.item["rarity"] == "green" and e.item["equip_id"] not in equipped)
        spare_bg = sum(1 for e in options if e.item["rarity"] in ("green", "blue") and e.item["equip_id"] not in equipped)
        spare_non_gold = sum(1 for e in options if e.item["rarity"] in ("green", "blue", "purple") and e.item["equip_id"] not in equipped)
        choices_list = [Choice("一键配装", "autoequip")]
        if spare_green:
            choices_list.append(Choice(f"卖绿装({spare_green})", "sell", "绿装"))
        if spare_bg > spare_green:
            choices_list.append(Choice(f"卖蓝绿装({spare_bg})", "sell", "蓝绿装"))
        if spare_non_gold > spare_bg:
            choices_list.append(Choice(f"清闲置装({spare_non_gold})", "sell", "闲置装备"))
        choices_list.extend((Choice("逛商店", "shop"), Choice("看仓库", "warehouse")))
        set_counts = self.catalog.gear_set_counts(e.item["base_id"] for e in options if e.item["equip_id"] in equipped)
        sets_worn = [f"{self.catalog.gear_sets[code]['name']} {count}/4 件" for code,count in set_counts.items()]
        return self.reply(
            "\n".join([page.heading(f"🧰 装备库 · {len(options)} 件"), "武器、护甲、挂件、核心各一槽，每槽可穿戴一件。",
                       f"🏅 资深探员加成（撤离 {min(25, self.player['extracts'])}/25 次）：生命+{self._veteran_bonus()['hp']} · 攻击+{self._veteran_bonus()['atk']} · 防御+{self._veteran_bonus()['def']}",
                       *(["套装穿戴 · " + "、".join(sets_worn)] if sets_worn else []),
                       *(rows or ["目前没有装备。"]),
                       "💡 快捷：卖绿装 / 卖蓝装 / 卖紫装 / 卖蓝绿装 / 清装备（绿蓝紫全清，保护已穿戴、金装和红装）｜卖装备 #1 #2"]),
            tuple(choices_list) + page.navigation("equipment", "页", next_label="装备下一页"), plain=False, as_image=True, forward_title="装备库",
        )

    def wear(self, text: str, _target: str) -> Reply:
        self._check_idle()
        selected = self._pick(text, self._equipment_options(), "wear", "想穿哪一件装备？")
        if isinstance(selected, Reply):
            return selected
        slots = json.loads(self.player["loadout"])
        slots[self.catalog.equipment[selected.item["base_id"]]["slot"]] = selected.item["equip_id"]
        self.player["loadout"] = encode(slots)
        return self.reply(f"🧰 已穿戴 {selected.label}。")

    def autoequip(self, _text: str, _target: str) -> Reply:
        self._check_idle()
        gear = self._equipment_options()
        equipped=set(json.loads(self.player["loadout"]).values())
        pools={slot:[e for e in gear if self.catalog.equipment[e.item["base_id"]]["slot"]==slot] for slot in SLOTS}
        selected={s:next((v for v in pool if v.item["equip_id"] in equipped),pool[0])
                  for s,pool in pools.items() if pool}
        def loadout_score(entries):
            stats, perks = self._gear_panel([entry.item for entry in entries])
            return combat.loadout_score(stats, perks)

        # Seed coordinated swaps so a two/four-piece bonus can beat four individual upgrades.
        best_score = loadout_score(selected.values())
        for code in self.catalog.gear_sets:
            matching = {slot:[e for e in pool if self.catalog.equipment[e.item["base_id"]].get("gear_set") == code]
                        for slot,pool in pools.items()}
            if sum(bool(pool) for pool in matching.values()) < 2:
                continue
            candidate = dict(selected)
            for slot, pool in matching.items():
                if pool:
                    others = [e for other,e in candidate.items() if other != slot]
                    candidate[slot] = max(pool, key=lambda e: loadout_score([*others,e]))
            score = loadout_score(candidate.values())
            if score > best_score:
                selected, best_score = candidate, score
        for _ in range(3):
            changed=False
            for slot,pool in pools.items():
                if not pool: continue
                others=[v for s,v in selected.items() if s!=slot]
                best=max([selected[slot],*pool],key=lambda option: loadout_score([*others,option]))
                if best is not selected[slot]:
                    selected[slot]=best
                    changed=True
            if not changed: break
        if not selected:
            raise GameError("装备库现在是空的。商店可以开装备盲盒，禁区里也会掉装备。")
        self.player["loadout"] = encode({slot: entry.item["equip_id"] for slot, entry in selected.items()})
        return self.reply(
            "🧰 已按综合战力配好装备。\n" +
            "\n".join(f"{slot} · {entry.label}" for slot, entry in selected.items()),
            (Choice("卖蓝绿装", "sell", "蓝绿装"), Choice("清闲置装备", "sell", "闲置装备"),
             Choice("带药下潜", "start", "推荐 | 带药"), Choice("逛商店", "shop")),
            plain=False,
        )

    def reroll(self, text: str, _target: str) -> Reply:
        self._check_idle()
        options = [entry for entry in self._equipment_options() if entry.item["rarity"] != "green"]
        if not options:
            raise GameError("蓝色及以上装备带有可重铸词条。")
        selected = self._pick(text, options, "reroll", "重铸会用金币和蓝料或洗练石，想洗哪一件？")
        if isinstance(selected, Reply):
            return selected
        item = selected.item
        tries = item["rerolls"]
        price = self.catalog.reforge_price + max(0, tries - 2) * 450
        self._charge(price)
        chosen = "reroll_stone" if self._matching_stock("reroll_stone") else "any_blue"
        self._consume(chosen, 1)
        affixes = self.catalog.roll_affixes(item["rarity"], self.rng)
        self.db.execute("UPDATE equipment SET affixes=?,rerolls=rerolls+1 WHERE user_id=? AND equip_id=?",
                        (encode(affixes), self.uid, item["equip_id"]))
        label = "、".join("【" + self.catalog.affixes[key]["name"] + "】" for key in affixes)
        return self.reply(
            f"🛠️ {selected.label} · 新词条 {label}\n消耗 {price:,} 金和一件重铸材料（钱包剩 {self.player['gold']:,} 金）。",
            (Choice("再洗一次", "reroll", f"#{item['equip_id']}"), Choice("穿戴", "wear", f"#{item['equip_id']}"), Choice("装备库", "equipment")),
            plain=False,
        )

    def _recipe_requirements(self, recipe: dict, *, with_progress: bool = False) -> str:
        parts = []
        for key, amount in recipe["requires"].items():
            if key == "any_blue":
                name, place = "任意蓝色材料", ""
            elif key == "any_green":
                name, place = "任意绿色材料", ""
            else:
                spec = self.catalog.items[key]
                name = spec["name"]
                place = MAP_SHORT.get(spec.get("map", ""), "")
            loc = f"({place})" if place else ""
            if with_progress:
                owned = sum(item["quantity"] for item in self._matching_stock(key))
                parts.append(f"{name}{loc} {owned}/{amount}")
            else:
                parts.append(f"{name}{loc}×{amount}")
        return " + ".join(parts)

    def black_market(self, text: str, _target: str) -> Reply:
        if self.run and self.run["event"].get("offer"):
            return self.shop("", "")
        recipes = self.catalog.weekly_recipes(self.week)
        page = _ItemPage.select(text, len(recipes))
        title = "🛒 黑市以物易物（消耗右侧材料 → 换取【成品】｜✅可换 □缺料 ★本周减料特惠）"
        lines, menu = [page.heading(title)], []
        for index, recipe in enumerate(recipes[page.rows], page.rows.start + 1):
            enough = all(sum(item["quantity"] for item in self._matching_stock(key)) >= amount
                         for key, amount in recipe["requires"].items())
            out_key = recipe["output"]
            label = self.catalog.items[out_key]["name"]
            reqs = self._recipe_requirements(recipe, with_progress=True)
            brief = self._item_brief(out_key)
            lines.append(f"{index}. {'✅' if enough else '□'}{'★' if recipe['featured'] else ''}【{label}】← {reqs}")
            if brief:
                lines.append(f"   作用：{brief}")
            if enough:
                menu.append(Choice("兑换 " + label, "craft", out_key))
        lines.append("发「兑换 序号/名称」制作，材料够时直接回序号即可（引用也行）；发「查 名称」看详情。")
        choices = tuple(menu[:4]) or (Choice("看仓库", "warehouse"), Choice("物品介绍", "help", "道具"))
        return self.reply(
            "\n".join(lines), choices + page.navigation("black_market"),
            plain=False, count_cooldown=False, as_image=True, forward_title="地下黑市",
        )

    def craft(self, text: str, _target: str) -> Reply:
        self._check_idle()
        if not text.strip():
            return self.black_market("", "")
        name, count = _quantity(text)
        if count <= 0 or count > 20:
            raise GameError("单次黑市兑换数量需要 1~20。")
        recipes = self.catalog.weekly_recipes(self.week)
        options = [ItemChoice(str(index), self.catalog.items[recipe["output"]]["name"],
                              self._item_names(recipe["output"]), recipe)
                   for index, recipe in enumerate(recipes, 1)]
        selected = self._pick(name, options, "craft", "打算兑换哪张黑市配方？")
        if isinstance(selected, Reply):
            return selected
        recipe = selected.item
        for _ in range(count):
            for key, amount in recipe["requires"].items():
                self._consume(key, amount)
            self.deposit(self.catalog.ordinary_item(recipe["output"], self.rng))
        self._advance_quest("trade", 1)
        brief = self._item_brief(recipe["output"])
        qty_tag = f" ×{count}" if count > 1 else ""
        return self.reply(
            f"🛒 黑市兑换成功！获得【{selected.label}】{qty_tag}，已入库。"
            + (f"\n作用：{brief}" if brief else ""),
            (Choice("再换1个", "craft", recipe["output"]), Choice("回黑市", "black_market"), Choice("看仓库", "warehouse")),
            plain=False,
        )

    def _open_one_crate(self, item: dict) -> dict:
        self._remove_stock(item, 1)
        quality = self.catalog.rarity(item)
        crate = self.catalog.progression["crates"][quality]
        rare_chance = crate["collection"]
        roll = self.rng.random()
        if roll < rare_chance:
            pool = [key for key, entry in self.catalog.collections.items()
                    if "map" not in entry and entry["rarity"] in ("gold","red") and (quality == "gold" or entry["rarity"] == "gold")]
            reward = self.catalog.ordinary_item(self.rng.choice(pool), self.rng)
        elif roll < rare_chance + 0.06:
            reward = self.catalog.ordinary_item("doll", self.rng)
        else:
            grade = self.rng.choices(crate["grades"],crate["weights"])[0]
            reward = self.catalog.roll_gear(self.rng, grade)
        self.deposit(reward)
        return reward

    def decode(self, text: str, _target: str) -> Reply:
        self._check_idle()
        crates = [item for item in self._stock() if self.catalog.items[item["id"]]["kind"] == "crate"]
        if not crates:
            raise GameError("仓库里还没有带出来的加密盲盒。去禁区摸箱或支援群友可以拿到盲盒！")
        raw = text.strip()
        total_crates = sum(c["quantity"] for c in crates)

        # Batch open all crates
        if raw in ("全部", "全", "全开", "all"):
            rewards: list[dict] = []
            for crate in crates:
                for _ in range(crate["quantity"]):
                    rewards.append(self._open_one_crate(crate))
                    crate["quantity"] -= 1
            reds = [r for r in rewards if self.catalog.rarity(r) == "red"]
            golds = [r for r in rewards if self.catalog.rarity(r) == "gold"]
            lines = [f"🎁 一口气解码全部 {len(rewards)} 个加密盲盒！收获已全部入库："]
            if reds:
                lines.append("🟥 爆红光！" + "、".join(self.catalog.name(r) for r in reds))
            if golds:
                lines.append("🟨 出金！" + "、".join(self.catalog.name(r) for r in golds))
            summary_items = [self.catalog.name(r) for r in rewards[:12]]
            if len(rewards) > 12:
                summary_items.append(f"另有 {len(rewards) - 12} 件")
            lines.append("清单：" + "、".join(summary_items))
            return self.reply(
                "\n".join(lines),
                (Choice("一键配装", "autoequip"), Choice("查看装备", "equipment"),
                 Choice("返回仓库", "warehouse"), Choice("逛商店", "shop")),
                plain=False,
            )

        # Support bare "开箱 5" / "开箱 10" to batch-open N crates right away
        if raw.isascii() and raw.isdigit():
            want = min(max(1, int(raw)), total_crates)
            rewards: list[dict] = []
            rem = want
            for crate in crates:
                take = min(rem, crate["quantity"])
                for _ in range(take):
                    rewards.append(self._open_one_crate(crate))
                    crate["quantity"] -= 1
                rem -= take
                if rem <= 0:
                    break
            return self._crate_summary_reply(rewards, total_crates - want)

        # Bare "开箱" with multiple crates -> show quick multi-open menu (1.一键全开 / 2.连开5个 / 单开)
        if not raw and total_crates > 1:
            opts = [Choice(f"一键全开全部({total_crates}个)", "decode", "全部")]
            if total_crates >= 5:
                opts.append(Choice("连开 5 个", "decode", "5"))
            opts.extend(Choice(f"单开 {e.label}", "decode", f"{e.reference} 1") for e in self._stock_options(crates)[:3])
            return self.reply(
                f"🎁 仓库共有 {total_crates} 个加密盲盒！支持直接发「开箱 全部」「开箱 5」「开箱 蓝 全部」：",
                tuple(opts), plain=False, multiline_menu=True, count_cooldown=False,
            )
        if not raw:
            target_item, open_count = crates[0], 1
        else:
            grade_alias = {"蓝": "蓝色盲盒", "蓝色": "蓝色盲盒", "紫": "紫色盲盒", "紫色": "紫色盲盒", "金": "金色盲盒", "金色": "金色盲盒"}
            open_all_grade = False
            cleaned_raw = raw
            for suf in (" 全部", " 全", "*全部", "*全"):
                if cleaned_raw.endswith(suf):
                    cleaned_raw = cleaned_raw[: -len(suf)].strip()
                    open_all_grade = True
                    break
            name, open_count = (cleaned_raw, 999) if open_all_grade else _quantity(cleaned_raw)
            name = grade_alias.get(name, name)
            selected = self._pick(name, self._stock_options(crates), "decode", "选一个已经撤离带出的加密盲盒。")
            if isinstance(selected, Reply):
                return selected
            target_item = selected.item
            open_count = min(max(1, open_count), target_item["quantity"])

        rewards = []
        for _ in range(open_count):
            rewards.append(self._open_one_crate(target_item))
            target_item["quantity"] -= 1
        return self._crate_summary_reply(rewards, total_crates - open_count)

    def _crate_summary_reply(self, rewards: list[dict], left: int) -> Reply:
        if len(rewards) == 1:
            reward = rewards[0]
            if self.catalog.rarity(reward) == "red":
                msg = f"🟥 盲盒爆红光了！解码开出传世红【{self.catalog.name(reward)}】，已稳稳入库！"
            else:
                msg = f"🎁 滋滋解码成功！开出【{self.catalog.name(reward)}】，已收入仓库。"
        else:
            reds = [r for r in rewards if self.catalog.rarity(r) == "red"]
            golds = [r for r in rewards if self.catalog.rarity(r) == "gold"]
            dolls = [r for r in rewards if r["id"] == "doll"]
            purples = [r for r in rewards if self.catalog.rarity(r) == "purple" and r["id"] != "doll"]
            blues = [r for r in rewards if self.catalog.rarity(r) == "blue"]
            lines = [f"🎁 连续解码 {len(rewards)} 个加密盲盒完成！收获已全部入库："]
            if reds:
                lines.append("🟥 传世红：" + "、".join(self.catalog.name(r) for r in reds))
            if golds:
                lines.append("🟨 金色高光：" + "、".join(self.catalog.name(r) for r in golds[:8]))
            if dolls:
                lines.append(f"🎭 替身娃娃 ×{len(dolls)}")
            if purples:
                lines.append(f"🟪 紫装 ×{len(purples)}：" + "、".join(self.catalog.name(r) for r in purples[:5]) + ("…" if len(purples) > 5 else ""))
            if blues:
                lines.append(f"🟦 蓝装 ×{len(blues)}")
            msg = "\n".join(lines)

        post_opts: list[Choice] = []
        if left > 0:
            if left >= 5:
                post_opts.append(Choice("再开 5 个", "decode", "5"))
            post_opts.append(Choice(f"全开剩余({left}个)", "decode", "全部"))
            post_opts.append(Choice("单开 1 个", "decode", "1"))
        else:
            post_opts.append(Choice("一键配装", "autoequip"))
        post_opts.extend((Choice("查看装备", "equipment"), Choice("返回仓库", "warehouse")))
        return self.reply(msg, tuple(post_opts[:4]), plain=False)

    def blessing(self, _text: str, _target: str) -> Reply:
        if self.db.execute("SELECT 1 FROM group_buffs WHERE group_id=? AND expires_at>?",
                           (self.gid, self.at)).fetchone():
            raise GameError("本群的金雨还在飘，等它结束再续吧。")
        self._charge(self.catalog.group_buff_price)
        self.db.execute("INSERT INTO group_buffs VALUES(?,?) ON CONFLICT(group_id) DO UPDATE SET expires_at=excluded.expires_at",
                        (self.gid, self.at + 3600))
        if self.run:
            self._note_rare_limit(chest=True, blessing=True)
        return self.reply(f"🌟 老板大气！{self.player['nickname']} 豪掷重金开启【全群金雨祈福】！\n"
                          "接下来 1 小时，本群大金和传世红爆率全面 +20%，还不快冲电梯！")

    def _lobby(self) -> Reply:
        if self.run:
            run, current = self.run, self.run["event"]
            line = f"🛗 {self._map_title()} · HP {run['hp']}/{run['stats']['hp']}"
            states = {
                "ready": "电梯门开着，能深入或撤离。",
                "combat": f"前面是{current.get('enemy', {}).get('name', '怪物')}。",
                "relic": "地上还有一个待处理的遗物盒。",
                "choice": f"正在处理「{self.catalog.events.get(current.get('id'), {}).get('name', '奇遇')}」。",
            }
            line += "\n" + states[current["kind"]]
            if current["kind"] == "combat":
                line += f" 剩余 {current['enemy']['hp']} HP。"
                if self._signal_active():
                    line += " 信号灯还亮着。"
            if current.get("offer"):
                line += f"\n走私贩报价 · {self.catalog.name(current['offer'])} {current['price']:,} 金"
            total = self._value(run["bag"] + run["safe"])
            return self.reply(f"{line}\n已搜到 {total:,} 金，发「背包」查看。")
        count = len(self.archives)
        lines = [
            f"🛗 无尽电梯 · 大厅（{self.player['nickname']}）",
            f"💰 {self.player['gold']:,} 金 ｜ 🎫 {self.player['tickets']}/{self.ticket_cap} 票 ｜ 🏆 大金 {count}/{len(self.catalog.collections)}",
            "快捷：进 / 连进 / 打 / 跑 / 撤 / 战报 ｜ 改名发「改名 新昵称」",
        ]
        return self.reply("\n".join(lines),
                          (Choice("下潜", "start"), Choice("✦ 群星寓言集", "prophecy"), Choice("模拟战", "simulate"),
                           Choice("交易所", "exchange"), Choice("仓库", "warehouse"), Choice("商店", "shop"),
                           Choice("装备", "equipment"), Choice("闪耀祝福","heirlooms"), Choice("黑市", "black_market"),
                           Choice("陈列室", "gallery"), Choice("悬赏", "quests"),
                           Choice("排行", "rank"), Choice("攻略", "help", "攻略")),
                          plain=False)

    def lobby(self, _text: str, _target: str) -> Reply:
        if not self.player.get("name_confirmed") and self.player["runs"] == 0 and self.run is None:
            result = self._first_time_name_prompt()
        elif self.player["runs"] == 0 and self.run is None and self.player["tickets"]:
            result = self.start("mall", "")
        else:
            result = self._lobby()
        result.text += "\n" + expedition.WEB_ENTRY
        return result

    def help(self, topic: str, _target: str) -> Reply:
        if not topic:
            result = self._lobby()
            result.text += "\n" + expedition.WEB_ENTRY
            return result
        if topic in ("攻略", "新手", "玩法", "指南", "教程"):
            lines = [
                "📖 无尽电梯 · 搜打撤游玩攻略",
                expedition.WEB_ENTRY,
                "1️⃣ 核心循环：发「下潜」进图→每层随机遇宝箱/怪物/奇遇/群友遗物盒→继续「进」赌更大，或见好就收「撤」把背包安全带回跨群仓库。",
                "2️⃣ 翻车与保底：前 3 局有新手保险（翻车全额保底）！之后翻车会丢临时背包，但锁进安全箱的货绝对不掉——摸到蓝紫金货立刻发「存箱」。",
                "3️⃣ 战斗与摇人：遇怪回「打」看逐回合战报，打不过回「跑」（敏捷越高越容易溜）；遇精英可发「摇人」，群友回「支援」免费空降补刀还能领盲盒！",
                "4️⃣ 带道具进图：商店/黑市换的道具都在仓库，进图时写「下潜 商场 | 冰美式 2 | 烟雾弹」带进局内，局内发「使用 冰美式」生效（替身娃娃揣包里自动保命）。",
                "5️⃣ 成长路线：白绿杂物发「卖垃圾」换钱→商店买装备盲盒 & 扩安全箱→「一键配装」+「重铸」洗词条→蓝紫材料留着去「黑市」换手雷/雷达/替身娃娃→集齐大金点亮「陈列室」领永久加成！",
                "6️⃣ 全服昵称：发「改名 新昵称」（1~16 字）设置跨群共享昵称，换群也不变；发「改名」可看当前昵称或同步当前群名片。",
                "👑 第 8、18、30 层及之后每 20 层迎战首领。击败回满血，正式局每次单独判定 0.5% 彩级背包技能。发「地图道具」「图鉴进度」查本局增益与长期进度。",
                "7️⃣ 查百科：发「物品介绍 / 概率规则 / 词条介绍 / 装备介绍 / 帮助 地图 / 黑市 / 查 名称」。",
            ]
            return self.reply(
                "\n".join(lines),
                (Choice("物品介绍", "help", "道具"), Choice("概率规则", "help", "概率"),
                 Choice("词条流派", "help", "词条"), Choice("地图与掉落", "help", "地图"),
                 Choice("模拟战", "simulate")),
                plain=False, as_image=True, image_caption=expedition.WEB_ENTRY, forward_title="电梯游玩攻略",
            )
        if topic in ("概率", "爆率", "规则", "公式", "掉率"):
            return self.reply(expedition.probability_guide(self.catalog),
                              (Choice("首领规则","help","首领"),Choice("局内道具手册","field_guide"),Choice("图鉴进度","atlas")),
                              plain=True,as_image=True,forward_title="概率与爆率规则")
        if topic.casefold() in ("首领","boss","首领规则","首领房"):
            return self.reply(expedition.boss_guide(),(Choice("背包技能","atlas"),Choice("地图道具手册","field_guide")),
                              plain=True,as_image=True,forward_title="首领攻略")
        if topic in ("图鉴","图鉴进度","彩级","虹彩","背包技能"):
            return self.atlas("","")
        if topic in ("局内道具","地图道具","临装"):
            return self.field_guide("","")
        if topic in ("地图", "难度"):
            lines = ["🛗 电梯停靠站（越往后风险越高、收益越肥）",
                     "新获得的金红藏品按基础价和下列地图倍率估算，角色估值加成与首领战损另计。"]
            for map_key, spec in self.catalog.maps.items():
                entry = f"门票 {spec['entry']:,} 金" if spec["entry"] else (
                    f"累计撤离 {spec['extracts']} 次开启" if spec["extracts"] else "直接开放")
                ambush = f"第 {spec['ambush_floor']} 层起 {spec['ambush_rate']:.0%} 撤离伏击（击杀本层怪后撤离免伏击）" if spec["ambush_rate"] else "各层可直接撤离"
                bm_mats = [
                    self.catalog.items[k]["name"]
                    for r in self.catalog.recipes for k in r["requires"]
                    if k not in ("any_blue", "any_green") and self.catalog.items.get(k, {}).get("map") == map_key
                ]
                bm_unique = list(dict.fromkeys(bm_mats))
                drop_line = f"\n   黑市关键料：{'、'.join(bm_unique)}" if bm_unique else ""
                collection_multiplier = expedition.collection_value_multiplier(spec)
                lines.append(f"• {spec['name']} · {entry}\n   基础大金率 {spec['rare_rate']:.1%} · 普通物资估值 ×{spec['value_multiplier']:g}"
                             f" · 金红藏品估值 ×{collection_multiplier:.2f} · {ambush}{drop_line}")
            return self.reply("\n".join(lines), (Choice("选择地图", "start"), Choice("模拟战", "simulate"), Choice("黑市配方", "black_market")), plain=False, as_image=True, forward_title="禁区地图一览")
        category, separator, page_text = topic.rpartition(" 页")
        if not (separator and page_text.isascii() and page_text.isdigit()):
            category, page_text = topic, ""
        if category in ("道具", "药品", "物品", "物品介绍", "盲盒", "介绍", "大全"):
            shop_lines = ["【商店常驻（金币直购）】"]
            shop_lines.extend("• " + entry.label for entry in self._shop_options())
            market_lines = ["【黑市专属（材料以物易物）】"]
            for recipe in self.catalog.recipes:
                out_key = recipe["output"]
                market_lines.append(f"• {self.catalog.items[out_key]['name']}：{self._item_brief(out_key)}")
            rare_lines = ["【禁区珍稀 & 加密盲盒】"]
            for key in ("vip_card", "crate_blue", "crate_purple", "crate_gold"):
                rare_lines.append(f"• {self.catalog.items[key]['name']}：{self._item_brief(key)}")
            sections = (shop_lines, market_lines, rare_lines)
            page = _ItemPage.select(page_text, len(sections), page_size=1)
            lines = [
                page.heading("🎒 道具与特殊物品全介绍（发「查 名称」看获取途径）"),
                *sections[page.number - 1],
                "💡 携带进图：「下潜 地图 | 冰美式 2 | 烟雾弹」；局内发「使用 名称」，替身娃娃揣包里自动生效。",
            ]
            choices = (Choice("逛商店", "shop"), Choice("逛黑市", "black_market"),
                       Choice("词条流派", "help", "词条"), Choice("游玩攻略", "help", "攻略"))
            return self.reply(
                "\n".join(lines), choices + page.navigation("help", f"{category} 页"),
                plain=False, count_cooldown=not page_text, as_image=True, forward_title="物品与道具大全",
            )
        if category in ("词条", "流派", "词条介绍"):
            grouped: dict[str, list[str]] = {}
            for affix in self.catalog.affixes.values():
                grouped.setdefault(affix["school"], []).append(f"【{affix['name']}】{affix['description']}")
            page = _ItemPage.select(page_text, len(grouped), page_size=2)
            lines = [page.heading("🧰 装备词条全解（绿装 0 条 / 蓝装 1 条 / 紫金装 2 条 / 红装 3 条，可「重铸」重洗）")]
            for school, entries in list(grouped.items())[page.rows]:
                lines.append(f"▸ {school}")
                lines.extend(f"  {entry}" for entry in entries)
            choices = (Choice("装备库", "equipment"), Choice("装备图鉴", "help", "装备"), Choice("游玩攻略", "help", "攻略"))
            return self.reply(
                "\n".join(lines), choices + page.navigation("help", f"{category} 页"),
                plain=False, count_cooldown=not page_text, as_image=True, forward_title="词条与流派大全",
            )
        if category in ("装备", "底材", "装备介绍"):
            grouped: dict[str, list[str]] = {}
            for spec in self.catalog.equipment.values():
                stat_text = " ".join(f"{STAT_NAMES[f]}{v:+d}" for f, v in spec["stats"].items())
                grouped.setdefault(spec["slot"], []).append(f"{spec['name']}（{stat_text}）")
            page = _ItemPage.select(page_text, len(grouped), page_size=2)
            lines = [page.heading("🛡️ 装备底材图鉴（四个部位各一件 · 品质倍率：绿×1.25 / 蓝×1.75 / 紫×2.45 / 金×3.35 / 红×4.25）")]
            for slot, entries in list(grouped.items())[page.rows]:
                lines.append(f"▸ {slot}：{'；'.join(entries)}")
            lines.append("💡 蓝装及以上自带词条；发「一键配装」自动挑最高分，发「重铸」可用金币+蓝料/洗练石重洗词条。")
            choices = (Choice("装备库", "equipment"), Choice("词条流派", "help", "词条"), Choice("游玩攻略", "help", "攻略"))
            return self.reply(
                "\n".join(lines), choices + page.navigation("help", f"{category} 页"),
                plain=False, count_cooldown=not page_text, as_image=True, forward_title="装备底材图鉴",
            )
        if topic in ("合成", "黑市", "兑换"):
            return self.black_market("", "")
        return self.lookup(topic, "")

    def lookup(self, text: str, _target: str) -> Reply:
        if heirlooms.piece_key(text) or heirlooms.set_key(text) and not text.strip().isdigit():
            return self.heirloom_menu(text,"")
        if not text:
            return self.reply("🔍 想查什么？可以发「查 脉冲手雷」「查 秃鹫嗅觉」「查 装备盲盒」，或直接点分类。",
                              (Choice("游玩攻略", "help", "攻略"), Choice("物品介绍", "help", "道具"),
                               Choice("词条流派", "help", "词条"), Choice("黑市配方", "black_market")), plain=False, as_image=True, forward_title="电梯百科")
        choices = []
        for key, (title, names, desc) in SHOP_VIRTUAL_LOOKUP.items():
            choices.append(ItemChoice(title, title, names, {"_virtual": key, "description": self._item_brief(key) if key in self.catalog.gear_boxes else desc}))
        for code, suit in self.catalog.gear_sets.items():
            choices.append(ItemChoice(suit["name"], suit["name"], (code,suit["name"],suit["name"]+"套装"), {"suit_code":code}))
        for key, item in self.catalog.items.items():
            names = self._item_names(key)
            choices.append(ItemChoice(item["name"], item["name"], names, {"_key": key, **item}))
        for key, field_item in self.catalog.field_items.items():
            choices.append(ItemChoice(field_item["name"],field_item["name"],(key,field_item["name"]),{"field_item":field_item}))
        for key, perk in self.catalog.affixes.items():
            names = (key, perk["name"])
            choices.append(ItemChoice(perk["name"], perk["name"], names, perk))
        for key, item in self.catalog.equipment.items():
            choices.append(ItemChoice(item["name"], item["name"], (key, item["name"]), item))
        for set_name, set_spec in self.catalog.sets.items():
            members = [it["name"] for it in self.catalog.collections.values() if it["set"] == set_name]
            choices.append(ItemChoice(set_name, set_name, (set_name,), {"_set_entry": set_name, "members": members, **set_spec}))
        found = self._pick(text, choices, "lookup", "想查哪一项？")
        if isinstance(found, Reply):
            return found
        spec = found.item
        line = f"🔍 {found.label}"
        detail_lines: list[str] = []
        if spec.get("_set_entry"):
            is_rainbow_set = spec["_set_entry"] == "虹彩共鸣"
            line += " · 🌈 虹彩套装共鸣" if is_rainbow_set else " · 💎 收藏套装"
            detail_lines.append(f"✨ 套装效果：{spec['description']}")
            if spec.get("members"):
                detail_lines.append("📦 套装成员：" + "、".join(spec["members"]))
            if spec.get("lore"):
                detail_lines.append(f"📜 档案逸闻：{spec['lore']}")
            if is_rainbow_set:
                detail_lines.append("👑 获取：集齐四件首领专属虹彩背包技能后自动永久觉醒。")
            return self.reply(line + "\n" + "\n".join(detail_lines), plain=False, as_image=True, forward_title="图鉴查询")
        if spec.get("description"):
            detail_lines.append(f"✨ 效果：{spec['description']}" if spec.get("boss_only") else spec["description"])
        if spec.get("field_item"):
            item=spec["field_item"]
            origin=self.catalog.maps[item["map"]]["name"] if item["map"]!="all" else "全图通用"
            detail_lines=[origin+" · 仅本局拾取与使用（离梯清空）",expedition.field_description(item)]
        elif spec.get("boss_only"):
            line += " · 🌈 虹彩背包技能（虹彩共鸣）"
            if spec.get("lore"):
                detail_lines.append(f"📜 档案逸闻：{spec['lore']}")
            detail_lines.append("👑 正式首领独立有 0.5% 掉落一件虹彩，四款等概率。掉落即入仓，首次获得永久点亮，同款可收藏多件。")
        elif spec.get("set"):
            line += f" · {spec['set']} · 基础估值 {spec['value']:,} 金"
            if spec.get("map"):
                detail_lines.append(f"出处：{self.catalog.maps[spec['map']]['name']}（第 {spec.get('min_floor', 1)} 层起）")
        elif spec.get("suit_code"):
            code = spec["suit_code"]
            detail_lines = self.catalog.gear_set_description(code).splitlines()
            detail_lines.extend(f"{piece['slot']} · {piece['name']}" for piece in self.catalog.equipment.values() if piece.get("gear_set") == code)
        elif spec.get("school"):
            line += f" · {spec['school']}"
        elif "stats" in spec:
            line += f" · {spec['slot']}底材"
            detail_lines = ["基础属性：" + " · ".join(f"{STAT_NAMES[field]} {value:+d}" for field, value in spec["stats"].items())]
            if spec.get("gear_set"):
                detail_lines.extend(self.catalog.gear_set_description(spec["gear_set"]).splitlines())
        elif spec.get("rarity"):
            line += f" · {self.catalog.rarities[spec['rarity']]['name']}"
            item_key = spec.get("_key", "")
            sources: list[str] = []
            if item_key in self.catalog.shop:
                sources.append(f"商店 {self.catalog.shop[item_key]:,} 金")
            recipe_hit = next((r for r in self.catalog.recipes if r["output"] == item_key), None)
            if recipe_hit:
                sources.append(f"黑市兑换（{self._recipe_requirements(recipe_hit)}）")
            if spec.get("map"):
                sources.append(f"{self.catalog.maps[spec['map']]['name']}掉落")
            elif spec.get("kind") == "supply":
                sources.append("禁区宝箱/怪物掉落")
            elif spec.get("kind") == "crate":
                sources.append("禁区掉落 / 支援群友奖励")
            if sources:
                detail_lines.append("获取：" + " / ".join(sources))
            used_for = [
                self.catalog.items[r["output"]]["name"]
                for r in self.catalog.recipes
                if item_key in r["requires"]
            ]
            if used_for:
                detail_lines.append("黑市用途：可用于兑换【" + "】【".join(used_for) + "】（别急着当垃圾卖啦）")
            elif spec.get("kind") == "material" and spec.get("rarity") == "blue":
                detail_lines.append("用途：可卖钱，也可留作「重铸」词条或黑市换洗练石。")
            elif spec.get("kind") == "supply":
                if item_key == "doll":
                    detail_lines.append("用法：下潜前带上（如「下潜 商场 | 替身娃娃」），揣包里自动触发保命。")
                else:
                    detail_lines.append(f"用法：下潜前带上（如「下潜 商场 | {found.label}」），局内发「使用 {found.label}」。")
        return self.reply(line + ("\n" + "\n".join(detail_lines) if detail_lines else ""), plain=False, as_image=True, forward_title="图鉴查询")

    def gallery(self, text: str, _target: str) -> Reply:
        selected_set = next((name for name in self.catalog.sets if text and text in name), None)
        stats, _ = self._stats()
        count = len(self.archives)
        label = f"💎 {self.player['nickname']}的陈列室 · {count}/{len(self.catalog.collections)}"
        vb = self._veteran_bonus()
        wallet = (f"{self.player['gold']:,} 金 · 通行证 {self.player['tickets']}/{self.ticket_cap} "
                  f"· 撤离 {self.player['extracts']} 次（资深加成 血+{vb['hp']} 攻+{vb['atk']} 防+{vb['def']}）")
        panel = " · ".join(f"{STAT_NAMES[key]} {amount}" for key, amount in stats.items())
        if selected_set:
            label += f" · {selected_set}"
        groups, wall, rainbow = [], [], []
        for name, data in self.catalog.sets.items():
            if selected_set and name != selected_set:
                continue
            members = [(key, item) for key, item in self.catalog.collections.items() if item["set"] == name]
            collected = sum(key in self.archives for key, _ in members)
            group = f"{name} {collected}/{len(members)}" + (f" · {data['description']}" if name in self.completed_sets else "")
            stamps = []
            for key, item in members:
                mark = "●" if key in self.archives else "○"
                stamps.append(f"{mark} {item['name']}")
            if name == "虹彩共鸣":
                rainbow = [f"【虹彩套装】{group}", *stamps]
            else:
                groups.append(group)
                wall.extend(stamps)
        titles = [
            f"{row['month']} {row['title']}" for row in self.db.execute(
                "SELECT month,title FROM season_titles WHERE user_id=? ORDER BY month DESC,category LIMIT 3", (self.uid,),
            )
        ]
        collected_names = [self.catalog.collections[key]["name"] for key in self.archives
                           if key in self.catalog.collections and key not in self.catalog.rainbow]
        active=expedition.milestones(self.catalog.progression,count,len(self.catalog.collections))
        progress=f"阶段增益 {len(active)}/{len(self.catalog.progression['atlas'])} · 发「图鉴进度」看全部背包技能"
        summary = [label, wallet, panel, progress]
        summary.extend(" ｜ ".join(groups[i:i+4]) for i in range(0, len(groups), 4))
        if titles:
            summary.append("🏅 " + " ｜ ".join(titles))
        if collected_names:
            summary.append("最近收藏 · " + "、".join(collected_names[-3:]))
        summary.extend(rainbow)
        return self.reply("\n".join(summary), (Choice("装备", "equipment"), Choice("排行榜", "rank"),
                                               Choice("大厅", "menu")), plain=False,
                          card="陈列室", card_lines=tuple([*summary[:4], *groups, *titles, *wall, *rainbow]), forward_title="陈列室")

    def _board_lines(self, category: str, map_id: str, *, all_groups: bool, overview: bool = False) -> list[str]:
        metric, _, order = BOARD[category]
        table = "season_map_stats" if map_id else "season_stats"
        extra = "" if all_groups else (
            "JOIN group_active_users g ON g.user_id=s.user_id AND g.month=s.month AND g.group_id=? "
        )
        parameters = (self.month,) if all_groups else (self.gid, self.month)
        map_filter = " AND s.map_id=?" if map_id else ""
        if map_id:
            parameters += (map_id,)
        rows = self.db.execute(
            f"SELECT p.nickname,s.*,a.payload AS active_payload,a.updated_at AS active_at FROM {table} s "
            "JOIN players p ON p.user_id=s.user_id LEFT JOIN active_runs a ON a.user_id=s.user_id " + extra
            + f"WHERE s.month=? AND s.{metric}>0{map_filter} ORDER BY {order},s.user_id LIMIT ?",
            (*parameters, 3 if overview else 8),
        ).fetchall()
        if not rows:
            return ["本月暂无记录。"]
        if not map_id:
            unit = BOARD_UNITS[category]
            return [f"{i}. {row['nickname']} · {row[metric]:,} {unit}" for i, row in enumerate(rows, 1)]
        results = []
        for i, row in enumerate(rows, 1):
            reached, exited = row["max_floor"], row["best_extract_floor"]
            line = f"{i}. {row['nickname']} · 到达 {reached} 层"
            if not overview:
                if exited:
                    line += f" · 最高撤离 {exited} 层，带出 {row['best_extract_value']:,} 金"
                else:
                    active = json.loads(row["active_payload"]) if (
                        row["active_at"] is not None and self.at - row["active_at"] < RUN_TIMEOUT
                    ) else {}
                    pending = not active.get("sim") and active.get("floor") == reached and active.get("map") == map_id
                    line += " · 探险中，带出待结算" if pending else " · 暂无带出记录"
            results.append(line)
        return results

    def _board(self, text: str, *, all_groups: bool) -> Reply:
        text = text.strip()
        action, other_action = ("all_rank", "rank") if all_groups else ("rank", "all_rank")
        scope, other_scope = ("全服", "本群") if all_groups else ("本群", "全服")
        depth_words = {"探索", "探索榜", "深度"}
        maps = self.catalog.maps
        if text in depth_words:
            return self.reply(
                f"🏆 {self.month} {scope}探索榜 · 选择地图",
                (*tuple(Choice(info["name"], action, key) for key, info in maps.items()),
                 Choice("查看全部榜单", action)),
                plain=False, count_cooldown=False, multiline_menu=True,
            )
        parts = text.split(maxsplit=1)
        map_query = parts[1] if len(parts) == 2 and parts[0] in depth_words else text
        map_id = self._resolve_map_key(map_query) if map_query != "推荐" else ""
        board_name = text.removesuffix("榜")
        category = "深度" if map_id else BOARD_ALIASES.get(board_name, board_name)
        if not map_id and category not in BOARD_UNITS:
            lines = [f"🏆 {self.month} {scope}月榜 · 各榜前三"]
            choices = []
            for key, info in maps.items():
                label = info["name"]
                lines.extend([f"【{label}】", *self._board_lines("深度", key, all_groups=all_groups, overview=True)])
                choices.append(Choice(label, action, key))
            for label in BOARD_UNITS:
                title = label + "榜"
                lines.extend([f"【{title}】", *self._board_lines(label, "", all_groups=all_groups, overview=True)])
                choices.append(Choice(title, action, label))
            choices.append(Choice("切到" + other_scope, other_action))
            return self.reply("\n".join(lines), tuple(choices), plain=False, count_cooldown=False,
                              card="赛季榜", card_lines=tuple(lines), forward_title="赛季月榜")
        title = "探索榜 · " + maps[map_id]["name"] if map_id else "月榜 · " + BOARD[category][1]
        header = f"🏆 {self.month} {scope}{title}"
        results = self._board_lines(category, map_id, all_groups=all_groups)
        argument = map_id or category
        choices = (Choice("切到" + other_scope, other_action, argument),
                   Choice("切换地图" if map_id else "按地图看探索榜", action, "深度"),
                   Choice("查看全部榜单", action))
        return self.reply("\n".join([header, *results[:5]]), choices, plain=False,
                          card="赛季榜", card_lines=tuple([header, *results]), forward_title="赛季月榜")

    def list_orders(self, _text: str, _target: str) -> Reply:
        rows = self.db.execute(
            "SELECT order_id,summary,steps_json,expires_at FROM ai_orders "
            "WHERE user_id=? AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 5",
            (self.uid, self.at),
        ).fetchall()
        if not rows:
            return self.reply("📋 你目前没有待确认的 AI 派单。直接@龙娘说你想怎么操作（如「帮我清装备买箱子配装」），她会算好后给你派单。", count_cooldown=False)
        lines = ["📋 你的待确认派单："]
        choices = []
        for r in rows:
            steps = json.loads(r["steps_json"])
            left_m = max(1, (r["expires_at"] - self.at + 59) // 60)
            lines.append(f"• 单号 {r['order_id']}（剩 {left_m} 分钟）｜{r['summary']}（共 {len(steps)} 步）")
            choices.append(Choice(f"确认 {r['order_id']}", "confirm_order", r["order_id"]))
            choices.append(Choice(f"取消 {r['order_id']}", "cancel_order", r["order_id"]))
        return self.reply("\n".join(lines), tuple(choices[:6]), plain=True, count_cooldown=False)

    def cancel_order(self, text: str, _target: str) -> Reply:
        oid = text.strip().upper()
        if oid:
            row = self.db.execute("SELECT * FROM ai_orders WHERE order_id=?", (oid,)).fetchone()
            if row is None:
                raise GameError(f"没有找到派单号 {oid}。")
            if row["user_id"] != self.uid:
                raise GameError("这张派单属于另一位乘客，只能由本人取消。")
            if row["status"] != "pending":
                raise GameError(f"派单 {oid} 当前状态已是「{row['status']}」，无需取消。")
        else:
            row = self.db.execute(
                "SELECT * FROM ai_orders WHERE user_id=? AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 1",
                (self.uid, self.at),
            ).fetchone()
            if row is None:
                raise GameError("目前没有可取消的待确认派单。")
            oid = row["order_id"]
        self.db.execute("UPDATE ai_orders SET status='cancelled' WHERE order_id=?", (oid,))
        return self.reply(f"🗑️ 已取消派单 {oid}（{row['summary']}）。", count_cooldown=False)

    def confirm_order(self, text: str, _target: str) -> Reply:
        oid = text.strip().upper()
        if oid:
            row = self.db.execute("SELECT * FROM ai_orders WHERE order_id=?", (oid,)).fetchone()
            if row is None:
                raise GameError(f"没有找到派单号 {oid}，请核对单号或发「我的派单」查看。")
            if row["user_id"] != self.uid:
                raise GameError("这张派单属于另一位乘客，只能由本人确认执行。")
            if row["status"] == "executed":
                raise GameError(f"派单 {oid} 已经执行过了，不会重复操作。")
            if row["status"] == "cancelled":
                raise GameError(f"派单 {oid} 已取消，如需操作请重新派单。")
            if row["status"] != "pending" or self.at > row["expires_at"]:
                self.db.execute("UPDATE ai_orders SET status='expired' WHERE order_id=?", (oid,))
                raise GameError(f"派单 {oid} 已超过 10 分钟有效期，请让龙娘重新算一次派单。")
        else:
            rows = self.db.execute(
                "SELECT * FROM ai_orders WHERE user_id=? AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 3",
                (self.uid, self.at),
            ).fetchall()
            if not rows:
                raise GameError("你目前没有待确认的有效派单。")
            if len(rows) > 1:
                lines = ["你有多个待确认派单，请指定单号（如「确认 " + rows[0]["order_id"] + "」）或点下面序号："]
                choices = tuple(Choice(f"确认 {r['order_id']}（{r['summary'][:12]}）", "confirm_order", r["order_id"]) for r in rows)
                return self.reply("\n".join(lines), choices, plain=True, count_cooldown=False)
            row = rows[0]
            oid = row["order_id"]

        self.db.execute("UPDATE ai_orders SET status='executed' WHERE order_id=?", (oid,))
        steps = json.loads(row["steps_json"])
        compact = len(steps) > _ORDER_COMPACT_AFTER
        starting_gold = self.player["gold"]
        results: list[str] = [f"📋 派单 {oid} 执行回执（{row['summary']}）"]
        succeeded = 0
        failure = ""
        for idx, step in enumerate(steps, 1):
            act = step["action"]
            arg = step["argument"]
            note = step.get("note") or f"{act} {arg}".strip()
            if act not in OPEN_AI_COMMANDS:
                failure = f"❌ {idx}. {note} → 不支持的接口 {act}"
                break
            exec_arg = arg
            if act == "sell" and exec_arg and not exec_arg.startswith("确认 ") and not exec_arg.endswith(" 确认"):
                exec_arg = f"确认 {exec_arg}"
            sp = f"ai_order_step_{idx}"
            before = copy.deepcopy((self.player, self.run, self.archives, self.completed_sets, self._quest_notes))
            limit_count = len(self.limit_notices)
            self.db.execute(f"SAVEPOINT {sp}")
            try:
                step_reply = self.execute_action(act, exec_arg, "", confirmed_order=True)
                self.db.execute(f"RELEASE {sp}")
                succeeded += 1
                if compact and succeeded > _ORDER_RECEIPT_PREVIEW:
                    continue
                step_lines = [ln for ln in (step_reply.text or "").splitlines() if ln.strip()]
                if compact:
                    label = " ".join(note.split())
                    detail = step_lines[0].strip() if step_lines else "完成"
                    if len(detail) > 120:
                        detail = detail[:119] + "…"
                    results.append(f"✅ {idx}. {label} → {detail}")
                elif len(steps) == 1 and step_lines:
                    results.append(f"✅ {note}")
                    results.extend(step_lines[:16])
                elif step_lines:
                    results.append(f"✅ {idx}. {note}")
                    for ln in step_lines[:10]:
                        results.append(f"   {ln}")
                else:
                    results.append(f"✅ {idx}. {note} → 完成")
            except GameError as err:
                self.db.execute(f"ROLLBACK TO {sp}")
                self.db.execute(f"RELEASE {sp}")
                self.player, self.run, self.archives, self.completed_sets, self._quest_notes = before
                del self.limit_notices[limit_count:]
                failure = f"❌ {idx}. {note} → 中止：{err}"
                break

        if compact and succeeded > _ORDER_RECEIPT_PREVIEW:
            results.append(f"另有 {succeeded - _ORDER_RECEIPT_PREVIEW} 步已完成。")
        if failure:
            results.append(failure)
        if self.run is not None:
            status_tail = f"🛗 当前状态：{self._map_title()} · HP {self.run['hp']}/{self.run['stats']['hp']} ｜ 钱包 {self.player['gold']:,} 金"
        else:
            status_tail = f"💰 当前钱包：{self.player['gold']:,} 金 ｜ 🎫 通行证：{self.player['tickets']}/{self.ticket_cap}"
        gold_delta = self.player["gold"] - starting_gold
        if compact and gold_delta:
            status_tail += f"｜本单净变化 {gold_delta:+,} 金"
        results.append(f"完成 {succeeded}/{len(steps)} 步{'（后续已中止）' if failure else '（全部完成）'}｜{status_tail}")
        body = "\n".join(results)
        fwd = f"派单回执 {oid}" if len(results) > 18 else ""
        return self.reply(body, self.actions(), plain=True, count_cooldown=False, forward_title=fwd)

    def rank(self, text: str, _target: str) -> Reply:
        return self._board(text, all_groups=False)

    def all_rank(self, text: str, _target: str) -> Reply:
        return self._board(text, all_groups=True)

    # ============================================================ 来自群星的寓言集
    def prophecy_menu(self, _text: str, _target: str) -> Reply:
        prophecy.settle_completed_weeks(self.db, self.prophecy_week, self.catalog, self.at)
        cfg = prophecy.weekly_config(self.prophecy_week, self.catalog)
        unlock = prophecy.unlock_status(self.catalog, self.archives)
        me = prophecy.player_record(self.db, self.prophecy_week, self.uid)
        top = prophecy.leaderboard(self.db, self.prophecy_week, limit=3)
        gear_line = " / ".join(f"{g['name']}({self.catalog.rarities[g['rarity']]['name']})" for g in cfg["gear"])
        supply_line = "、".join(f"{s['name']}×{s['quantity']}" for s in cfg["supplies"])
        lines = [
            f"✦ 【{prophecy.TITLE}】{self.prophecy_week} · {cfg['name']}",
            f"🌌 「{cfg['omen']}」",
            f"⚡ 高难 AI 试炼 · 上限 F{prophecy.MAX_FLOOR} · 零费用入场 · 零带出（不产出虹彩/闪耀/物资）",
            "────────────────────────",
            f"⚔️ 本周固定配装：{gear_line}",
            f"✨ 本周固定祝福（{len(cfg['blessings'])}槽）：{'、'.join(cfg['blessing_names']) or '无'}",
            f"🌈 本周固定虹彩（{len(cfg['rainbows'])}件）：{'、'.join(cfg['rainbow_names']) or '无'}",
            f"🎒 本周统一补给：{supply_line}",
            prophecy.STAR_RULE,
            f"📊 统一入场面板：HP {cfg['stats']['hp']} · 攻 {cfg['stats']['atk']} · 防 {cfg['stats']['def']} · 敏 {cfg['stats']['agi']} · 运 {cfg['stats']['luk']}",
            "────────────────────────",
            (
                f"🔓 入场资格：已解锁（红/金藏品图鉴 {unlock['owned']}/{unlock['total']}）"
                if unlock["unlocked"]
                else f"🔒 入场资格：需集齐全部红/金藏品图鉴（当前 {unlock['owned']}/{unlock['total']}，还差 {unlock['missing_count']} 件：{'、'.join(unlock['missing_preview'])}）"
            ),
        ]
        if self._is_prophecy():
            lines.append("本局星器：" + " ｜ ".join(f"{r['name']} {r['rank']}阶（{r['label']} +{r['bonus']}）"
                                                  for r in prophecy.star_rows(self.run)))
        if me:
            lines.append(f"🏅 本周个人纪录：全服第 {me['rank']} 名 · 推进 F{me['max_floor']}（通过 F{me['cleared_floor']} · 挑战 {me['attempts']} 次）")
        if top:
            lines.append("🏆 本周前三：" + " ｜ ".join(f"{i}.{r['nickname']}(F{r['cleared_floor']})" for i, r in enumerate(top, 1)))
        choices = []
        if unlock["unlocked"] and self.run is None:
            choices.append(Choice("✦ 踏入星兆（免费挑战）", "prophecy_start"))
        choices.extend((Choice("本周寓言榜", "prophecy_rank"), Choice("上周结算榜", "prophecy_rank", "上周"), Choice("返回大厅", "menu")))
        return self.reply("\n".join(lines), tuple(choices), plain=True, as_image=True, forward_title=prophecy.TITLE, count_cooldown=False)

    def prophecy_pick(self, text: str, _target: str) -> Reply:
        run = self._check_run()
        if not run.get("prophecy"):
            raise GameError("星匣只会出现在群星寓言集试炼内。")
        parts = text.split()
        if len(parts) != 2 or not parts[0].isascii() or not parts[0].isdigit() or len(parts[0]) > 4:
            raise GameError("用「嵌入星器 层数 星器名」选择当前星匣的一件。")
        key = next((k for k, spec in prophecy.STAR_RELICS.items() if parts[1] == spec[0]), parts[1])
        try:
            note = prophecy.pick_star(run, int(parts[0]), key)
        except ValueError as exc:
            raise GameError(str(exc)) from exc
        self._floor_result()["star"] = note
        return self.reply(note, count_cooldown=False)

    def prophecy_start(self, _text: str, _target: str) -> Reply:
        if not self.player.get("name_confirmed") and self.player["runs"] == 0 and self.run is None:
            return self._first_time_name_prompt()
        self._check_idle()
        prophecy.settle_completed_weeks(self.db, self.prophecy_week, self.catalog, self.at)
        unlock = prophecy.unlock_status(self.catalog, self.archives)
        if not unlock["unlocked"]:
            missing = "、".join(unlock["missing_preview"])
            raise GameError(
                f"【{prophecy.TITLE}】需要集齐全部红、金藏品图鉴才可入场"
                f"（当前 {unlock['owned']}/{unlock['total']}，还差 {unlock['missing_count']} 件，例如：{missing}）。"
            )
        cfg = prophecy.weekly_config(self.prophecy_week, self.catalog)
        kit_rng = Random(f"prophecy:kit:{self.prophecy_week}")
        kit = []
        for entry in cfg["supplies"]:
            for _ in range(entry["quantity"]):
                it = self.catalog.ordinary_item(entry["id"], kit_rng)
                it["sim_kit"] = True
                kit.append(it)
        stats = copy.deepcopy(cfg["stats"])
        perks = dict(cfg["perks"])
        self.run = {
            "map": cfg["base_map"],
            "prophecy": True,
            "prophecy_week": self.prophecy_week,
            "prophecy_name": cfg["name"],
            "star_ranks": {},
            "floor": 1,
            "hp": stats["hp"],
            "stats": stats,
            "perks": perks,
            "bag": kit,
            "safe": [],
            "safe_extra": 0,
            "tea_until": 0,
            "sos_used": False,
            "last_stand": False,
            "lockpick": False,
            "peek": "",
            "gold_room": False,
            "guaranteed_loot": False,
            "shield": int(perks.get("shield_start", 0)) + int(perks.get("prism_shield", 0)),
            "atk_boost": 0.0,
            "combat_cleared_floor": 0,
            "last_killed_name": "",
            "sim": False,
            "sim_gold": 15000,
            "value_bonus": 0.0,
            "rare_bonus": 0.0,
            "event_bonus": 0.0,
            "field_seed": f"prophecy:{self.prophecy_week}",
            "rules_version": 2,
            "archive_version": expedition.ARCHIVE_VERSION,
            "event": {"kind": "ready"},
        }
        intro = (
            f"✦ ━━━ 星门开启 · 【{prophecy.TITLE}】 ━━━ ✦\n"
            f"🌌 {self.prophecy_week} · {cfg['name']}（上限 F{prophecy.MAX_FLOOR} · 0 消耗 · 零带出）\n"
            f"⚔️ 固定构筑：{' / '.join(g['name'] for g in cfg['gear'])}\n"
            f"✨ 固定祝福：{'、'.join(cfg['blessing_names'])} ｜ 🌈 固定虹彩：{'、'.join(cfg['rainbow_names'])}\n"
            f"{prophecy.STAR_RULE}\n"
        )
        return self.reply(intro + self._enter_floor())

    def prophecy_rank(self, text: str, _target: str) -> Reply:
        prophecy.settle_completed_weeks(self.db, self.prophecy_week, self.catalog, self.at)
        query = text.strip()
        if query in ("上周", "上期", "prev", "last"):
            target_week = prophecy.previous_week_key(self.prophecy_week)
            finished = True
        elif query and len(query) <= 10 and "-W" in query.upper():
            target_week = query.upper()
            finished = target_week < self.prophecy_week
        else:
            target_week = self.prophecy_week
            finished = False
        settled_row = self.db.execute("SELECT summary FROM prophecy_settled WHERE week=?", (target_week,)).fetchone()
        if settled_row is not None:
            body = settled_row["summary"]
        else:
            try:
                cfg = prophecy.weekly_config(target_week, self.catalog)
            except Exception as exc:
                raise GameError("周次格式无效，请输入如 2026-W48 或「上周」。") from exc
            rows = prophecy.leaderboard(self.db, target_week, limit=15)
            body = prophecy.format_board_text(target_week, cfg, rows, finished=finished)
        choices = (
            Choice("本周寓言榜", "prophecy_rank"),
            Choice("上周结算榜", "prophecy_rank", "上周"),
            Choice("寓言集详情", "prophecy"),
        )
        return self.reply(body, choices, plain=True, as_image=True, forward_title=f"{prophecy.TITLE}周榜", count_cooldown=False)

    # ============================================================ 电梯交易所
    def _resolve_exchange_stock(self, token: str) -> dict:
        stock = self._stock()
        if token.startswith("#") and token[1:].isdigit():
            sid = int(token[1:])
            row = next((r for r in stock if r["stock_id"] == sid), None)
            if row is None:
                raise GameError(f"仓库中找不到编号 {token} 的物品。")
            return row
        q = token.casefold()
        matches = [
            r for r in stock
            if q == r["id"].casefold()
            or q == self.catalog.name(r, symbol=False).casefold()
            or q == self.catalog.definition(r["id"])["name"].casefold()
            or q in {a.casefold() for a in self.catalog.definition(r["id"]).get("aliases", ())}
        ]
        if not matches:
            raise GameError(f"仓库里找不到「{token}」，可用仓库编号（如 #12）或准确名称。")
        return max(matches, key=lambda r: (r["quantity"], r["value"]))

    def _resolve_exchange_equip(self, token: str) -> dict:
        if not token.startswith("#") or not token[1:].isdigit():
            raise GameError("挂单或交换装备请使用装备编号，例如「装备 #12」。")
        eid = int(token[1:])
        row = next((r.item for r in self._equipment_options() if r.item["equip_id"] == eid), None)
        if row is None:
            raise GameError(f"装备库中找不到装备 #{eid}。")
        return row

    def exchange_board(self, text: str, _target: str) -> Reply:
        listings = exchange.visible_listings(self.db, self.catalog, self.uid, self.at, self.month)
        mode = text.strip()
        if mode in ("我的", "mine"):
            filtered = [r for r in listings if r["is_mine"]]
            title = "我的挂单"
        elif mode in ("私密", "1对1", "定向", "direct"):
            filtered = [r for r in listings if r["scope"] == "direct"]
            title = "1 对 1 交易"
        else:
            filtered = listings
            title = "交易所大厅"
        page = _ItemPage.select(mode if mode.isdigit() else "", len(filtered), page_size=8)
        lines = [
            page.heading(f"⚖️ 无尽电梯交易所 · {title}"),
            "规则：支持公开售卖与 1 对 1 交易；支持金币买卖、同品质以物换物或两者兼可；虹彩仅允许交易重复副本（至少留 1 件）。",
        ]
        choices: list[Choice] = []
        if not filtered:
            lines.append("当前暂无进行中的挂单。")
        for row in filtered[page.rows]:
            scope_tag = "🤝1对1" if row["scope"] == "direct" else "📢公开"
            if row["mode"] == "both":
                terms = f"💰 {row['price_gold']:,} 金 或 🔄 换 {row['want_item_name']}"
            elif row["mode"] == "gold":
                terms = f"💰 {row['price_gold']:,} 金"
            else:
                terms = f"🔄 换 {row['want_item_name']}"
            who = "【我】" if row["is_mine"] else ("【发给我】" if row["is_for_me"] else row["seller_name"])
            lines.append(
                f"#{row['listing_id']} {scope_tag} [{row['rarity_label']}{row['category_label']}] "
                f"{row['item_name']} ×{row['quantity']} ｜ {terms} ｜ 卖家：{who}"
            )
            if row["is_mine"]:
                choices.append(Choice(f"撤单 #{row['listing_id']}", "exchange_cancel", str(row["listing_id"])))
            elif row["mode"] in ("gold", "both"):
                choices.append(Choice(f"金币买 #{row['listing_id']}({row['price_gold']:,}金)", "exchange_buy", str(row["listing_id"])))
            elif row["is_for_me"]:
                choices.append(Choice(f"拒绝 #{row['listing_id']}", "exchange_reject", str(row["listing_id"])))
        lines.append("快捷指令：挂单 #仓库编号 金币 价格 ｜ 挂单 装备 #编号 金币 价格 ｜ 挂单 闪耀 B01-1 换 B08-4 ｜ 挂单 给 QQ号 熔阳刻痕 金币 1500000 ｜ 交易 挂单号 [交换物]")
        choices.extend(page.navigation("exchange"))
        choices.extend((Choice("我的挂单", "exchange", "我的"), Choice("1对1交易", "exchange", "私密"), Choice("返回大厅", "menu")))
        return self.reply("\n".join(lines), tuple(choices), plain=True, as_image=True, forward_title="无尽电梯交易所", count_cooldown=False)

    def _parse_exchange_list_text(self, text: str) -> dict:
        if text.startswith("json:"):
            return json.loads(text[5:])
        tokens = text.strip().split()
        if not tokens:
            raise GameError(
                "用法示例：\n"
                "• 公开金币：挂单 #仓库编号 金币 50000（或 挂单 装备 #编号 金币 350000 / 挂单 闪耀 B01-1 金币 200000）\n"
                "• 公开换物：挂单 #仓库编号 换 [指定同品质同类物品名]\n"
                "• 1对1交易：挂单 给 对方QQ 物品名 金币 1500000（或 换 目标物品名）\n"
                f"最低底价：虹彩 {exchange.RAINBOW_MIN_GOLD:,} 金｜闪耀 {exchange.SHINING_MIN_GOLD:,} 金｜红装 350,000 金｜金装 80,000 金｜传世红藏品 ≥120,000 金"
            )
        scope, target_uid = "public", ""
        idx = 0
        if tokens[0] in ("给", "私密", "定向", "1对1") and len(tokens) >= 3:
            scope = "direct"
            target_uid = tokens[1].lstrip("@")
            idx = 2
        if idx >= len(tokens):
            raise GameError("请指定要上架的物品和交易条件。")
        asset_type = "inventory"
        stock_id = equip_id = None
        piece = ""
        quantity = 1
        if tokens[idx] in ("装备", "equip"):
            if idx + 1 >= len(tokens):
                raise GameError("请提供装备编号，如：挂单 装备 #12 金币 80000")
            asset_type = "equipment"
            equip_id = self._resolve_exchange_equip(tokens[idx + 1])["equip_id"]
            idx += 2
        elif tokens[idx] in ("闪耀", "祝福", "shining"):
            if idx + 1 >= len(tokens):
                raise GameError("请提供闪耀祝福名称或编号，如：挂单 闪耀 B01-1 金币 200000")
            asset_type = "shining"
            piece = tokens[idx + 1]
            idx += 2
        else:
            row = self._resolve_exchange_stock(tokens[idx])
            stock_id = row["stock_id"]
            idx += 1
            if idx < len(tokens) and tokens[idx].isdigit() and tokens[idx] not in ("金币", "换"):
                quantity = int(tokens[idx])
                idx += 1
        if idx >= len(tokens):
            raise GameError("请指定交易方式：「金币 价格」或「换 [目标物品]」。")
        mode_word = tokens[idx]
        idx += 1
        if mode_word in ("金币", "卖", "金", "gold"):
            if idx >= len(tokens) or not tokens[idx].isdigit():
                raise GameError("请填写整数金币价格。")
            price_val = int(tokens[idx])
            idx += 1
            if idx < len(tokens) and tokens[idx] in ("换", "或换", "以物换物", "换物", "barter"):
                want_item = " ".join(tokens[idx + 1:]).strip()
                if want_item in ("任意", "随便", "同品质"):
                    want_item = ""
                return {
                    "scope": scope, "target_user_id": target_uid, "mode": "both",
                    "asset_type": asset_type, "stock_id": stock_id, "equip_id": equip_id, "piece": piece,
                    "quantity": quantity, "price_gold": price_val, "want_item_id": want_item,
                }
            return {
                "scope": scope, "target_user_id": target_uid, "mode": "gold",
                "asset_type": asset_type, "stock_id": stock_id, "equip_id": equip_id, "piece": piece,
                "quantity": quantity, "price_gold": price_val, "want_item_id": "",
            }
        if mode_word in ("换", "以物换物", "换物", "barter"):
            rest = tokens[idx:]
            gold_kw_idx = next((i for i, tk in enumerate(rest) if tk in ("金币", "或金", "或卖", "gold")), None)
            if gold_kw_idx is not None:
                if gold_kw_idx + 1 >= len(rest) or not rest[gold_kw_idx + 1].isdigit():
                    raise GameError("请填写整数金币价格。")
                want_item = " ".join(rest[:gold_kw_idx]).strip()
                if want_item in ("任意", "随便", "同品质"):
                    want_item = ""
                return {
                    "scope": scope, "target_user_id": target_uid, "mode": "both",
                    "asset_type": asset_type, "stock_id": stock_id, "equip_id": equip_id, "piece": piece,
                    "quantity": quantity, "price_gold": int(rest[gold_kw_idx + 1]), "want_item_id": want_item,
                }
            want_item = " ".join(rest).strip()
            if want_item in ("任意", "随便", "同品质"):
                want_item = ""
            return {
                "scope": scope, "target_user_id": target_uid, "mode": "barter",
                "asset_type": asset_type, "stock_id": stock_id, "equip_id": equip_id, "piece": piece,
                "quantity": quantity, "price_gold": 0, "want_item_id": want_item,
            }
        raise GameError("交易方式请写「金币 <价格>」「换 [目标物品]」或「金币 <价格> 或换 [目标物品]」。")

    def exchange_list(self, text: str, target: str) -> Reply:
        self._check_idle()
        params = self._parse_exchange_list_text(text)
        if target and params.get("scope") == "public" and not params.get("target_user_id"):
            params["scope"] = "direct"
            params["target_user_id"] = target
        try:
            created = exchange.create_listing(
                self.db, self.catalog,
                seller_id=self.uid, seller_name=self.player["nickname"],
                loadout=json.loads(self.player["loadout"]), slots=self.heirloom_slots,
                scope=params["scope"], target_user_id=params.get("target_user_id", ""),
                mode=params["mode"], asset_type=params["asset_type"],
                stock_id=params.get("stock_id"), equip_id=params.get("equip_id"),
                piece=params.get("piece", ""), quantity=int(params.get("quantity", 1)),
                price_gold=int(params.get("price_gold", 0)), want_item_id=params.get("want_item_id", ""),
                now=self.at, month=self.month,
            )
        except exchange.ExchangeError as exc:
            raise GameError(str(exc)) from exc
        self.heirloom_focus, self.heirloom_slots, self.heirloom_owned = heirlooms.load(self.db, self.uid)
        scope_label = f"1 对 1 定向给 {created['target_user_id']}" if created["scope"] == "direct" else "公开售卖"
        if created["mode"] == "both":
            terms = f"{created['price_gold']:,} 金 或 换 {created['want_item_name']}"
        elif created["mode"] == "gold":
            terms = f"{created['price_gold']:,} 金"
        else:
            terms = f"同品质同分类换 {created['want_item_name']}"
        return self.reply(
            f"⚖️ 上架成功！挂单 #{created['listing_id']}（{scope_label}）\n"
            f"物品：{created['item_name']} ×{created['quantity']} ｜ 条件：{terms}\n"
            f"物品已由交易所托管，成交前可发「撤单 {created['listing_id']}」取回。",
            (Choice("查看交易所", "exchange"), Choice(f"撤单 #{created['listing_id']}", "exchange_cancel", str(created["listing_id"]))),
            plain=True,
        )

    def exchange_buy(self, text: str, _target: str) -> Reply:
        self._check_idle()
        if text.startswith("json:"):
            params = json.loads(text[5:])
            listing_id = int(params["listing_id"])
            offer_stock_id = params.get("offer_stock_id")
            offer_equip_id = params.get("offer_equip_id")
            offer_piece = params.get("offer_piece", "")
            pay_mode = params.get("pay_mode", "")
        else:
            pay_mode = ""
            tokens = text.strip().split()
            if not tokens:
                raise GameError("请指定挂单号，例如「交易 12」或「交易 12 装备 #5」/「交易 12 闪耀 B08-4」。")
            raw_id = tokens[0].lstrip("#")
            if not raw_id.isdigit():
                raise GameError("挂单号应为数字，例如「交易 12」。")
            listing_id = int(raw_id)
            offer_stock_id = offer_equip_id = None
            offer_piece = ""
            if len(tokens) >= 2:
                if tokens[1] in ("装备", "equip"):
                    if len(tokens) < 3:
                        raise GameError("请提供用于交换的装备编号，如「交易 12 装备 #5」。")
                    offer_equip_id = self._resolve_exchange_equip(tokens[2])["equip_id"]
                elif tokens[1] in ("闪耀", "祝福", "shining"):
                    if len(tokens) < 3:
                        raise GameError("请提供用于交换的闪耀祝福，如「交易 12 闪耀 B08-4」。")
                    offer_piece = tokens[2]
                else:
                    offer_stock_id = self._resolve_exchange_stock(tokens[1])["stock_id"]
        try:
            row, spent = exchange.fulfill_listing(
                self.db, self.catalog,
                listing_id=listing_id, buyer_id=self.uid, buyer_name=self.player["nickname"],
                buyer_gold=self.player["gold"],
                buyer_loadout=json.loads(self.player["loadout"]), buyer_slots=self.heirloom_slots,
                offer_stock_id=offer_stock_id, offer_equip_id=offer_equip_id, offer_piece=offer_piece,
                pay_mode=pay_mode, now=self.at, month=self.month,
            )
        except exchange.ExchangeError as exc:
            raise GameError(str(exc)) from exc
        if spent:
            self.player["gold"] -= spent
        self.heirloom_focus, self.heirloom_slots, self.heirloom_owned = heirlooms.load(self.db, self.uid)
        self.archives = {
            r["item_id"]: r["count"] for r in self.db.execute("SELECT item_id,count FROM collections WHERE user_id=?", (self.uid,))
        }
        pay_line = f"支付 {spent:,} 金" if spent else "完成同品质同分类以物换物"
        return self.reply(
            f"🤝 交易成交！挂单 #{listing_id}：{pay_line}，获得【{row['item_name']}】×{row['quantity']}（已入库）！",
            (Choice("返回交易所", "exchange"), Choice("查看仓库", "warehouse"), Choice("查看装备", "equipment")),
            plain=True,
        )

    def exchange_cancel(self, text: str, _target: str) -> Reply:
        self._check_idle()
        raw_id = text.strip().lstrip("#")
        if not raw_id.isdigit():
            raise GameError("请指定要撤销的挂单号，例如「撤单 12」。")
        try:
            row = exchange.cancel_listing(
                self.db, self.catalog, listing_id=int(raw_id), seller_id=self.uid, now=self.at, month=self.month,
            )
        except exchange.ExchangeError as exc:
            raise GameError(str(exc)) from exc
        self.heirloom_focus, self.heirloom_slots, self.heirloom_owned = heirlooms.load(self.db, self.uid)
        return self.reply(
            f"↩️ 已撤销挂单 #{row['listing_id']}，【{row['item_name']}】×{row['quantity']} 已退回你的仓库。",
            (Choice("查看交易所", "exchange"), Choice("返回大厅", "menu")),
            plain=True,
        )

    def exchange_reject(self, text: str, _target: str) -> Reply:
        self._check_idle()
        raw_id = text.strip().lstrip("#")
        if not raw_id.isdigit():
            raise GameError("请指定要拒绝的 1 对 1 交易挂单号，例如「拒单 12」。")
        try:
            row = exchange.reject_listing(
                self.db, self.catalog, listing_id=int(raw_id), user_id=self.uid,
                user_name=self.player["nickname"], now=self.at, month=self.month,
            )
        except exchange.ExchangeError as exc:
            raise GameError(str(exc)) from exc
        return self.reply(
            f"🚫 已拒绝来自 {row['seller_name']} 的 1 对 1 交易 #{row['listing_id']}（{row['item_name']}），物品已退回对方仓库。",
            (Choice("查看交易所", "exchange"), Choice("返回大厅", "menu")),
            plain=True,
        )
