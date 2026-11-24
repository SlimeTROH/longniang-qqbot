"""农场与钓鱼统一操控引擎（读写一体 + 经济收益精算）。

覆盖两个商店插件：
  · nonebot_plugin_farm（真寻农场：开通、签到、收获、铲除、除虫除草浇水、买种、播种、卖菜、开垦、升级土地、偷菜、改名）
  · nonebot_plugin_fishing2（电子钓鱼：抛竿垂钓、购买渔具鱼饵、单卖/批量清包卖鱼、祈愿、放生、数学期望 EV 测算）

所有接口均做懒加载与异常隔离：插件未安装或未初始化时安全降级，绝不阻塞主聊天。
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from nonebot import logger

SOIL_LEVEL_NAMES = {0: "普通地", 1: "红土地", 2: "黑土地", 3: "金土地"}
SOIL_ISSUES = (
    ("wiltStatus", 1, "枯萎"),
    ("bugStatus", 1, "虫害"),
    ("weedStatus", 1, "杂草"),
    ("waterStatus", 1, "缺水"),
)

DEFAULT_SIGN_MILESTONES = {
    "3": {"point": 60, "exp": 12},
    "7": {"point": 180, "exp": 40},
    "15": {"point": 450, "exp": 90, "plant": {"胡萝卜": 3}},
    "30": {"point": 1200, "exp": 240, "vipPoint": 10},
}


# ======================================================================
# 通用执行结果结构
# ======================================================================
@dataclass
class ActionOutcome:
    ok: bool
    domain: str           # "farm" | "fishing"
    action: str
    summary: str
    detail: str = ""
    cooldown_sec: int = 0


# ======================================================================
# 一、农场数据结构与读取
# ======================================================================
@dataclass
class SoilInfo:
    index: int
    plant: str = ""
    level: int = 0
    mature_at: int = 0
    planted_at: int = 0
    harvest_count: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.plant

    @property
    def wilted(self) -> bool:
        return "枯萎" in self.issues

    def status(self, now: float) -> str:
        if self.empty:
            return "空着"
        if self.issues:
            return "、".join(self.issues)
        if self.mature_at and now >= self.mature_at:
            return "已成熟"
        return "生长中"

    def remain_text(self, now: float) -> str:
        if self.empty or not self.mature_at:
            return ""
        diff = self.mature_at - now
        if diff <= 0:
            return "可收获"
        minutes = int(diff // 60)
        if minutes < 60:
            return f"还需 {max(1, minutes)} 分钟"
        hours, minutes = divmod(minutes, 60)
        return f"还需 {hours} 小时 {minutes} 分"


@dataclass
class FarmSnapshot:
    uid: str
    exists: bool = False
    name: str = ""
    point: int = 0
    vip_point: int = 0
    level: int = 0
    cur_exp: int = 0
    need_exp: int = 200
    soil_total: int = 0
    soils: list[SoilInfo] = field(default_factory=list)
    warehouse: dict[str, int] = field(default_factory=dict)
    seeds: dict[str, int] = field(default_factory=dict)
    signed_today: bool = False
    next_reclaim_level: int = 0
    next_reclaim_cost: int = 0
    error: str = ""

    @property
    def planted(self) -> list[SoilInfo]:
        return [s for s in self.soils if not s.empty]

    @property
    def ripe(self) -> list[SoilInfo]:
        now = time.time()
        return [
            s for s in self.planted if s.mature_at and now >= s.mature_at and not s.wilted
        ]

    @property
    def wilted_soils(self) -> list[SoilInfo]:
        return [s for s in self.planted if s.wilted]

    @property
    def troubled(self) -> list[SoilInfo]:
        return [s for s in self.planted if s.issues]

    @property
    def care_needed(self) -> list[SoilInfo]:
        return [s for s in self.planted if any(x in s.issues for x in ("虫害", "杂草", "缺水"))]

    @property
    def empty_soils(self) -> list[SoilInfo]:
        return [s for s in self.soils if s.empty]

    @property
    def next_mature_at(self) -> int:
        now = time.time()
        growing = [s.mature_at for s in self.planted if s.mature_at > now and not s.wilted]
        return min(growing) if growing else 0


@dataclass
class CropInfo:
    name: str
    level: int = 0
    seed_cost: int = 0
    unit_price: int = 0
    harvest: int = 0
    times: int = 1
    hours: float = 0.0
    experience: int = 0
    can_buy: bool = True

    @property
    def revenue(self) -> int:
        return self.unit_price * self.harvest * self.times

    @property
    def profit(self) -> int:
        return self.revenue - self.seed_cost

    @property
    def per_hour(self) -> float:
        return self.profit / self.hours if self.hours > 0 else 0.0

    @property
    def total_exp(self) -> int:
        return self.experience * self.times

    @property
    def exp_per_hour(self) -> float:
        return self.total_exp / self.hours if self.hours > 0 else 0.0


def _farm_service() -> Any | None:
    try:
        from nonebot_plugin_farm.dbService import g_pDBService

        return g_pDBService
    except Exception:  # noqa: BLE001
        return None


async def ensure_farm_ready() -> Any | None:
    svc = _farm_service()
    if svc is None:
        return None
    if getattr(svc, "user", None) is not None:
        return svc
    try:
        from nonebot_plugin_farm.database.database import g_pSqlManager
        from nonebot_plugin_farm.json import g_pJsonManager

        if getattr(g_pSqlManager, "m_pDB", None) is None:
            await g_pSqlManager.init()
        if not getattr(g_pJsonManager, "m_pLevel", None):
            await g_pJsonManager.init()
        await svc.init()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"game_controller: 农场服务补初始化失败 {exc!r}")
        return None
    return svc if getattr(svc, "user", None) is not None else None


async def read_farm(uid: str) -> Optional[FarmSnapshot]:
    """读取用户完整农场状态快照。"""
    svc = await ensure_farm_ready()
    if svc is None:
        return None

    uid = str(uid)
    snap = FarmSnapshot(uid=uid)
    try:
        info = await svc.user.getUserInfoByUid(uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"game_controller: 读取农场用户失败 {exc!r}")
        return None

    if not info:
        return snap

    snap.exists = True
    snap.name = str(info.get("name") or "")
    snap.point = int(info.get("point") or 0)
    snap.vip_point = int(info.get("vipPoint") or 0)
    snap.soil_total = int(info.get("soil") or 0)

    # 注意：getUserLevelByUid 返回 (当前等级, 升至下级累计经验上限, 当前等级已获经验)
    try:
        lv, next_total_exp, cur_exp = await svc.user.getUserLevelByUid(uid)
        if lv >= 0:
            snap.level = int(lv)
            snap.cur_exp = int(cur_exp)
            snap.need_exp = int(next_total_exp)
    except Exception:  # noqa: BLE001
        pass

    # 今日是否已签到
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        snap.signed_today = bool(await svc.userSign.hasSigned(uid, today))
    except Exception:  # noqa: BLE001
        pass

    # 下一块地开垦条件
    try:
        from nonebot_plugin_farm.json import g_pJsonManager

        rec_map = (getattr(g_pJsonManager, "m_pLevel", None) or {}).get("reclamation") or {}
        nxt = rec_map.get(str(snap.soil_total + 1))
        if isinstance(nxt, dict):
            snap.next_reclaim_level = int(nxt.get("level") or 0)
            snap.next_reclaim_cost = int(nxt.get("point") or 0)
    except Exception:  # noqa: BLE001
        pass

    for idx in range(1, max(1, snap.soil_total) + 1):
        try:
            row = await svc.userSoil.getUserSoil(uid, idx)
        except Exception:  # noqa: BLE001
            row = None
        if not row or int(row.get("isSoilPlanted", 1) or 0) == 0 or not row.get("plantName"):
            lvl = int(row.get("soilLevel") or 0) if row else 0
            snap.soils.append(SoilInfo(index=idx, plant="", level=lvl))
            continue
        soil = SoilInfo(
            index=idx,
            plant=str(row.get("plantName") or ""),
            level=int(row.get("soilLevel") or 0),
            mature_at=int(row.get("matureTime") or 0),
            planted_at=int(row.get("plantTime") or 0),
            harvest_count=int(row.get("harvestCount") or 0),
        )
        for key, flag, label in SOIL_ISSUES:
            if int(row.get(key) or 0) >= flag:
                soil.issues.append(label)
        snap.soils.append(soil)

    try:
        snap.warehouse = {
            str(k): int(v)
            for k, v in (await svc.userPlant.getUserPlantByUid(uid) or {}).items()
            if int(v) > 0
        }
    except Exception:  # noqa: BLE001
        pass
    try:
        snap.seeds = {
            str(k): int(v)
            for k, v in (await svc.userSeed.getUserSeedByUid(uid) or {}).items()
            if int(v) > 0
        }
    except Exception:  # noqa: BLE001
        pass

    return snap


async def load_crop_market(
    limit_level: Optional[int] = None,
    top: int = 12,
    *,
    only_buyable: bool = True,
    sort_by: str = "profit",
) -> list[CropInfo]:
    """读取作物表并按收益或经验效率排序。"""
    svc = await ensure_farm_ready()
    if svc is None:
        return []
    try:
        rows = await svc.plant.listPlants()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"game_controller: 读取作物表失败 {exc!r}")
        return []

    out: list[CropInfo] = []
    for row in rows or []:
        try:
            name = str(row.get("name") or "").strip()
            hours = float(row.get("time") or 0)
            can_buy = int(row.get("isBuy", 1) or 0) != 0
            if not name or hours <= 0:
                continue
            if only_buyable and not can_buy:
                continue
            level = int(row.get("level") or 0)
            if limit_level is not None and level > limit_level:
                continue
            out.append(
                CropInfo(
                    name=name,
                    level=level,
                    seed_cost=int(row.get("buy") or 0),
                    unit_price=int(row.get("price") or 0),
                    harvest=int(row.get("harvest") or 0),
                    times=max(1, int(row.get("crop") or 1)),
                    hours=hours,
                    experience=int(row.get("experience") or 0),
                    can_buy=can_buy,
                )
            )
        except Exception:  # noqa: BLE001
            continue

    if sort_by == "exp":
        out.sort(key=lambda c: (c.exp_per_hour, c.per_hour), reverse=True)
    else:
        out.sort(key=lambda c: (c.per_hour, c.exp_per_hour), reverse=True)
    return out[:top] if top > 0 else out


# ======================================================================
# 二、农场直接操控动作
# ======================================================================
def do_farm_sign_sqlite(db_path: str, uid: str) -> tuple[bool, str]:
    """离线安全的农场签到实现（修复原插件吞异常及旧补丁 monthSignDays 恒为 1 的 bug）。"""
    uid = str(uid)
    today = datetime.now().strftime("%Y-%m-%d")
    month = today[:7]

    con = sqlite3.connect(db_path, timeout=10)
    try:
        user_row = con.execute("SELECT point, exp FROM user WHERE uid=?", (uid,)).fetchone()
        if not user_row:
            return False, "尚未开通农场，请先开通农场后再签到。"

        row = con.execute(
            "SELECT 1 FROM userSignLog WHERE uid=? AND signDate=?", (uid, today)
        ).fetchone()
        if row:
            days = con.execute(
                "SELECT currentMonth, monthSignDays, continuousDays FROM userSignSummary WHERE uid=?",
                (uid,),
            ).fetchone()
            if days:
                cur_m, m_days, c_days = str(days[0] or ""), int(days[1] or 0), int(days[2] or 0)
                m_days = m_days if cur_m == month else 1
            else:
                m_days, c_days = 1, 1
            return False, f"今天已经签过了（本月已签 {m_days} 天，连续 {c_days} 天）。"

        exp = random.randint(5, 50)
        point = random.randint(200, 2000)

        summary = con.execute(
            "SELECT totalSignDays, currentMonth, monthSignDays, continuousDays, lastSignDate "
            "FROM userSignSummary WHERE uid=?",
            (uid,),
        ).fetchone()
        if summary:
            total = int(summary[0] or 0) + 1
            prev_month = str(summary[1] or "")
            month_days = (int(summary[2] or 0) + 1) if prev_month == month else 1
            prev_cont = int(summary[3] or 0)
            last_date = str(summary[4] or "")
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            cont = prev_cont + 1 if last_date == yesterday else 1
        else:
            total, month_days, cont = 1, 1, 1

        bonus = DEFAULT_SIGN_MILESTONES.get(str(cont)) or DEFAULT_SIGN_MILESTONES.get(str(total))
        extra_point = extra_exp = extra_vip = 0
        seed_rewards: dict[str, int] = {}
        extra_parts: list[str] = []
        if bonus:
            extra_point = int(bonus.get("point", 0))
            extra_exp = int(bonus.get("exp", 0))
            extra_vip = int(bonus.get("vipPoint", 0))
            seed_rewards = {str(k): int(v) for k, v in (bonus.get("plant") or {}).items() if int(v) > 0}
            msg_bits = [f"+{extra_point} 币", f"+{extra_exp} 经验"]
            if extra_vip > 0:
                msg_bits.append(f"+{extra_vip} 点券")
            for sname, scnt in seed_rewards.items():
                msg_bits.append(f"{sname}种子×{scnt}")
            extra_parts.append(f"\n🎁 签到里程碑（连续 {cont} 天）：" + "，".join(msg_bits))

        point += extra_point
        exp += extra_exp

        con.execute(
            "INSERT INTO userSignLog (uid, signDate, isSupplement, exp, point) VALUES (?, ?, 0, ?, ?)",
            (uid, today, exp, point),
        )
        con.execute(
            "INSERT INTO userSignSummary "
            "(uid, totalSignDays, currentMonth, monthSignDays, lastSignDate, continuousDays, supplementCount) "
            "VALUES (?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(uid) DO UPDATE SET "
            "totalSignDays=?, currentMonth=?, monthSignDays=?, lastSignDate=?, continuousDays=?",
            (uid, total, month, month_days, today, cont, total, month, month_days, today, cont),
        )
        con.execute(
            "UPDATE user SET point=point+?, exp=exp+?, vipPoint=vipPoint+? WHERE uid=?",
            (point, exp, extra_vip, uid),
        )
        for sname, scnt in seed_rewards.items():
            existing_seed = con.execute(
                "SELECT count FROM userSeed WHERE uid=? AND seed=?", (uid, sname)
            ).fetchone()
            if existing_seed:
                con.execute(
                    "UPDATE userSeed SET count=count+? WHERE uid=? AND seed=?",
                    (scnt, uid, sname),
                )
            else:
                con.execute(
                    "INSERT INTO userSeed (uid, seed, count) VALUES (?, ?, ?)",
                    (uid, sname, scnt),
                )
        con.commit()

        after_row = con.execute("SELECT point FROM user WHERE uid=?", (uid,)).fetchone()
        now_point = int(after_row[0]) if after_row else 0
        extra_text = "".join(extra_parts)
        return True, (
            f"✅ 签到成功（本月 {month_days} 天，连续 {cont} 天）\n"
            f"+{point} 农场币，+{exp} 经验{extra_text}\n"
            f"当前农场币：{now_point}"
        )
    finally:
        con.close()


async def farm_register(uid: str, name: str = "旅行者的农场") -> ActionOutcome:
    svc = await ensure_farm_ready()
    if svc is None:
        return ActionOutcome(False, "farm", "register", "农场插件未安装或未就绪")
    uid = str(uid)
    info = await svc.user.getUserInfoByUid(uid)
    if info:
        return ActionOutcome(True, "farm", "register", f"已开通农场（当前 {info.get('point', 0)} 币）")

    safe_name = "".join(ch for ch in (name or "农场") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")[:16] or "农场"
    now_str = datetime.now().strftime("%Y-%m-%d")
    try:
        async with svc.user._transaction():
            await svc.user.m_pDB.execute(
                "INSERT INTO user (uid, name, exp, point, vipPoint, soil, stealTime, stealCount) "
                "VALUES (?, ?, 0, 500, 0, 3, ?, 5)",
                (uid, safe_name, now_str),
            )
        return ActionOutcome(True, "farm", "register", f"成功开通农场「{safe_name}」（初始 500 农场币，3 块土地）")
    except Exception as exc:  # noqa: BLE001
        return ActionOutcome(False, "farm", "register", f"开通农场失败：{exc}")


async def farm_sign_in(uid: str) -> ActionOutcome:
    svc = await ensure_farm_ready()
    if svc is None:
        return ActionOutcome(False, "farm", "sign_in", "农场插件不可用")
    try:
        from nonebot_plugin_farm.config import g_sDBFilePath

        ok, msg = await asyncio.to_thread(do_farm_sign_sqlite, str(g_sDBFilePath), str(uid))
        first_line = msg.replace("\n", "｜")
        return ActionOutcome(ok, "farm", "sign_in", first_line, detail=msg)
    except Exception as exc:  # noqa: BLE001
        return ActionOutcome(False, "farm", "sign_in", f"农场签到异常：{exc}")


async def farm_harvest(uid: str) -> ActionOutcome:
    svc = await ensure_farm_ready()
    if svc is None:
        return ActionOutcome(False, "farm", "harvest", "农场插件不可用")
    uid = str(uid)
    if not await svc.user.isUserExist(uid):
        return ActionOutcome(False, "farm", "harvest", "尚未开通农场")
    from nonebot_plugin_farm.farm.farm import g_pFarmManager

    res = str(await g_pFarmManager.harvest(uid) or "").strip()
    ok = ("没有可收获" not in res) and ("失败" not in res)
    return ActionOutcome(ok, "farm", "harvest", res.replace("\n", "；"), detail=res)


async def farm_eradicate(uid: str, soil_index: int = 0, force: bool = False) -> ActionOutcome:
    svc = await ensure_farm_ready()
    if svc is None:
        return ActionOutcome(False, "farm", "eradicate", "农场插件不可用")
    uid = str(uid)
    if not await svc.user.isUserExist(uid):
        return ActionOutcome(False, "farm", "eradicate", "尚未开通农场")

    if soil_index > 0 and force:
        row = await svc.userSoil.getUserSoil(uid, soil_index)
        if not row or not row.get("plantName"):
            return ActionOutcome(False, "farm", "eradicate", f"{soil_index}号地本来就是空的")
        plant_name = row.get("plantName")
        await svc.userSoil.updateUserSoilFields(
            uid,
            soil_index,
            {
                "plantName": "",
                "plantTime": 0,
                "matureTime": 0,
                "wiltStatus": 0,
                "bugStatus": 0,
                "weedStatus": 0,
                "waterStatus": 0,
                "harvestCount": 0,
                "isSoilPlanted": 0,
            },
        )
        await svc.userSteal.deleteStealRecord(uid, soil_index)
        return ActionOutcome(True, "farm", "eradicate", f"已强制铲除 {soil_index}号地的「{plant_name}」")

    from nonebot_plugin_farm.farm.farm import g_pFarmManager

    res = str(await g_pFarmManager.eradicate(uid) or "").strip()
    ok = "成功" in res
    return ActionOutcome(ok, "farm", "eradicate", res, detail=res)


async def farm_care_soils(uid: str) -> ActionOutcome:
    """一键清理所有地块的缺水、杂草与虫害状态。"""
    svc = await ensure_farm_ready()
    if svc is None:
        return ActionOutcome(False, "farm", "care", "农场插件不可用")
    uid = str(uid)
    snap = await read_farm(uid)
    if not snap or not snap.exists:
        return ActionOutcome(False, "farm", "care", "尚未开通农场")

    fixed: list[str] = []
    for s in snap.care_needed:
        await svc.userSoil.updateUserSoilFields(
            uid, s.index, {"bugStatus": 0, "weedStatus": 0, "waterStatus": 0}
        )
        fixed.append(f"{s.index}号地")
    if not fixed:
        return ActionOutcome(True, "farm", "care", "所有地块状态良好，无需浇水/除草/除虫")
    return ActionOutcome(True, "farm", "care", f"已照料清理异常地块：{'、'.join(fixed)}")


async def farm_buy_seed(uid: str, name: str, num: int = 1) -> ActionOutcome:
    svc = await ensure_farm_ready()
    if svc is None:
        return ActionOutcome(False, "farm", "buy_seed", "农场插件不可用")
    uid = str(uid)
    name = (name or "").strip()
    if not name or num <= 0:
        return ActionOutcome(False, "farm", "buy_seed", "购买种子参数无效")
    if not await svc.user.isUserExist(uid):
        return ActionOutcome(False, "farm", "buy_seed", "尚未开通农场")
    from nonebot_plugin_farm.farm.shop import g_pShopManager

    res = str(await g_pShopManager.buySeed(uid, name, int(num)) or "").strip()
    ok = "成功购买" in res
    return ActionOutcome(ok, "farm", "buy_seed", res, detail=res)


async def farm_sow(uid: str, name: str, num: int = -1) -> ActionOutcome:
    svc = await ensure_farm_ready()
    if svc is None:
        return ActionOutcome(False, "farm", "sow", "农场插件不可用")
    uid = str(uid)
    name = (name or "").strip()
    if not name:
        return ActionOutcome(False, "farm", "sow", "未指定要播种的作物名称")
    if not await svc.user.isUserExist(uid):
        return ActionOutcome(False, "farm", "sow", "尚未开通农场")
    from nonebot_plugin_farm.farm.farm import g_pFarmManager

    res = str(await g_pFarmManager.sowing(uid, name, int(num)) or "").strip()
    ok = "成功" in res
    return ActionOutcome(ok, "farm", "sow", res, detail=res)


async def farm_sell_plant(uid: str, name: str = "", num: int = -1) -> ActionOutcome:
    svc = await ensure_farm_ready()
    if svc is None:
        return ActionOutcome(False, "farm", "sell_plant", "农场插件不可用")
    uid = str(uid)
    if not await svc.user.isUserExist(uid):
        return ActionOutcome(False, "farm", "sell_plant", "尚未开通农场")
    from nonebot_plugin_farm.farm.shop import g_pShopManager

    clean_name = (name or "").strip()
    if clean_name in ("全部", "所有", "all", "*"):
        clean_name = ""
    res = str(await g_pShopManager.sellPlantByUid(uid, clean_name, int(num)) or "").strip()
    ok = "成功出售" in res
    return ActionOutcome(ok, "farm", "sell_plant", res, detail=res)


async def farm_reclaim(uid: str) -> ActionOutcome:
    svc = await ensure_farm_ready()
    if svc is None:
        return ActionOutcome(False, "farm", "reclaim", "农场插件不可用")
    uid = str(uid)
    if not await svc.user.isUserExist(uid):
        return ActionOutcome(False, "farm", "reclaim", "尚未开通农场")
    from nonebot_plugin_farm.farm.farm import g_pFarmManager

    res = str(await g_pFarmManager.reclamation(uid) or "").strip()
    ok = "成功" in res
    return ActionOutcome(ok, "farm", "reclaim", res, detail=res)


async def farm_upgrade_soil(uid: str, soil_index: int = 1) -> ActionOutcome:
    svc = await ensure_farm_ready()
    if svc is None:
        return ActionOutcome(False, "farm", "upgrade_soil", "农场插件不可用")
    uid = str(uid)
    if not await svc.user.isUserExist(uid):
        return ActionOutcome(False, "farm", "upgrade_soil", "尚未开通农场")
    from nonebot_plugin_farm.farm.farm import g_pFarmManager

    res = str(await g_pFarmManager.soilUpgrade(uid, int(soil_index)) or "").strip()
    ok = "成功升级" in res
    return ActionOutcome(ok, "farm", "upgrade_soil", res, detail=res)


async def farm_steal(uid: str, target_uid: str) -> ActionOutcome:
    svc = await ensure_farm_ready()
    if svc is None:
        return ActionOutcome(False, "farm", "steal", "农场插件不可用")
    uid, target_uid = str(uid), str(target_uid).strip()
    if not target_uid or uid == target_uid:
        return ActionOutcome(False, "farm", "steal", "偷菜目标无效")
    if not await svc.user.isUserExist(uid):
        return ActionOutcome(False, "farm", "steal", "你尚未开通农场")
    if not await svc.user.isUserExist(target_uid):
        return ActionOutcome(False, "farm", "steal", f"目标 {target_uid} 尚未开通农场")
    from nonebot_plugin_farm.farm.farm import g_pFarmManager

    res = str(await g_pFarmManager.stealing(uid, target_uid) or "").strip()
    ok = "成功偷到" in res
    return ActionOutcome(ok, "farm", "steal", res.replace("\n", "；"), detail=res)


async def farm_rename(uid: str, new_name: str) -> ActionOutcome:
    svc = await ensure_farm_ready()
    if svc is None:
        return ActionOutcome(False, "farm", "rename", "农场插件不可用")
    uid = str(uid)
    from nonebot_plugin_farm.tool import g_pToolManager

    safe_name = g_pToolManager.sanitize_username((new_name or "").strip())
    if not safe_name or safe_name == "神秘农夫":
        return ActionOutcome(False, "farm", "rename", "新农场名不合法")
    ok = bool(await svc.user.updateUserNameByUid(uid, safe_name))
    return ActionOutcome(
        ok, "farm", "rename", f"农场名已改为「{safe_name}」" if ok else "更改农场名失败"
    )


# ======================================================================
# 三、钓鱼数据结构、数学期望精算与直接操控
# ======================================================================
@dataclass
class FishingSnapshot:
    uid: str
    exists: bool = False
    coin: int = 0
    coin_name: str = "绿宝石"
    frequency: int = 0
    next_time: int = 0
    cooldown_left: int = 0
    is_busy: bool = False
    fishes: dict[str, int] = field(default_factory=dict)
    special_fishes: dict[str, int] = field(default_factory=dict)
    achievements: list[str] = field(default_factory=list)
    rods: dict[str, int] = field(default_factory=dict)
    baits: dict[str, int] = field(default_factory=dict)
    sellable: dict[str, int] = field(default_factory=dict)


@dataclass
class FishingEV:
    tools: list[str]
    gross_ev: float
    tool_cost_per_cast: float
    net_ev: float
    no_fish_prob: float
    expected_sleep_sec: float
    buy_needed: dict[str, int] = field(default_factory=dict)  # 商品名 -> 需购买份数
    upfront_buy_cost: int = 0
    note: str = ""


def _fishing_env() -> dict[str, Any] | None:
    try:
        import nonebot_plugin_fishing2 as fp_mod
        from nonebot_plugin_fishing2 import config as fp_cfg
        from nonebot_plugin_fishing2 import data_source as fp_ds
        from nonebot_plugin_fishing2 import fish_helper as fp_fh
        from nonebot_plugin_fishing2 import model as fp_model
        from nonebot_plugin_orm import get_session
        from sqlalchemy import select, update

        return {
            "mod": fp_mod,
            "cfg": fp_cfg.config,
            "ds": fp_ds,
            "fh": fp_fh,
            "model": fp_model,
            "get_session": get_session,
            "select": select,
            "update": update,
        }
    except Exception:  # noqa: BLE001
        return None


async def read_fishing(uid: str) -> Optional[FishingSnapshot]:
    env = _fishing_env()
    if env is None:
        return None

    uid = str(uid)
    cfg = env["cfg"]
    fh = env["fh"]
    FishingRecord = env["model"].FishingRecord
    select = env["select"]

    snap = FishingSnapshot(uid=uid, coin_name=str(cfg.fishing_coin_name or "绿宝石"))
    snap.is_busy = uid in getattr(env["mod"], "block_user_list", [])

    try:
        session = env["get_session"]()
        async with session.begin():
            rec = await session.scalar(select(FishingRecord).where(FishingRecord.user_id == uid))
            if not rec:
                return snap
            snap.exists = True
            snap.coin = int(rec.coin or 0)
            snap.frequency = int(rec.frequency or 0)
            snap.next_time = int(rec.time or 0)
            snap.cooldown_left = max(0, snap.next_time - int(time.time()))
            raw_fishes = json.loads(rec.fishes or "{}")
            snap.fishes = {k: int(v) for k, v in raw_fishes.items() if int(v) > 0}
            raw_spec = json.loads(rec.special_fishes or "{}")
            snap.special_fishes = {k: int(v) for k, v in raw_spec.items() if int(v) > 0}
            snap.achievements = list(json.loads(rec.achievements or "[]"))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"game_controller: 读取钓鱼档案失败 {exc!r}")
        return None

    for name, count in snap.fishes.items():
        fobj = fh.get_fish_by_name(name)
        if not fobj:
            continue
        if fobj.props and fobj.type == "item":
            snap.rods[name] = count
        if fobj.props and fobj.type == "fish":
            snap.baits[name] = count
        if fobj.can_sell:
            snap.sellable[name] = count
    return snap


def load_fishing_shop() -> list[dict[str, Any]]:
    env = _fishing_env()
    if env is None:
        return []
    fh = env["fh"]
    items: list[dict[str, Any]] = []
    for f in fh.config_fishes:
        if f.can_buy:
            pack_cost = int(f.buy_price * f.amount)
            items.append(
                {
                    "name": f.name,
                    "type": "鱼竿" if f.type == "item" else "鱼饵",
                    "amount": int(f.amount),
                    "unit_buy_price": int(f.buy_price),
                    "pack_price": pack_cost,
                    "description": f.description,
                }
            )
    return items


def calculate_fishing_ev(
    tools: list[str] | None = None,
    *,
    owned_items: dict[str, int] | None = None,
) -> Optional[FishingEV]:
    """根据钓鱼插件真实权重表计算指定 [鱼竿, 鱼饵] 组合的单竿数学期望。"""
    env = _fishing_env()
    if env is None:
        return None

    cfg = env["cfg"]
    ds = env["ds"]
    fh = env["fh"]
    owned = owned_items or {}
    clean_tools = [t for t in (tools or []) if t]

    # 校验工具合法性（同类最多一个）
    seen_types: set[str] = set()
    tool_cost = 0.0
    for tname in clean_tools:
        fobj = fh.get_fish_by_name(tname)
        if not fobj or not fobj.props:
            return None
        if fobj.type in seen_types:
            return None
        seen_types.add(fobj.type)
        # 成本核算：背包已有时，可出售鱼饵按出售机会成本计，不可出售耐久品按商店折旧计；未拥有按商店购买单次成本计
        if owned.get(tname, 0) > 0:
            if fobj.can_sell:
                tool_cost += float(fobj.price)
            elif fobj.can_buy:
                tool_cost += float(fobj.buy_price)
            else:
                tool_cost += 0.0
        elif fobj.can_buy:
            tool_cost += float(fobj.buy_price)
        else:
            return None  # 既没有也买不到

    adjusts, no_add, _sp_add = ds.get_adjusts_from_tools(clean_tools)
    no_t = min(max(float(cfg.no_fish_probability) + float(no_add), 0.0), 1.0)
    fish_names, weights = ds.adjusted(adjusts)
    total_w = sum(weights)
    if total_w <= 0:
        return None

    probs = [w / total_w for w in weights]
    gross_catch_ev = 0.0
    catch_sleep = 0.0
    for fname, p in zip(fish_names, probs):
        fobj = fh.get_fish_by_name(fname)
        if not fobj:
            continue
        val = float(fobj.price * fobj.amount) if fobj.can_sell else 0.0
        gross_catch_ev += val * p
        catch_sleep += float(fobj.sleep_time or 20) * p

    gross_ev = gross_catch_ev * (1.0 - no_t)
    exp_sleep = catch_sleep * (1.0 - no_t) + 15.0 * no_t
    return FishingEV(
        tools=clean_tools,
        gross_ev=round(gross_ev, 2),
        tool_cost_per_cast=round(tool_cost, 2),
        net_ev=round(gross_ev - tool_cost, 2),
        no_fish_prob=round(no_t, 4),
        expected_sleep_sec=round(exp_sleep, 1),
    )


def recommend_best_fishing_loadout(
    snap: FishingSnapshot,
    *,
    casts: int = 1,
    reserve_coins: int = 0,
    preferred_rod: str = "",
    preferred_bait: str = "",
) -> FishingEV:
    """结合玩家当前背包与绿宝石余额，推演最优鱼竿+鱼饵配置。"""
    env = _fishing_env()
    if env is None:
        return FishingEV([], 0.0, 0.0, 0.0, 0.1, 15.0, note="钓鱼插件未就绪")

    fh = env["fh"]
    usable_coins = max(0, snap.coin - max(0, reserve_coins))
    owned = dict(snap.fishes)

    rod_candidates = [""]
    bait_candidates = [""]
    for f in fh.config_fishes:
        if not f.props:
            continue
        if f.type == "item" and (owned.get(f.name, 0) > 0 or f.can_buy):
            rod_candidates.append(f.name)
        elif f.type == "fish" and (owned.get(f.name, 0) > 0 or f.can_buy):
            bait_candidates.append(f.name)

    if preferred_rod and preferred_rod in rod_candidates:
        rod_candidates = [preferred_rod]
    if preferred_bait and preferred_bait in bait_candidates:
        bait_candidates = [preferred_bait]

    feasible: list[FishingEV] = []
    for rod in rod_candidates:
        for bait in bait_candidates:
            combo = [x for x in (rod, bait) if x]
            ev = calculate_fishing_ev(combo, owned_items=owned)
            if ev is None:
                continue

            # 计算完成 casts 竿需要额外购买多少份
            buy_needed: dict[str, int] = {}
            upfront = 0
            possible = True
            for item_name in combo:
                have = owned.get(item_name, 0)
                shortage = max(0, casts - have)
                if shortage > 0:
                    fobj = fh.get_fish_by_name(item_name)
                    if not fobj or not fobj.can_buy:
                        possible = False
                        break
                    packs = math.ceil(shortage / max(1, int(fobj.amount)))
                    cost = packs * int(fobj.buy_price * fobj.amount)
                    buy_needed[item_name] = packs
                    upfront += cost
            if not possible or upfront > usable_coins:
                continue
            # 未钓过鱼（无记录）时无法先去商店买东西，第一竿只能徒手
            if not snap.exists and upfront > 0:
                continue

            ev.buy_needed = buy_needed
            ev.upfront_buy_cost = upfront
            label = " + ".join(combo) if combo else "徒手垂钓（无道具）"
            ev.note = f"{label}｜单竿毛期望 {ev.gross_ev:.1f}，净期望 {ev.net_ev:.1f} {snap.coin_name}，空军率 {ev.no_fish_prob*100:.0f}%"
            feasible.append(ev)

    if not feasible:
        base = calculate_fishing_ev([]) or FishingEV([], 0.0, 0.0, 0.0, 0.1, 15.0)
        base.note = f"徒手垂钓｜单竿净期望 {base.net_ev:.1f} {snap.coin_name}"
        return base

    feasible.sort(key=lambda x: (x.net_ev, -x.upfront_buy_cost), reverse=True)
    return feasible[0]


async def fishing_cast(
    uid: str,
    tools: list[str] | None = None,
    *,
    wait_sleep: bool = False,
) -> ActionOutcome:
    """直接执行一次抛竿钓鱼。

    wait_sleep=False（对话即时操控）：不卡住聊天 60~120 秒，而是把鱼的脱钩耗时计入下次冷却时间戳，
    与原版插件在经济、成就与总冷却上 100% 等价。
    wait_sleep=True（后台托管任务）：按真实耗时等待收竿。
    """
    env = _fishing_env()
    if env is None:
        return ActionOutcome(False, "fishing", "fish", "钓鱼插件不可用")

    uid = str(uid)
    mod = env["mod"]
    cfg = env["cfg"]
    ds = env["ds"]
    fh = env["fh"]
    FishingRecord = env["model"].FishingRecord
    select = env["select"]
    update = env["update"]

    if uid in mod.block_user_list:
        return ActionOutcome(False, "fishing", "fish", "你有一竿正在垂钓中，请稍候")

    snap = await read_fishing(uid)
    if snap and snap.cooldown_left > 0:
        return ActionOutcome(
            False,
            "fishing",
            "fish",
            f"钓鱼冷却中，还需等待 {snap.cooldown_left} 秒",
            cooldown_sec=snap.cooldown_left,
        )

    # 规范化工具顺序（道具 + 鱼饵）
    clean_tools = [t.strip() for t in (tools or []) if t and t.strip()][:2]
    check_err = await ds.check_tools(uid, clean_tools)
    if check_err:
        return ActionOutcome(False, "fishing", "fish", str(check_err))

    mod.block_user_list.append(uid)
    try:
        await ds.remove_tools(uid, clean_tools)
        adjusts, no_add, sp_add = ds.get_adjusts_from_tools(clean_tools)
        used_note = f"（使用 {'+'.join(clean_tools)}）" if clean_tools else "（徒手）"

        # 1. 空军判定
        no_prob = min(max(float(cfg.no_fish_probability) + float(no_add), 0.0), 1.0)
        if random.random() < no_prob:
            empty_wait = random.randint(10, 20)
            if wait_sleep:
                await asyncio.sleep(empty_wait)
            return ActionOutcome(
                True,
                "fishing",
                "fish",
                f"抛竿{used_note} → 空军了，什么都没钓上来",
                cooldown_sec=empty_wait,
            )

        # 2. 特殊鱼判定
        if await ds.can_catch_special_fish(sp_add):
            sp_name = await ds.random_get_a_special_fish()
            sp_wait = random.randint(10, 20)
            if wait_sleep:
                await asyncio.sleep(sp_wait)
            await ds.save_special_fish(uid, sp_name)
            return ActionOutcome(
                True,
                "fishing",
                "fish",
                f"抛竿{used_note} → 钓到特殊鱼「{sp_name}」！",
                cooldown_sec=int(cfg.fishing_cooldown_time_min) + sp_wait,
            )

        # 3. 常规/稀有鱼抽取
        caught = ds.choice(adjusts)
        fobj = fh.get_fish_by_name(caught)
        hook_sec = int(fobj.sleep_time if fobj and fobj.sleep_time else 15)
        if wait_sleep:
            await asyncio.sleep(hook_sec)
        await ds.save_fish(uid, caught)

        # 若未同步阻塞等待上钩时间，则将 hook_sec 补偿进冷却截止时间，保证公平性
        if not wait_sleep and hook_sec > 0:
            session = env["get_session"]()
            async with session.begin():
                rec = await session.scalar(select(FishingRecord).where(FishingRecord.user_id == uid))
                if rec:
                    await session.execute(
                        update(FishingRecord)
                        .where(FishingRecord.user_id == uid)
                        .values(time=int(rec.time) + hook_sec)
                    )
                    await session.commit()

        ach_list = await ds.check_achievement(uid) or []
        val_note = f"（价值 {fobj.price * fobj.amount} {cfg.fishing_coin_name}）" if fobj and fobj.can_sell else ""
        ach_note = f"｜🏆 解锁成就：{'、'.join(a.splitlines()[0] for a in ach_list)}" if ach_list else ""
        after_snap = await read_fishing(uid)
        cd = after_snap.cooldown_left if after_snap else (hook_sec + int(cfg.fishing_cooldown_time_min))
        return ActionOutcome(
            True,
            "fishing",
            "fish",
            f"抛竿{used_note} → 钓起「{caught}」{val_note}{ach_note}",
            cooldown_sec=cd,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"game_controller: 钓鱼执行失败 {exc!r}")
        return ActionOutcome(False, "fishing", "fish", f"垂钓异常：{exc}")
    finally:
        if uid in mod.block_user_list:
            mod.block_user_list.remove(uid)


async def fishing_buy(uid: str, item_name: str, quantity: int = 1) -> ActionOutcome:
    env = _fishing_env()
    if env is None:
        return ActionOutcome(False, "fishing", "buy", "钓鱼插件不可用")
    uid = str(uid)
    item_name = (item_name or "").strip()
    if not item_name or quantity <= 0:
        return ActionOutcome(False, "fishing", "buy", "购买渔具参数无效")
    ds = env["ds"]
    res = str(await ds.buy_fish(uid, item_name, int(quantity)) or "").strip()
    ach = await ds.check_achievement(uid) or []
    if ach:
        res += f"（解锁成就：{len(ach)} 项）"
    ok = "买入了" in res
    return ActionOutcome(ok, "fishing", "buy", res, detail=res)


async def fishing_sell(
    uid: str,
    target: str = "all",
    quantity: int = -1,
    *,
    keep_small_fish: int = 0,
    exclude: list[str] | None = None,
    as_special: bool = False,
) -> ActionOutcome:
    """出售渔获（支持单卖与批量清包卖鱼，可保留小鱼作鱼饵）。"""
    env = _fishing_env()
    if env is None:
        return ActionOutcome(False, "fishing", "sell", "钓鱼插件不可用")
    uid = str(uid)
    ds = env["ds"]
    clean_target = (target or "").strip()
    excluded = {x.strip() for x in (exclude or []) if x and x.strip()}

    if clean_target in ("", "all", "全部", "所有", "*"):
        snap = await read_fishing(uid)
        if not snap or not snap.exists or not snap.sellable:
            return ActionOutcome(False, "fishing", "sell", "背包里没有可出售的鱼")
        sold_parts: list[str] = []
        before_coin = snap.coin
        for fname, cnt in list(snap.sellable.items()):
            if fname in excluded:
                continue
            sell_qty = cnt
            if fname == "小鱼" and keep_small_fish > 0:
                sell_qty = max(0, cnt - keep_small_fish)
            if sell_qty <= 0:
                continue
            out = await ds.sell_fish(uid, fname, sell_qty, False, False)
            if "卖出了" in str(out):
                sold_parts.append(f"{fname}×{sell_qty}")
        after = await read_fishing(uid)
        gained = (after.coin - before_coin) if after else 0
        if not sold_parts:
            return ActionOutcome(True, "fishing", "sell", "没有需要出售的鱼（已按策略保留鱼饵/指定鱼）")
        return ActionOutcome(
            True,
            "fishing",
            "sell",
            f"批量售出 {'、'.join(sold_parts)}，共获 +{gained} {snap.coin_name}（当前 {after.coin if after else before_coin}）",
        )

    snap = await read_fishing(uid)
    if quantity <= 0:
        if snap and not as_special:
            quantity = int(snap.fishes.get(clean_target, 1) or 1)
        elif snap and as_special:
            quantity = int(snap.special_fishes.get(clean_target, 1) or 1)
        else:
            quantity = 1
    res = str(await ds.sell_fish(uid, clean_target, int(quantity), False, as_special) or "").strip()
    ok = "卖出了" in res
    return ActionOutcome(ok, "fishing", "sell", res, detail=res)


async def fishing_lottery(uid: str) -> ActionOutcome:
    env = _fishing_env()
    if env is None:
        return ActionOutcome(False, "fishing", "lottery", "钓鱼插件不可用")
    uid = str(uid)
    snap = await read_fishing(uid)
    if snap and snap.cooldown_left > 0:
        return ActionOutcome(
            False, "fishing", "lottery", f"河神冷却中，还需 {snap.cooldown_left} 秒", cooldown_sec=snap.cooldown_left
        )
    res = str(await env["ds"].lottery(uid) or "").strip()
    ok = ("获得" in res) or ("血亏" in res) or ("给了你" in res) or ("还完了" in res)
    return ActionOutcome(ok, "fishing", "lottery", res, detail=res)


async def fishing_free(uid: str, fish_name: str) -> ActionOutcome:
    env = _fishing_env()
    if env is None:
        return ActionOutcome(False, "fishing", "free", "钓鱼插件不可用")
    if not env["cfg"].special_fish_enabled:
        return ActionOutcome(False, "fishing", "free", "当前服务器未开启特殊鱼放生功能")
    res = str(await env["ds"].free_fish(str(uid), (fish_name or "").strip()) or "").strip()
    ok = "放生了" in res
    return ActionOutcome(ok, "fishing", "free", res, detail=res)


# ======================================================================
# 四、提示词格式化与批量即时动作执行器
# ======================================================================
def format_market(crops: list[CropInfo], level: int) -> str:
    if not crops:
        return ""
    lines = [f"【作物行情（等级 ≤ {level} 可购种，按每小时净收益排序）】"]
    for c in crops:
        lines.append(
            f"· {c.name}（Lv{c.level}）：种子 {c.seed_cost} 币，"
            f"收 {c.harvest}×{c.unit_price}={c.revenue} 币，长 {c.hours:g}h"
            + (f"，{c.times}季" if c.times > 1 else "")
            + f"，净赚 {c.profit}（{c.per_hour:.1f} 币/h，经验 {c.exp_per_hour:.1f}/h）"
        )
    return "\n".join(lines) + "\n"


def format_farm_for_prompt(snap: FarmSnapshot | None) -> str:
    if snap is None:
        return ""
    if not snap.exists:
        return (
            "【农场状态】\n"
            "他尚未开通农场（你可以直接替他开通，或让他发 开通农场）。\n"
        )

    now = time.time()
    sign_tag = "已签到" if snap.signed_today else "今日未签到"
    reclaim_tag = (
        f"｜下块地开垦需 Lv{snap.next_reclaim_level} + {snap.next_reclaim_cost} 币"
        if snap.next_reclaim_cost > 0
        else "｜土地已满垦"
    )
    lines = [
        "【他的农场（实时真实数据）】",
        f"农场名：{snap.name or '（未命名）'}｜农场币：{snap.point}｜等级：Lv{snap.level}（经验 {snap.cur_exp}/{snap.need_exp}）｜{sign_tag}{reclaim_tag}",
        f"地块：共 {snap.soil_total} 块（已种 {len(snap.planted)}，空闲 {len(snap.empty_soils)}，成熟可收 {len(snap.ripe)}，枯萎 {len(snap.wilted_soils)}）",
    ]
    for soil in snap.soils:
        tag = SOIL_LEVEL_NAMES.get(soil.level, f"Lv{soil.level}")
        if soil.empty:
            lines.append(f"  · {soil.index}号地（{tag}）：空着")
        else:
            st = soil.status(now)
            rem = soil.remain_text(now)
            extra = f"，{rem}" if rem else ""
            lines.append(
                f"  · {soil.index}号地（{tag}）：{soil.plant}（{st}{extra}"
                + (f"，已收{soil.harvest_count}次" if soil.harvest_count else "")
                + "）"
            )
    if snap.warehouse:
        lines.append("仓库作物：" + "、".join(f"{k}×{v}" for k, v in snap.warehouse.items()))
    if snap.seeds:
        lines.append("持有种子：" + "、".join(f"{k}×{v}" for k, v in snap.seeds.items()))
    return "\n".join(lines) + "\n"


async def build_farm_block(uid: str) -> str:
    try:
        snap = await read_farm(uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"game_controller: 构建农场块失败 {exc!r}")
        return ""
    if snap is None:
        return ""
    block = format_farm_for_prompt(snap)
    if not snap.exists:
        return block
    try:
        crops = await load_crop_market(limit_level=snap.level, top=8)
        market = format_market(crops, snap.level)
        if market:
            block += "\n" + market
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"game_controller: 农场行情构建失败 {exc!r}")
    return block


async def build_fishing_block(uid: str) -> str:
    """构建给主模型参考的钓鱼实时状态与期望收益文本。"""
    try:
        snap = await read_fishing(uid)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"game_controller: 构建钓鱼块失败 {exc!r}")
        return ""
    if snap is None:
        return ""

    lines = ["【他的钓鱼状态（实时真实数据）】"]
    if not snap.exists:
        lines.append(f"尚未抛过竿｜{snap.coin_name}：0｜冷却：就绪（可直接抛第一竿）")
    else:
        cd_text = f"冷却中（还剩 {snap.cooldown_left} 秒）" if snap.cooldown_left > 0 else "就绪（可立即抛竿）"
        lines.append(
            f"{snap.coin_name}：{snap.coin}｜累计钓起：{snap.frequency} 条｜状态：{cd_text}"
        )
        if snap.rods:
            lines.append("持有鱼竿（剩余耐久）：" + "、".join(f"{k}×{v}" for k, v in snap.rods.items()))
        if snap.baits:
            lines.append("可用鱼饵：" + "、".join(f"{k}×{v}" for k, v in snap.baits.items()))
        if snap.sellable:
            lines.append("可售渔获：" + "、".join(f"{k}×{v}" for k, v in snap.sellable.items()))
        if snap.achievements:
            lines.append(f"已获成就（{len(snap.achievements)}项）：" + "、".join(snap.achievements[:6]))

    best = recommend_best_fishing_loadout(snap, casts=1)
    if best.note:
        lines.append(f"当前最优单竿策略：{best.note}")
    shop = load_fishing_shop()
    if shop:
        lines.append(
            "渔具店可购："
            + "；".join(
                f"{it['name']}（{it['pack_price']}{snap.coin_name}/{it['amount']}次）" for it in shop
            )
        )
    return "\n".join(lines) + "\n"


async def execute_single_action(uid: str, act: dict[str, Any], *, wait_sleep: bool = False) -> ActionOutcome:
    """执行单条结构化农场/钓鱼动作。"""
    domain = str(act.get("domain") or "").strip().lower()
    op = str(act.get("action") or "").strip().lower()
    name = str(act.get("name") or "").strip()
    try:
        num = int(act.get("count") if act.get("count") is not None else act.get("num", -1))
    except (TypeError, ValueError):
        num = -1

    if domain == "farm":
        if op == "register":
            return await farm_register(uid, name or "旅行者的农场")
        if op in ("sign_in", "signin", "sign"):
            return await farm_sign_in(uid)
        if op == "harvest":
            return await farm_harvest(uid)
        if op == "eradicate":
            soil_idx = int(act.get("soil_index") or 0)
            force = bool(act.get("force", soil_idx > 0))
            return await farm_eradicate(uid, soil_index=soil_idx, force=force)
        if op == "care":
            return await farm_care_soils(uid)
        if op == "buy_seed":
            return await farm_buy_seed(uid, name, max(1, num if num > 0 else 1))
        if op in ("sow", "plant"):
            return await farm_sow(uid, name, num)
        if op in ("sell_plant", "sell"):
            return await farm_sell_plant(uid, name, num)
        if op == "reclaim":
            return await farm_reclaim(uid)
        if op == "upgrade_soil":
            soil_idx = max(1, int(act.get("soil_index") or 1))
            return await farm_upgrade_soil(uid, soil_idx)
        if op == "steal":
            target_uid = str(act.get("target_uid") or name)
            return await farm_steal(uid, target_uid)
        if op == "rename":
            return await farm_rename(uid, name)
        return ActionOutcome(False, "farm", op, f"未知农场操作：{op}")

    if domain == "fishing":
        if op in ("fish", "cast"):
            tools: list[str] = []
            if isinstance(act.get("tools"), list):
                tools = [str(x).strip() for x in act["tools"] if str(x).strip()]
            else:
                for k in ("rod", "bait"):
                    v = str(act.get(k) or "").strip()
                    if v:
                        tools.append(v)
                if name and not tools:
                    tools = [x for x in name.split() if x][:2]
            return await fishing_cast(uid, tools, wait_sleep=wait_sleep)
        if op in ("buy", "buy_item"):
            return await fishing_buy(uid, name, max(1, num if num > 0 else 1))
        if op in ("sell", "sell_fish"):
            keep_bait = int(act.get("keep_small_fish") or 0)
            excl = act.get("exclude") if isinstance(act.get("exclude"), list) else None
            return await fishing_sell(
                uid,
                name or "all",
                num,
                keep_small_fish=keep_bait,
                exclude=excl,
                as_special=bool(act.get("as_special", False)),
            )
        if op in ("lottery", "pray"):
            return await fishing_lottery(uid)
        if op == "free":
            return await fishing_free(uid, name)
        return ActionOutcome(False, "fishing", op, f"未知钓鱼操作：{op}")

    return ActionOutcome(False, domain or "unknown", op, f"未知操作域：{domain}")


async def execute_direct_actions(
    uid: str, actions: list[dict[str, Any]]
) -> tuple[list[ActionOutcome], str, str]:
    """按顺序执行一组即时操控动作，返回 (结果列表, 给玩家看的回执文本, 注入模型的上下文块)。"""
    outcomes: list[ActionOutcome] = []
    for act in actions[:8]:
        if not isinstance(act, dict):
            continue
        out = await execute_single_action(uid, act, wait_sleep=False)
        outcomes.append(out)

    if not outcomes:
        return [], "", ""

    receipt_lines = ["📌 操作执行回执："]
    prompt_lines = ["【本轮你已亲手替他完成的操作（已在游戏数据库真实生效）】"]
    for idx, o in enumerate(outcomes, 1):
        icon = "✅" if o.ok else "⚠️"
        tag = "农场" if o.domain == "farm" else "钓鱼"
        receipt_lines.append(f"  {icon} [{tag}] {o.summary}")
        prompt_lines.append(f"  {idx}. [{tag}·{'成功' if o.ok else '未成'}] {o.summary}")
    prompt_lines.append(
        "（以上操作已经由你执行完毕，回复时自然带出结果即可，不要再叫他自己去敲指令。）"
    )
    return outcomes, "\n".join(receipt_lines), "\n".join(prompt_lines) + "\n"
