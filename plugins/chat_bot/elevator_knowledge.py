"""Live read-only knowledge and player-status tools for Endless Elevator (无尽电梯搜打撤)."""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from math import prod
from pathlib import Path
from random import Random

from . import llm

_ROOT = Path(__file__).resolve().parents[2]
_CONTENT_PATH = _ROOT / "plugins" / "endless_elevator" / "content.json"
_DB_PATH = _ROOT / "state" / "endless_elevator.sqlite3"
_SHANGHAI = timezone(timedelta(hours=8))
_INVENTORY_PAGE_SIZE = 40

_STAT_NAMES = {"hp": "生命", "atk": "攻击", "def": "防御", "agi": "敏捷", "luk": "幸运"}
_MAP_SHORT = {"mall": "商场", "subway": "地铁", "abyss": "实验室", "auction": "拍卖行", "tide":"动力站", "zenith":"天文台"}
_RARITY_CN = {"white": "白色", "green": "绿色", "blue": "蓝色", "purple": "紫色", "gold": "金色", "red": "传世红", "rainbow":"虹彩"}

_GENERIC_QUERIES = {
    "总览", "全部", "攻略", "玩法", "介绍", "怎么玩", "无尽电梯", "搜打撤", "电梯",
    "商店", "市场", "补给站", "黑市", "配方", "合成", "兑换", "地图", "难度",
    "道具", "物品", "药品", "盲盒", "装备", "底材", "词条", "流派",
    "套装", "大金", "珍藏", "陈列室", "奇遇", "事件", "悬赏", "任务", "每日任务", "指令", "命令",
    "爆率", "概率", "掉率", "出红", "出金", "公式", "机制",
}

_VIRTUAL_SHOP = {
    "ticket": (
        "通行证（入场票）",
        ("ticket", "通行证", "车票", "票", "门票"),
        "下潜禁区每局消耗 1 张。",
    ),
    "gear_box": (
        "新手装备箱（初级）",
        ("gear_box", "新手装备箱", "绿箱", "初级箱", "装备盲盒", "装备箱"),
        "",
    ),
    "gear_box_mid": (
        "进阶装备箱（中级）",
        ("gear_box_mid", "进阶装备箱", "蓝箱", "中级箱", "精良箱"),
        "",
    ),
    "gear_box_high": (
        "尊享装备箱（高级）",
        ("gear_box_high", "尊享装备箱", "尊享箱", "尊享金装匣", "金箱", "金装匣", "高级箱", "紫箱"),
        "",
    ),
    "safebox": (
        "安全箱 / 安全箱扩容",
        ("safebox", "安全箱", "扩容", "扩容安全箱"),
        "锁进安全箱的货即使翻车也 100% 不掉落。初始 1 格，商店可永久扩容：2 格 6,000 金 / 3 格 30,000 金 / 4 格 88,000 金；局内用「哆啦的临时扩容袋」还能本局临时 +1 格。",
    ),
}

_QUEST_POOL_TEXT = (
    "• 深入禁区累计 12 层 → 650 金",
    "• 放倒 3 只禁区怪物 → 蓝色加密黑市盲盒 ×1",
    "• 带着物资安全撤离 2 次 → 900 金",
    "• 触发并处理 3 次奇遇 → 冰美式续命液 ×2",
    "• 摸开 5 个禁区宝箱 → 750 金",
    "• 仓库卖货或黑市兑换 1 次 → 十倍浓缩冰红茶 ×2",
)

_cache_mtime: float = -1.0
_cache_data: dict = {}


def _load_content() -> dict:
    global _cache_mtime, _cache_data
    stat = _CONTENT_PATH.stat()
    if stat.st_mtime != _cache_mtime or not _cache_data:
        _cache_data = json.loads(_CONTENT_PATH.read_text(encoding="utf-8"))
        _cache_mtime = stat.st_mtime
    return _cache_data


def _current_week() -> str:
    iso = datetime.now(tz=_SHANGHAI).isocalendar()
    return f"{iso.year}-{iso.week}"


def _weekly_recipes(data: dict) -> list[dict]:
    recipes = data.get("recipes", [])
    featured = set(Random(_current_week()).sample(range(len(recipes)), min(3, len(recipes))))
    out = []
    for idx, recipe in enumerate(recipes):
        reqs = dict(recipe["requires"])
        if idx in featured:
            first = next(iter(reqs))
            if reqs[first] > 1:
                reqs[first] -= 1
            elif len(reqs) > 1:
                del reqs[first]
        out.append({**recipe, "requires": reqs, "std_requires": recipe["requires"], "featured": idx in featured})
    return out


def _fmt_reqs(data: dict, requires: dict) -> str:
    items = data["items"]
    parts = []
    for key, amount in requires.items():
        if key == "any_blue":
            parts.append(f"任意蓝色材料×{amount}")
        elif key == "any_green":
            parts.append(f"任意绿色材料×{amount}")
        else:
            spec = items.get(key, {})
            place = _MAP_SHORT.get(spec.get("map", ""), "")
            loc = f"({place}掉落)" if place else ""
            parts.append(f"{spec.get('name', key)}{loc}×{amount}")
    return " + ".join(parts)


def _season_rules():
    from importlib import import_module
    package=type(_get_live_elevator_game()).__module__.rsplit(".",1)[0]
    return import_module(package+".expedition")


def _combat_rules():
    from importlib import import_module
    package = type(_get_live_elevator_game()).__module__.rsplit(".",1)[0]
    return import_module(package + ".combat")


def _heirloom_rules():
    from importlib import import_module
    package = type(_get_live_elevator_game()).__module__.rsplit(".",1)[0]
    return import_module(package + ".heirlooms")


def _render_formulas() -> str:
    groups = _combat_rules().formula_guide()
    return "\n".join("【" + g["title"] + "】\n" + "\n".join(g["lines"]) for g in groups)


def _ticket_details(data: dict) -> str:
    rules = _season_rules()
    price = data["shop"]["ticket"]
    return (
        f"免费票每天零点补足至 {rules.TICKET_DAILY_REFILL} 张，下潜优先消耗免费票。"
        f"商店 {price:,} 金一张，每日最多加购 {rules.TICKET_DAILY_PURCHASE_LIMIT} 张。"
        f"付费票最多持有 {rules.TICKET_PAID_CAP} 张，剩余付费票可留到次日。"
    )


def _run_field_text(run, data):
    items=data["expedition"]["field_items"]
    held="、".join(f"{items[k]['name']}×{v}" for k,v in run.get("field_items",{}).items() if k in items) or "空"
    active="、".join(f"{items[k]['name']}至F{v}" for k,v in run.get("field_buffs",{}).items() if k in items) or "无"
    slots=run.get("field_slots")
    if slots is None:
        slots=(run.get("field_worn"), None)
    worn="、".join(f"{index}号槽 {items.get(key, {}).get('name', '空')}" for index, key in enumerate(slots, 1))
    charges = "、".join(
        f"{'旧版锁血蓄力' if charge.get('blood') else items[charge['id']]['name']}剩{charge['left']}次"
        for kind in ("burst", "guard") if (charge := run.get(kind)) and charge["id"] in items
    )
    scale = prod(items.get(key, {}).get("damage_multiplier", 1) for key in slots)
    damage = f"双方直接战斗伤害 ×{scale:g}。" if scale != 1 else ""
    return f"本局独立道具包：{held}。限层增益：{active}；临装：{worn}；次数效果：{charges or '无'}。{damage}"


_FIELD_TOPICS = ("地图限定道具", "地图限定的道具", "地图专属道具", "地图道具", "局内道具", "临时道具", "临装", "道具特效")
_FIELD_MODES = {"格挡": "guard", "蓄力": "burst", "限层": "timed", "佩戴": "wear", "一次性": "instant"}
_FIELD_EFFECTS = {
    "wish": "「为了我所期待的」全图首次进新层独立 0.1%，本局首次抵达或跨过 F500 保底一件。"
            "确认后清掉背包和安全箱中的本局拾取并击杀当前活敌。其他临装、入场补给、已入库奖励保留。",
    "agi": "敏捷影响先手、闪避和普通逃跑判定。敏捷相同时玩家先手。",
    "crit": "暴击率加成按百分点直接加到暴击判定。",
    "dodge": "闪避率加成按百分点直接加到闪避判定。",
    "combo": "每回合主攻后，敌人仍存活才判追击。追击使用六成攻击力，另行判定闪避和暴击，不吃首回合重击。",
    "leech": "吸血按本回合主攻和追击的实际伤害计算，不含过杀伤害。吸血率上限 42%，单回合回血最多为最大生命的 10%（回血上限至少为 1 点）。",
    "thorns": "承受攻击后按实际掉血和护盾吸收量反伤。玩家当场倒下时不触发。",
    "regen": "每次进新层时按最大生命回血。旧版已经激活的锁血蓄力期间暂停进层回血。",
    "pierce": "穿透按比例忽略敌人的防御，和装备的穿透合计最多忽略 75%。",
    "first": "首回合攻击加成只应用于战斗第一回合的主攻，追击不享受。",
    "boss": "对首领伤害加成只在攻击首领时生效。",
    "guard": "格挡在受到敌方攻击时扣一次，闪避成功不扣。先算减伤与格挡，再由护盾抵扣剩余伤害。",
    "burst": "蓄力只用于主攻，主攻即消耗一次。没有必中效果时仍可能被闪避。已有蓄力时不能再叠加。",
    "judgement": "只可在战斗中对当前敌人使用，每次消耗一件。50% 立即击败当前敌人（包括首领），按正常胜利结算奖励。"
                 "另有 50% 自己立即死亡，背包和安全箱物资全部丢失，新手保险和替身娃娃均无效。",
    "damage_multiplier": "持续佩戴时，双方的直接战斗伤害按倍率结算。攻击、失败逃跑时的受击、手雷和反伤都适用，护盾照常抵扣。"
                         "两槽佩戴同款时倍率相乘。陷阱、自伤、概念集合体-公正与队友支援按原本规则结算。",
    "timed": "限层增益包含使用的那一层，同时最多三种。重复使用同款不会叠强度，可在后续层续时。",
    "wear": "本局有两个临装槽。拾取时可装备或放弃。有空槽时手动确认装备到第一个空槽，槽满时可选一个槽替换。"
            "两槽可戴同款，属性加成和副作用累加，再与限层增益一起按各属性上下限结算。特殊伤害倍率按道具说明相乘。换装只替换选中的槽。"
            "待选临装会保存在当前局内，先完成选择才能继续楼层操作。其他临时道具照常放入局内道具包。",
}


def _field_pool_summary(data):
    counts = Counter(item["map"] for item in data["expedition"]["field_items"].values())
    per_map = "、".join(f"{spec['name']} {counts[key]} 种" for key, spec in data["maps"].items())
    return f"局内道具共 {sum(counts.values())} 种，全图通用 {counts['all']} 种。专属分布为{per_map}。"


def _field_pickup_weight_text(rules) -> str:
    heal_weight, worn_weight = rules.FIELD_HEAL_WEIGHT, rules.FIELD_EQUIPPED_WEIGHT
    return (
        "触发拾取后才按本图道具池的相对权重抽取。"
        f"有回血、进层回血或吸血正效果的道具，权重乘 {heal_weight:g}。"
        f"当前已装备的同款临装权重乘 {worn_weight:g}，两槽都穿同款也只乘一次，仍有机会再次抽到。"
        f"回血类临装同时满足两条时乘 {heal_weight * worn_weight:g}。"
        "楼层拾取率和普通物资掉落各自独立结算。"
    )


def _field_reference(data, query=""):
    rules = _season_rules()
    query = query.strip().casefold()
    show_weight = any(word in query for word in ("掉率", "掉落", "权重", "概率", "出率", "爆率", "拾取"))
    items = []
    for key, item in data["expedition"]["field_items"].items():
        area = data["maps"].get(item["map"], {}).get("name", "全图通用")
        items.append((key, item, area, rules.field_description(item)))
    if query:
        by_name = [row for row in items if any(query in name.casefold() or name.casefold() in query
                                              for name in (row[0], row[1]["name"]))
                   or (row[1].get("judgement") and "公正" in query)
                   or (row[1].get("damage_multiplier") and any(word in query for word in ("平衡性调整", "双向伤害")))]
        if by_name:
            items = by_name
        elif "临装" in query and any(word in query for word in ("槽", "装位", "换装", "同款", "叠加", "重复", "佩戴", "放弃", "卸下", "解除")):
            answer = (_FIELD_EFFECTS["wear"] + "网页在本层界面点选，QQ 发「局内道具」打开菜单后回对应序号。"
                      "同款卸下一件时，网页按槽位卸下，QQ 可发「局内道具 解除 名称 1」或「局内道具 解除 名称 2」。")
            return answer + ("\n" + _field_pickup_weight_text(rules) if show_weight else "")
        elif any(category in query for category in _FIELD_TOPICS) and any(
            word in query for word in ("有没有", "多少", "几种", "分布")
        ):
            examples = "、".join(data["expedition"]["field_items"][key]["name"] for key in
                                ("field_apron", "field_ticket", "field_singularity", "field_hammer"))
            return (_field_pool_summary(data) + f"各图的专属例子依次是{examples}。"
                    "只在本局拾取和使用。群里发「地图道具」看全表，网页局内点「临装」看当前持有。")
        elif any(category in query for category in _FIELD_TOPICS) and any(
            name in query for name in _MAP_SHORT.values()
        ):
            items = [row for row in items if row[1]["map"] in _MAP_SHORT and _MAP_SHORT[row[1]["map"]] in query]
        elif "通用" in query and any(category in query for category in _FIELD_TOPICS):
            items = [row for row in items if row[1]["map"] == "all"]
        elif query != "道具" and not any(category in query for category in _FIELD_TOPICS):
            items = [row for row in items if query in row[2].casefold() or query in row[3].casefold()
                     or query == _MAP_SHORT.get(row[1]["map"]) or _FIELD_MODES.get(query) == row[1]["mode"]]
    if not items:
        return ""
    lines = ["【地图限定道具】" + _field_pool_summary(data),
             "仅本局可用，独立于普通掉落。首层、首领及首领前一层必拾，其余每层 52%。"
             "网页点「临装」，QQ 发「局内道具」查看和使用。",
             _field_pickup_weight_text(rules)]
    effects = set()
    for _, item, area, description in items:
        weight = f"，基础抽取权重 {item['weight']:g}" if show_weight else ""
        lines.append(f"• {item['name']}（{area}{weight}）：{description}")
        effects.update(item.get("mods", {}))
        effects.add(item["mode"])
        if item.get("judgement"):
            effects.add("judgement")
        if item.get("damage_multiplier"):
            effects.add("damage_multiplier")
    lines.extend(f"• 特效机制：{description}" for key, description in _FIELD_EFFECTS.items() if key in effects)
    if "wear" in effects:
        caps = []
        for mod, (lower, upper) in rules.MOD_CAPS.items():
            if mod in effects:
                bounds = f"{lower:+g} 至 {upper:+g}" if mod == "agi" else f"{lower:+.0%} 至 {upper:+.0%}"
                caps.append(f"{rules.MOD_LABELS[mod]} {bounds}")
        if caps:
            lines.append("• 相关局内属性加成范围（两槽与限层增益共享）：" + "、".join(caps))
    return "\n".join(lines)


