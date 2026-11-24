"""好感度系统。

规则（按需求实现）：
  · 每个玩家（QQ 号）独立计算
  · 范围 -50 ~ 100
  · 每日增加上限 10，每日减少上限 15（按自然日重置）
  · 非冒犯的日常对话：每次随机增加 0 到 0.4 分
  · 明显夸赞 / 共鸣：+2，5 分钟冷却
  · 明显冒犯 / 贬低：-3，5 分钟冷却
  · 冒犯的那一次不叠加日常加成（否则逻辑矛盾）
  · 7 档阶段人设，按分数区间匹配
"""
import json
import os
import random
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------- 常量
AFFECTION_MIN = -50.0
AFFECTION_MAX = 100.0
DAILY_GAIN_CAP = 10.0
DAILY_LOSS_CAP = 15.0
COOLDOWN_SECONDS = 30.0         # 30 秒（夸赞/冒犯加分的间隔）
# 好感度重置的间隔：7 天（一周一次）
RESET_INTERVAL_SECONDS = 7 * 24 * 3600
# 显式日常微调和常规对话随机加分的范围。
DAILY_MIN = -0.5
DAILY_MAX = 0.5
DAILY_BONUS_MIN = 0.0
DAILY_BONUS_MAX = 0.4
PRAISE_BONUS = 2.0
OFFENSE_PENALTY = -3.0

# 好感度变化类型
EVENT_NONE = "none"
EVENT_PRAISE = "praise"
EVENT_OFFENSE = "offense"
VALID_EVENTS = {EVENT_NONE, EVENT_PRAISE, EVENT_OFFENSE}


@dataclass
class UserAffection:
    """一个玩家的好感度状态。"""

    affection: float = 0.0
    day: str = ""                     # 上次结算的日期 YYYY-MM-DD
    daily_gain: float = 0.0           # 当天累计增加（正）
    daily_loss: float = 0.0           # 当天累计减少（正数表示减少量）
    last_praise_at: float = 0.0       # 上次 +2 的时间戳
    last_offense_at: float = 0.0      # 上次 -3 的时间戳
    last_reset_at: float = 0.0        # 上次重置的时间戳（每 7 天限一次）
    last_reset_month: str = ""        # 兼容旧数据（YYYY-MM），新代码不再写入

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ApplyResult:
    """一次对话的好感度结算结果。"""

    before: float = 0.0
    after: float = 0.0
    delta: float = 0.0
    detail: list = field(default_factory=list)
    event: str = EVENT_NONE
    tier_changed: bool = False
    old_tier: str = ""
    new_tier: str = ""
    # 拆开给「每日展示」用：
    daily_delta: float = 0.0      # 日常微调（可正可负，不占额度）
    event_gain: float = 0.0       # 实际吃到的 +2（可能被每日额度裁剪）
    event_loss: float = 0.0       # 实际吃到的 -3（可能被每日额度裁剪）
    praise_hit: bool = False      # 本次是否真的触发了一次夸赞（过了冷却）
    offense_hit: bool = False     # 本次是否真的触发了一次冒犯（过了冷却）
    gain_clipped: float = 0.0     # 被每日增加额度裁掉的量
    loss_clipped: float = 0.0     # 被每日减少额度裁掉的量


def _today(now: float) -> str:
    return datetime.fromtimestamp(now).strftime("%Y-%m-%d")


def _roll_day(state: UserAffection, now: float) -> None:
    """跨天则重置每日额度。"""
    today = _today(now)
    if state.day != today:
        state.day = today
        state.daily_gain = 0.0
        state.daily_loss = 0.0


def _clamp(value: float) -> float:
    return max(AFFECTION_MIN, min(AFFECTION_MAX, value))


