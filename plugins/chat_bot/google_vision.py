"""Google Cloud Vision：Web Detection（以图搜图 + 全网溯源）。

用途：
    用户问「这是谁 / 出自哪部 / 什么梗 / 哪来的」这类**需要精准溯源**的问题时，
    把图丢给 Google 的全网索引，拿回：最佳推测、相关实体（带置信度）、
    以及包含这张图的网页（标题 + URL）。结果作为线索交给主模型。

与百度识图的分工（见 vision_router.image_tools）：
    客观提取（读文字、列可见内容）→ 百度
    精准溯源（认人、出处、来源）    → Google Vision
    评价 / 解释 / 引申             → 主模型自己看

凭据：
    复用 Vertex 的服务账号（CHAT_VERTEX_AUTH）和 cloud-platform scope。
    项目需要启用结算和 Cloud Vision API。
    免费额度：每月前 1,000 次（每月 1 日重置）。

请求：
    POST https://vision.googleapis.com/v1/images:annotate
    Authorization: Bearer <GCP access token>
"""
from __future__ import annotations

import base64
import html
import io
import re
import time
from dataclasses import dataclass, field

import httpx

from nonebot import logger

VISION_URL = "https://vision.googleapis.com/v1/images:annotate"
WEB_DETECTION = "WEB_DETECTION"

# 单张图上限：Vision 接受 20MB，但请求体过大经代理容易断。
# 只在 >3MB 或长边 >2048 时才压，而且用 q90 —— 溯源依赖细节，不能压狠。
MAX_BYTES = 3 * 1024 * 1024
MAX_SIDE = 2048
JPEG_QUALITY = 90


@dataclass
class WebResult:
    ok: bool = False
    best_guess: str = ""
    entities: list[tuple[str, float]] = field(default_factory=list)
    pages: list[tuple[str, str]] = field(default_factory=list)
    text: str = ""
    error: str = ""
    elapsed: float = 0.0
    error_reason: str = ""


def _prepare(image_bytes: bytes) -> bytes:
    """只在 >3MB 或长边 >2048 时压缩（q90），其余原样发送。"""
    try:
        from PIL import Image
    except ImportError:
        return image_bytes
    try:
        img = Image.open(io.BytesIO(image_bytes))
        if max(img.size) <= MAX_SIDE and len(image_bytes) <= MAX_BYTES:
            return image_bytes
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if max(img.size) > MAX_SIDE:
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return image_bytes


# Vision 的 webEntities 里混着大量通用类别词（cartoon/anime/…），
# 它们置信度往往最高，却对「这是谁 / 出自哪部」毫无用处，还会把真正的
# 作品名（如 Honkai: Star Rail 0.46）挤出前几名。这里把它们降级展示。
GENERIC_LABELS = {
    "cartoon", "animation", "anime", "manga", "comics", "comic", "illustration",
    "illustrations", "art", "artwork", "drawing", "graphics", "design",
    "clip art", "visual arts", "digital art", "fan art", "fictional character",
    "cute", "poster", "font", "line", "text", "image", "photography",
    "anime art", "cartoon art", "animation studio", "screen", "screen capture",
    "screenshot", "film", "television", "video", "music video", "youtube",
}


def _is_generic(name: str) -> bool:
    return name.strip().lower() in GENERIC_LABELS


def _format(
    best_guess: str,
    entities: list[tuple[str, float]],
    pages: list[tuple[str, str, str]],
    labels: list[str],
    image_matches: list[tuple[str, list[str]]],
) -> str:
    """把结果排成「作品名优先」的线索，通用类别单独放一行。"""
    specific = [(n, s) for n, s in entities if not _is_generic(n)]
    generic = [(n, s) for n, s in entities if _is_generic(n)]

    lines: list[str] = []
    if best_guess and not _is_generic(best_guess):
        lines.append(f"最佳推测：{best_guess}")
    # 主体/出处单独一行，并且放在最前面——这是「这是谁/哪里/什么牌子」的关键
    if specific:
        lines.append(
            "疑似主体/出处："
            + "、".join(f"{n}（{s:.2f}）" for n, s in specific[:8])
        )
    if best_guess and _is_generic(best_guess):
        lines.append(f"最佳推测：{best_guess}（通用类别，只说明画风，不含出处）")
    if generic:
        lines.append(
            "通用标签：" + "、".join(f"{n}（{s:.2f}）" for n, s in generic[:4])
        )
    if labels:
        lines.append("其他候选标签：" + "、".join(labels[:3]))
    if pages:
        lines.append(f"包含匹配图的页面（共 {len(pages)} 条，按匹配类型标注）：")
        lines.extend(f"- [{match}] {title or '无标题'} {url}" for title, url, match in pages)
    for label, urls in image_matches:
        if urls:
            lines.append(f"{label}（共 {len(urls)} 条，以下为图片地址）：")
            lines.extend(f"- {url}" for url in urls[:2])
    # Keep complete URLs within the tool's 8 KiB output budget.
    bounded = []
    remaining = 7600
    for line in lines:
        cost = len(line.encode("utf-8")) + 1
        if cost <= remaining:
            bounded.append(line)
            remaining -= cost
    return "\n".join(bounded)


def _plain_label(value, limit: int = 100) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", str(value or ""))).split())[:limit]


def _match_urls(items: list, limit: int) -> list[str]:
    urls = (str(item.get("url") or "").strip() for item in items if isinstance(item, dict))
    return list(dict.fromkeys(url for url in urls if url.startswith(("https://", "http://")) and len(url) <= 1000))[:limit]