def _render_web() -> str:
    return (f"无尽电梯网页版：{_season_rules().WEB_URL}\n"
            "手机、平板和电脑都能玩。网页与 QQ 群共用角色、装备、仓库和正在进行的探险。"
            "\n注册账号需 3–32 位字母、数字、点、下划线或短横线，以字母或数字开头，密码 8–128 字符。"
            "填写自己的 QQ 号，在已启用游戏的群里用该 QQ 发送网页生成的「/电梯验证 密钥」。一个 QQ 绑定一个网页账号。"
            "\n可用注册账号或已绑定的 QQ 号登录。如果同一号码和密码同时匹配两种身份，网页会让你选身份。"
            "勾选「记住此设备」，同一浏览器下次会自动登录并续存凭证。Cookie 每次最多保存 400 天。"
            "\n改密填原密码和新密码，忘记密码填注册账号和新密码。两种请求都需要原绑定 QQ 在群内确认。"
            "密钥有效 10 分钟且只能使用一次。改密后所有设备会退出，旧密码和旧验证密钥失效。")


def _render_web_play() -> str:
    return (
        "【网页版操作】网页与 QQ 共用存档。从 QQ 切回网页，可点顶部同步按钮取得最新状态。\n"
        "• 手机点底部标签切换探索、仓库、装备、交易和图鉴，也可在空白区域左右滑。"
        "电脑 1–5 切换这些页。探索页 E 下潜或深入，Shift+E 按已保存的档位连战，F 战斗，R 撤离，Q 背包，T 临装，B 战报。\n"
        "• 在探索页「连战设置」可选层数、金红掉落是否暂停、奇遇处理方式，以及是否自动打精英。设置与 QQ 共用。\n"
        "• 选地图后可从仓库选携带补给。楼层号和生命下方显示本层拾取。战斗后直接显示胜负、伤害、战利品和逐回合战报。"
        "奇遇结算显示收获、代价和效果，跳层时还会标明出发与抵达层。\n"
        "• 点物品卡片任意处打开详情。网页仓库每页 72 件，默认按品质从高到低，再按单价排列。"
        "仓库页可切为本局物资，查看背包、安全箱、临装和生效中的效果。交易页可在商店与黑市之间切换。\n"
        "• 拾到持续佩戴类临装后，在本层界面确认占用第一个空槽或放弃。槽满时可选择替换 1 号槽或 2 号槽。"
        "两槽可戴同款并叠加效果，可按槽位卸下。\n"
        "• 安全箱里点「取回背包」可腾出空位。箱满时，在背包里点想保护的物品，再点「换入安全箱」并指定替换格。取出的物品回到普通背包。\n"
        "• 网页发普通或强效求援会播报到绑定群，群友发「支援」可帮忙。"
        "绑定群还会播报新解锁虹彩、成功带出的红货或单件估值至少 30,000 金的物品，以及 F8 起的指定月度撤离纪录。"
    )


def _render_safe() -> str:
    return (
        "【安全箱】正式局翻车时，箱里的物品也能带回仓库。部分仅限本局使用的任务钥匙只能放背包。\n"
        "• 网页点背包物品可「锁入安全箱」。满箱时点该物品的「换入安全箱」，指定取出的格。"
        "点箱内的物品选「取回背包」可单独腾位，取出的物品变为普通背包物品。\n"
        "• QQ 发「存箱」自动保护背包里最贵的合格物品，也可指定物品。发「换箱」自动用更值钱的物品换出最低价物品，"
        "指定交换可用「换箱 新物品 | 旧物品」。"
        "需要按格位取出的网页操作对应 take_box。\n"
        "• 可在商店永久扩容。哆啦的临时扩容袋本局多开一格，每局限用一次。"
    )


def _render_exploration() -> str:
    return (
        "【探索、撤离和遗物】深入一层会生成新的遭遇。奇遇选项也可能让你跳层，"
        "但连进、跃迁卡、奇遇都会在下一处未通过的首领层停下。\n"
        "• 连战预设 3、5、10 层，默认 3 层。发「连战设置」或打开网页探索页的连战设置。"
        "奇遇默认暂停，可改为从付得起的选项中随机选择并正常结算，或无视奇遇继续深入。"
        "精英默认暂停，可开启自动打精英。金红掉落是否暂停可单独设置。"
        "首领、遗物、待选临装、生命不超过一半、战斗平手及触发撤离伏击前的停点仍会暂停。\n"
        "• 本层清怪后会安全撤离。地图触发撤离伏击时，有在场怪就会由它守门并保留当前生命，空层会出现精英守门怪。"
        "打倒守门怪或用烟雾弹就能出门。普通战斗用烟雾弹是脱战留在本层，首领或守门怪战斗中用是立即撤离。\n"
        "• 本局连续 30 分钟没操作会自动安全撤离，物资入库并计有效撤离。背包或安全箱里的替身娃娃在致死时自动消耗，保住物资，本局按战败结算。"
        "前 3 局翻车有保险，之后失去普通背包物资。\n"
        "• 无保险翻车丢的背包价值至少 300 金才留下遗物盒，保存 24 小时。"
        "遗物限同地图、与掉落层差不超过一层，自己不会碰到自己的盒。带走遗物得活着撤离，归还则直接让原主收回物资。"
        "归还基础赏金是估值的 35%，词条可调整。同一对玩家每天前两次归还有赏金。"
    )


def _render_combat() -> str:
    return (
        "【战斗与撤退】敏捷高者先手，敏捷相同玩家先手。普通怪单次 15 回合，精英自然遭遇比旧版少 40%，第 8 回合整备，第 16 回合平手退场。"
        "首领每 4 回合整备。中场保留血量与累计回合数，可用药、换临装或请求支援。"
        "首领每第 4 回合重击 ×1.26。\n"
        "• 首领和守门怪平手时保留双方生命和进度，下次可继续。普通怪和精英的平手结局是怪物退走，没有战利品。"
        "失败的逃跑先挨一次攻击，活着就立即开战。首领无法普通逃跑。\n"
        "• 正式击败首领回满生命。每场还额外独立判定一次红色套装装备，商场首领 F8 为 0.40%，"
        "随地图和首领层序增加，上限 3%。装备需要带出。另有独立 0.5% 概率掉落一件虹彩。"
        "四款等概率，每款每次正式击败首领的概率为 0.125%。掉落立即入仓，首次获得当场生效，翻车也保留。"
        "同款可收藏多件，技能只计一次，集齐四种才有共鸣。\n"
        f"• 深渊实验室的一次性道具「概念集合体-公正」。{_FIELD_EFFECTS['judgement']}\n"
        f"• 全图通用临装「策划的平衡性调整」。{_FIELD_EFFECTS['damage_multiplier']}\n"
        "• 网页旅客面板支持悬停或点击基础属性看计算来源，二级属性随当前敌人更新，点「查看计算公式」可读完整规则。热成像雷达使用后显示下一层层数及遭遇。\n"
        "• 网页在探索首屏看当次逐回合战报和战利品，QQ 发「战报」看最近一场。"
        "地图限定道具的追击、蓄力、吸血、格挡等效果可用 field 按名称查询。"
    )


def _render_support() -> str:
    return (
        "【求援和群友支援】正式局遇精英或首领时，可用一次免费信号，容许一名群友加入。"
        "强效信号弹每枚最多可让三名不同群友帮忙。正在生效的信号不能叠发，模拟战没有真实求援。\n"
        "• 信号有效 3 分钟。网页版发出的信号会在账号绑定群报出楼层、敌人和生命，群友发「支援」或「救」即可攻击。"
        "有多个有效信号时，默认选最新的、自己还没帮过的那个；也可指定发信号的 QQ。\n"
        "• 信号等待期间发起者不能主动硬刚或普通逃跑。支援者不能帮自己，同一信号每人只帮一次。"
        "每日前 5 次成功支援默认给蓝色盲盒，「热心」词条给紫色，领满当天奖励还可以继续支援。\n"
        "• 支援造成的单次首领伤害最多是首领最大生命的 22%。最后一击的战利品归被支援者，守门怪被打倒后被支援者直接撤离。"
    )


def _render_loot(data: dict) -> str:
    rules = _season_rules()
    rows = [
        "【掉落路径与估值】爆率决定掉什么，估值倍率决定掉落值多少金。已获得的物品以存档记录的单价为准。",
        "• 金红档按地图基础率乘楼层、幸运及局内加成判定，最高 68%。"
        "宝箱命中金档后，有 6% 概率由金盲盒或 VIP 卡等概率替代。非白的普通档有 16% 掉同档装备。"
        "蓝紫档在另一个不重叠的 6% 区间掉同档盲盒。",
        "• 各地图金红藏品倍率为" + "、".join(f"{m['name']} ×{rules.collection_value_multiplier(m):.2f}"
                                         for m in data["maps"].values())
        + "。角色估值加成和首领战损另计。深渊实验室每局首次结算物资另发一件紫装。",
        "• 普通藏品实际入库时点亮图鉴。安全撤离、保险箱保住的入库和解码入库都算，之后出售仍保留图鉴。",
    ]
    quality_names = {"worn": "战损", "plain": "普通", "mint": "九九新", "signed": "签名", "shiny": "欧皇"}
    rows.append("• 材料品相权重与估值倍率为" + "、".join(
        f"{quality_names.get(key, key)} {record['weight']}% ×{record['multiplier']:g}"
        for key, record in data["qualities"].items()
    ) + "。")
    restricted = [
        f"{item['name']}限{data['maps'][item['map']]['name']} F{item.get('min_floor', 1)} 起"
        for item in data["items"].values()
        if item["kind"] == "collection" and item.get("map") in data["maps"]
    ]
    if restricted:
        rows.append("• 地图专属藏品为" + "、".join(restricted) + "。加密盲盒的藏品池只含通用藏品。")
    rows.append("• 蓝、紫盲盒的藏品池只有通用大金，金盲盒的藏品池包括通用大金和传世红。"
                "每档另有固定 6% 概率给替身娃娃，其余结果发装备。")
    for grade, crate in data["expedition"]["crates"].items():
        gear = "、".join(f"{_RARITY_CN[g]}装 {weight}%" for g, weight in zip(crate["grades"], crate["weights"]))
        rows.append(f"  {_RARITY_CN[grade]}盲盒藏品率 {crate['collection']:.0%}。进入装备分支后为{gear}。")
    rows.append("• VIP 卡会预约下一处普通楼层的隐藏金库。若先撞到首领，金库保留到首领之后。"
                "进入金库连开三箱，每箱蓝色起步。"
                "吸金磁铁使用时马上抽一件宝箱物资，并让后续掉落的估值加成累加 25 个百分点。"
        "游商一次只卖一件，报价为估值的 70% 向下取整，至少 1 金。"
        "开价时有 45% 抽到绿、蓝或紫装三档等概率，否则从冰美式、烟雾弹、冰红茶和临时扩容袋等概率选一件。")
    return "\n".join(rows)


def _render_ranks() -> str:
    return (
        "【月榜与称号】发「排行」看本群月榜总览，发「全服排行」看全服月榜总览。"
        "总览同时显示各张地图的探索榜和收藏、老六、雷锋、倒霉榜，回菜单序号看单榜详情。"
        "也可用「探索榜 商场」「全服排行 商场」或「排行 收藏」等指令直接查看。\n"
        "• 探索榜按地图分别记录当月最高到达层，同层比较在纪录层成功撤离带出的最高估值。"
        "另列最深成功撤离的层数和带出估值。零物资成功撤离会记 0 金。\n"
        "• 收藏榜比图鉴已收集种数，老六榜比捡走遗物估值，雷锋榜比归还遗物次数，倒霉榜比丢失物资估值。\n"
        "• 角色进度、库存照常保留。各图以新榜上线后的地图记录为准。\n"
        "• 跨月后永久称号仍按原有全服统计规则评定。有正成绩的各项第一获得深渊开拓王、大金收藏家、头号舔包老六、感动全服活雷锋或悲情倒霉蛋。"
        "模拟战不记正式榜。"
    )


def _render_overview(data: dict) -> str:
    rules = _season_rules()
    return (f"{rules.WEB_ENTRY}\n"
            f"跨群角色首次确认昵称，起步 500 金和 {rules.TICKET_DAILY_REFILL} 张免费票。"
            f"免费票零点补足，商店每日限购 {rules.TICKET_DAILY_PURCHASE_LIMIT} 张。"
            f"付费票最多持有 {rules.TICKET_PAID_CAP} 张，可跨日使用。下潜先用免费票。前三局有保险。"
            "一局可直接回菜单序号、带药下潜、连进、撤离、捞遗物、打模拟战。"
            "武器、护甲、挂件、核心各一件，共60种底材，含8套红装，每套分2件和4件穿戴效果。图鉴按不同藏品数量解锁阶段奖励。"
            "首领在8、18、30层及之后每20层。正式胜利独立0.5%掉一件虹彩，四款等概率，可重复入仓；四种集齐共鸣。"
            f"{_field_pool_summary(data)}地图道具独立拾取和使用。持续佩戴类临装有两个槽，拾到后自己选槽或放弃。"
            "群里发「地图道具」查全表，网页局内点「临装」。"
            "\n"+_render_shop(data))


