"""OpenAI image generation tool, quota reservation, and asynchronous delivery."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from network_config import proxy_for_url
from nonebot import logger

from .image_quota import UNAVAILABLE_TEXT, Balance, ImageQuota
from .image_settings import ImageSettings, QUALITY_LEVELS
from .llm import ChatTool

FLARE_MODEL = "gpt-image-2.6-flare"
SUNBURST_MODEL = "gpt-image-2.6-sunburst"
MODEL_LABELS = {FLARE_MODEL: "Flare", SUNBURST_MODEL: "Sunburst"}
URL = "https://api.openai.com/v1/images/generations"
EDIT_URL = "https://api.openai.com/v1/images/edits"
MAX_REFERENCE_BYTES = 50 * 1024 * 1024
REFERENCE_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}
_tasks: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Delivery:
    text: str
    image: bytes = b""
    started: bool = False


@dataclass(frozen=True)
class Generation:
    image: bytes
    output_tokens: int | None = None
    error: str = ""


def _output_tokens(payload: dict) -> int | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("output_tokens_details") or {}
    # This endpoint renders a single raster image. Older responses report its count in output_tokens.
    count = details.get("image_tokens", usage.get("output_tokens")) if isinstance(details, dict) else None
    return count if type(count) is int and count >= 0 else None


def _reference_files(images: tuple[bytes, ...]) -> list[tuple[str, tuple[str, bytes, str]]]:
    from PIL import Image

    field = "image" if len(images) == 1 else "image[]"
    files = []
    for index, data in enumerate(images, 1):
        if len(data) >= MAX_REFERENCE_BYTES:
            raise ValueError("单张参考图需小于 50 MB。")
        try:
            with Image.open(io.BytesIO(data)) as source:
                file_type = REFERENCE_FORMATS.get(source.format)
                if file_type is None or getattr(source, "is_animated", False):
                    output = io.BytesIO()
                    source.convert("RGBA").save(output, "PNG")
                    data = output.getvalue()
                    file_type = REFERENCE_FORMATS["PNG"]
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValueError("参考图暂时无法读取，请重新发送图片。") from exc
        if len(data) >= MAX_REFERENCE_BYTES:
            raise ValueError("单张参考图需小于 50 MB。")
        extension, mime = file_type
        files.append((field, (f"reference-{index}.{extension}", data, mime)))
    return files


async def generate(api_key: str, user_id: str, prompt: str, settings: ImageSettings, *,
                   model: str = FLARE_MODEL, references: tuple[bytes, ...] = ()) -> Generation:
    payload = {
        "model": model,
        "prompt": prompt,
        "size": settings.size,
        "quality": settings.quality,
        "n": 1,
        "output_format": settings.output_format,
        "safety_identifier": hashlib.sha256(f"qqbot:{user_id}".encode()).hexdigest(),
    }
    if settings.animated:
        payload.update(frame_count=settings.frame_count, frame_duration_ms=settings.frame_duration_ms)

    url = URL
    request = {"json": payload}
    if references:
        try:
            files = await asyncio.to_thread(_reference_files, references)
        except ValueError as exc:
            return Generation(b"", 0, str(exc))
        except ImportError:
            return Generation(b"", 0, "暂时无法读取参考图。")
        url = EDIT_URL
        request = {"data": payload, "files": files}
    try:
        async with httpx.AsyncClient(
            proxy=proxy_for_url(url), trust_env=False, timeout=httpx.Timeout(180.0, connect=8.0),
        ) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                **request,
            )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        logger.warning(f"image_generation: {type(exc).__name__}")
        return Generation(b"", 0, "生图接口暂时连不上，本次额度已退回。")
    except httpx.HTTPError as exc:
        logger.warning(f"image_generation: {type(exc).__name__}")
        return Generation(b"", None, "生图请求中断，用量待确认。")

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    tokens = _output_tokens(payload)
    if response.status_code != 200:
        logger.warning(f"image_generation: HTTP {response.status_code} model={model}")
        if response.status_code in (400, 401, 403, 404, 413, 422, 429) and tokens is None:
            tokens = 0
        return Generation(b"", tokens, f"生图失败（HTTP {response.status_code}）。")

    entries = payload.get("data") or []
    encoded = entries[0].get("b64_json") if isinstance(entries, list) and entries and isinstance(entries[0], dict) else None
    try:
        image = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
    except binascii.Error:
        image = b""
    if not image:
        return Generation(b"", tokens, "生图接口没有返回可发送的图片。")
    if settings.animated and not image.startswith((b"GIF87a", b"GIF89a")):
        return Generation(b"", tokens, "生图接口没有返回可发送的 GIF。")
    return Generation(image, tokens)


def balance_line(balance: Balance) -> str:
    return f"{balance.day} 图像配额剩余 {balance.remaining:,} / {balance.limit:,} token"


async def _generate_and_deliver(
    ledger: ImageQuota, request_id: str, user_id: str, api_key: str, prompt: str, settings: ImageSettings,
    requested: ImageSettings, get_limit: Callable[[], int], notify: Callable[[Delivery], Awaitable[None]],
    *, original_size: tuple[int, int], model: str, references: tuple[bytes, ...],
) -> None:
    try:
        outcome = await generate(api_key, user_id, prompt, settings, model=model, references=references)
        if outcome.output_tokens is not None:
            ledger.settle(request_id, outcome.output_tokens, cancelled=not outcome.image and outcome.output_tokens == 0)
        balance = ledger.balance(user_id, get_limit())
        if outcome.image:
            used = (f"本次输出 {outcome.output_tokens:,} token" if outcome.output_tokens is not None
                    else f"本次用量待确认，已预留 {settings.reservation_tokens:,} token")
            lines = [f"已画好 · {MODEL_LABELS[model]} · {settings.label}", used, balance_line(balance)]
            if settings != requested:
                changes = []
                if settings.quality != requested.quality:
                    changes.append("降低画质")
                if settings.size != requested.size:
                    changes.append("缩小尺寸")
                lines.insert(1, f"原设置 {requested.label} 会超出配额，已{'并'.join(changes)}。")
            if original_size != (requested.width, requested.height):
                lines.insert(1, f"为保留 {requested.frame_count} 帧，按动图尺寸限制缩小了画面。")
        else:
            lines = [outcome.error, balance_line(balance)]
            if outcome.output_tokens is None:
                lines.append(f"本次请求仍预留 {settings.reservation_tokens:,} token")
        await notify(Delivery("\n".join(lines), outcome.image))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"image_generation: 完成生图或发送结果失败 {type(exc).__name__}")
        try:
            await notify(Delivery("图片结果暂时发不出来。用「图像配额」可以查当前余额。"))
        except Exception as notify_error:  # noqa: BLE001
            logger.warning(f"image_generation: 发送失败说明失败 {type(notify_error).__name__}")


def image_tool(
    *, ledger: ImageQuota, message_key: str, user_id: str, api_key: str,
    get_limit: Callable[[], int], notify: Callable[[Delivery], Awaitable[None]],
    images: tuple[bytes, ...] = (),
) -> ChatTool:
    request_id = hashlib.sha256(message_key.encode()).hexdigest()

    async def start(args: dict) -> str:
        original_size = ((args["width"] + 8) // 16 * 16, (args["height"] + 8) // 16 * 16)
        try:
            requested = ImageSettings.from_request(
                *original_size, args["quality"], args["frame_count"], args["frame_duration_ms"],
            )
        except ValueError as exc:
            return str(exc)
        prompt = args["prompt"].strip()
        if not prompt or len(prompt) > 4000:
            return "请提供 1 到 4000 字符的清晰画面描述。"
        indexes = args["reference_image_indexes"]
        if any(index < 1 or index > len(images) for index in indexes):
            return "参考图编号应来自本轮附件，请对方发送或引用要处理的图片。"
        if len(indexes) != len(set(indexes)):
            return "请为每张参考图选一个编号。"
        references = tuple(images[index - 1] for index in indexes)
        model = args["model"]
        try:
            limit = get_limit()
            if limit <= 0:
                return UNAVAILABLE_TEXT
            reservation = ledger.reserve(request_id, user_id, limit, requested)
        except sqlite3.Error as exc:
            logger.warning(f"image_generation: 读取图像配额失败 {type(exc).__name__}")
            return "暂时读取不到生图额度，稍后可以再试。"
        if reservation.duplicate:
            return "这条消息的生图请求已经处理过了。"
        if reservation.retry_after:
            return f"还要等 {reservation.retry_after} 秒才能再次生图。"
        settings = reservation.settings
        if settings is None:
            return f"余额不足以按该比例生成。{balance_line(reservation.balance)}。"

        try:
            await notify(Delivery(args["notice"], started=True))
        except (Exception, asyncio.CancelledError):
            ledger.settle(request_id, 0, cancelled=True)
            raise
        task = asyncio.create_task(
            _generate_and_deliver(
                ledger, request_id, user_id, api_key, prompt, settings, requested, get_limit, notify,
                original_size=original_size, model=model, references=references,
            )
        )
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return "图片任务已开始，程序已告知进度，完成会自动发图片和余额。继续简短回应，避免把图片说成已经生成。"

    return ChatTool(
        "根据本轮用户明确要求生成一张静图或 GIF 动图，也可以参考图片进行重绘、替换或修改。"
        "仅想看懂图片、找原图或听创意建议时，无需调用。程序会先估算用户配额，再告知开始、实际设置和生成结果。",
        {
            "prompt": {"type": "string", "description": "写清画面或改图要求。附参考图时，说明每张图的用途、要改的位置和需要保留的内容。动图还需描述动作和循环方式。"},
            "model": {"type": "string", "enum": list(MODEL_LABELS), "description": (
                "按任务选择。常规文字生图和快速草稿优先 Flare。有参考图、替换物体、换背景、"
                "精确保留细节、复杂排版或精细构图时优先 Sunburst。用户指定模型时使用指定模型。"
            )},
            "reference_image_indexes": {"type": "array", "items": {"type": "integer"}, "description": (
                "选择本轮附件编号，从 1 开始。需要改原图时，底图放第一项，其他参考图按用途排列。"
                "纯文字生图填空数组。动图参考会使用首帧。"
            )},
            "width": {"type": "integer", "description": "宽度像素，选 16 的倍数。编辑图片时按底图比例或用户指定尺寸选择。新静图方形默认 1024，横图 1536，竖图 1024。GIF 方形默认 512。"},
            "height": {"type": "integer", "description": "高度像素，选 16 的倍数。编辑图片时按底图比例或用户指定尺寸选择。新静图方形默认 1024，横图 1024，竖图 1536。GIF 方形默认 512。"},
            "quality": {
                "type": "string", "enum": list(QUALITY_LEVELS),
                "description": "按用户要求、细节复杂度和速度选择。普通图用 medium，快速试稿用 low，高清或细节丰富用 high，更精细时用 xhigh 或 max。",
            },
            "frame_count": {"type": "integer", "description": "静图用 1。用户明确要动图或 GIF 时用 2 到 16，未指定时用 4。"},
            "frame_duration_ms": {"type": "integer", "description": "GIF 每帧时长，20 到 2000 毫秒，默认 200。静图填 200。"},
        },
        start,
        defer_notice=True,
    )