class AffectionStore:
    """好感度存储（JSON 持久化，原子写）。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._users: dict[str, UserAffection] = {}
        self.load()

    # ---------------------------------------------------------- 持久化
    def load(self) -> None:
        self._users = {}
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        for uid, item in (raw or {}).items():
            if not isinstance(item, dict):
                continue
            self._users[str(uid)] = UserAffection(
                affection=float(item.get("affection", 0.0)),
                day=str(item.get("day", "")),
                daily_gain=float(item.get("daily_gain", 0.0)),
                daily_loss=float(item.get("daily_loss", 0.0)),
                last_praise_at=float(item.get("last_praise_at", 0.0)),
                last_offense_at=float(item.get("last_offense_at", 0.0)),
                last_reset_at=float(item.get("last_reset_at", 0.0)),
                last_reset_month=str(item.get("last_reset_month", "")),
            )

    def save(self) -> None:
        """原子写：先写临时文件再替换，避免中途崩溃留下半个文件。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {uid: st.to_dict() for uid, st in self._users.items()}
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".affection-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ---------------------------------------------------------- 读取
    def get(self, user_id: str) -> UserAffection:
        state = self._users.get(str(user_id))
        if state is None:
            state = UserAffection()
            self._users[str(user_id)] = state
        return state

    def peek(self, user_id: str) -> float:
        """只看分数，不创建记录。"""
        state = self._users.get(str(user_id))
        return state.affection if state else 0.0

    # ---------------------------------------------------------- 结算
    def apply(
        self,
        user_id: str,
        event: str,
        now: Optional[float] = None,
        rng: Optional[random.Random] = None,
        daily_hint: Optional[float] = None,
    ) -> ApplyResult:
        """应用一次对话的好感度变化。

        event:
            "none"    普通对话 → 只有日常微调
            "praise"  夸赞/共鸣 → +2（带冷却）+ 日常微调
            "offense" 冒犯/贬低 → -3（带冷却），不叠加日常

        daily_hint:
            调用方显式指定的日常微调。常规对话省略，由本地随机增加 0 到 0.4 分。
        """
        now = time.time() if now is None else now
        rng = rng or random
        if event not in VALID_EVENTS:
            event = EVENT_NONE

        state = self.get(user_id)
        _roll_day(state, now)
        before = state.affection
        detail: list[str] = []
        # 三类分开算：
        #   event_gain / event_loss —— 夸赞 +2、冒犯 -3，**占用每日额度**
        #   daily_delta            —— 日常微调，独立于夸赞和冒犯的每日额度
        event_gain = 0.0
        event_loss = 0.0
        daily_delta = 0.0
        praise_hit = False
        offense_hit = False

        if event == EVENT_OFFENSE:
            if now - state.last_offense_at >= COOLDOWN_SECONDS:
                event_loss = abs(OFFENSE_PENALTY)
                state.last_offense_at = now
                offense_hit = True
                detail.append(f"冒犯 -{abs(OFFENSE_PENALTY):.0f}")
            else:
                wait = COOLDOWN_SECONDS - (now - state.last_offense_at)
                detail.append(f"冒犯（冷却中，还要 {int(wait)}s）")
            # 冒犯不叠加日常
        else:
            if daily_hint is not None:
                daily = max(DAILY_MIN, min(DAILY_MAX, float(daily_hint)))
            else:
                daily = rng.uniform(DAILY_BONUS_MIN, DAILY_BONUS_MAX)
            daily_delta = daily
            detail.append(f"日常 {daily:+.3f}")

            if event == EVENT_PRAISE:
                if now - state.last_praise_at >= COOLDOWN_SECONDS:
                    event_gain = PRAISE_BONUS
                    state.last_praise_at = now
                    praise_hit = True
                    detail.append(f"夸赞 +{PRAISE_BONUS:.0f}")
                else:
                    wait = COOLDOWN_SECONDS - (now - state.last_praise_at)
                    detail.append(f"夸赞（冷却中，还要 {int(wait)}s）")

        # ---- 每日额度裁剪（只裁夸赞/冒犯，日常微调不设限）----
        gain_clipped = 0.0
        loss_clipped = 0.0
        if event_gain > 0:
            room = max(0.0, DAILY_GAIN_CAP - state.daily_gain)
            applied_gain = min(event_gain, room)
            if applied_gain < event_gain:
                gain_clipped = event_gain - applied_gain
                detail.append(
                    f"（今日增加额度只剩 {room:.2f}，裁剪 {gain_clipped:.2f}）"
                )
            state.daily_gain += applied_gain
        else:
            applied_gain = 0.0

        if event_loss > 0:
            room = max(0.0, DAILY_LOSS_CAP - state.daily_loss)
            applied_loss = min(event_loss, room)
            if applied_loss < event_loss:
                loss_clipped = event_loss - applied_loss
                detail.append(
                    f"（今日减少额度只剩 {room:.2f}，裁剪 {loss_clipped:.2f}）"
                )
            state.daily_loss += applied_loss
        else:
            applied_loss = 0.0

        # ---- 合并 + 上下限裁剪 ----
        raw_delta = daily_delta + applied_gain - applied_loss
        after = _clamp(before + raw_delta)
        actual_delta = after - before
        if abs(actual_delta - raw_delta) > 1e-9:
            detail.append(f"（触及边界，实际 {actual_delta:+.2f}）")

        state.affection = after
        self.save()

        return ApplyResult(
            before=before,
            after=after,
            delta=actual_delta,
            detail=detail,
            event=event,
            daily_delta=daily_delta,
            event_gain=applied_gain,
            event_loss=applied_loss,
            praise_hit=praise_hit,
            offense_hit=offense_hit,
            gain_clipped=gain_clipped,
            loss_clipped=loss_clipped,
        )

    # ---------------------------------------------------------- 重置
    def reset(
        self, user_id: str, now: Optional[float] = None
    ) -> tuple[bool, float, float, str]:
        """把好感度归零。每 7 天只能一次。

        返回 (是否成功, 重置前分数, 重置后分数, 说明)。
        成功时记下当前时间戳，7 天内不能再重置。
        """
        now = time.time() if now is None else now
        state = self.get(user_id)

        # 兼容旧数据：以前存的是月份字符串，取那个月的 1 号当重置时间
        if state.last_reset_at <= 0 and state.last_reset_month:
            try:
                old = datetime.strptime(state.last_reset_month, "%Y-%m")
                state.last_reset_at = old.timestamp()
            except ValueError:
                state.last_reset_at = 0.0

        if state.last_reset_at > 0:
            elapsed = now - state.last_reset_at
            if elapsed < RESET_INTERVAL_SECONDS:
                left = RESET_INTERVAL_SECONDS - elapsed
                days = int(left // 86400)
                hours = int((left % 86400) // 3600)
                wait = f"{days} 天 {hours} 小时" if days else f"{hours} 小时"
                return False, state.affection, state.affection, f"还要等 {wait}"

        before = state.affection
        if abs(before) < 1e-9:
            return False, before, before, "现在就是 0，不用重置"

        state.affection = 0.0
        state.last_reset_at = now
        state.last_reset_month = ""      # 新代码不再用这个字段
        # 重置就是把这个人从她脑子里擦掉，当天的额度也一起清
        state.daily_gain = 0.0
        state.daily_loss = 0.0
        state.last_praise_at = 0.0
        state.last_offense_at = 0.0
        self.save()
        return True, before, 0.0, "已重置"

    def reset_ready_in(self, user_id: str, now: Optional[float] = None) -> float:
        """还要多少秒才能再重置（0 表示现在就能）。"""
        now = time.time() if now is None else now
        state = self.get(user_id)
        if state.last_reset_at <= 0:
            return 0.0
        left = RESET_INTERVAL_SECONDS - (now - state.last_reset_at)
        return max(0.0, left)


def tier_of(affection: float, tiers: list[dict]) -> dict:
    """按好感度匹配阶段。区间规则 min <= x < max，最后一档含 max。"""
    if not tiers:
        return {}
    for item in tiers:
        lo = float(item.get("min", AFFECTION_MIN))
        hi = float(item.get("max", AFFECTION_MAX))
        if lo <= affection < hi:
            return item
    # 等于最高档上界（100）
    last = tiers[-1]
    if affection >= float(last.get("max", AFFECTION_MAX)):
        return last
    return tiers[0]


def describe_tier(tiers: list[dict], affection: float) -> str:
    """返回形如「亲近 (55~80)」的简短描述。"""
    item = tier_of(affection, tiers)
    if not item:
        return "未知"
    return f"{item.get('name', '?')}({item.get('min')}~{item.get('max')})"