def _render_shop(data: dict) -> str:
    items = data["items"]
    lines = ["【市场 / 电梯补给站（金币直购，发「商店」或「市场」打开）】"]
    for key, price in data["shop"].items():
        if key in _VIRTUAL_SHOP:
            title, _, desc = _VIRTUAL_SHOP[key]
            if key == "ticket":
                desc += _ticket_details(data)
            elif key in data["expedition"]["gear_boxes"]:
                desc = _season_rules().box_description(data["expedition"], key)
            lines.append(f"• {title}：{price:,} 金 — {desc}")
        else:
            spec = items[key]
            lines.append(f"• {spec['name']}：{price:,} 金 — {spec.get('description', '')}")
    sb = " / ".join(f"{k} 格 {v:,} 金" for k, v in data["safebox_prices"].items())
    lines.append(f"• 安全箱永久扩容：{sb}（初始 1 格，锁进安全箱的货翻车绝不掉落）")
    lines.append(f"• 全群金雨祈福：发「金雨祈福」消耗 {data['group_buff_price']:,} 金，全群 1 小时大金与传世红爆率 +20%。")
    lines.append("• 商店有装备箱、回复药剂、战术补给、通行与扩容四类货架。群内先选货架，再回商品序号或序号加数量。"
                 "网页版直接按货架展开，同时显示箱子档次、售价及各品质概率。")
    return "\n".join(lines)


def _render_black_market(data: dict) -> str:
    items = data["items"]
    weekly = _weekly_recipes(data)
    lines = [
        f"【黑市以物易物（消耗材料 → 换取成品｜当前周期 {_current_week()}，★ 为本周 3 项减料特惠）】",
    ]
    for idx, r in enumerate(weekly, 1):
        out_spec = items[r["output"]]
        star = "★本周特惠 " if r["featured"] else ""
        std_note = f"（原配方：{_fmt_reqs(data, r['std_requires'])}）" if r["featured"] else ""
        lines.append(
            f"{idx}. {star}换取【{out_spec['name']}】← 消耗：{_fmt_reqs(data, r['requires'])}{std_note}\n"
            f"   效果：{out_spec.get('description', '')}"
        )
    lines.append("大厅空闲时可兑换，每次 1–20 件。每周三张特惠配方的首项材料少要一件。"
                 "首项原本只要一件、配方还有其他材料时，特惠会省掉该首项。发「黑市」看当周配方和材料进度。")
    return "\n".join(lines)


def _render_maps(data: dict) -> str:
    lines = ["【禁区地图总览】",
             "新获得的金红藏品按基础价和下列地图倍率估算，角色估值加成与首领战损另计。"]
    collection_value_multiplier = _season_rules().collection_value_multiplier
    for mk, m in data["maps"].items():
        conds = []
        if m.get("extracts"):
            conds.append(f"累计撤离 {m['extracts']} 次")
        if m.get("entry"):
            conds.append(f"门票 {m['entry']:,} 金")
        access = " + ".join(conds) if conds else "直接开放"
        ambush = f"第 {m['ambush_floor']} 层起 {m['ambush_rate']:.0%} 撤离伏击（击杀本层怪后撤离免伏击）" if m.get("ambush_rate") else "无撤离伏击"
        bm_mats = list(dict.fromkeys(
            data["items"][k]["name"]
            for r in data["recipes"] for k in r["requires"]
            if k not in ("any_blue", "any_green") and data["items"].get(k, {}).get("map") == mk
        ))
        lines.append(
            f"• {m['name']}（简称：{ _MAP_SHORT.get(mk, mk) }）｜准入：{access}｜怪强 ×{m['multiplier']:g}｜基础金红率 {m['rare_rate']:.2%}｜普通物资估值 ×{m['value_multiplier']:g}｜金红藏品估值 ×{collection_value_multiplier(m):.2f}｜{ambush}\n"
            f"  普通怪：{'、'.join(m['monsters'])}｜精英怪：{'、'.join(m['elites'])}｜首领：{'、'.join(m['bosses'])}\n"
            f"  黑市关键材料：{'、'.join(bm_mats) if bm_mats else '通用绿/蓝材料'}"
        )
    return "\n".join(lines)


def _render_items(data: dict) -> str:
    items = data["items"]
    lines = ["【局内道具 & 加密盲盒全览】"]
    for key, spec in items.items():
        if spec["kind"] in ("supply", "crate"):
            sources = ["正式首领独立 0.5% 掉虹彩，四款等概率；立即入仓，同款技能只计一次。"] if spec.get("boss_only") else []
            if key in data["shop"]:
                sources.append(f"商店 {data['shop'][key]:,} 金")
            rec = next((r for r in data["recipes"] if r["output"] == key), None)
            if rec:
                sources.append(f"黑市（{_fmt_reqs(data, rec['requires'])}）")
            if spec["kind"] == "crate":
                sources.append("禁区掉落/支援奖励，带回大厅发「解码」开奖")
            elif not sources:
                sources.append("禁区宝箱/怪物掉落")
            lines.append(f"• {spec['name']}（{_RARITY_CN.get(spec['rarity'], spec['rarity'])}）：{spec.get('description', '')}｜获取：{' / '.join(sources)}")
    stone = items.get("reroll_stone")
    if stone:
        lines.append(f"• {stone['name']}（蓝色）：{stone.get('description', '')}｜获取：黑市（任意蓝色材料×3）")
    return "\n".join(lines)


_GEAR_SLOTS = {
    "武器": ("武器", "主手", "兵器", "weapon"),
    "护甲": ("护甲", "防具", "衣服", "胸甲", "上衣", "铠甲", "armor"),
    "挂件": ("挂件", "饰品", "配饰", "挂饰", "吊坠", "charm"),
    "核心": ("核心", "核心槽", "芯片", "core"),
}


def _gear_parts(data: dict, code: str, slots=()) -> str:
    name = data["expedition"]["gear_sets"][code]["name"]
    parts = [f"{slot}「{piece['name']}」" for slot in (slots or _GEAR_SLOTS)
             for piece in data["equipment"].values() if piece.get("gear_set") == code and piece["slot"] == slot]
    return f"【{name}】" + "、".join(parts)


def _matched_gear_parts(data: dict, q: str) -> list[str]:
    slots = [slot for slot, aliases in _GEAR_SLOTS.items() if any(alias in q for alias in aliases)]
    matches = []
    for code, suit in data["expedition"].get("gear_sets", {}).items():
        name = suit["name"]
        # Accept both halves of common four-character names when the player asks about a set or slot.
        full = name in q or code in q
        short = len(name) == 4 and (slots or "套" in q) and (name[:2] in q or name[2:] in q)
        piece = any(e.get("gear_set") == code and (e["name"] in q or key in q) for key,e in data["equipment"].items())
        if full or short or piece:
            matches.append(_gear_parts(data, code, slots))
    return matches


def _render_equipment(data: dict) -> str:
    grouped: dict[str, list[str]] = {}
    for spec in data["equipment"].values():
        st = " ".join(f"{_STAT_NAMES[f]}{v:+d}" for f, v in spec["stats"].items())
        grouped.setdefault(spec["slot"], []).append(f"{spec['name']}({st})")
    lines = [
        f"【四槽 · {len(data['equipment'])} 件装备底材与养成规则】",
        "品质倍率：绿装 ×1.25（0 词条）/ 蓝装 ×1.75（1 词条）/ 紫装 ×2.45（2 词条）/ 金装 ×3.35（2 词条）/ 红装 ×4.25（3 词条，专属套装底材）；另每成功撤离 1 次永久获得资深探员加成（上限 25 次：最高 生命+100 / 攻击+25 / 防御+12）。",
        "装备每项属性独立浮动 ±10%。品质倍率只放大正属性，负属性只参与浮动。同名词条只取最高值。",
        f"蓝装及以上可重铸。每件装备前 3 次每次 {data['reforge_price']:,} 金，第 4 次起每次再加 450 金。"
        "优先消耗一件洗练石，其次一件蓝色材料。只重抽词条，可能抽回同名词条。发「一键配装」按综合战力选择四槽装备。",
    ]
    for slot, arr in grouped.items():
        lines.append(f"• {slot}（{len(arr)}件）：{'；'.join(arr)}")
    cat = _get_live_elevator_game().catalog
    lines.append("红装每件有三个不同词条，单件回收 24,000 金。同套穿戴两件或四件解锁对应加成，四件时两档同时生效。")
    lines.extend(_gear_parts(data, code) + " · " + cat.gear_set_description(code) for code in cat.gear_sets)
    return "\n".join(lines)


def _render_affixes(data: dict) -> str:
    grouped: dict[str, list[str]] = {}
    for af in data["affixes"].values():
        grouped.setdefault(af["school"], []).append(f"【{af['name']}】{af['description']}")
    lines = [f"【五大流派 {len(data['affixes'])} 条装备词条】"]
    for school, arr in grouped.items():
        lines.append(f"▸ {school}：" + " ｜ ".join(arr))
    return "\n".join(lines)


def _render_sets(data: dict) -> str:
    lines = [f"【{len(data['sets'])} 套陈列室大金收藏与永久加成】"]
    for sname, sdata in data["sets"].items():
        members = [
            f"{it['name']}({_RARITY_CN.get(it['rarity'], it['rarity'])}"
            + (f",仅{_MAP_SHORT.get(it['map'], it['map'])}{it.get('min_floor', 1)}层+" if it.get("map") else "")
            + ")"
            for it in data["items"].values()
            if it["kind"] == "collection" and it.get("set") == sname
        ]
        lines.append(f"• {sname}（集齐永久加成：{sdata['description']}）：{'、'.join(members)}")
    cat = _get_live_elevator_game().catalog
    for code in cat.gear_sets:
        lines.append(_gear_parts(data, code) + " · " + cat.gear_set_description(code))
    return "\n".join(lines)


def _describe_choice_static(data: dict, opt: dict) -> str:
    costs = []
    if opt.get("cost"):
        costs.append(f"花{opt['cost']:,}金")
    if opt.get("hp_current_cost"):
        costs.append(f"耗{opt['hp_current_cost']:.0%}当前血")
    if opt.get("buy_offer"):
        costs.append("七折价买走私货")

    prob = ""
    if "guess" in opt:
        prob = "33%猜中"
    elif opt.get("agi_check"):
        prob = "敏捷检定(48%+敏×1.2%-层数×1.8%+奇遇加成,10%~94%)"
    elif "chance" in opt:
        prob = f"基础{opt['chance']:.0%}(+奇遇加成)"

    gains = []
    if opt.get("buy_offer"):
        gains.append("得七折装备或补给")
    if opt.get("loot"):
        mr = opt.get("min_rarity")
        tag = (_RARITY_CN.get(mr, mr) + "起") if mr and mr != "white" else "随机"
        gains.append(f"得{opt['loot']}件{tag}物资")
    if opt.get("random_supply"):
        gains.append("随机得" + "/".join(data["items"][k]["name"] for k in opt["random_supply"]))
    if opt.get("items"):
        gains.append("得" + "+".join(data["items"][k]["name"] for k in opt["items"]))
    if opt.get("heal"):
        gains.append(f"回{opt['heal']:.0%}血")
    if opt.get("grant_shield"):
        gains.append(f"+{opt['grant_shield']}护盾")
    if opt.get("atk_boost"):
        gains.append(f"本局攻+{opt['atk_boost']:.0%}")
    if opt.get("rare_boost"):
        gains.append(f"本局金红率+{opt['rare_boost']:.0%}")
    if opt.get("hint"):
        gains.append("偷看下层")
    if opt.get("buff_loot"):
        gains.append("下箱品质+1档")
    if opt.get("skip_floors"):
        gains.append(f"直降{opt['skip_floors']}层")

    fail = ""
    if prob:
        if opt.get("failure_loot"):
            fail = f"；失败得{opt['failure_loot']}件安慰货"
        elif opt.get("failure_hp"):
            fail = f"；失败扣{opt['failure_hp']}HP"
        elif opt.get("failure_fraction"):
            fail = f"；失败扣{opt['failure_fraction']:.0%}最大血"

    if not costs and not gains and not prob:
        return f"{opt['label']}(安全离开)"
    c_str = ("/".join(costs) + ",") if costs else ""
    g_str = "+".join(gains) if gains else "无事"
    return f"{opt['label']}({c_str}{prob + '→' if prob else '100%→'}{g_str}{fail})"


def _render_events(data: dict) -> str:
    lines = [f"【{len(data['events'])} 种奇遇与各选项的基础条件】",
             "固定概率和敏捷判定会计入奇遇加成，实时胜算限制在 10%–94%。猜三选一的结果由隐藏答案决定。"
             "伤害按护盾和减伤结算；跳层最多到下一处首领层。网页选完后在首屏查看实际结算。"]
    for ev in data["events"]:
        details = " ｜ ".join(_describe_choice_static(data, c) for c in ev["choices"])
        lines.append(f"• {ev['name']}：{details}")
    return "\n".join(lines)


def _render_quests() -> str:
    return (
        "【每日悬赏委托（发「悬赏」或「每日任务」查看）】\n"
        "每天自动为每位玩家抽取 3 项悬赏，正式局累计，达成时自动发奖。"
        "深度任务按实际到访的楼层累积，跳过的层不计。安全撤离任务包含超时安全撤离和零物资撤离。任务池如下。\n"
        + "\n".join(_QUEST_POOL_TEXT)
    )


