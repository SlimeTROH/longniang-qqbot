"""随机决策助手 —— 学习用插件。

提供三个命令：
    /roll 2d6      掷骰子（默认 1d6）
    /pick 火锅 烧烤  /pick 火锅,烧烤   随机选一个
    /coin          抛硬币

这个文件覆盖了写 NoneBot 插件最常用的语法，注释里标了对应官方文档章节。
"""
# ① import：把别人写好的功能拿进来用
import random          # Python 自带的随机数库
import re              # 正则表达式库，用来解析 "/roll 2d6" 这种输入

from nonebot import on_command                       # 创建「命令响应器」
from nonebot.adapters.onebot.v11 import Message, MessageEvent   # 消息类型
from nonebot.params import CommandArg                # 自动提取命令后面的参数


# ============================================================
# ② 掷骰子：/roll 或 /roll 2d6
# ============================================================
# on_command(...) 创建一个响应器，返回值赋给 roll 这个变量
#   "roll"          主命令名（用户发 /roll）
#   aliases={"骰子"} 别名，发 /骰子 也一样触发
#   priority=5      数字越小越先匹配
#   block=True      命中后不再传给其它响应器
roll = on_command("roll", aliases={"骰子"}, priority=5, block=True)


# ③ @roll.handle() 是「装饰器」：把下面这个函数注册成「命中 /roll 时要执行的代码」
# ④ async def 表示异步函数；参数由 NoneBot 自动注入（这叫「依赖注入」）
@roll.handle()
async def handle_roll(arg: Message = CommandArg()):
    # ⑤ 取出命令后面那段文字，去掉首尾空格，转小写
    text = arg.extract_plain_text().strip().lower()

    # ⑥ 默认值：没写参数就是 1 颗 6 面骰
    count, sides = 1, 6

    if text:                                    # ⑦ if：用户确实写了参数
        # ⑧ 正则匹配 "2d6" 这种格式：(\d*) 是可选数字，(\d+) 是必有数字
        matched = re.fullmatch(r"(\d*)d(\d+)", text)
        if matched is None:                     # ⑨ 格式不对
            await roll.finish("试试「骰子 2d6」，掷两颗六面骰。")
        count = int(matched.group(1) or 1)      # 前面没写就默认 1
        sides = int(matched.group(2))

    # ⑩ 范围校验：防止有人发 /roll 9999d9999 把机器人搞卡
    if not (1 <= count <= 20 and 2 <= sides <= 1000):
        await roll.finish("一次可掷 1–20 颗骰子，每颗 2–1000 面。")

    # ⑪ 列表推导式：一次性生成 count 个随机数
    results = [random.randint(1, sides) for _ in range(count)]
    total = sum(results)                        # 求和

    # ⑫ if/else 分支返回不同文案
    if count == 1:
        await roll.finish(f"掷出 {total} 点。")

    detail = " + ".join(str(x) for x in results)   # 把 [3,5] 变成 "3 + 5"
    await roll.finish(f"{count}d{sides} 掷出 {detail}，合计 {total} 点。")


# ============================================================
# ③ 随机选一个：/pick 火锅 烧烤 日料
# ============================================================
pick = on_command("pick", aliases={"选择", "选一个"}, force_whitespace=True, priority=5, block=True)


@pick.handle()
async def handle_pick(arg: Message = CommandArg()):
    raw = arg.extract_plain_text()

    # ⑬ 用正则按「空格 / 逗号 / 中文逗号 / 顿号」切分，并过滤掉空字符串
    options = [x for x in re.split(r"[\s,，、]+", raw) if x]

    if len(options) < 2:
        await pick.finish("给我两个或更多选项，比如「选择 火锅 烧烤」。")
    if len(options) > 20:
        await pick.finish("一次最多选 20 项，请去掉几个。")

    # ⑭ random.choice 从列表里随机取一个
    chosen = random.choice(options)
    await pick.finish(f"我选「{chosen}」。")


# ============================================================
# ④ 抛硬币：/coin
# ============================================================
coin = on_command("coin", aliases={"抛硬币"}, force_whitespace=True, priority=5, block=True)


# ⑮ 这个命令不需要参数，所以函数可以不写参数（但括号和冒号不能少）
@coin.handle()
async def handle_coin():
    result = random.choice(["正面", "反面"])
    await coin.finish(f"抛到了{result}。")
