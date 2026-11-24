"""自动化管理派单与流程精算引擎（Automation Work-Order Planner & Runner）。

核心职责：
  1. 识别到玩家需要自动化管理（农场 / 钓鱼 / 双栖）时，读取实时游戏存档与经济表，
     自主推演满足预算、时长、偏好与收益/经验目标的完整操作流程；
  2. 生成带全局唯一标识符（如 `ORD-7K9M2X`）的派单，落盘至 `state/automation_orders.json`，
     输出清晰步骤清单与测算指标供玩家确认；
  3. 玩家凭唯一标识符确认后（校验归属人与有效期），立即执行首轮即时步骤，并将冷却循环/成熟等待步骤
     交由后台异步调度器按真实时间轴自动执行，完成后主动向会话推送结算单。
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from nonebot import logger
from shared_history import _file_lock, atomic_json

from . import game_controller as gc

ORDER_CHARSET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
ORDER_CONFIRM_TTL = 900.0  # 待确认派单默认有效期 15 分钟
ORDERS_FILE = Path(__file__).resolve().parent.parent.parent / "state" / "automation_orders.json"

_ORDER_ID_RE = re.compile(r"(?:#?\bORD-([2-9A-HJ-NP-Z]{6})\b)", re.IGNORECASE)
_SHORT_CODE_RE = re.compile(r"#?\b([2-9A-HJ-NP-Z]{6})\b", re.IGNORECASE)


def normalize_order_id(raw: str) -> str:
    """把用户输入（`ORD-7K9M2X` / `#ORD-7K9M2X` / `7k9m2x`）规范化为 `ORD-XXXXXX`。无效返回空串。"""
    t = (raw or "").strip().upper().lstrip("#")
    if t.startswith("ORD-"):
        code = t[4:].strip()
    else:
        code = t
    if len(code) == 6 and all(ch in ORDER_CHARSET for ch in code):
        return f"ORD-{code}"
    return ""


def extract_order_id(text: str, *, allow_bare_code: bool = False) -> str:
    """从一句话里提取唯一派单号。"""
    s = (text or "").strip()
    if not s:
        return ""
    m = _ORDER_ID_RE.search(s)
    if m:
        return f"ORD-{m.group(1).upper()}"
    if allow_bare_code:
        m2 = _SHORT_CODE_RE.search(s)
        if m2:
            return f"ORD-{m2.group(1).upper()}"
    return ""


@dataclass
class AutomationSpec:
    """玩家自动化诉求的结构化约束。"""

    is_automation: bool = False
    domain: str = "farm"             # "farm" | "fishing" | "both"
    goal: str = ""
    priority_mode: str = "profit"    # "profit" | "exp" | "balanced"
    duration_minutes: int = 0        # 托管时长（分钟），0 表示按轮数
    rounds: int = 0                  # 循环轮数 / 钓鱼竿数，0 表示自动推算
    preferred_crop: str = ""         # 指定作物
    preferred_rod: str = ""          # 指定鱼竿
    preferred_bait: str = ""         # 指定鱼饵
    reserve_coins: int = 0           # 保留底金（不花光）
    target_coins: int = 0            # 目标金币/绿宝石（达到即停或触发开垦）
    auto_sell: bool = True           # 是否自动出售产物
    auto_reclaim: bool = False       # 达标后是否自动开垦新地
    exclude_sell: list[str] = field(default_factory=list)


@dataclass
class OrderStep:
    """派单中的单步操作定义。"""

    step_no: int
    phase: str                       # "immediate" | "scheduled"
    delay_seconds: int               # 距离上一步需等待的秒数（0 = 立即执行）
    domain: str                      # "farm" | "fishing"
    kind: str                        # "single" | "farm_cycle" | "fishing_loop"
    title: str                       # 展示给玩家的流程说明
    calc_note: str = ""              # 机器人推算该步的依据
    action: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"          # "pending" | "done" | "skipped" | "failed"
    result_text: str = ""


@dataclass
class AutomationOrder:
    """一份完整的自动化管理派单。"""

    order_id: str
    uid: str
    group_id: str                    # 群聊号，私聊为空串
    created_at: float
    expires_at: float
    status: str                      # "pending_confirm" | "running" | "completed" | "cancelled" | "expired" | "failed"
    domain: str
    goal_summary: str
    calc_summary: str
    estimated_minutes: int
    steps: list[OrderStep] = field(default_factory=list)
    current_step: int = 0
    logs: list[str] = field(default_factory=list)
    confirmed_at: float = 0.0
    finished_at: float = 0.0
    next_wake_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AutomationOrder":
        steps_raw = raw.get("steps") or []
        steps = [OrderStep(**s) for s in steps_raw if isinstance(s, dict)]
        return cls(
            order_id=str(raw["order_id"]),
            uid=str(raw["uid"]),
            group_id=str(raw.get("group_id") or ""),
            created_at=float(raw.get("created_at") or 0),
            expires_at=float(raw.get("expires_at") or 0),
            status=str(raw.get("status") or "pending_confirm"),
            domain=str(raw.get("domain") or "farm"),
            goal_summary=str(raw.get("goal_summary") or ""),
            calc_summary=str(raw.get("calc_summary") or ""),
            estimated_minutes=int(raw.get("estimated_minutes") or 0),
            steps=steps,
            current_step=int(raw.get("current_step") or 0),
            logs=[str(x) for x in (raw.get("logs") or [])],
            confirmed_at=float(raw.get("confirmed_at") or 0),
            finished_at=float(raw.get("finished_at") or 0),
            next_wake_at=float(raw.get("next_wake_at") or 0),
        )


# ======================================================================
# 一、自然语言诉求兜底解析器（与 DeepSeek 判定结果融合）
# ======================================================================
_AUTO_TRIGGERS = (
    "自动", "托管", "代管", "派单", "全套打理", "全自动", "帮我打理", "帮我管",
    "替我管", "替我打理", "规划一下怎么", "制定流程", "操作流程", "挂机", "定个计划",
    "一条龙", "连续钓", "自动钓", "自动种", "自动收",
)
_FISH_WORDS = ("钓鱼", "抛竿", "垂钓", "绿宝石", "鱼竿", "鱼饵", "渔具", "卖鱼", "祈愿", "钢制鱼竿", "钛金鱼竿")
_FARM_WORDS = ("农场", "种地", "种菜", "收菜", "偷菜", "播种", "开垦", "铲除", "种子", "作物", "土地", "农场币")


def parse_automation_from_text(text: str) -> AutomationSpec:
    """从用户自然语言中提取自动化管理意图与定量参数（零延迟确定性规则）。"""
    t = (text or "").strip()
    spec = AutomationSpec()
    if not t:
        return spec

    has_auto = any(w in t for w in _AUTO_TRIGGERS)
    # 「钓 5 次鱼」「种 3 轮菜」这类多轮指令同样属于自动化管理派单范畴
    multi_fish = re.search(r"(?:钓|抛竿|垂钓)\s*([2-9]|[1-9]\d+)\s*(?:次|竿|轮|下)", t)
    multi_farm = re.search(r"(?:种|收|循环|托管)\s*([2-9]|[1-9]\d+)\s*(?:轮|波|季|次)", t)
    dur_match = re.search(r"(\d+(?:\.\d+)?)\s*(小时|钟头|h|hr|分钟|分|min)", t, re.IGNORECASE)

    has_fish = any(w in t for w in _FISH_WORDS) or bool(multi_fish)
    has_farm = any(w in t for w in _FARM_WORDS) or bool(multi_farm)

    if not (has_auto or multi_fish or multi_farm or (dur_match and (has_fish or has_farm))):
        return spec
    if not (has_fish or has_farm):
        # 泛指「帮我自动托管 / 派单」默认双栖检查，优先农场+钓鱼
        has_farm = True
        has_fish = True

    spec.is_automation = True
    if has_farm and has_fish:
        spec.domain = "both"
    elif has_fish:
        spec.domain = "fishing"
    else:
        spec.domain = "farm"

    if any(w in t for w in ("经验", "升级", "冲级", "升到")):
        spec.priority_mode = "exp"
    elif any(w in t for w in ("均衡", "兼顾")):
        spec.priority_mode = "balanced"
    else:
        spec.priority_mode = "profit"

    if dur_match:
        val = float(dur_match.group(1))
        unit = dur_match.group(2).lower()
        if unit in ("小时", "钟头", "h", "hr"):
            spec.duration_minutes = max(5, min(720, int(val * 60)))
        else:
            spec.duration_minutes = max(5, min(720, int(val)))

    if multi_fish:
        spec.rounds = max(1, min(20, int(multi_fish.group(1))))
    elif multi_farm:
        spec.rounds = max(1, min(6, int(multi_farm.group(1))))
    else:
        r_m = re.search(r"(\d+)\s*(?:轮|次|竿|波)", t)
        if r_m:
            spec.rounds = max(1, min(20, int(r_m.group(1))))

    res_m = re.search(r"(?:留|保留|剩|预留)\s*(\d+)\s*(?:币|农场币|绿宝石|块)?", t)
    if res_m:
        spec.reserve_coins = max(0, int(res_m.group(1)))

    tgt_m = re.search(r"(?:赚够|攒够|凑够|达到|目标)\s*(\d+)\s*(?:币|农场币|绿宝石)?", t)
    if tgt_m:
        spec.target_coins = max(0, int(tgt_m.group(1)))

    if "开垦" in t:
        spec.auto_reclaim = True

    if any(w in t for w in ("不卖", "别卖", "不要卖", "留仓库", "存着")):
        # 若只说「别卖XX」，则提取保留名单而非关闭全部出售
        excl_m = re.findall(r"(?:不卖|别卖|留着)([\u4e00-\u9fa5A-Za-z~♡]{1,8})", t)
        if excl_m:
            spec.exclude_sell = [x for x in excl_m if x not in ("掉", "了", "钱", "菜", "鱼")]
        if not spec.exclude_sell:
            spec.auto_sell = False

    for rod in ("钛金鱼竿", "钢制鱼竿", "附魔鱼竿"):
        if rod in t:
            spec.preferred_rod = rod
            break
    for bait in ("传奇棒棒糖", "棒棒糖", "大米", "小鱼"):
        if bait in t and ("不用" + bait) not in t:
            spec.preferred_bait = bait
            break

    spec.goal = t[:120]
    return spec


def merge_automation_specs(local_spec: AutomationSpec, llm_dict: dict[str, Any] | None) -> AutomationSpec:
    """合并本地规则解析与模型提取的自动化参数。"""
    if not llm_dict or not isinstance(llm_dict, dict):
        return local_spec

    is_auto = bool(llm_dict.get("is_automation")) or local_spec.is_automation
    if not is_auto:
        return AutomationSpec(is_automation=False)

    domain = str(llm_dict.get("domain") or "").strip().lower()
    if domain not in ("farm", "fishing", "both"):
        domain = local_spec.domain

    pmode = str(llm_dict.get("priority_mode") or "").strip().lower()
    if pmode not in ("profit", "exp", "balanced"):
        pmode = local_spec.priority_mode

    def _pos_int(k: str, fallback: int, cap: int) -> int:
        try:
            v = int(llm_dict.get(k) or 0)
            return min(cap, v) if v > 0 else fallback
        except (TypeError, ValueError):
            return fallback

    auto_sell = local_spec.auto_sell
    if "auto_sell" in llm_dict and isinstance(llm_dict["auto_sell"], bool):
        auto_sell = llm_dict["auto_sell"] and local_spec.auto_sell

    excl = list(local_spec.exclude_sell)
    if isinstance(llm_dict.get("exclude_sell"), list):
        for item in llm_dict["exclude_sell"]:
            s = str(item or "").strip()
            if s and s not in excl:
                excl.append(s)

    return AutomationSpec(
        is_automation=True,
        domain=domain,
        goal=str(llm_dict.get("goal") or local_spec.goal or "自动化托管")[:120],
        priority_mode=pmode,
        duration_minutes=_pos_int("duration_minutes", local_spec.duration_minutes, 720),
        rounds=_pos_int("rounds", local_spec.rounds, 20),
        preferred_crop=str(llm_dict.get("preferred_crop") or local_spec.preferred_crop or "").strip(),
        preferred_rod=str(llm_dict.get("preferred_rod") or local_spec.preferred_rod or "").strip(),
        preferred_bait=str(llm_dict.get("preferred_bait") or local_spec.preferred_bait or "").strip(),
        reserve_coins=_pos_int("reserve_coins", local_spec.reserve_coins, 10_000_000),
        target_coins=_pos_int("target_coins", local_spec.target_coins, 100_000_000),
        auto_sell=auto_sell,
        auto_reclaim=bool(llm_dict.get("auto_reclaim")) or local_spec.auto_reclaim,
        exclude_sell=excl,
    )


# ======================================================================
# 二、派单持久化仓库（跨协程/重启安全）
# ======================================================================
class OrderStore:
    def __init__(self, path: Path = ORDERS_FILE) -> None:
        self.path = path
        self._orders: dict[str, AutomationOrder] = {}
        self._loaded = False

    def load(self) -> None:
        if not self.path.is_file():
            self._loaded = True
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._orders = {
                    str(k): AutomationOrder.from_dict(v)
                    for k, v in raw.items()
                    if isinstance(v, dict) and v.get("order_id")
                }
            self._loaded = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"automation: 读取派单库失败 {exc!r}")

    def save(self) -> None:
        try:
            # 只保留最近 80 单，避免历史无限膨胀
            items = sorted(self._orders.values(), key=lambda o: o.created_at)
            if len(items) > 80:
                keep_ids = {
                    o.order_id for o in items[-80:]
                } | {o.order_id for o in items if o.status in ("pending_confirm", "running")}
                self._orders = {k: v for k, v in self._orders.items() if k in keep_ids}
            payload = {k: v.to_dict() for k, v in self._orders.items()}
            with _file_lock(self.path.with_suffix(".write.lock")):
                atomic_json(self.path, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"automation: 保存派单库失败 {exc!r}")

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()
        now = time.time()
        changed = False
        for o in self._orders.values():
            if o.status == "pending_confirm" and o.expires_at > 0 and now > o.expires_at:
                o.status = "expired"
                changed = True
        if changed:
            self.save()

    def new_unique_id(self) -> str:
        self._ensure()
        for _ in range(32):
            code = "".join(secrets.choice(ORDER_CHARSET) for _ in range(6))
            oid = f"ORD-{code}"
            if oid not in self._orders:
                return oid
        return f"ORD-{int(time.time()) % 1000000:06d}"

    def put(self, order: AutomationOrder) -> None:
        self._ensure()
        self._orders[order.order_id] = order
        self.save()

    def get(self, order_id: str) -> Optional[AutomationOrder]:
        self._ensure()
        oid = normalize_order_id(order_id)
        return self._orders.get(oid) if oid else None

    def list_by_user(self, uid: str, limit: int = 8) -> list[AutomationOrder]:
        self._ensure()
        uid = str(uid)
        rows = [o for o in self._orders.values() if o.uid == uid]
        rows.sort(key=lambda o: o.created_at, reverse=True)
        return rows[:limit]

    def running_orders(self) -> list[AutomationOrder]:
        self._ensure()
        return [o for o in self._orders.values() if o.status == "running"]


order_store = OrderStore()


# ======================================================================
# 三、自主流程计算器（结合实时存档推演最优操作链）
# ======================================================================
def _pick_best_crop(
    crops: list[gc.CropInfo],
    *,
    empty_plots: int,
    usable_coins: int,
    owned_seeds: dict[str, int],
    preferred_crop: str,
    priority_mode: str,
    duration_minutes: int,
) -> Optional[gc.CropInfo]:
    if not crops or empty_plots <= 0:
        return None

    if preferred_crop:
        for c in crops:
            if c.name == preferred_crop:
                return c
        for c in crops:
            if preferred_crop in c.name or c.name in preferred_crop:
                return c

    scored: list[tuple[float, gc.CropInfo]] = []
    for c in crops:
        have = int(owned_seeds.get(c.name, 0))
        can_afford = (usable_coins // c.seed_cost) if c.seed_cost > 0 else empty_plots
        plantable = min(empty_plots, have + can_afford)
        if plantable <= 0:
            continue

        if priority_mode == "exp":
            base_rate = c.exp_per_hour * 10.0 + c.per_hour * 0.05
        elif priority_mode == "balanced":
            base_rate = c.per_hour + c.exp_per_hour * 4.0
        else:
            base_rate = c.per_hour

        total_rate = base_rate * plantable
        # 若玩家限定了托管时长，优先挑选能在托管窗口内至少成熟一季的作物
        if duration_minutes > 0 and c.hours * 60 > duration_minutes * 1.25:
            total_rate *= 0.35
        scored.append((total_rate, c))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


async def compute_and_create_order(
    uid: str,
    group_id: str,
    spec: AutomationSpec,
    *,
    raw_request: str = "",
) -> AutomationOrder:
    """读取实时存档，由机器人自主计算最优执行步骤并创建待确认派单。"""
    uid = str(uid)
    steps: list[OrderStep] = []
    calc_notes: list[str] = []
    est_minutes = 0

    def _add_step(
        phase: str,
        delay_sec: int,
        domain: str,
        kind: str,
        title: str,
        calc_note: str,
        action: dict[str, Any],
    ) -> None:
        steps.append(
            OrderStep(
                step_no=len(steps) + 1,
                phase=phase,
                delay_seconds=max(0, int(delay_sec)),
                domain=domain,
                kind=kind,
                title=title,
                calc_note=calc_note,
                action=action,
            )
        )

    # -------------------- A. 农场流程精算 --------------------
    if spec.domain in ("farm", "both"):
        f_snap = await gc.read_farm(uid)
        all_crops = await gc.load_crop_market(limit_level=None, top=0, only_buyable=True)
        # 若用户正文提到了具体作物名而 spec 没填，自动识别
        if not spec.preferred_crop and raw_request and all_crops:
            for c in all_crops:
                if c.name in raw_request:
                    spec.preferred_crop = c.name
                    break

        if f_snap is not None:
            proj_coins = f_snap.point
            proj_level = f_snap.level
            proj_soils = f_snap.soil_total
            proj_empty = len(f_snap.empty_soils)
            proj_warehouse = dict(f_snap.warehouse)
            proj_seeds = dict(f_snap.seeds)
            proj_farm_gain = 0

            if not f_snap.exists:
                _add_step(
                    "immediate",
                    0,
                    "farm",
                    "single",
                    "开通农场（初始化 500 农场币与 3 块土地）",
                    "检测到尚未开通农场",
                    {"domain": "farm", "action": "register"},
                )
                proj_coins = 500
                proj_level = 0
                proj_soils = 3
                proj_empty = 3
                signed_today = False
            else:
                signed_today = f_snap.signed_today

            if not signed_today:
                _add_step(
                    "immediate",
                    0,
                    "farm",
                    "single",
                    "执行农场每日签到（保底 +200~2000 农场币、+5~50 经验）",
                    "今日尚未签到，优先补充启动资金",
                    {"domain": "farm", "action": "sign_in"},
                )
                proj_coins += 200
                proj_farm_gain += 800

            if f_snap.exists and f_snap.care_needed:
                plots_str = "、".join(f"{s.index}号" for s in f_snap.care_needed)
                _add_step(
                    "immediate",
                    0,
                    "farm",
                    "single",
                    f"照料异常地块（{plots_str}：除虫/除草/浇水）",
                    "消除负面状态，保障正常收获",
                    {"domain": "farm", "action": "care"},
                )

            crop_map = {c.name: c for c in all_crops}
            if f_snap.exists and f_snap.ripe:
                ripe_names = "、".join(f"{s.index}号{s.plant}" for s in f_snap.ripe[:5])
                est_rev = 0
                freed_after_harvest = 0
                for s in f_snap.ripe:
                    ci = crop_map.get(s.plant)
                    if ci:
                        bonus_pct = 28 if s.level == 3 else (20 if s.level == 2 else 10)
                        qty = math.floor(ci.harvest * (100 + bonus_pct) // 100)
                        proj_warehouse[s.plant] = proj_warehouse.get(s.plant, 0) + qty
                        est_rev += qty * ci.unit_price
                        if s.harvest_count + 1 >= ci.times:
                            freed_after_harvest += 1
                _add_step(
                    "immediate",
                    0,
                    "farm",
                    "single",
                    f"收获全部成熟地块（{ripe_names}）",
                    f"预计入库价值约 {est_rev} 农场币",
                    {"domain": "farm", "action": "harvest"},
                )
            else:
                freed_after_harvest = 0

            wilted_total = len(f_snap.wilted_soils) + freed_after_harvest
            if wilted_total > 0:
                _add_step(
                    "immediate",
                    0,
                    "farm",
                    "single",
                    f"铲除枯萎荒废作物（共 {wilted_total} 块地，+{wilted_total * 3} 经验）",
                    "腾出空地用于新一轮播种",
                    {"domain": "farm", "action": "eradicate"},
                )
                proj_empty += wilted_total

            if spec.auto_sell and proj_warehouse:
                wh_val = sum(
                    cnt * (crop_map[k].unit_price if k in crop_map else 0)
                    for k, cnt in proj_warehouse.items()
                    if k not in spec.exclude_sell
                )
                if wh_val > 0 or proj_warehouse:
                    _add_step(
                        "immediate",
                        0,
                        "farm",
                        "single",
                        "出售仓库存粮回笼资金"
                        + (f"（预计 +{wh_val} 农场币）" if wh_val > 0 else ""),
                        "最大化可用购种/开垦预算",
                        {"domain": "farm", "action": "sell_plant", "name": ""},
                    )
                    proj_coins += wh_val
                    proj_farm_gain += wh_val
                    proj_warehouse.clear()

            # 检查是否可立即开垦新土地
            if (
                f_snap.exists
                and f_snap.next_reclaim_cost > 0
                and proj_level >= f_snap.next_reclaim_level
                and (spec.auto_reclaim or (proj_coins - spec.reserve_coins >= f_snap.next_reclaim_cost + 300))
                and (proj_coins - spec.reserve_coins >= f_snap.next_reclaim_cost)
            ):
                _add_step(
                    "immediate",
                    0,
                    "farm",
                    "single",
                    f"开垦第 {proj_soils + 1} 块新土地（消耗 {f_snap.next_reclaim_cost} 农场币）",
                    f"等级 Lv{proj_level} ≥ Lv{f_snap.next_reclaim_level} 且资金充足，永久提升产能",
                    {"domain": "farm", "action": "reclaim"},
                )
                proj_coins -= f_snap.next_reclaim_cost
                proj_soils += 1
                proj_empty += 1

            unlocked_crops = [c for c in all_crops if c.level <= proj_level]
            usable_budget = max(0, proj_coins - spec.reserve_coins)
            chosen_crop = _pick_best_crop(
                unlocked_crops,
                empty_plots=max(1, proj_empty),
                usable_coins=usable_budget,
                owned_seeds=proj_seeds,
                preferred_crop=spec.preferred_crop,
                priority_mode=spec.priority_mode,
                duration_minutes=spec.duration_minutes,
            )

            if proj_empty > 0 and chosen_crop is not None:
                owned_cnt = int(proj_seeds.get(chosen_crop.name, 0))
                affordable = (
                    (usable_budget // chosen_crop.seed_cost)
                    if chosen_crop.seed_cost > 0
                    else proj_empty
                )
                sow_cnt = min(proj_empty, owned_cnt + affordable)
                buy_cnt = max(0, sow_cnt - owned_cnt)
                if buy_cnt > 0:
                    cost = buy_cnt * chosen_crop.seed_cost
                    _add_step(
                        "immediate",
                        0,
                        "farm",
                        "single",
                        f"购买最优作物「{chosen_crop.name}」种子 ×{buy_cnt}（花费 {cost} 农场币）",
                        f"Lv{chosen_crop.level} 最优解：{chosen_crop.per_hour:.1f} 币/h，{chosen_crop.exp_per_hour:.1f} 经验/h",
                        {"domain": "farm", "action": "buy_seed", "name": chosen_crop.name, "count": buy_cnt},
                    )
                    proj_coins -= cost
                if sow_cnt > 0:
                    cycle_net = sow_cnt * chosen_crop.profit
                    _add_step(
                        "immediate",
                        0,
                        "farm",
                        "single",
                        f"在空地播种「{chosen_crop.name}」×{sow_cnt}（周期 {chosen_crop.hours:g}h，预计净赚 +{cycle_net} 币）",
                        f"单块毛收 {chosen_crop.revenue} 币，净利 {chosen_crop.profit} 币",
                        {"domain": "farm", "action": "sow", "name": chosen_crop.name, "count": sow_cnt},
                    )
                    proj_farm_gain += cycle_net

            # 计算后续定时成熟巡检轮次
            now_ts = int(time.time())
            next_wait_sec = 0
            if f_snap.exists and f_snap.next_mature_at > now_ts:
                next_wait_sec = max(60, f_snap.next_mature_at - now_ts + 5)
            elif chosen_crop is not None and proj_empty > 0:
                next_wait_sec = max(60, int(chosen_crop.hours * 3600) + 5)

            planned_cycles = 0
            if next_wait_sec > 0:
                if spec.rounds > 1:
                    planned_cycles = min(5, spec.rounds - 1)
                elif spec.duration_minutes > 0:
                    cycle_mins = max(1, next_wait_sec // 60)
                    planned_cycles = min(5, max(1, spec.duration_minutes // cycle_mins))
                elif spec.domain == "farm" and len(steps) == 0:
                    # 地全都种满了还没熟 → 自动排一轮成熟定时收种
                    planned_cycles = 1

            crop_target_name = chosen_crop.name if chosen_crop else spec.preferred_crop
            for cyc in range(1, planned_cycles + 1):
                wait_s = next_wait_sec if cyc == 1 else int((chosen_crop.hours if chosen_crop else 1.0) * 3600) + 5
                wait_m = max(1, round(wait_s / 60))
                est_minutes += wait_m
                _add_step(
                    "scheduled",
                    wait_s,
                    "farm",
                    "farm_cycle",
                    f"[定时 +{wait_m}分钟] 第 {cyc} 轮成熟自动巡检：收获 → 清枯萎 → "
                    + ("出售变现 → " if spec.auto_sell else "")
                    + f"补种「{crop_target_name or '当前最优作物'}」",
                    "后台按作物精确成熟时间戳自动唤醒执行",
                    {
                        "preferred_crop": crop_target_name,
                        "priority_mode": spec.priority_mode,
                        "reserve_coins": spec.reserve_coins,
                        "auto_sell": spec.auto_sell,
                        "auto_reclaim": spec.auto_reclaim,
                        "target_coins": spec.target_coins,
                    },
                )

            calc_notes.append(
                f"农场：当前 {f_snap.point} 币 / Lv{proj_level}"
                + (f" → 优选「{chosen_crop.name}」（{chosen_crop.per_hour:.1f}币/h）" if chosen_crop else "")
                + f"，预计周期净增约 +{max(0, proj_farm_gain)} 农场币"
            )

    # -------------------- B. 钓鱼流程精算 --------------------
    if spec.domain in ("fishing", "both"):
        fish_snap = await gc.read_fishing(uid)
        if fish_snap is not None:
            if spec.rounds > 0 and spec.domain == "fishing":
                casts = min(20, max(1, spec.rounds))
            elif spec.duration_minutes > 0:
                # 平均每竿（含 75s 冷却 + ~15s 上钩）约 1.5 分钟
                casts = min(20, max(1, round(spec.duration_minutes / 1.6)))
            else:
                casts = 3

            # 破产保护机制：绿宝石 ≤ 30 且冷却就绪时，祈愿 100% 稳赚不赔（+1~49）
            if fish_snap.exists and fish_snap.coin <= 30 and fish_snap.cooldown_left == 0:
                _add_step(
                    "immediate",
                    0,
                    "fishing",
                    "single",
                    f"触发低保祈愿（当前 {fish_snap.coin}≤30 {fish_snap.coin_name}，100% 正收益稳拿 +1~49）",
                    "利用河神低保机制无风险补充本金",
                    {"domain": "fishing", "action": "lottery"},
                )

            loadout = gc.recommend_best_fishing_loadout(
                fish_snap,
                casts=casts,
                reserve_coins=spec.reserve_coins,
                preferred_rod=spec.preferred_rod,
                preferred_bait=spec.preferred_bait,
            )

            for item_name, packs in loadout.buy_needed.items():
                _add_step(
                    "immediate",
                    0,
                    "fishing",
                    "single",
                    f"购入渔具/鱼饵「{item_name}」×{packs} 份",
                    f"支撑 {casts} 竿最优期望配装",
                    {"domain": "fishing", "action": "buy", "name": item_name, "count": packs},
                )

            use_small_bait = "小鱼" in loadout.tools
            tool_desc = " + ".join(loadout.tools) if loadout.tools else "徒手"
            first_cd = fish_snap.cooldown_left
            fish_minutes = max(1, round((first_cd + casts * (75 + loadout.expected_sleep_sec)) / 60))
            est_minutes = max(est_minutes, fish_minutes)
            est_net_emeralds = round(loadout.net_ev * casts)

            # 第 1 竿如果当前无冷却，先立即抛第 1 竿给玩家即时反馈！
            remaining_loop_casts = casts
            if first_cd == 0 and not fish_snap.is_busy:
                _add_step(
                    "immediate",
                    0,
                    "fishing",
                    "single",
                    f"立即抛第 1 竿（配装：{tool_desc}，单竿净期望 +{loadout.net_ev:.1f} {fish_snap.coin_name}）",
                    loadout.note,
                    {"domain": "fishing", "action": "fish", "tools": loadout.tools},
                )
                remaining_loop_casts -= 1

            if remaining_loop_casts > 0:
                init_wait = first_cd if first_cd > 0 else 80
                _add_step(
                    "scheduled",
                    init_wait,
                    "fishing",
                    "fishing_loop",
                    f"[冷却循环] 自动连续垂钓剩余 {remaining_loop_casts} 竿（配装：{tool_desc}，每次冷却结束自动抛竿）",
                    f"按 60~90s 浮动冷却动态等待，绝不触发河神禁言惩罚",
                    {
                        "casts": remaining_loop_casts,
                        "tools": loadout.tools,
                        "recycle_small_fish": use_small_bait,
                        "target_coins": spec.target_coins,
                    },
                )

            if spec.auto_sell:
                keep_bait_cnt = 2 if use_small_bait else 0
                keep_note = f"（自动留存 {keep_bait_cnt} 条小鱼作后续鱼饵）" if keep_bait_cnt else ""
                _add_step(
                    "scheduled" if remaining_loop_casts > 0 else "immediate",
                    0,
                    "fishing",
                    "single",
                    f"自动清包出售渔获变现{keep_note}",
                    "将钓获自动转化为绿宝石",
                    {
                        "domain": "fishing",
                        "action": "sell",
                        "name": "all",
                        "keep_small_fish": keep_bait_cnt,
                        "exclude": spec.exclude_sell,
                    },
                )

            calc_notes.append(
                f"钓鱼：共 {casts} 竿（{tool_desc}，单竿净期望 {loadout.net_ev:.1f}），"
                f"预计净收益约 {est_net_emeralds:+d} {fish_snap.coin_name}"
            )

    if not steps:
        _add_step(
            "immediate",
            0,
            "farm",
            "single",
            "农场签到与状态巡检",
            "当前无待处理异常，执行常规签到检查",
            {"domain": "farm", "action": "sign_in"},
        )

    now = time.time()
    order = AutomationOrder(
        order_id=order_store.new_unique_id(),
        uid=uid,
        group_id=str(group_id or ""),
        created_at=now,
        expires_at=now + ORDER_CONFIRM_TTL,
        status="pending_confirm",
        domain=spec.domain,
        goal_summary=spec.goal or "农场/钓鱼自动化托管",
        calc_summary="；".join(calc_notes) if calc_notes else "按当前最优收益测算",
        estimated_minutes=est_minutes,
        steps=steps,
    )
    order_store.put(order)
    logger.info(
        f"automation: 已生成派单 {order.order_id} uid={uid} domain={order.domain} steps={len(steps)}"
    )
    return order


def format_order_card(order: AutomationOrder) -> str:
    """生成带唯一标识符的派单流程确认单。"""
    domain_label = {"farm": "农场托管", "fishing": "钓鱼托管", "both": "农场 + 钓鱼双栖托管"}.get(
        order.domain, order.domain
    )
    dur_note = f"（预计全程约 {order.estimated_minutes} 分钟）" if order.estimated_minutes > 0 else "（确认后立即执行完结）"
    lines = [
        "📋【自动化管理派单 · 待玩家确认】",
        f"🔑 唯一标识符：{order.order_id}",
        f"🎯 托管类型：{domain_label}{dur_note}",
        f"📊 收益精算：{order.calc_summary}",
        "",
        "⚙️ 机器人计算出的操作流程：",
    ]
    for s in order.steps:
        tag = "立即" if s.phase == "immediate" else "后台定时"
        lines.append(f"  {s.step_no}. [{tag}] {s.title}")
        if s.calc_note:
            lines.append(f"     └ 依据：{s.calc_note}")

    lines.extend(
        [
            "",
            "⏳ 请在 15 分钟内携带唯一标识符确认执行：",
            f"👉 确认 {order.order_id}",
            f"（取消：取消 {order.order_id} ｜ 进度查询：查单 {order.order_id}）",
        ]
    )
    return "\n".join(lines)


def format_order_for_prompt(order: AutomationOrder) -> str:
    """给主模型看的简短上下文摘要（拼在本轮消息尾部，不污染前缀缓存）。"""
    step_brief = " → ".join(s.title.split("（")[0] for s in order.steps[:5])
    return (
        "【本轮你已替他推算并派发的自动化管理方案】\n"
        f"唯一派单号：{order.order_id}\n"
        f"测算结论：{order.calc_summary}\n"
        f"核心步骤：{step_brief}\n"
        f"（完整的流程确认单系统会自动附在你的回复下方；你只需用自己的语气点评两句他的农场/钓鱼现状，"
        f"并提醒他核对无误后发「确认 {order.order_id}」让你正式动手。）\n"
    )


def format_order_status(order: AutomationOrder) -> str:
    status_map = {
        "pending_confirm": "🕓 待玩家确认",
        "running": "⚙️ 后台自动执行中",
        "completed": "✅ 已全部完成",
        "cancelled": "🛑 已取消",
        "expired": "⌛ 已过期未确认",
        "failed": "⚠️ 执行中断",
    }
    lines = [
        f"📋 派单详情｜{order.order_id}",
        f"状态：{status_map.get(order.status, order.status)}",
        f"测算：{order.calc_summary}",
    ]
    if order.status == "running" and order.next_wake_at > time.time():
        rem = int(order.next_wake_at - time.time())
        mins, secs = divmod(rem, 60)
        lines.append(f"下次动作倒计时：{mins}分{secs}秒")
    lines.append("步骤进度：")
    for s in order.steps:
        mark = {"done": "✅", "failed": "⚠️", "skipped": "⏭️"}.get(s.status, "⏳")
        lines.append(f"  {mark} {s.step_no}. {s.title}" + (f" → {s.result_text}" if s.result_text else ""))
    if order.status == "pending_confirm":
        lines.append(f"\n回复「确认 {order.order_id}」立即启动")
    elif order.status == "running":
        lines.append(f"\n回复「取消 {order.order_id}」可随时中止后台托管")
    return "\n".join(lines)


# ======================================================================
# 四、后台自动化调度执行器（支持即时步骤同步回执 + 定时步骤异步唤醒）
# ======================================================================
class AutomationRunner:
    def __init__(self, store: OrderStore) -> None:
        self.store = store
        self._tasks: dict[str, asyncio.Task] = {}

    async def _notify_user(self, order: AutomationOrder, message: str) -> None:
        try:
            import nonebot

            bots = nonebot.get_bots()
            if not bots:
                return
            bot = next(iter(bots.values()))
            if order.group_id:
                from nonebot.adapters.onebot.v11 import MessageSegment

                msg = MessageSegment.at(int(order.uid)) + MessageSegment.text(" \n" + message)
                await bot.send_group_msg(group_id=int(order.group_id), message=msg)
            else:
                await bot.send_private_msg(user_id=int(order.uid), message=message)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"automation: 推送派单通知失败 {order.order_id}: {exc!r}")

    async def _run_farm_cycle(self, uid: str, params: dict[str, Any]) -> str:
        """动态执行一轮成熟后的农场收-清-卖-买-种闭环。"""
        logs: list[str] = []
        snap = await gc.read_farm(uid)
        if not snap or not snap.exists:
            return "农场未开通"

        if snap.care_needed:
            c_out = await gc.farm_care_soils(uid)
            if c_out.ok:
                logs.append(c_out.summary)
        if snap.ripe:
            h_out = await gc.farm_harvest(uid)
            logs.append(h_out.summary)
        snap = await gc.read_farm(uid)
        if snap and snap.wilted_soils:
            e_out = await gc.farm_eradicate(uid)
            if e_out.ok:
                logs.append(e_out.summary)
        if params.get("auto_sell", True):
            s_out = await gc.farm_sell_plant(uid, "", -1)
            if s_out.ok:
                logs.append(s_out.summary)

        snap = await gc.read_farm(uid)
        if not snap:
            return "；".join(logs) or "巡检完成"

        target_coins = int(params.get("target_coins") or 0)
        if params.get("auto_reclaim") and snap.next_reclaim_cost > 0:
            if snap.level >= snap.next_reclaim_level and snap.point >= snap.next_reclaim_cost:
                r_out = await gc.farm_reclaim(uid)
                if r_out.ok:
                    logs.append(r_out.summary)
                    snap = await gc.read_farm(uid) or snap

        if target_coins > 0 and snap.point >= target_coins:
            logs.append(f"已达成目标资金 {snap.point}/{target_coins} 币")
            return "；".join(logs)

        empty_n = len(snap.empty_soils)
        if empty_n > 0:
            crops = await gc.load_crop_market(limit_level=snap.level, top=0, only_buyable=True)
            usable = max(0, snap.point - int(params.get("reserve_coins") or 0))
            best = _pick_best_crop(
                crops,
                empty_plots=empty_n,
                usable_coins=usable,
                owned_seeds=snap.seeds,
                preferred_crop=str(params.get("preferred_crop") or ""),
                priority_mode=str(params.get("priority_mode") or "profit"),
                duration_minutes=0,
            )
            if best:
                have = int(snap.seeds.get(best.name, 0))
                can_buy = (usable // best.seed_cost) if best.seed_cost > 0 else empty_n
                sow_n = min(empty_n, have + can_buy)
                buy_n = max(0, sow_n - have)
                if buy_n > 0:
                    b_out = await gc.farm_buy_seed(uid, best.name, buy_n)
                    logs.append(b_out.summary)
                if sow_n > 0:
                    p_out = await gc.farm_sow(uid, best.name, sow_n)
                    logs.append(p_out.summary)

        return "；".join(logs) if logs else "本轮巡检无新成熟作物"

    async def _run_fishing_loop(self, order: AutomationOrder, step: OrderStep) -> str:
        params = step.action
        casts = max(1, int(params.get("casts") or 1))
        tools = [str(x) for x in (params.get("tools") or []) if x]
        target_coins = int(params.get("target_coins") or 0)
        caught_summary: list[str] = []

        for idx in range(casts):
            if order.status != "running":
                break
            snap = await gc.read_fishing(order.uid)
            if snap and target_coins > 0 and snap.coin >= target_coins:
                caught_summary.append(f"已达目标 {snap.coin} {snap.coin_name}，提前收竿")
                break
            if snap and snap.cooldown_left > 0:
                wait_s = snap.cooldown_left + 2
                order.next_wake_at = time.time() + wait_s
                self.store.save()
                await asyncio.sleep(wait_s)
            if order.status != "running":
                break

            # 确认背包里的道具仍然齐全，不足则退化为当前可用道具，避免报错中断
            snap = await gc.read_fishing(order.uid)
            active_tools = [t for t in tools if snap and snap.fishes.get(t, 0) > 0]
            out = await gc.fishing_cast(order.uid, active_tools, wait_sleep=True)
            caught_summary.append(f"第{idx + 1}竿:{out.summary}")
            step.result_text = f"已完成 {idx + 1}/{casts} 竿"
            order.logs.append(f"[{time.strftime('%H:%M:%S')}] {out.summary}")
            self.store.save()

            if idx + 1 < casts and order.status == "running":
                snap2 = await gc.read_fishing(order.uid)
                cd = (snap2.cooldown_left + 2) if (snap2 and snap2.cooldown_left > 0) else 65
                order.next_wake_at = time.time() + cd
                self.store.save()
                await asyncio.sleep(cd)

        order.next_wake_at = 0.0
        return "｜".join(caught_summary[-4:]) if caught_summary else "垂钓循环结束"

    async def _execute_step(self, order: AutomationOrder, step: OrderStep, *, in_background: bool) -> str:
        if step.kind == "farm_cycle":
            return await self._run_farm_cycle(order.uid, step.action)
        if step.kind == "fishing_loop":
            return await self._run_fishing_loop(order, step)
        out = await gc.execute_single_action(order.uid, step.action, wait_sleep=in_background)
        return out.summary

    async def _background_worker(self, order_id: str) -> None:
        order = self.store.get(order_id)
        if not order or order.status != "running":
            return
        try:
            while order.current_step < len(order.steps) and order.status == "running":
                step = order.steps[order.current_step]
                if step.status == "done":
                    order.current_step += 1
                    continue

                # 对于 fishing_loop，其内部自己精准控制每竿冷却等待；其余定时步骤先按 delay_seconds 等待
                if step.delay_seconds > 0 and step.kind != "fishing_loop":
                    order.next_wake_at = time.time() + step.delay_seconds
                    self.store.save()
                    await asyncio.sleep(step.delay_seconds)
                    order.next_wake_at = 0.0

                if order.status != "running":
                    break

                res_text = await self._execute_step(order, step, in_background=True)
                step.status = "done"
                step.result_text = res_text
                order.logs.append(f"[{time.strftime('%H:%M:%S')}] Step {step.step_no}: {res_text}")
                order.current_step += 1
                self.store.save()

            if order.status == "running":
                order.status = "completed"
                order.finished_at = time.time()
                order.next_wake_at = 0.0
                self.store.save()
                report = [
                    f"✅【自动化派单已完成】{order.order_id}",
                    "执行明细：",
                ]
                for s in order.steps:
                    report.append(f"  · {s.step_no}. {s.result_text or s.title}")
                await self._notify_user(order, "\n".join(report))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(f"automation: 后台任务异常 {order_id}: {exc!r}")
            order.status = "failed"
            order.logs.append(f"异常中断：{exc}")
            self.store.save()
            await self._notify_user(order, f"⚠️ 派单 {order_id} 执行遇到异常中断：{exc}")
        finally:
            self._tasks.pop(order_id, None)

    def _spawn_background(self, order_id: str) -> None:
        existing = self._tasks.get(order_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._background_worker(order_id))
        self._tasks[order_id] = task

    async def confirm_and_start(self, uid: str, order_id: str, *, is_admin: bool = False) -> tuple[bool, str]:
        """校验唯一标识符与归属人，立即执行首轮步骤并启动后台定时循环。"""
        oid = normalize_order_id(order_id)
        if not oid:
            return False, "未提供有效的派单唯一标识符（格式如：ORD-7K9M2X）。"
        order = self.store.get(oid)
        if order is None:
            return False, f"找不到派单 {oid}，请发「我的派单」核对单号。"
        if order.uid != str(uid) and not is_admin:
            return False, f"派单 {oid} 不属于你，无法确认他人的派单。"
        if order.status == "expired":
            return False, f"派单 {oid} 已超过 15 分钟有效期，请重新发起托管计算最新流程。"
        if order.status == "running":
            return True, f"派单 {oid} 已经在运行中了。\n\n" + format_order_status(order)
        if order.status == "completed":
            return True, f"派单 {oid} 已经执行完毕。\n\n" + format_order_status(order)
        if order.status == "cancelled":
            return False, f"派单 {oid} 已被取消，请重新派单。"

        order.status = "running"
        order.confirmed_at = time.time()
        self.store.save()

        immediate_lines: list[str] = []
        while order.current_step < len(order.steps):
            step = order.steps[order.current_step]
            if step.phase != "immediate" or step.delay_seconds > 0 or step.kind == "fishing_loop":
                break
            res_text = await self._execute_step(order, step, in_background=False)
            step.status = "done"
            step.result_text = res_text
            order.logs.append(f"[{time.strftime('%H:%M:%S')}] Step {step.step_no}: {res_text}")
            immediate_lines.append(f"  ✅ {step.step_no}. {res_text}")
            order.current_step += 1
            self.store.save()

        if order.current_step >= len(order.steps):
            order.status = "completed"
            order.finished_at = time.time()
            self.store.save()
            header = f"🚀 派单 {oid} 已确认并全部执行完毕！"
            body = "\n".join(immediate_lines) if immediate_lines else "  （无额外步骤）"
            return True, f"{header}\n{body}"

        # 还有后续定时/冷却步骤 → 转入后台协程继续托管
        self._spawn_background(oid)
        rem_count = len(order.steps) - order.current_step
        next_step = order.steps[order.current_step]
        header = f"🚀 派单 {oid} 已确认启动！"
        parts = [header]
        if immediate_lines:
            parts.append("首轮即时步骤已完成：\n" + "\n".join(immediate_lines))
        parts.append(
            f"⏳ 剩余 {rem_count} 个定时/循环步骤已转入后台自动托管\n"
            f"下一步：{next_step.title}\n"
            f"（完成后会自动通知你；随时可发「查单 {oid}」或「取消 {oid}」）"
        )
        return True, "\n\n".join(parts)

    confirm_order = confirm_and_start

    def cancel_order(self, uid: str, order_id: str = "", *, is_admin: bool = False) -> tuple[bool, str]:
        """按唯一标识符取消待确认或运行中的派单。"""
        if order_id:
            oid = normalize_order_id(order_id)
            if not oid:
                return False, "派单号格式不正确（示例：ORD-7K9M2X）。"
            order = self.store.get(oid)
            if not order:
                return False, f"找不到派单 {oid}。"
            if order.uid != str(uid) and not is_admin:
                return False, f"派单 {oid} 不属于你，无权取消。"
            if order.status not in ("pending_confirm", "running"):
                return False, f"派单 {oid} 当前状态为 {order.status}，无需取消。"
            order.status = "cancelled"
            order.finished_at = time.time()
            order.next_wake_at = 0.0
            task = self._tasks.pop(oid, None)
            if task and not task.done():
                task.cancel()
            self.store.save()
            return True, f"🛑 已取消派单 {oid}。"

        # 未填单号：若有且仅有一个活跃单，提示或取消全部本人活跃单
        active = [
            o for o in self.store.list_by_user(str(uid), limit=20)
            if o.status in ("pending_confirm", "running")
        ]
        if not active:
            return False, "你当前没有待确认或运行中的派单。"
        cancelled_ids: list[str] = []
        for o in active:
            o.status = "cancelled"
            o.finished_at = time.time()
            o.next_wake_at = 0.0
            t = self._tasks.pop(o.order_id, None)
            if t and not t.done():
                t.cancel()
            cancelled_ids.append(o.order_id)
        self.store.save()
        return True, f"🛑 已停止你的活跃派单：{', '.join(cancelled_ids)}"

    def recover_running_orders(self) -> None:
        """机器人启动时自动恢复上次未跑完的后台托管派单。"""
        running = self.store.running_orders()
        for o in running:
            logger.info(f"automation: 恢复未完成后台派单 {o.order_id} (uid={o.uid})")
            self._spawn_background(o.order_id)

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


runner = AutomationRunner(order_store)