def _render_api_reference(data: dict) -> str:
    shop_items = [f"{key}({data['items'][key]['name']} {price:,}金)" for key, price in data["shop"].items()
                  if key in data["items"]]
    maps_s = " / ".join(f"{mk}({m['name']})" for mk, m in data["maps"].items())
    ticket_limit = _season_rules().TICKET_DAILY_PURCHASE_LIMIT
    return (
        "【《无尽电梯》全开放 AI 接口手册（供 create_elevator_order 派单调用）】\n"
        "一张派单最多 60 步。每步格式为「action|argument|中文说明（含预计金流/效果）」。"
        "确认后按顺序执行，每步独立 SAVEPOINT，sell 步骤自动带确认。\n"
        "同名金红藏品各留单价最高一件，直接调用 plan_elevator_collection_cleanup 计算整仓并生成一张待确认派单。\n"
        "━━ 一、大厅经营与整理接口（需在大厅空闲状态）━━\n"
        "• sell：\n"
        "  - 清仓（一键卖非黑市白绿蓝料+闲置绿装）｜杂物（白+绿料）｜非黑市蓝｜白/绿/蓝/紫（按品质卖材料）\n"
        "  - 绿装 / 蓝装 / 紫装 / 金装 / 红装 / 蓝绿装 / 闲置装备（卖全部未穿戴绿+蓝+紫装，自动保护已穿戴、金装和红装）\n"
        "  - 装备 #id1 #id2 ...（精准卖指定未穿戴装备编号）｜#stock_id [数量/全部]｜物品名 [数量/全部]\n"
        "• buy：\n"
        f"  - 补票（按今日限购余量、付费票空位和余额购买）｜买药（冰美式×3+烟雾弹×1）｜safebox（扩容安全箱+1格）｜ticket 1~{ticket_limit}\n"
        "  - 三档装备箱：gear_box N（新手400金）｜gear_box_mid N（进阶1600金）｜gear_box_high N（尊享5500金）\n"
        f"  - 直购道具（支持 名称/id 数量）：{'、'.join(shop_items)}\n"
        "• decode：全部 / N / 蓝 全部 / 紫 全部 / 金 全部（解码仓库加密盲盒）\n"
        "• craft：配方成品名或item_id [数量 1~20]（黑市以物易物兑换）\n"
        "• autoequip：（空参数，按综合战力自动穿戴最强套装）\n"
        "• wear：#equip_id（穿戴指定装备）\n"
        "• reroll：#equip_id（重铸指定蓝/紫/金装词条，耗金币+1洗练石或任意蓝料）\n"
        "• blessing：（空参数，花金币开启全群 1 小时大金/红货爆率 +20% 金雨）\n"
        "• rename：新昵称（1~16 字，修改全服跨群昵称）\n"
        "━━ 二、开局与探索接口 ━━\n"
        f"• start：地图名 [| 道具 数量 ... 或 | 带药]（消耗 1 通行证+门票；地图：{maps_s}；可写「推荐 | 带药」）\n"
        f"• simulate：地图名（免费模拟战，0消耗/不带出/不计榜，全地图开放并自带演习补给包）\n"
        "━━ 三、局内动作接口（需在电梯探索局内）━━\n"
        "• down：（空，深入下一层）｜rush：最多10层（留空用玩家档位。首领必停，奇遇、精英和金红按连战设置处理）\n"
        "• rush_settings：3/5/10、跳过金红/遇金红暂停、遇奇遇暂停/随机奇遇/无视奇遇、遇精英暂停/自动打精英，可用空格组合。\n"
        "• fight：（空，硬刚当前怪）｜flee：（空，溜走）｜extract：（空，撤离回大厅；本层清怪后撤离 0% 伏击）\n"
        "• save_box：[物品名、背包序号或空]（锁最贵或指定物品进安全箱）"
        "｜swap_box：空参数自动换入更贵货，或填「背包物品 | 保险箱物品」指定交换"
        "｜take_box：按保险箱从 1 开始的格位取回背包\n"
        "• use：道具名（冰美式/红茶/烟雾弹/脉冲雷/撬锁器/雷达/强化信号弹/扩容袋/肾上腺素/便携护盾/VIP门禁卡/空间跃迁卡）\n"
        "• field_pick：楼层:道具ID:槽位（有空槽时只接受第一个空槽，槽满时任选 1 或 2，0 放弃。楼层和道具 ID 必须与待选项一致。）\n"
        "• field_use：道具名或 ID（使用局内道具）；旧存档临装仍在道具包时，用「道具ID 槽位」选择 1 或 2。"
        "使用「概念集合体-公正」需有当前敌人，判负会丢光背包和安全箱物资。\n"
        "• event：选项序号（1/2/3 处理奇遇）｜rob：（舔包）｜rescue：（捞尸拿赏金）｜leave：（离开遗物/奇遇）\n"
        "• flare：（空，精英/守门怪求救，模拟战不可用）｜support：[目标或空]（空降支援群友领盲盒，大厅可用）\n"
        "━━ 四、查询展示接口 ━━\n"
        "• field（本局道具与待选临装）/ field_guide（地图临装手册）/ atlas（图鉴与背包技能）/ menu / shop / warehouse [模式] / equipment / black_market / quests / gallery / rank / all_rank / bag / battle_log / lookup 名称 / help 主题"
    )


def _render_commands() -> str:
    return (
        "【常用指令与快捷操作速查（所有菜单均支持直接回序号，引用也行）】\n"
        "• 大厅与开局：电梯、下潜（回「2 冰美式 2 烟雾弹」或「2 带药」带药进图）、悬赏 / 每日任务、改名 新昵称（全服跨群共享）\n"
        "• 局内操作：进(深入)、连战、打(硬刚)、跑(溜走)、撤(撤离)、战报(合并转发看上一场逐回合详情)、存箱 / 换箱、摇人、使用 名称、背包。\n"
        "• 连战设置可组合使用，例如「连战设置 5 随机奇遇 自动打精英」。"
        "可选 3/5/10 层、跳过金红/遇金红暂停、遇奇遇暂停/随机奇遇/无视奇遇、遇精英暂停/自动打精英。首领仍会暂停。\n"
        "• 临装拾取：回菜单序号确认装备到第一个空槽，槽满时可选替换任一槽。也可放弃。"
        "错过菜单可发「局内道具」或「临装」重新打开。"
        "同款可戴两件。卸下时可发「局内道具 解除 名称 1」或「局内道具 解除 名称 2」指定槽位。\n"
        "• 免费模拟战：发「模拟战」或「模拟 拍卖行」可 0 消耗免费演练全地图（自带虚拟演习补给包，套用当前真实装备与属性；不扣门票/金币/道具，也不带出任何战利品或计榜）\n"
        "• 防刷屏优化：商店/仓库/黑市/装备/攻略/悬赏/排行榜/战报在群内自动折叠为合并转发卡片（点开看图，关掉后直接回序号操作），局内战报与菜单默认采用紧凑模式。\n"
        "• 群友互动：支援 / 救（空降补刀领盲盒）、舔包 / 捞尸、金雨祈福\n"
        "• 商店与开箱快捷：商店、补票(买满今日票)、买药(冰美式×3+烟雾弹×1)、抽绿箱 5 / 抽蓝箱 5 / 抽金箱 5、全开 / 开箱 5 / 开箱 蓝 全部 / 开箱 全部\n"
        "• 仓库与极速出售：仓库（12件/页，支持分类/品质/价值/最新/盲盒/道具/材料/珍藏/黑市）、一键清仓(白绿料+非黑市蓝+闲置绿装)、卖绿装 / 卖蓝装 / 卖紫装 / 卖金装 / 卖蓝绿装 / 清装备(绿蓝紫全清，保护已穿戴、金装和红装)、卖装备 #1 #2（加「确认」可跳过二次确认）\n"
        "• 黑市与养成快捷：黑市、兑换 名称 数量、装备、穿 序号、配装(一键配装)、重铸、带药下潜、陈列室。\n"
        "• 月榜：排行 / 全服排行，查看各地图和其他榜单的前三名，按序号看详情。也可发探索榜 商场、全服排行 商场、排行 收藏等直接查看。\n"
        "• AI 代操作派单：直接跟龙娘说你想怎么操作（如「帮我卖闲置蓝绿装再买5瓶冰美式抽3个进阶箱配装」），龙娘会读真实存档算清账目并派发带唯一单号（如 E7K3M）的逐条清单，回复「确认 E7K3M」或「确认派单」立即执行，「取消 E7K3M」作废。\n"
        "• 首领、局内道具与网页版：局内道具 / 地图道具、图鉴进度 / 虹彩、首领规则、查 名称（支持查虹彩与套装背景故事）；网页版可在群内发「/电梯验证 密钥」绑定 QQ 共用存档。\n"
        "• 图像百科（已预热秒出）：电梯攻略 / 怎么玩、物品介绍、概率规则、词条介绍、装备介绍、帮助 地图、查 名称"
    )


def _render_rates(data: dict) -> str:
    return _season_rules().probability_guide(_get_live_elevator_game().catalog)


def _search_knowledge(data: dict, query: str) -> str:
    q = query.strip().casefold()
    if not q:
        return ""
    hits = _matched_gear_parts(data, q)
    if any(w in q for w in ("祝福","闪耀","追寻","闻岑", "测试破限","破甲")) or re.search(r"\bb0[1-8]",q):
        rules = _heirloom_rules()
        hits.append("【闻岑的闪耀祝福】四个独立卡槽，单件少量属性。两件套在二级属性常规结算后破限，四件触发专属机制。"
                    "整套四件或两套各两件，从 F51 起分别按 60% 或 44% 渐近上限深层破甲，公式为上限×(楼层−50)/(楼层−50+85)。仅玩家主攻和追击生效。"
                    "正式下潜前发「追寻 1 8」选两套。正式首领每场独立判定闪耀，F8 3%，随楼层增加，最高 10%。"
                    "成功后两套等概率，套内缺件权重 2、已集得权重 1。发「祝福」查看遗物，发「祝福装配 槽位 编号」装配。")
        if match := rules.set_key(q):
            suit = rules.SETS[match]
            hits.append("\n".join([suit["name"],suit["lore"],suit["two"],suit["four"]]))
        else:
            for code,suit in rules.SETS.items():
                if (code.casefold() in q or suit["name"] in q or any(word in q for word in ("全部","所有","介绍","套装","增强","文案"))):
                    hits.append(f"{code} {suit['name']}  两件 {suit['two']}  四件 {suit['four']}")
        for code,item in rules.PIECES.items():
            if code.casefold() in q or item["name"] in q or item["alias"] in q:
                hits.append(f"{code} {item['name']} · {rules.SETS[item['set']]['name']}  {item['lore']}")
    if "为了我所期待的" in q or ("献祭" in q and ("500" in q or "道具" in q)):
        hits.append(_FIELD_EFFECTS["wish"])
    if "虹彩" in q and ("套" in q or any(slot in q for slot in _GEAR_SLOTS)):
        names = "、".join(item["name"] for item in data["expedition"]["rainbow"].values())
        hits.append(f"【虹彩共鸣】四件永久技能为{names}。点亮两、三、四种时独立乘攻、生命和防御 ×1.03、×1.07、×1.12，只取当前档；四种额外敏捷 +5、追击 +6 点、开局盾 +30。星环进层盾最多 88。")
    if any(term in q for term in ("计算公式","二级属性","暴击率","命中率","暴击伤害","闪避率","属性来源")):
        hits.append(_render_formulas())
    if any(word in q for word in ("网页", "网站", "绑定qq", "qq绑定", "绑定 qq", "登录", "密码", "记住设备")):
        hits.append(_render_web())
    if any(word in q for word in ("快捷键", "滑动", "本层拾取", "点物品", "网页操作")) or (
        "网页" in q and any(word in q for word in ("仓库", "战斗", "下潜", "操作", "保险箱", "安全箱", "求援", "结算"))
    ):
        hits.append(_render_web_play())
    if any(word in q for word in ("保险箱", "安全箱", "换箱", "取回背包", "扩容袋")):
        hits.append(_render_safe())
    if any(word in q for word in ("撤离", "伏击", "遗物", "舔包", "捞尸", "超时", "连进", "连战", "随机奇遇", "无视奇遇", "自动打精英")):
        hits.append(_render_exploration())
    if any(word in q for word in ("战斗", "战报", "平手", "先手", "逃跑", "败逃", "回合")):
        hits.append(_render_combat())
    if any(word in q for word in ("求援", "摇人", "支援", "信号弹", "群播报")):
        hits.append(_render_support())
    if any(word in q for word in ("估值", "倍率", "品相", "掉落池", "盲盒", "解码", "专属藏品")):
        hits.append(_render_loot(data))
    if any(word in q for word in ("月榜", "排行榜", "排行", "探索榜", "地图榜", "收藏榜", "老六榜", "雷锋榜", "倒霉榜", "赛季榜", "赛季称号", "怎么是0金", "怎么是 0 金", "带出0金", "带出 0 金")):
        hits.append(_render_ranks())
    if any(w in q for w in ("首领","boss","虹彩","彩级","背包技能")): hits.append(_season_rules().boss_guide())
    if any(w in q for w in ("图鉴进度","完成率","虹彩","彩级","背包技能","共鸣")):
        hits.append("\n".join(_season_rules().atlas_lines(data["expedition"],set(),sum(i["kind"]=="collection" for i in data["items"].values()))))
    field=_field_reference(data,q)
    if field: hits.append(field)

    if any(k in q for k in ("出红", "出金", "爆率", "概率", "掉率", "传世红", "大金", "区别", "解码", "公式", "暴击", "闪避", "逃跑", "成长", "数值", "资深", "探员", "怪物", "平衡")):
        hits.append(_render_rates(data))
    if any(k in q for k in ("箱子", "装备箱", "盲盒", "连抽", "五连", "十连", "开箱", "全开")):
        hits.append(_render_shop(data))
    if any(k in q for k in ("改名", "昵称", "引用", "带药", "仓库", "卖", "出售", "二次确认", "模拟", "演习", "连进", "自动", "战报", "刷屏", "转发")):
        hits.append(_render_commands())

    # 1. Virtual shop entries
    for key, (title, aliases, desc) in _VIRTUAL_SHOP.items():
        if q in title.casefold() or any(q in a.casefold() or a.casefold() in q for a in aliases):
            if key == "ticket":
                desc += _ticket_details(data)
            elif key in data["expedition"]["gear_boxes"]:
                desc = _season_rules().box_description(data["expedition"], key)
            hits.append(f"[商店特供] {title}：{desc}")

    # 2. Maps
    for mk, m in data["maps"].items():
        short = _MAP_SHORT.get(mk, mk)
        if (
            q in mk.casefold() or q in m["name"].casefold() or q in short
            or any(q in mon.casefold() or mon.casefold() in q for mon in (*m["monsters"], *m["elites"], *m["bosses"]))
        ):
            conds = []
            if m.get("extracts"):
                conds.append(f"累计撤离 {m['extracts']} 次")
            if m.get("entry"):
                conds.append(f"门票 {m['entry']:,} 金")
            access = " + ".join(conds) if conds else "直接开放"
            ambush = f"第 {m['ambush_floor']} 层起 {m['ambush_rate']:.0%} 撤离伏击（清怪后撤离免伏击）" if m.get("ambush_rate") else "无伏击"
            collection_multiplier = _season_rules().collection_value_multiplier(m)
            hits.append(
                f"[地图] {m['name']}（准入：{access}，怪强×{m['multiplier']:g}，大金率 {m['rare_rate']:.1%}，普通物资估值×{m['value_multiplier']:g}，新金红藏品地图倍率×{collection_multiplier:.2f}，{ambush}；"
                f"普通怪：{'、'.join(m['monsters'])}；精英：{'、'.join(m['elites'])}；首领：{'、'.join(m['bosses'])}）"
            )

    # 3. Items (supplies, crates, materials, collections)
    weekly = _weekly_recipes(data)
    for key, spec in data["items"].items():
        names = [key, spec["name"], *spec.get("aliases", ())]
        if any(q in n.casefold() or (len(n) >= 2 and n.casefold() in q) for n in names):
            info = [f"[物品] {spec['name']}（{_RARITY_CN.get(spec['rarity'], spec['rarity'])}/{spec['kind']}）"]
            if spec.get("description"):
                label = "物品描述" if spec["kind"] == "collection" and not spec.get("boss_only") else "效果"
                info.append(f"{label}：{spec['description']}")
            if spec.get("lore"):
                info.append(f"背景故事：{spec['lore']}")
            if spec.get("set"):
                sdesc = data["sets"].get(spec["set"], {}).get("description", "")
                info.append(f"所属套装：{spec['set']}（集齐：{sdesc}），基础估值 {spec['value']:,} 金")
            sources = ["正式首领独立 0.5% 掉一件虹彩，四款等概率。立即入仓，首次永久点亮，同款可重复收藏，无金币回收价值。"] if spec.get("boss_only") else []
            if key in data["shop"]:
                sources.append(f"商店 {data['shop'][key]:,} 金")
            wrec = next((r for r in weekly if r["output"] == key), None)
            if wrec:
                tag = "★本周特惠" if wrec["featured"] else "标准"
                sources.append(f"黑市兑换[{tag}]（消耗 {_fmt_reqs(data, wrec['requires'])}）")
            if spec.get("map"):
                mname = data["maps"][spec["map"]]["name"]
                fl = f"第 {spec['min_floor']} 层起" if spec.get("min_floor") else ""
                sources.append(f"{mname}{fl}掉落")
            if sources:
                info.append("获取：" + " / ".join(sources))
            used_in = [
                data["items"][r["output"]]["name"]
                for r in data["recipes"]
                if key in r["requires"]
            ]
            if used_in:
                info.append("黑市用途：可用于兑换【" + "】【".join(used_in) + "】")
            hits.append("；".join(info))

    # 4. Affixes
    for key, af in data["affixes"].items():
        if q in key.casefold() or q in af["name"].casefold() or q in af["school"].casefold() or af["name"].casefold() in q:
            hits.append(f"[词条] 【{af['name']}】（{af['school']}）：{af['description']}")

    # 5. Equipment
    suits = data["expedition"].get("gear_sets", {})
    for code, suit in suits.items():
        if q in (suit["name"], code, "红装", "红装套装") or suit["name"] in q:
            hits.append(_get_live_elevator_game().catalog.gear_set_description(code))
    for key, eq in data["equipment"].items():
        if q in key.casefold() or q in eq["name"].casefold() or eq["name"].casefold() in q:
            st = " ".join(f"{_STAT_NAMES[f]}{v:+d}" for f, v in eq["stats"].items())
            suit = suits.get(eq.get("gear_set"), {}).get("name", "")
            hits.append(f"[装备底材] {eq['name']}（{eq['slot']}，基础属性：{st}）" + (f" · 红装套装 {suit}" if suit else ""))

    # 6. Sets
    for sname, sdata in data["sets"].items():
        if q in sname.casefold() or sname.casefold() in q:
            members = [it["name"] for it in data["items"].values() if it.get("set") == sname]
            lore_note = f"；背景故事：{sdata['lore']}" if sdata.get("lore") else ""
            hits.append(f"[收藏套装] {sname}（永久加成：{sdata['description']}，成员：{'、'.join(members)}{lore_note}）")

    # 7. Events
    for ev in data["events"]:
        if q in ev["name"].casefold() or q in ev["id"].casefold() or ev["name"].casefold() in q:
            details = " ｜ ".join(_describe_choice_static(data, c) for c in ev["choices"])
            hits.append(f"[奇遇] {ev['name']}：{ev['text']}｜选项：{details}")

    if hits:
        return "【无尽电梯资料检索结果】\n" + "\n".join(hits[:15])
    return ""