def _web_result(web: dict, max_results: int, elapsed: float) -> WebResult:
    labels = list(dict.fromkeys(label for item in web.get("bestGuessLabels") or []
                               if (label := _plain_label(item.get("label")))))
    best = labels[0] if labels else ""
    entities: list[tuple[str, float]] = []
    for item in web.get("webEntities") or []:
        name = _plain_label(item.get("description"))
        if not name:
            continue
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        entities.append((name, score))
    pages: dict[str, tuple[str, int]] = {}
    for page in web.get("pagesWithMatchingImages") or []:
        urls = _match_urls([page], 1)
        if not urls:
            continue
        match = 2 if page.get("fullMatchingImages") else 1 if page.get("partialMatchingImages") else 0
        title, previous_match = pages.get(urls[0], ("", 0))
        pages[urls[0]] = (title or _plain_label(page.get("pageTitle"), 150), max(match, previous_match))
    kinds = ("含匹配图", "部分匹配", "完全匹配")
    ranked = sorted(pages.items(), key=lambda page: page[1][1], reverse=True)[:max_results]
    matches = [(title, url, kinds[match]) for url, (title, match) in ranked]
    image_matches = [
        (label, _match_urls(web.get(key) or [], max_results))
        for key, label in (("fullMatchingImages", "完全匹配图"), ("partialMatchingImages", "部分匹配图"),
                           ("visuallySimilarImages", "视觉相似图"))
    ]
    if not (best or entities or matches or any(urls for _, urls in image_matches)):
        return WebResult(error="没有检索到可用结果（图可能太小/太常见/未被索引）", elapsed=elapsed)
    return WebResult(
        ok=True, best_guess=best, entities=entities, pages=[(title, url) for title, url, _ in matches],
        text=_format(best, entities, matches, labels[1:], image_matches), elapsed=elapsed,
    )


async def web_detect(
    image_bytes: bytes,
    auth_file: str,
    proxy: str = "",
    max_results: int = 10,
    timeout: float = 25.0,
    monthly_cap: int = 1000,
    quota_path=None,
) -> WebResult:
    """以图搜图。失败返回 ok=False，绝不抛异常。

    monthly_cap：本地硬上限（默认 1000 = 免费额度）。到上限直接不联网，
    保证永远不会产生超出免费层级的费用。
    """
    started = time.time()
    if not image_bytes:
        return WebResult(ok=False, error="空图片")
    if not auth_file:
        return WebResult(ok=False, error="未配置 CHAT_VERTEX_AUTH")

    from . import vision_quota

    allowed, quota = vision_quota.consume(
        "google_vision", monthly_cap, quota_path
    )
    if not allowed:
        logger.warning(
            f"google_vision: 本月免费额度已用完（{quota['used']}/{quota['cap']}，"
            f"{quota['month']}），跳过以图搜源"
        )
        return WebResult(
            ok=False,
            error=(
                f"本月免费额度已用完（{quota['used']}/{quota['cap']}，"
                f"{quota['month']}），未调用 Google Vision"
            ),
            elapsed=time.time() - started,
        )

    # 复用 Vertex 的 token（同一个服务账号、同一个 scope）
    from . import vertex

    try:
        token = await vertex._get_token(auth_file, proxy)
    except Exception as exc:  # noqa: BLE001
        return WebResult(
            ok=False,
            error=f"取 GCP token 失败：{type(exc).__name__}: {exc}",
            elapsed=time.time() - started,
        )

    payload = {
        "requests": [
            {
                "image": {"content": base64.b64encode(_prepare(image_bytes)).decode("ascii")},
                "features": [{"type": WEB_DETECTION, "maxResults": int(max_results)}],
            }
        ]
    }
    kwargs: dict = {"timeout": httpx.Timeout(timeout)}
    if proxy:
        kwargs["proxy"] = proxy

    try:
        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.post(
                VISION_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except Exception as exc:  # noqa: BLE001
        return WebResult(
            ok=False,
            error=f"请求失败：{type(exc).__name__}: {exc}",
            elapsed=time.time() - started,
        )

    try:
        data = resp.json()
    except ValueError:
        return WebResult(
            ok=False,
            error=f"返回非 JSON（HTTP {resp.status_code}）",
            elapsed=time.time() - started,
        )

    if resp.status_code != 200:
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        status = str(err.get("status") or "")
        msg = str(err.get("message") or "")[:200]
        reason = next(
            (info["reason"] for info in err.get("details") or []
             if isinstance(info, dict) and info.get("domain") == "googleapis.com"
             and isinstance(info.get("reason"), str)),
            "",
        )
        if reason == "BILLING_DISABLED":
            msg = "Google Cloud 项目未启用结算"
        elif reason == "SERVICE_DISABLED" or (status == "PERMISSION_DENIED" and "has not been used" in msg):
            msg = "Cloud Vision API 尚未启用：去 GCP 控制台点「启用」"
        elif status == "PERMISSION_DENIED":
            msg = f"没有权限：{msg}"
        return WebResult(
            ok=False,
            error=f"HTTP {resp.status_code} {msg}",
            elapsed=time.time() - started,
            error_reason=reason,
        )

    responses = data.get("responses") or [{}]
    web = responses[0].get("webDetection") or {}
    return _web_result(web, max_results, time.time() - started)
