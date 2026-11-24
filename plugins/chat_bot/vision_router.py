"""Google 图像溯源和百度客观识图工具，由主模型选择。"""
from __future__ import annotations

import re

from nonebot import logger

from . import baidu_vision, google_vision, llm, vision_quota


def baidu_ready(cfg: dict) -> bool:
    return bool((cfg.get("ak") and cfg.get("sk")) or cfg.get("qianfan_key"))


def baidu_status_text(cfg: dict) -> str:
    mode = "仅客观提取（文字/可见内容）"
    if cfg.get("ak") and cfg.get("sk"):
        return f"✅ AIP AK/SK，{mode}" + (" + 文字识别" if cfg.get("ocr") else "")
    if cfg.get("qianfan_key"):
        return f"✅ 千帆 {cfg.get('qianfan_model')}，{mode}"
    return "❌ 未配置（.env 填 CHAT_BAIDU_AK/SK，或用 CHAT_BAIDU_SEARCH_KEY）"


def vision_web_status_text(
    *, enabled: bool, vertex_auth: str, cap: int, reserve: int = 50, free_tier: int = 1000
) -> str:
    if not (enabled and vertex_auth):
        return "❌ 未启用（需要 CHAT_VERTEX_AUTH）"
    q = vision_quota.peek("google_vision", cap)
    shown = q["used"] + reserve
    return f"✅ Google Vision 本月已用 {shown}/{free_tier} 次（含预留 {reserve} 次，到顶自动停）"


def image_tools(
    images: list[bytes], *,
    baidu_cfg: dict,
    vision_web_enabled: bool,
    vertex_auth: str,
    vertex_proxy: str,
    vision_web_max: int,
    vision_web_cap: int,
) -> dict[str, llm.ChatTool]:
    if not images:
        return {}
    tools = {}
    index = {"type": "integer", "enum": list(range(1, len(images) + 1)), "description": "本轮附件编号，从 1 开始。"}

    async def trace(args: dict) -> str:
        result = await google_vision.web_detect(
            images[args["image_index"] - 1], vertex_auth, vertex_proxy,
            max_results=vision_web_max, monthly_cap=vision_web_cap,
        )
        if result.ok:
            return (
                "Google Vision 图像匹配线索。优先引用可核实的原作者或官方出处，"
                "结合完全匹配、部分匹配和页面标题判断。只有相似图时标明推测。"
                "实体标签和网页标题可以用来查证角色或作品的官方中文名。\n" + result.text
            )
        if result.error.startswith("没有检索到可用结果"):
            return "Google Vision 已检索，这次没找到可用的出处线索。"
        if result.error.startswith("本月免费额度已用完"):
            return "Google Vision 已到本月用量上限，这次没有发出请求。"
        http_error = re.match(r"HTTP \d{3}", result.error)
        detail = http_error.group() if http_error else result.error.split("：", 1)[0][:60]
        logger.warning(f"chat_bot: Google 图像溯源失败 {detail} {result.error_reason}".rstrip())
        if result.error_reason == "BILLING_DISABLED":
            return "Google Cloud 项目未启用结算，Google Vision 这次无法以图搜源。"
        return "Google Vision 请求失败，这次没有拿到结果。"

    async def read(args: dict) -> str:
        clue = await baidu_vision.describe(
            images[args["image_index"] - 1], baidu_cfg["ak"], baidu_cfg["sk"],
            want_ocr=baidu_cfg["ocr"], timeout=12,
            qianfan_key=baidu_cfg["qianfan_key"], qianfan_model=baidu_cfg["qianfan_model"],
            mode=args["mode"],
        )
        return clue.text if clue.ok else "这次没有提取出可用的图中文字或物体信息。"

    if vision_web_enabled and vertex_auth:
        tools["trace_image"] = llm.ChatTool(
            "按图像匹配查找原图、画师、图片出处，或借助匹配确认作品和角色。"
            "核实截图里的事件、报道和文字原帖时，提取文字线索交给网页搜索。",
            {"image_index": index},
            trace,
        )
    if baidu_cfg.get("clue") and baidu_ready(baidu_cfg):
        tools["inspect_image"] = llm.ChatTool(
            "补充提取图中文字或可见物体。读不清截图文字，或需要精确转写时选 ocr。"
            "需要辅助识别可见物体时选 objects。提取的文字可用来回答转写问题或继续搜索公开资料。",
            {"image_index": index, "mode": {"type": "string", "enum": ["ocr", "objects"]}},
            read,
        )
    return tools