def lookup_guide(topic: str, query: str) -> str:
    data = _load_content()
    q = (query or "").strip()
    renderers = {
        "overview": lambda: _render_overview(data),
        "shop": lambda: _render_shop(data),
        "black_market": lambda: _render_black_market(data),
        "maps": lambda: _render_maps(data),
        "items": lambda: _render_items(data),
        "equipment": lambda: _render_equipment(data),
        "affixes": lambda: _render_affixes(data),
        "sets": lambda: _render_sets(data),
        "events": lambda: _render_events(data),
        "quests": _render_quests,
        "commands": _render_commands,
        "formulas": _render_formulas,
        "web": _render_web,
        "web_play": _render_web_play,
        "safe": _render_safe,
        "exploration": _render_exploration,
        "combat": _render_combat,
        "support": _render_support,
        "loot": lambda: _render_loot(data),
        "ranks": _render_ranks,
        "rates": lambda: _render_rates(data),
        "api": lambda: _render_api_reference(data),
        "boss": lambda: _season_rules().boss_guide(),
        "field": lambda: _field_reference(data),
        "atlas": lambda: "\n".join(_season_rules().atlas_lines(data["expedition"],set(),sum(i["kind"]=="collection" for i in data["items"].values()))),
    }
    if topic == "atlas" and q:
        parts = _matched_gear_parts(data, q.casefold())
        if parts:
            return "\n".join(parts)
    if topic in ("formulas", "api", "boss", "atlas", "web", "web_play", "safe", "exploration", "combat", "support", "loot", "ranks"):
        return renderers[topic]()
    if topic=="field":
        filtered=_field_reference(data,q)
        return filtered or "没有查到对应的局内道具。可以按道具名、地图名或特效名查询。"
    if topic == "search" or (q and q not in _GENERIC_QUERIES):
        searched = _search_knowledge(data, q)
        if searched:
            if topic in renderers and len(searched) < 1200 and topic in ("black_market", "shop", "maps"):
                return searched + "\n\n" + renderers[topic]()
            return searched
        if topic in renderers:
            return f"没找到完全匹配「{q}」的单项，附上相关分类资料：\n" + renderers[topic]()
        return f"无尽电梯资料库里没找到「{q}」，附上游戏总览：\n" + _render_overview(data)
    if topic in renderers:
        return renderers[topic]()
    return _render_overview(data)


def _item_name(data: dict, key: str) -> str:
    spec = data["items"].get(key) or data["equipment"].get(key, {})
    return spec.get("name", key)


def _run_observations(data: dict, run: dict, safe_capacity: int, *, compact_bag: bool = False) -> list[str]:
    floor = run["floor"]
    place = data["maps"].get(run["map"], {}).get("name", run["map"])
    phase = "模拟战" if run.get("sim") else "正式局"
    cleared = "｜本层已清怪，安全撤离" if run.get("combat_cleared_floor") == floor else ""
    lines = [
        f"当前局内：{phase} · {place} F{floor}｜HP {run['hp']}/{run['stats']['hp']}｜盾 {run.get('shield', 0)}"
        f"｜本局临时攻击 +{int(run.get('atk_boost', 0)*100)}%{cleared}",
        _run_field_text(run, data),
    ]
    offer = run.get("field_offer")
    if offer:
        key, offer_floor = offer["id"], offer["floor"]
        name = data["expedition"]["field_items"].get(key, {}).get("name", key)
        worn = run.get("field_slots", [run.get("field_worn"), None])
        choices = [worn.index(None) + 1] if None in worn else [1, 2]
        options = [f"{offer_floor}:{key}:{slot}（{slot}号槽）" for slot in choices]
        options.append(f"{offer_floor}:{key}:0（放弃）")
        lines.append(f"F{offer_floor} 待选临装：{name}。先选槽或放弃再继续操作。"
                     f"field_pick 可传入 {'、'.join(options)}。")
    event = run.get("event", {})
    kind = event.get("kind", "ready")
    if kind == "combat" and event.get("enemy"):
        enemy = event["enemy"]
        label = "首领" if enemy.get("boss") else "守门怪" if enemy.get("ambush") else "精英" if enemy.get("elite") else "普通怪"
        lines.append(f"当前{label}：{enemy['name']} HP {enemy['hp']}/{enemy['max_hp']}"
                     f"｜攻击 {enemy['atk']}｜防御 {enemy['def']}｜敏捷 {enemy['agi']}")
    elif kind == "choice":
        config = next((cfg for cfg in data["events"] if cfg["id"] == event.get("id")), None)
        if config:
            options = "｜".join(f"{i}.{_describe_choice_static(data, option)}"
                                 for i, option in enumerate(config["choices"], 1))
            lines.append(f"当前奇遇：{config['name']}。{config['text']}｜选项 {options}")
        if event.get("offer"):
            lines.append(f"本次报价：{_item_name(data, event['offer']['id'])} {event['price']:,} 金")
    elif kind == "relic":
        lines.append("当前有待处理的遗物盒，可带走、归还或离开。")
    elif not offer:
        lines.append("当前等待继续深入或撤离。")

    for key, label in (("bag", "局内背包"), ("safe", "安全箱")):
        items = run.get(key, [])
        number = f"{len(items)}/{safe_capacity} 格" if key == "safe" else f"{len(items)} 件"
        listed = list(enumerate(items, 1))
        if key == "bag" and compact_bag and len(listed) > 12:
            listed = sorted(listed, key=lambda entry: entry[1]["value"], reverse=True)[:12]
            number += "，仅列最贵 12 件"
        shown = "、".join(f"{i}.{_item_name(data, item['id'])}（{item['value']:,} 金）"
                         for i, item in listed)
        lines.append(f"{label}（{number}）：" + (shown or "空"))

    result = run.get("floor_result") or {}
    if result.get("floor") == floor:
        loot = "、".join(f"{_item_name(data, item['id'])}（{item['value']:,} 金）" for item in result.get("loot", ()))
        if loot:
            lines.append("本层已拾取：" + loot)
        field = data["expedition"]["field_items"]
        field_loot = "、".join(f"{field[key]['name']} ×{qty}" for key, qty in result.get("field", {}).items() if key in field)
        if field_loot:
            lines.append("本层拾取的临装：" + field_loot)
        battle = result.get("battle")
        if battle:
            outcome = {"victory": "胜利", "defeat": "战败", "draw": "平手"}.get(battle["outcome"], battle["outcome"])
            lines.append(f"本层最近战斗：{battle['enemy']} · {outcome} · {battle['rounds']} 回合"
                         f"｜造成 {battle['dealt']} 伤害｜承受 {battle['taken']} 伤害。")
    adventure = run.get("adventure")
    if adventure and adventure["destination"] == floor:
        extra = list(adventure.get("lines", ()))
        extra.extend(f"获得 {_item_name(data, item['id'])}（{item['value']:,} 金）"
                     for item in adventure.get("loot", ()))
        lines.append(f"奇遇结算：{adventure['name']} · {adventure['option']} · "
                     f"F{adventure['floor']} 到 F{adventure['destination']}。" + " ".join(extra))
    return lines


def _monthly_rank_lines(con, data: dict, month: str, group_id: str = "") -> list[str]:
    scope = "本群" if group_id else "全服"
    group_join = ("JOIN group_active_users g ON g.user_id=s.user_id AND g.month=s.month AND g.group_id=? "
                  if group_id else "")
    params = (group_id, month) if group_id else (month,)
    query = ("SELECT p.nickname,s.max_floor,s.best_extract_floor,s.best_extract_value "
             "FROM season_map_stats s JOIN players p ON p.user_id=s.user_id " + group_join
             + "WHERE s.month=? AND s.map_id=? AND s.max_floor>0 "
              "ORDER BY s.max_floor DESC,s.floor_value DESC,s.user_id LIMIT 3")
    lines = []
    for map_id, map_data in data["maps"].items():
        players = []
        for index, row in enumerate(con.execute(query, (*params, map_id)), 1):
            floor = row["best_extract_floor"]
            extract = f"最深撤离 F{floor}，带出 {row['best_extract_value']:,} 金" if floor else "暂无带出记录"
            players.append(f"{index}.{row['nickname']} F{row['max_floor']}（{extract}）")
        if players:
            lines.append(f"{scope}探索榜 · {map_data['name']}：" + "；".join(players))
    for name, metric, unit in (("收藏", "collection_count", "件"), ("老六", "rob_value", "金"),
                               ("雷锋", "rescues", "次"), ("倒霉", "death_value", "金")):
        rows = con.execute(
            f"SELECT p.nickname,s.{metric} AS score FROM season_stats s JOIN players p ON p.user_id=s.user_id "
            + group_join + f"WHERE s.month=? AND s.{metric}>0 ORDER BY s.{metric} DESC,s.user_id LIMIT 3",
            params,
        )
        players = [f"{index}.{row['nickname']} {row['score']:,} {unit}" for index, row in enumerate(rows, 1)]
        if players:
            lines.append(f"{scope}{name}榜：" + "；".join(players))
    return lines


def lookup_player_status(user_id: str, section: str, group_id: str = "") -> str:
    if not _DB_PATH.exists():
        return "目前还没有无尽电梯的存档数据。"
    data = _load_content()
    con = sqlite3.connect(_DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        now_ts = int(time.time())
        month = datetime.now(tz=_SHANGHAI).strftime("%Y-%m")
        if section == "group":
            lines = [f"【本群《无尽电梯》实时动态（{month}）】"]
            if group_id:
                buff = con.execute("SELECT expires_at FROM group_buffs WHERE group_id=? AND expires_at>?", (group_id, now_ts)).fetchone()
                lines.append(f"本群金雨祈福：{'生效中（剩 ' + str(max(1, (buff['expires_at']-now_ts)//60)) + ' 分钟，大金/红率+20%）' if buff else '未开启'}")
                sigs = con.execute(
                    "SELECT s.owner_id,s.expires_at,s.capacity,s.helpers,p.nickname,a.payload FROM signals s "
                    "JOIN players p ON p.user_id=s.owner_id JOIN active_runs a ON a.user_id=s.owner_id "
                    "WHERE s.group_id=? AND s.expires_at>?", (group_id, now_ts),
                ).fetchall()
                if sigs:
                    sig_txts = []
                    for s in sigs:
                        st = json.loads(s["payload"])
                        en = st.get("event", {}).get("enemy", {})
                        helpers = json.loads(s["helpers"])
                        sig_txts.append(f"{s['nickname']}({data['maps'].get(st['map'],{}).get('name',st['map'])} F{st['floor']} vs {en.get('name','?')} 剩{en.get('hp','?')}HP，已支援{len(helpers)}/{s['capacity']})")
                    lines.append("可支援信号弹：" + "；".join(sig_txts))
                else:
                    lines.append("可支援信号弹：当前无")
                active_g = con.execute(
                    "SELECT p.nickname,a.payload FROM active_runs a JOIN players p ON p.user_id=a.user_id "
                    "WHERE a.group_id=? AND a.updated_at>?", (group_id, now_ts - 1800),
                ).fetchall()
                if active_g:
                    act_s = []
                    for a in active_g[:10]:
                        st = json.loads(a["payload"])
                        tag = "[模拟]" if st.get("sim") else ""
                        act_s.append(f"{a['nickname']}({tag}{data['maps'].get(st['map'],{}).get('name',st['map'])} F{st['floor']})")
                    lines.append("正在电梯里：" + "、".join(act_s))
                lines.extend(_monthly_rank_lines(con, data, month, group_id))
            lines.extend(_monthly_rank_lines(con, data, month))
            return "\n".join(lines)

        rules = _season_rules()
        player = con.execute("SELECT * FROM players WHERE user_id=?", (user_id,)).fetchone()
        if player is None:
            return (f"这位群友还没玩过《无尽电梯》（发「电梯」或「下潜」即可开玩，"
                    f"新人起步送 500 金和 {rules.TICKET_DAILY_REFILL} 张免费通行证，前 3 局自带防爆保险）。")
        today = datetime.now(tz=_SHANGHAI).date().isoformat()
        archives = {r["item_id"] for r in con.execute("SELECT item_id FROM collections WHERE user_id=?", (user_id,))}
        completed_sets = [
            sname for sname in data["sets"]
            if all(k in archives for k, it in data["items"].items() if it.get("set") == sname)
        ]
        ticket_cap = rules.ticket_capacity(completed_sets, data["sets"])
        tickets, bought_today, paid_tickets = rules.ticket_status(player, today, ticket_cap)
        ticket_balance = (
            f"{tickets}/{ticket_cap} 张（免费 {tickets - paid_tickets}，付费 {paid_tickets}/{rules.TICKET_PAID_CAP}，"
            f"今日已购 {bought_today}/{rules.TICKET_DAILY_PURCHASE_LIMIT}）"
        )
        run_row = con.execute("SELECT payload FROM active_runs WHERE user_id=?", (user_id,)).fetchone()

        if section == "atlas":
            total=sum(v["kind"]=="collection" for v in data["items"].values())
            return "\n".join(_season_rules().atlas_lines(data["expedition"],archives,total))
        if section in ("all", "plan"):
            detail_limit = 60 if section == "all" else 0
            lv = min(25, int(player["extracts"]))
            eq_prices = data["expedition"]["gear_sell"]
            safe_costs = data.get("safebox_prices", {"2": 6000, "3": 30000, "4": 88000})
            if isinstance(safe_costs, dict):
                nxt_val = safe_costs.get(str(player["safebox"] + 1))
                next_safe = f"{nxt_val:,}金" if nxt_val else "已满4格"
            else:
                next_safe = f"{safe_costs[player['safebox'] - 1]:,}金" if 1 <= player["safebox"] <= len(safe_costs) else "已满4格"
            total_stats = dict(data["stats"])
            total_stats["hp"] += lv * 4
            total_stats["atk"] += lv
            total_stats["def"] += lv // 2
            for sname in completed_sets:
                for attr, val in data["sets"][sname].get("stats", {}).items():
                    total_stats[attr] = total_stats.get(attr, 0) + val
            loadout = json.loads(player["loadout"] or "{}")
            eq_ids = set(loadout.values())
            eq_rows = con.execute("SELECT equip_id,base_id,rarity,stats,affixes,rerolls FROM equipment WHERE user_id=? ORDER BY equip_id", (user_id,)).fetchall()
            active_perks: dict[str, float] = {}
            worn_txt, spare_txt = [], []
            spare_counts = {"green": 0, "blue": 0, "purple": 0, "gold": 0}
            reforge_base = int(data.get("reforge_price", 600))
            for r in eq_rows:
                spec = data["equipment"].get(r["base_id"], {"name": r["base_id"], "slot": "?"})
                af_keys = json.loads(r["affixes"])
                afs = "·".join(data["affixes"][a]["name"] for a in af_keys if a in data["affixes"])
                st = json.loads(r["stats"])
                st_s = " ".join(f"{k}+{v}" for k, v in st.items())
                re_cost = reforge_base + max(0, r["rerolls"] - 2) * 450
                tag = f"#{r['equip_id']}[{r['rarity']}/{spec['slot']}]{spec['name']}({st_s}{'|'+afs if afs else ''})"
                if r["equip_id"] in eq_ids:
                    for k, v in st.items():
                        total_stats[k] = total_stats.get(k, 0) + v
                    for a in af_keys:
                        if a in data["affixes"]:
                            active_perks[a] = max(active_perks.get(a, 0), data["affixes"][a]["power"])
                    worn_txt.append(f"{tag}(洗{re_cost}金)")
                else:
                    spare_counts[r["rarity"]] = spare_counts.get(r["rarity"], 0) + 1
                    spare_txt.append(f"{tag}(卖{eq_prices.get(r['rarity'],260)}金)")

            live=_get_live_elevator_game()
            live.catalog.apply_gear_sets(total_stats, active_perks,
                                         (r["base_id"] for r in eq_rows if r["equip_id"] in eq_ids))
            _season_rules().apply_archive(total_stats,active_perks,archives,live.catalog)
            perk_names = "、".join(data["affixes"][a]["name"] for a in active_perks if a in data["affixes"]) or "无"
            unlocked_maps = [f"{m['name']}({mk})" for mk, m in data["maps"].items() if player["extracts"] >= m["extracts"]]
            locked_maps = [f"{m['name']}(差{m['extracts']-player['extracts']}次撤离)" for mk, m in data["maps"].items() if player["extracts"] < m["extracts"]]
            col_total = sum(1 for it in data["items"].values() if it["kind"] == "collection")

            parts = [
                f"【{player['nickname']} 当前存档和进度】",
                f"资产：钱包 {player['gold']:,} 金｜通行证 {ticket_balance}，票价 {data['shop']['ticket']} 金｜安全箱 {player['safebox']} 格（下级扩容 {next_safe}）",
                f"进度：下潜 {player['runs']} 局｜撤离 {player['extracts']} 次（资深加成 血+{lv*4}/攻+{lv}/防+{lv//2}）｜收藏 {len(archives)}/{col_total}（成套：{','.join(completed_sets) or '无'}）",
                f"实时战斗面板：HP {total_stats['hp']} · ATK {total_stats['atk']} · DEF {total_stats['def']} · AGI {total_stats['agi']} · LUK {total_stats['luk']}｜生效词条：{perk_names}",
                f"地图权限：已解锁 {', '.join(unlocked_maps)}" + (f"｜未解锁 {', '.join(locked_maps)}" if locked_maps else ""),
            ]

            if detail_limit:
                parts.extend(_season_rules().atlas_lines(data["expedition"],archives,col_total))
            view = {"stats":total_stats,"hp":total_stats["hp"],"perks":active_perks,"floor":1,"tea_until":0}
            if run_row:
                run = json.loads(run_row["payload"])
                view = run
                if run.get("archive_version",0) < _season_rules().ARCHIVE_VERSION:
                    view = {**run,"stats":total_stats,"perks":active_perks,
                            "hp":max(1,round(run["hp"]*total_stats["hp"]/max(1,run["stats"]["hp"])))}
                parts.extend(_run_observations(data, run, player["safebox"] + run.get("safe_extra", 0),
                                               compact_bag=not detail_limit))
                parts.append(f"局内状态：撬锁={'有' if run.get('lockpick') else '无'}｜VIP金库={'有' if run.get('gold_room') else '无'}"
                             f"｜侦察={run.get('peek') or '无'}" + (f"｜模拟币 {run.get('sim_gold', 0)}" if run.get("sim") else ""))
            else:
                parts.append("当前局内：在大厅空闲")
            sec = _combat_rules().secondary_stats(view,in_run=bool(run_row))
            hit = f"{sec['hit']:.2%}" if sec["hit"] is not None else "遇敌后按敌方敏捷计算"
            parts.append(f"二级面板：主攻命中 {hit}｜闪避 {sec['dodge']:.2%}｜暴击 {sec['crit']:.2%}"
                         f"｜暴击伤害 ×{sec['crit_multiplier']:g}｜穿透 {sec['pierce']:.2%}"
                         f"｜追击 {sec['combo']:.2%}｜吸血 {sec['leech']:.2%}｜对首领增伤 {sec['boss_bonus']:.2%}")

            parts.append("已穿戴：" + ("；".join(worn_txt) or "无"))
            sc_str = " ".join(f"{k}×{v}({v*eq_prices[k]:,}金)" for k, v in spare_counts.items() if v)
            non_gold_sum = sum(spare_counts[k] * eq_prices[k] for k in ("green", "blue", "purple"))
            bg_sum = sum(spare_counts[k] * eq_prices[k] for k in ("green", "blue"))
            parts.append(f"闲置装备 {len(spare_txt)} 件｜{sc_str or '无'}｜蓝绿合计 {bg_sum:,} 金｜绿蓝紫全清 {non_gold_sum:,} 金")
            if detail_limit and spare_txt:
                parts.append("闲置明细：" + "；".join(spare_txt[:detail_limit])
                             + ("（前 60 件）" if len(spare_txt) > detail_limit else ""))

            inv = con.execute("SELECT stock_id,item_id,quality,value,quantity FROM inventory WHERE user_id=? ORDER BY value DESC", (user_id,)).fetchall()
            bm_uses: dict[str, list[str]] = {}
            for r in data["recipes"]:
                out_n = data["items"][r["output"]]["name"]
                for k in r["requires"]:
                    if k not in ("any_blue", "any_green"):
                        bm_uses.setdefault(k, []).append(out_n)
            inv_lines = []
            crate_cnt = 0
            clear_mat_gold = 0
            stock_by_id: dict[str, int] = {}
            any_blue_cnt = 0
            any_green_cnt = 0
            for r in inv:
                sp = data["items"].get(r["item_id"], {"name": r["item_id"], "kind": "?", "rarity": "white"})
                stock_by_id[r["item_id"]] = stock_by_id.get(r["item_id"], 0) + r["quantity"]
                if sp["kind"] == "crate":
                    crate_cnt += r["quantity"]
                if sp["kind"] == "material":
                    if sp.get("rarity") == "blue":
                        any_blue_cnt += r["quantity"]
                    elif sp.get("rarity") == "green":
                        any_green_cnt += r["quantity"]
                    if r["item_id"] not in bm_uses and sp.get("rarity") in ("white", "green", "blue"):
                        clear_mat_gold += r["value"] * r["quantity"]
                if len(inv_lines) < detail_limit:
                    bm = f"[黑市:{'/'.join(bm_uses[r['item_id']])}]" if r["item_id"] in bm_uses else ""
                    inv_lines.append(f"#{r['stock_id']}{sp['name']}({r['item_id']}/{sp['kind']}/{sp.get('rarity','?')}{bm})×{r['quantity']}@{r['value']}金")
            parts.append(
                f"仓库 {len(inv)} 项｜盲盒共 {crate_cnt} 个｜一键清仓可得 {clear_mat_gold + spare_counts['green']*eq_prices['green']:,} 金"
            )
            if inv_lines:
                parts.append("仓库明细：" + "；".join(inv_lines)
                             + ("（前 60 项）" if len(inv) > len(inv_lines) else ""))
            if not detail_limit and (inv or spare_txt):
                parts.append("按编号或条件查具体物资和闲置装备时，用 query_elevator_inventory_page 筛选。")

            weekly = _weekly_recipes(data)
            bm_lines = []
            for r in weekly:
                out_name = data["items"][r["output"]]["name"]
                can_n = 99
                req_parts = []
                for rk, need in r["requires"].items():
                    if rk == "any_blue":
                        have, rname = any_blue_cnt, "任意蓝料"
                    elif rk == "any_green":
                        have, rname = any_green_cnt, "任意绿料"
                    else:
                        have, rname = stock_by_id.get(rk, 0), data["items"][rk]["name"]
                    can_n = min(can_n, have // need)
                    req_parts.append(f"{rname}{have}/{need}")
                star = "★" if r["featured"] else ""
                bm_lines.append(f"{star}{out_name}({r['output']})←{'+'.join(req_parts)}[可换{can_n}次]")
            parts.append("本周黑市：" + "；".join(bm_lines))

            qrow = con.execute("SELECT payload FROM daily_quests WHERE day=? AND user_id=?", (today, user_id)).fetchone()
            if qrow:
                qs = json.loads(qrow["payload"])
                parts.append("今日悬赏：" + "；".join(f"{'✅' if q['done'] else '⬜'}{q['title']}({q['progress']}/{q['target']})" for q in qs))

            s_prices = [f"{key}({_item_name(data, key)} {price:,}金)" for key, price in data["shop"].items()]
            parts.append(f"商店直购：safebox（扩容 {next_safe}） / " + " / ".join(s_prices))

            if group_id:
                buff = con.execute("SELECT expires_at FROM group_buffs WHERE group_id=? AND expires_at>?", (group_id, now_ts)).fetchone()
                sigs = con.execute("SELECT owner_id,capacity,helpers FROM signals WHERE group_id=? AND expires_at>?", (group_id, now_ts)).fetchall()
                parts.append(f"本群状态：金雨={'生效中' if buff else '无'}｜待支援信号弹={len(sigs)}个")

            try:
                orders = con.execute(
                    "SELECT order_id,summary,steps_json,expires_at FROM ai_orders WHERE user_id=? AND status='pending' AND expires_at>? ORDER BY created_at DESC LIMIT 3",
                    (user_id, now_ts),
                ).fetchall()
                if orders:
                    o_txts = []
                    for o in orders:
                        steps = json.loads(o["steps_json"])
                        o_txts.append(f"{o['order_id']}[剩{max(1,(o['expires_at']-now_ts)//60)}分]：{o['summary']}（{len(steps)} 步）")
                    parts.append("待确认派单：" + "；".join(o_txts))
            except sqlite3.OperationalError:
                pass
            return "\n".join(parts)

        if section == "summary":
            run_note = "当前在大厅空闲"
            if run_row:
                run = json.loads(run_row["payload"])
                mname = data["maps"].get(run["map"], {}).get("name", run["map"])
                run_note = f"正在禁区：{mname} 第 {run['floor']} 层，HP {run['hp']}/{run['stats']['hp']}，盾 {run.get('shield', 0)}，背包 {len(run['bag'])} 件，安全箱 {len(run['safe'])} 件"
            lv = min(25, int(player["extracts"]))
            return (
                f"【{player['nickname']} 的无尽电梯概况（全服共享昵称）】\n"
                f"钱包：{player['gold']:,} 金｜通行证 {ticket_balance}｜安全箱：{player['safebox']} 格\n"
                f"累计下潜：{player['runs']} 局｜成功撤离：{player['extracts']} 次（资深探员永久加成：血+{lv*4} 攻+{lv} 防+{lv//2}）\n"
                f"大金收藏：{len(archives)}/{sum(1 for it in data['items'].values() if it['kind']=='collection')} 件（已成套：{', '.join(completed_sets) or '无'}）\n"
                f"状态：{run_note}"
            )

        if section == "warehouse":
            rows = con.execute(
                "SELECT item_id, quality, value, quantity FROM inventory WHERE user_id=? ORDER BY value DESC, quantity DESC LIMIT 25",
                (user_id,),
            ).fetchall()
            if not rows:
                return f"{player['nickname']} 的仓库目前是空的（钱包 {player['gold']:,} 金）。"
            lines = [f"【{player['nickname']} 的仓库（前 {len(rows)} 项，钱包 {player['gold']:,} 金）】"]
            bm_uses: dict[str, list[str]] = {}
            for r in data["recipes"]:
                out_name = data["items"][r["output"]]["name"]
                for k in r["requires"]:
                    bm_uses.setdefault(k, []).append(out_name)
            for row in rows:
                spec = data["items"].get(row["item_id"], {})
                name = spec.get("name", row["item_id"])
                use_note = f" [黑市可换:{'/'.join(bm_uses[row['item_id']])}]" if row["item_id"] in bm_uses else ""
                lines.append(f"• {name}（{_RARITY_CN.get(spec.get('rarity',''), '')}）×{row['quantity']} · 单价 {row['value']:,} 金{use_note}")
            return "\n".join(lines)

        if section == "equipment":
            loadout = json.loads(player["loadout"] or "{}")
            eq_rows = con.execute("SELECT * FROM equipment WHERE user_id=? ORDER BY equip_id DESC LIMIT 15", (user_id,)).fetchall()
            if not eq_rows:
                return f"{player['nickname']} 目前一件装备都没有（可在商店花 400 金买新手装备箱 / 1,600 金买进阶装备箱 / 5,500 金买尊享装备箱，或进图摸箱）。"
            eq_ids = set(loadout.values())
            lines = [f"【{player['nickname']} 的装备库（共展示 {len(eq_rows)} 件，已穿戴标 ★）】"]
            for r in eq_rows:
                base = data["equipment"].get(r["base_id"], {})
                st = json.loads(r["stats"])
                af = [data["affixes"][k]["name"] for k in json.loads(r["affixes"]) if k in data["affixes"]]
                mark = "★已穿 " if r["equip_id"] in eq_ids else ""
                st_str = " ".join(f"{_STAT_NAMES[k]}{v:+d}" for k, v in st.items())
                af_str = f" 词条:{'、'.join(af)}" if af else ""
                lines.append(f"• {mark}#{r['equip_id']} {_RARITY_CN.get(r['rarity'], r['rarity'])}{base.get('name', r['base_id'])}（{base.get('slot','')}：{st_str}{af_str}）")
            return "\n".join(lines)

        if section == "quests":
            qrow = con.execute("SELECT payload FROM daily_quests WHERE day=? AND user_id=?", (today, user_id)).fetchone()
            if qrow is None:
                return "今天还没刷出悬赏进度（发「悬赏」或下潜一局即可自动激活今日 3 项悬赏）。"
            quests = json.loads(qrow["payload"])
            lines = [f"【{player['nickname']} 的今日悬赏】"]
            for q in quests:
                mark = "已完成" if q["done"] else f"{q['progress']}/{q['target']}"
                rw = f"{q['gold']:,} 金" if q.get("gold") else f"{data['items'][q['item']]['name']} ×{q.get('qty', 1)}"
                lines.append(f"• {q['title']}（{mark}）— 奖励 {rw}")
            return "\n".join(lines)

        if section == "run":
            if not run_row:
                return f"{player['nickname']} 当前不在电梯里（大厅空闲，剩余通行证 {tickets}/{ticket_cap} 张）。"
            run = json.loads(run_row["payload"])
            return "\n".join([f"【{player['nickname']} 当前局内状态】",
                              *_run_observations(data, run, player["safebox"] + run.get("safe_extra", 0))])
        return "未知查询分段。"
    finally:
        con.close()


def _get_live_elevator_game():
    from nonebot import get_plugin
    plugin = get_plugin("endless_elevator")
    if plugin and hasattr(plugin.module, "game"):
        return plugin.module.game
    from plugins.endless_elevator.game import ElevatorGame
    return ElevatorGame(_DB_PATH)


def lookup_inventory_page(user_id: str, area: str, rarity: str, query: str, page: int) -> str:
    if area not in {"stock", "spare_equipment"} or type(page) is not int or page < 1:
        return "请选择仓库物资或闲置装备，页码从 1 开始。"
    if not _DB_PATH.exists():
        return "目前还没有无尽电梯的存档数据。"
    data = _load_content()
    needle = query.strip().casefold()[:80]
    exact_id = needle.startswith("#") and needle[1:].isdigit()
    listing: list[tuple[str, int]] = []
    con = sqlite3.connect(_DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        if area == "stock":
            title = "仓库物资"
            recipe_materials = {key for recipe in data["recipes"] for key in recipe["requires"]}
            rows = con.execute(
                "SELECT stock_id,item_id,quality,value,quantity FROM inventory WHERE user_id=? "
                "ORDER BY value DESC,stock_id", (user_id,),
            )
            for row in rows:
                key = row["item_id"]
                spec = data["items"].get(key, {})
                grade = spec.get("rarity", "white")
                if rarity not in ("all", grade):
                    continue
                name = spec.get("name", key)
                metadata = [key, spec.get("kind", "未知"), _RARITY_CN.get(grade, grade)]
                if key in recipe_materials:
                    metadata.append("黑市原料")
                if row["quality"]:
                    metadata.append(row["quality"])
                details = (f"#{row['stock_id']} {name} {'/'.join(metadata)} "
                           f"{row['quantity']}×{row['value']:,}金")
                matches = needle == f"#{row['stock_id']}" if exact_id else needle in details.casefold()
                if matches:
                    listing.append((details, row["value"] * row["quantity"]))
        else:
            title = "闲置装备"
            player = con.execute("SELECT loadout FROM players WHERE user_id=?", (user_id,)).fetchone()
            worn = set(json.loads(player["loadout"] or "{}").values()) if player else set()
            prices = data["expedition"]["gear_sell"]
            rows = con.execute(
                "SELECT equip_id,base_id,rarity,stats,affixes FROM equipment WHERE user_id=? ORDER BY equip_id",
                (user_id,),
            )
            for row in rows:
                grade = row["rarity"]
                if row["equip_id"] in worn or rarity not in ("all", grade):
                    continue
                key = row["base_id"]
                spec = data["equipment"].get(key, {})
                affixes = "、".join(data["affixes"].get(tag, {}).get("name", tag)
                                    for tag in json.loads(row["affixes"])) or "无"
                stats = " ".join(f"{_STAT_NAMES.get(name, name)}{amount:+g}"
                                 for name, amount in json.loads(row["stats"]).items()) or "无"
                price = prices.get(grade, 260)
                details = (f"#{row['equip_id']} {spec.get('name', key)}（{key}｜"
                           f"{spec.get('slot', '未知')}｜{_RARITY_CN.get(grade, grade)}）"
                           f"｜{stats}｜词条 {affixes}｜回收 {price:,} 金")
                matches = needle == f"#{row['equip_id']}" if exact_id else needle in details.casefold()
                if matches:
                    listing.append((details, price))
    finally:
        con.close()
    if not listing:
        return f"{title}里没有符合本次筛选的记录。"
    start = (page - 1) * _INVENTORY_PAGE_SIZE
    pages = (len(listing) + _INVENTORY_PAGE_SIZE - 1) // _INVENTORY_PAGE_SIZE
    if page > pages:
        return f"{title}本次筛选共 {pages} 页，没有第 {page} 页。"
    visible = listing[start:start + _INVENTORY_PAGE_SIZE]
    total = sum(value for _, value in listing)
    return "\n".join([f"【{title} 第 {page}/{pages} 页】匹配 {len(listing)} 项，合计 {total:,} 金。",
                      *(details for details, _ in visible)])


def _parse_order_steps(raw_steps: list[str]) -> list[dict]:
    parsed: list[dict] = []
    for raw in raw_steps:
        s = str(raw).strip()
        if not s:
            continue
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict) and obj.get("action"):
                    parsed.append({
                        "action": str(obj.get("action", "")).strip(),
                        "argument": str(obj.get("argument", "")).strip(),
                        "note": str(obj.get("note", "")).strip(),
                    })
                    continue
            except Exception:
                pass
        parts = s.split("|", 2)
        act = parts[0].strip()
        arg = parts[1].strip() if len(parts) > 1 else ""
        note = parts[2].strip() if len(parts) > 2 else f"{act} {arg}".strip()
        parsed.append({"action": act, "argument": arg, "note": note})
    return parsed


class ElevatorToolOutput:
    def __init__(self) -> None:
        self.receipts: list[str] = []
        self.latest_lookup = ""

    def record_lookup(self, result: str) -> str:
        if result:
            self.latest_lookup = llm.clip_text(result, 3450)
        return result

    def card(self) -> str:
        return "\n\n".join(dict.fromkeys(self.receipts))


_ORDER_CONFIRMATION = re.compile(r"(?:确认(?:派单|执行)?|执行(?:派单)?|同意(?:执行|派单)?|通过)\s*(E[A-Z0-9]{4})", re.I)
_CONFIRM_PENDING_ORDER = frozenset({"确认派单", "执行派单", "同意派单"})
_ORDER_CANCELLATION = re.compile(r"(?:取消(?:派单|单号)?|作废)\s*(E[A-Z0-9]{4})", re.I)


def elevator_tools(
    user_id: str,
    lookup_notes: list[str] | None = None,
    *,
    group_id: str = "",
    nickname: str = "群友",
    user_text: str = "",
    output: ElevatorToolOutput | None = None,
) -> dict[str, llm.ChatTool]:
    created_this_turn: set[str] = set()
    output = output if output is not None else ElevatorToolOutput()
    order_text = user_text.strip().rstrip("。！!").strip()

    async def _run_guide(args: dict) -> str:
        topic = args["topic"]
        query = args["query"].strip()[:80]
        result = await asyncio.to_thread(lookup_guide, topic, query)
        if lookup_notes is not None and result:
            compact = " ".join(result.split())[:320]
            lookup_notes.append(f"查无尽电梯资料({topic}:{query}) → {compact}")
        return output.record_lookup(result)

    async def _run_status(args: dict) -> str:
        section = args["section"]
        result = await asyncio.to_thread(lookup_player_status, user_id, section, group_id)
        if lookup_notes is not None and result:
            compact = " ".join(result.split())[:260]
            lookup_notes.append(f"查无尽电梯存档({section}) → {compact}")
        return output.record_lookup(result)

    async def _run_inventory_page(args: dict) -> str:
        result = await asyncio.to_thread(lookup_inventory_page, user_id, args["area"], args["rarity"],
                                         args["query"], args["page"])
        return output.record_lookup(result)

    async def _run_collection_cleanup(args: dict) -> str:
        if args["create_order"] and created_this_turn:
            return "本轮已有待确认派单。用户可先确认或取消，程序会附上单号。"

        def prepare():
            game = _get_live_elevator_game()
            return game.plan_collection_cleanup(user_id, nickname, group_id or "private",
                                                create_order=args["create_order"])

        try:
            plan = await asyncio.to_thread(prepare)
        except ValueError as exc:
            return output.record_lookup(str(exc))
        if not plan["sale_count"]:
            return output.record_lookup("已核对整个仓库，没有符合规则的重复金红藏品，无需生成派单。")
        lines = [
            "已核对整个仓库。每种同名的金、红藏品各保留单价最高的一件。",
            f"预计出售 {plan['sale_count']:,} 件，获得 {plan['gold']:,} 金。",
        ]
        for item in plan["collections"]:
            if item["sale_count"]:
                rarity = _RARITY_CN.get(item["rarity"], item["rarity"])
                lines.append(f"• {rarity} {item['name']}｜留单价 {item['keep_value']:,} 金的一件，"
                             f"卖 {item['sale_count']} 件，得 {item['gold']:,} 金")
        if order_id := plan.get("order_id"):
            created_this_turn.add(order_id)
            output.receipts.append(f"派单 {order_id} · 同名金红藏品各留最贵一件\n"
                                   f"预计卖 {plan['sale_count']:,} 件，得 {plan['gold']:,} 金。\n"
                                   f"10 分钟内回复「确认 {order_id}」或「取消 {order_id}」")
            lines.append(f"待确认派单 {order_id}（10 分钟内有效）。回复「确认 {order_id}」后出售，"
                         f"回复「取消 {order_id}」可作废。")
            if lookup_notes is not None:
                lookup_notes.append(f"生成金红藏品去重派单 {order_id}，预计出售 {plan['sale_count']} 件，待本人确认")
        else:
            lines.append("以上为预计结果，未生成派单。")
        return output.record_lookup("\n".join(lines))

    async def _run_create_order(args: dict) -> str:
        if created_this_turn:
            return "本轮已有待确认派单。用户可先确认或取消，程序会附上单号。"

        summary = args["summary"].strip()
        raw_steps = args["steps"]
        steps = _parse_order_steps(raw_steps)
        _IDLE_ONLY = {"sell", "buy", "craft", "decode", "wear", "autoequip", "reroll", "start", "simulate"}
        _RUN_ONLY = {"down", "rush", "fight", "flee", "extract", "save_box", "swap_box", "take_box", "use", "field_use", "field_pick", "event", "rob", "rescue", "leave", "flare"}
        def _create():
            g = _get_live_elevator_game()
            in_run = g.has_run(user_id)
            for idx, st in enumerate(steps, 1):
                act = st["action"]
                if act in _IDLE_ONLY and in_run:
                    raise ValueError(f"第 {idx} 步 {act} 需要在大厅执行，但玩家当前在局内；请先安排 extract 撤离或提醒玩家。")
                if act in _RUN_ONLY and not in_run:
                    raise ValueError(f"第 {idx} 步 {act} 需要在局内执行，但玩家当前在大厅；请先安排 start 或 simulate 进图。")
                if act in ("start", "simulate"):
                    in_run = True
                elif act == "extract":
                    in_run = False
            return g.create_order(user_id, nickname, group_id or "private", summary, steps)
        try:
            order = await asyncio.to_thread(_create)
        except Exception as exc:
            return output.record_lookup(f"派单校验未通过：{exc}")
        oid = order["order_id"]
        created_this_turn.add(oid)
        steps = order["steps"]
        brief = [llm.clip_text(step["note"], 220) for step in steps[:4]]
        if len(steps) > len(brief):
            brief.append(f"另有 {len(steps) - len(brief)} 步")
        output.receipts.append("\n".join([
            f"派单 {oid} · {order['summary']}（{len(steps)} 步）",
            " → ".join(brief),
            f"10 分钟内回复「确认 {oid}」或「取消 {oid}」",
        ]))
        lines = [
            f"【已生成正式派单，等待用户确认】",
            f"唯一派单号：{oid}（10 分钟内有效，仅限本人确认）",
            f"任务摘要：{order['summary']}",
            "逐条操作清单：",
        ]
        for idx, st in enumerate(order["steps"], 1):
            lines.append(f"{idx}. {st['note']}（接口：{st['action']} {st['argument']}）")
        preview = "逐条说明操作" if len(order["steps"]) <= 8 else "可以按条件合并相同操作，说明处理范围、件数和预计金币"
        lines.append(f"回复可{preview}。程序会附上单号和确认方式。请保持等待用户确认。")
        out = "\n".join(lines)
        if lookup_notes is not None:
            lookup_notes.append(f"生成无尽电梯派单 {oid}({order['summary']}，共{len(order['steps'])}步，待确认)")
        return out

    async def _run_confirm_order(args: dict) -> str:
        confirmation = _ORDER_CONFIRMATION.fullmatch(order_text)
        if not confirmation and order_text not in _CONFIRM_PENDING_ORDER:
            return output.record_lookup("请让乘客本人回复「确认 单号」执行。")
        oid = confirmation[1].upper() if confirmation else ""
        if oid and oid != args["order_id"].strip().upper():
            return output.record_lookup("工具单号与乘客确认的单号不同，请使用乘客确认的单号。")
        if oid in created_this_turn or (not oid and created_this_turn):
            return output.record_lookup("这张派单刚刚生成。乘客本人下一条消息确认后才能执行。")
        def _confirm():
            g = _get_live_elevator_game()
            return g.execute(user_id, nickname, group_id or "private", "confirm_order", oid)
        rep = await asyncio.to_thread(_confirm)
        output.receipts.append(rep.text)
        if lookup_notes is not None:
            lookup_notes.append(f"执行无尽电梯派单 {oid} → {' '.join(rep.text.splitlines()[:3])[:220]}")
        return rep.text

    async def _run_cancel_order(args: dict) -> str:
        oid = args["order_id"].strip().upper()
        cancellation = _ORDER_CANCELLATION.fullmatch(order_text)
        if not cancellation or cancellation[1].upper() != oid:
            return output.record_lookup("请让乘客本人回复「取消 单号」作废。")
        def _cancel():
            g = _get_live_elevator_game()
            return g.execute(user_id, nickname, group_id or "private", "cancel_order", oid)
        rep = await asyncio.to_thread(_cancel)
        output.receipts.append(rep.text)
        if lookup_notes is not None:
            lookup_notes.append(f"取消无尽电梯派单 {oid} → {rep.text[:140]}")
        return rep.text

    return {
        "query_elevator_guide": llm.ChatTool(
            "静默查询本群《无尽电梯：群友搜打撤》的最新资料，包括网页版网址、注册、QQ 绑定、登录和密码设置，玩法攻略、商店售价、黑市配方和特惠、六张地图及掉落、道具、首领、四槽装备、虹彩背包技能、地图临时道具、词条、藏品套装、奇遇、每日通行证与悬赏、出金出红和盲盒概率、战斗公式与指令。支持按关键词查物品及用途。"
            "问某套哪个部位叫什么或某件属于哪套，用 equipment 或 sets，query 带上套装名和部位或单件名，按资料中的实际名称回答。"
            "推荐临装时，查询玩家的槽位和生效中的增益，合计正负效果，再按共有的属性上下限计算。",
            {
                "topic": {
                    "type": "string",
                    "enum": [
                        "overview", "shop", "black_market", "maps", "items",
                        "equipment", "affixes", "sets", "events", "quests", "commands", "formulas", "rates", "api", "search", "boss", "field", "atlas", "web",
                        "web_play", "safe", "exploration", "combat", "support", "loot", "ranks",
                    ],
                    "description": "field 查地图限定道具分布、效果与使用；web 查网址、绑定与登录；web_play 查网页操作；"
                                   "safe 查安全箱；exploration 查深入、伏击、撤离和遗物；combat 查回合、伤害和逃跑；"
                                   "support 查网页群播报和求援；loot 查掉落、估值与盲盒；ranks 查各榜总览与称号；"
                                   "formulas 查命中、闪避、暴击、基础属性来源以及战斗/掉落/经济公式；api 查派单接口。",
                },
                "query": {
                    "type": "string",
                    "description": "物品、地图、套装名和部位（如「潮汐引航 挂件」），或单件名称。查整个分类时填分类中文名。",
                },
            },
            _run_guide,
            silent=True,
            blank_fields=frozenset({"query"}),
        ),
        "query_my_elevator_status": llm.ChatTool(
            "静默查看当前发言人的真实游戏存档。按问题选择最小范围；派单或计算优先用 plan 查完整汇总。"
            "需要具体物品编号或带条件找物品时使用分页筛选。",
            {
                "section": {
                    "type": "string",
                    "enum": ["plan", "all", "group", "summary", "warehouse", "equipment", "quests", "run", "atlas"],
                    "description": "plan 查钱包、票、安全箱、仓库和装备汇总、当前遭遇、黑市、商店、悬赏和待确认派单；"
                                   "all 另外附库存和装备样本、图鉴详情；summary 查简要进度；"
                                   "group 查本群金雨、可支援信号、在局玩家、本群及全服当月各榜前三；"
                                   "run 查当前敌人、实际奇遇选项、箱格、两槽临装、待选临装和本层结算；还可查 summary/warehouse/equipment/quests/atlas。",
                },
            },
            _run_status,
            silent=True,
        ),
        "create_elevator_order": llm.ChatTool(
            "为用户明确要求的买卖、补给、装备、探索等操作生成待确认派单。先用 query_my_elevator_status(plan) 或 run 查清必要状态，再用本工具按顺序生成一张派单，确认前不扣资源。",
            {
                "summary": {
                    "type": "string",
                    "description": "简短任务摘要，如「卖闲置蓝绿装+买5瓶冰美式+抽2个进阶箱并一键配装」。",
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "1~60 条按顺序执行的步骤，每条格式为「action|argument|中文说明（含预计金币/效果）」。"
                        "开放全部接口 action："
                        "sell(参数: 清仓/杂物/非黑市蓝/绿装/蓝装/紫装/金装/蓝绿装/闲置装备/装备 #id1 #id2/#stock_id 数量/物品名 全部)、"
                        "buy(参数: 补票/买药/coffee 5/smoke 2/gear_box 5/gear_box_mid 3/gear_box_high 2/safebox/ticket 2 等)、"
                        "decode(参数: 全部/5/蓝 全部)、craft(参数: 配方名 数量)、wear(参数: #equip_id)、autoequip(空)、reroll(参数: #equip_id)、"
                        "rename(参数: 新昵称)、blessing(空)、start(参数: 地图名 | 冰美式 2 烟雾弹 1 或 推荐 | 带药)、"
                        "simulate(参数: 地图名)、down(空)、rush(参数: 1~10，留空使用个人档位)、"
                        "rush_settings(参数可组合: 3/5/10、跳过金红/遇金红暂停、遇奇遇暂停/随机奇遇/无视奇遇、"
                        "遇精英暂停/自动打精英)、fight(空)、flee(空)、extract(空)、"
                        "save_box(空或物品名)、swap_box(空或 背包物品 | 保险箱物品)、take_box(保险箱格位，从 1 开始)、"
                        "use(道具名)、field_pick(楼层:道具ID:槽位，按实时状态选可用槽位或 0 放弃，需用户明确决定)、"
                        "field_use(道具名或ID，旧临装可填道具ID 槽位)、field(参数: 解除 名称，用于卸下当前效果)、"
                        "event(选项序号)、rob(空)、rescue(空)、leave(空)、flare(空)、support(空或目标 QQ)。"
                        "示例：['sell|蓝绿装|卖出闲置绿装×2+蓝装×3（按当前回收价估算）', 'buy|gear_box_mid 2|买2个进阶装备箱（-3,200金）', 'autoequip||一键穿戴最高战力装备']"
                    ),
                },
            },
            _run_create_order,
            silent=True,
        ),
        "query_elevator_inventory_page": llm.ChatTool(
            "静默分页查当前发言人的无尽电梯仓库或闲置装备。每页最多 40 项。"
            "要按条件批量清理而存档总览明细被截断时，用本工具读完必要的筛选页，按确实存在的编号派单。",
            {
                "area": {"type": "string", "enum": ["stock", "spare_equipment"],
                         "description": "stock 查仓库物资，spare_equipment 查未穿戴的装备。"},
                "rarity": {"type": "string", "enum": ["all", "white", "green", "blue", "purple", "gold", "red", "rainbow"],
                           "description": "明确有品质条件时填写对应品质，否则填 all。"},
                "query": {"type": "string", "description": "可填物品名、编号、装备部位或词条关键词。无需关键词时填空字符串。"},
                "page": {"type": "integer", "description": "从 1 开始，按回复中的页数继续读取所需页。"},
            },
            _run_inventory_page,
            silent=True,
            blank_fields=frozenset({"query"}),
        ),
        "plan_elevator_collection_cleanup": llm.ChatTool(
            "核对当前发言人的整仓金红藏品。同名同品质各留单价最高的一件，从高价批次也只留一件，"
            "其余统一估价。无需读仓库总览或逐页扫描。没有额外限制且用户明确要整理或卖出时，生成一张普通待确认派单，"
            "确认前不会动库存。只问清单或估值就只做预览。",
            {"create_order": {"type": "boolean", "description": "明确要求整理并出售时填 true。仅预览时填 false。"}},
            _run_collection_cleanup,
            silent=True,
        ),
        "confirm_elevator_order": llm.ChatTool(
            "执行用户本人在【后续新消息】中明确确认同意的《无尽电梯》派单（传入唯一单号如 E7K3M）。严禁在生成派单的同一轮调用！",
            {
                "order_id": {
                    "type": "string",
                    "description": "用户确认执行的 5 位唯一派单号（如 E7K3M）。",
                },
            },
            _run_confirm_order,
            silent=True,
        ),
        "cancel_elevator_order": llm.ChatTool(
            "取消用户本人的待确认《无尽电梯》派单（传入唯一单号如 E7K3M）。",
            {
                "order_id": {
                    "type": "string",
                    "description": "要取消的 5 位唯一派单号（如 E7K3M）。",
                },
            },
            _run_cancel_order,
            silent=True,
        ),
    }
