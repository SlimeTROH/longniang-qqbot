"""Image generation, quota reservation, and asynchronous delivery."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import re
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from network_config import proxy_for_url
from nonebot import logger

from .historical_images import GeneratedImage
from .image_quota import UNAVAILABLE_TEXT, Balance, ImageQuota
from .image_settings import ImageSettings, QUALITY_LEVELS
from .llm import DEEPSEEK_MODEL, OPENAI_RESPONSES_URL, ChatTool, _call_deepseek

FLARE_MODEL = "gpt-image-2.6-flare"
SUNBURST_MODEL = "gpt-image-2.6-sunburst"
MODEL_LABELS = {FLARE_MODEL: "Flare", SUNBURST_MODEL: "Sunburst"}
SEEDREAM_MODEL = "doubao-seedream-5-5-261028"
IMAGE_MODELS = (FLARE_MODEL, SUNBURST_MODEL, SEEDREAM_MODEL)
SEEDREAM_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
_SEEDREAM_ALIASES = r"(?:豆包|奶黄包|豆姐|火山(?:引擎|方舟)?|(?<![a-z0-9_])seed(?:ream)?(?![a-z0-9_]))"
_OUTPUT_SIZE = re.compile(r"(?<!\d)(\d{3,5})\s*[x×*]\s*(\d{3,5})(?!\d)", re.IGNORECASE)
_GIF_REQUEST = re.compile(r"(?<![a-z0-9])gif(?![a-z0-9])|动图|动态图", re.IGNORECASE)
_FRAME_COUNT = re.compile(r"(?<!\d)(\d{1,2})\s*帧")
_MODEL_ALIASES = {
    FLARE_MODEL: FLARE_MODEL,
    "flare": FLARE_MODEL,
    SUNBURST_MODEL: SUNBURST_MODEL,
    "sunburst": SUNBURST_MODEL,
    SEEDREAM_MODEL: SEEDREAM_MODEL,
    "seedream": SEEDREAM_MODEL,
    "seedream5.5": SEEDREAM_MODEL,
    "seedream-5.5": SEEDREAM_MODEL,
    "seedream 5.5": SEEDREAM_MODEL,
}
_IMAGE_ROUTER = (
    "你是生图接口路由器，只负责根据输入文字判断是否路由到生图接口，以及选择对应的模型 ID。"
    "只做分流，不回复用户、不解释、不改写描述、不输出任何多余字段。\n"
    "必须严格输出一个合法 JSON 对象：\n"
    '- 无需调用生图接口（聊天、看图、识图、解题、查资料、评价或讨论模型）：{"generate": false}\n'
    '- 需要调用生图接口（画图、生图、改图、修图、重绘等）：{"generate": true, "model": "模型ID"}\n'
    "模型ID 只能从以下三个值中选一个：\n"
    f'- "{SEEDREAM_MODEL}"：仅当文字明确选用豆包、奶黄包、豆姐、Seed/Seedream 或火山生图/改图时选择（谈论模型、问用法、画一盒豆包不算指定）。\n'
    f'- "{SUNBURST_MODEL}"：文字明确指定 Sunburst，或要求改图、修图、重绘、精细构图、多细节时选择。\n'
    f'- "{FLARE_MODEL}"：其余常规生图默认选择，或文字明确指定 Flare 时选择。'
)
URL = "https://api.openai.com/v1/images/generations"
EDIT_URL = "https://api.openai.com/v1/images/edits"
IMAGE_HOST_MODEL = "gpt-6-luna"
MAX_REFERENCE_BYTES = 50 * 1024 * 1024
REFERENCE_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}
_tasks: set[asyncio.Task] = set()


@dataclass(frozen=True)
class Delivery:
    text: str
    image: bytes = b""
    started: bool = False
    seedream_url: str = ""
    openai_response_id: str = ""


@dataclass(frozen=True)
class Generation:
    image: bytes
    output_tokens: int | None = None
    error: str = ""
    seedream_url: str = ""
    openai_response_id: str = ""
    estimated: bool = False


def mentions_seedream(text: str) -> bool:
    return bool(re.search(_SEEDREAM_ALIASES, text, re.IGNORECASE))


def _resolve_image_model(value: object, *, has_images: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    key = " ".join(value.strip().lower().split())
    if key in _MODEL_ALIASES:
        return _MODEL_ALIASES[key]
    if key in ("gpt_image", "gpt-image"):
        return SUNBURST_MODEL if has_images else FLARE_MODEL
    return ""


def _image_metadata(attachments: list[tuple[str, bytes]]) -> list[dict]:
    from PIL import Image
    metadata = []
    for index, (origin, data) in enumerate(attachments, 1):
        size = None
        try:
            with Image.open(io.BytesIO(data)) as image:
                size = image.size
        except (OSError, Image.DecompressionBombError):
            pass
        metadata.append({"index": index, "source": origin, "size": size})
    return metadata


async def inspect_image_turn(
    text: str, attachments: list[tuple[str, bytes]], *, quote_text: str = "", api_key: str, model: str,
) -> tuple[dict, list[dict]]:
    metadata = await asyncio.to_thread(_image_metadata, attachments)
    if not api_key or api_key.startswith("sk-proj-"):
        return {}, metadata
    cleaned = text.strip()
    if not cleaned:
        return {"generate": False}, metadata
    quoted = quote_text.strip()
    content = (
        f"引用文字：{quoted[:1500]}\n用户文字：{cleaned[:4000]}"
        if quoted else cleaned[:4000]
    )
    router_model = model if model.startswith("deepseek-") else DEEPSEEK_MODEL
    result, _, error = await _call_deepseek(
        _IMAGE_ROUTER, content, api_key, router_model, timeout=12.0, max_output_tokens=96, json_mode=True, fast=True,
    )
    if not error:
        try:
            plan = json.loads(result)
            if isinstance(plan, dict) and type(plan.get("generate")) is bool:
                if not plan["generate"]:
                    return {"generate": False}, metadata
                chosen = _resolve_image_model(plan.get("model") or plan.get("provider"), has_images=bool(metadata))
                if chosen:
                    return {"generate": True, "model": chosen}, metadata
        except ValueError:
            pass
    logger.info(
        f"image_generation: DeepSeek 生图分流未完成"
        + (f" err={error[:160]}" if error else f" raw={result[:120]!r}")
    )
    return {}, metadata


def direct_image_args(prompt: str, plan: dict, metadata: list[dict]) -> dict:
    indexes = list(range(1, len(metadata) + 1))
    model = _resolve_image_model(plan.get("model") or plan.get("provider"), has_images=bool(indexes))
    if not model:
        raise ValueError("未指定有效的生图模型。")
    seedream = model == SEEDREAM_MODEL
    frames = 1
    if not seedream and _GIF_REQUEST.search(prompt):
        match = _FRAME_COUNT.search(prompt)
        parsed = int(match[1]) if match else 4
        frames = parsed if 2 <= parsed <= 16 else 4
    duration = 200
    quality = plan.get("quality")
    if seedream or quality not in QUALITY_LEVELS:
        quality = "medium"
    if explicit := _OUTPUT_SIZE.search(prompt):
        size = int(explicit[1]), int(explicit[2])
    else:
        source = metadata[0].get("size") if metadata else None
        size = (512, 512) if frames > 1 and not seedream else (1024, 1024)
        if source and frames == 1 and source[0] != source[1]:
            size = (1280, 832) if source[0] > source[1] else (832, 1280)
    return {
        "prompt": prompt, "model": model, "quality": quality,
        "frame_count": frames, "frame_duration_ms": duration, "reference_image_indexes": indexes,
        "width": size[0], "height": size[1], "notice": "我来改，等我一下" if indexes else "我去画，等我一下",
    }


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


async def _reference_data_urls(images: tuple[bytes, ...], max_bytes: int) -> list[str]:
    files = await asyncio.to_thread(_reference_files, images)
    encoded = []
    for _, (_, data, mime) in files:
        if len(data) > max_bytes:
            raise ValueError(f"单张参考图不能超过 {max_bytes // (1024 * 1024)} MB。")
        encoded.append(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}")
    return encoded


def _ark_image_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and not parsed.username and not parsed.password
                and parsed.port in (None, 443) and (parsed.hostname or "").endswith(".volces.com")):
            return value
    except ValueError:
        pass
    return ""


async def _download_ark_image(client: httpx.AsyncClient, url: str) -> bytes:
    raw = bytearray()
    try:
        async with client.stream("GET", url, timeout=12, follow_redirects=False) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_REFERENCE_BYTES:
                    return b""
    except httpx.HTTPError as exc:
        logger.warning(f"image_generation: 下载 Seedream 原图失败 {type(exc).__name__}")
        return b""
    return bytes(raw) if raw.startswith(b"\x89PNG\r\n\x1a\n") else b""


def _json_payload(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _rejected_reference(status: int, payload: dict, seedream: bool) -> bool:
    if status not in (400, 404):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    code, param = error.get("code"), error.get("param")
    if not seedream:
        return param == "previous_response_id" or code == "previous_response_not_found"
    message = str(error.get("message", "")).lower()
    return (param == "image" or "image" in message) and "download" in message


async def _responses_request(prompt: str, settings: ImageSettings, model: str, safety_identifier: str,
                             references: tuple[bytes, ...], previous_id: str) -> dict:
    content = [{"type": "input_text", "text": prompt}]
    if references and not previous_id:
        for url in await _reference_data_urls(references, 50 * 1024 * 1024):
            content.append({"type": "input_image", "image_url": url, "detail": "auto"})
    body = {
        "model": IMAGE_HOST_MODEL,
        "reasoning": {"effort": "low"},
        "input": [{"role": "user", "content": content}],
        "tools": [{
            "type": "image_generation",
            "model": model,
            "action": "edit" if references else "generate",
            "size": settings.size,
            "quality": settings.quality,
            "output_format": settings.output_format,
        }],
        "tool_choice": {"type": "image_generation"},
        "parallel_tool_calls": False,
        "max_tool_calls": 1,
        "store": True,
        "safety_identifier": safety_identifier,
    }
    if previous_id:
        body["previous_response_id"] = previous_id
    return body


def _decode_image(value: object) -> bytes:
    try:
        return base64.b64decode(value, validate=True) if isinstance(value, str) else b""
    except binascii.Error:
        return b""


def _responses_result(payload: dict, settings: ImageSettings) -> Generation:
    calls = [item for item in payload.get("output") or []
             if isinstance(item, dict) and item.get("type") == "image_generation_call"]
    usage = payload.get("tool_usage")
    tokens = _output_tokens({"usage": usage.get("image_gen")}) if isinstance(usage, dict) else None
    image = next((_decode_image(item["result"]) for item in reversed(calls) if item.get("result")), b"")
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        if not image and payload.get("status") in ("completed", "failed") and all(not c.get("result") for c in calls):
            tokens = tokens or 0
        return Generation(b"", tokens, "GPT Image 没有返回可发送的图片。")
    response_id = payload.get("id")
    return Generation(
        image, tokens if tokens else settings.reservation_tokens,
        openai_response_id=response_id if isinstance(response_id, str) and response_id.startswith("resp_") else "",
        estimated=not tokens,
    )


async def generate(api_key: str, user_id: str, prompt: str, settings: ImageSettings, *,
                   model: str = FLARE_MODEL, references: tuple[bytes, ...] = (),
                   previous_image: GeneratedImage | None = None, use_responses: bool = False) -> Generation:
    seedream = model == SEEDREAM_MODEL
    responses = use_responses and not seedream and not settings.animated
    source = previous_image if len(references) == 1 and previous_image and previous_image.data is references[0] else None
    ark_reference = _ark_image_url(source.seedream_url) if source and seedream else ""
    previous_id = source.openai_response_id if source and responses else ""
    payload = {
        "model": model,
        "prompt": prompt,
        "size": settings.size,
        "output_format": settings.output_format,
        "safety_identifier": hashlib.sha256(f"qqbot:{user_id}".encode()).hexdigest(),
    }
    try:
        if seedream:
            if settings.animated:
                return Generation(b"", 0, "Seedream 5.5 支持静图。要生成 GIF，请指定 GPT Image。")
            url = SEEDREAM_URL
            payload["response_format"] = "url"
            payload["watermark"] = False
            if references:
                if len(references) > 10:
                    return Generation(b"", 0, "Seedream 5.5 最多支持 10 张参考图。")
                urls = [ark_reference] if ark_reference else await _reference_data_urls(references, 30 * 1024 * 1024)
                payload["image"] = urls[0] if len(urls) == 1 else urls
            request = {"json": payload}
        elif responses:
            url = OPENAI_RESPONSES_URL
            request = {"json": await _responses_request(
                prompt, settings, model, payload["safety_identifier"], references, previous_id,
            )}
        else:
            url = URL
            payload.update(quality=settings.quality, n=1)
            if settings.animated:
                payload.update(frame_count=settings.frame_count, frame_duration_ms=settings.frame_duration_ms)
            request = {"json": payload}
            if references:
                files = await asyncio.to_thread(_reference_files, references)
                url = EDIT_URL
                request = {"data": payload, "files": files}
    except ValueError as exc:
        return Generation(b"", 0, str(exc))
    except ImportError:
        return Generation(b"", 0, "暂时无法读取参考图。")
    try:
        async with httpx.AsyncClient(
            proxy=None if seedream else proxy_for_url(url),
            trust_env=False, timeout=httpx.Timeout(180.0, connect=8.0),
        ) as client:
            headers = {"Authorization": f"Bearer {api_key}"}
            response = await client.post(url, headers=headers, **request)
            result = _json_payload(response)
            if (ark_reference or previous_id) and _rejected_reference(response.status_code, result, seedream):
                if seedream:
                    request["json"]["image"] = (await _reference_data_urls(references, 30 * 1024 * 1024))[0]
                else:
                    request["json"] = await _responses_request(
                        prompt, settings, model, payload["safety_identifier"], references, "",
                    )
                response = await client.post(url, headers=headers, **request)
                result = _json_payload(response)
            image_url = ""
            if seedream and response.status_code == 200:
                entries = result.get("data")
                if isinstance(entries, list) and entries and isinstance(entries[0], dict):
                    image_url = _ark_image_url(entries[0].get("url"))
                if image_url:
                    # The signed TOS URL already authorizes downloads.
                    image = await _download_ark_image(client, image_url)
    except (ValueError, ImportError):
        return Generation(b"", 0, "暂时无法读取参考图。")
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        logger.warning(f"image_generation: {type(exc).__name__}")
        return Generation(b"", 0, "生图接口暂时连不上，本次额度已退回。")
    except httpx.HTTPError as exc:
        logger.warning(f"image_generation: {type(exc).__name__}")
        return Generation(b"", None, "生图请求中断，用量待确认。")

    payload = result
    tokens = None if responses else _output_tokens(payload)
    if response.status_code != 200:
        logger.warning(f"image_generation: HTTP {response.status_code} model={model}")
        if response.status_code in (400, 401, 403, 404, 413, 422, 429) and tokens is None:
            tokens = 0
        error = payload.get("error")
        if seedream and isinstance(error, dict) and error.get("code") == "ModelNotOpen":
            return Generation(b"", 0, "Seedream 5.5 暂未开通。请管理员在火山方舟开通该模型后再试。")
        provider = "Seedream 5.5 " if seedream else ""
        return Generation(b"", tokens, f"{provider}生图失败（HTTP {response.status_code}）。")
    if responses:
        return _responses_result(payload, settings)

    entries = payload.get("data") or []
    entry = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else {}
    if not image_url:
        image = _decode_image(entry.get("b64_json"))
    if seedream and tokens is None:
        size = entry.get("size")
        dimensions = re.fullmatch(r"([0-9]{1,5})x([0-9]{1,5})", size) if isinstance(size, str) else None
        if dimensions:
            tokens = int(dimensions[1]) * int(dimensions[2]) // 256
    if not image:
        return Generation(b"", tokens, "生图接口没有返回可发送的图片。")
    if settings.animated and not image.startswith((b"GIF87a", b"GIF89a")):
        return Generation(b"", tokens, "生图接口没有返回可发送的 GIF。")
    return Generation(image, tokens, seedream_url=image_url)


def balance_line(balance: Balance) -> str:
    return f"{balance.day} 图像配额剩余 {balance.remaining:,} / {balance.limit:,} token"


async def _generate_and_deliver(
    ledger: ImageQuota, request_id: str, user_id: str, api_key: str, prompt: str, settings: ImageSettings,
    requested: ImageSettings, get_limit: Callable[[], int], notify: Callable[[Delivery], Awaitable[None]],
    *, original_size: tuple[int, int], model: str, references: tuple[bytes, ...],
    previous_image: GeneratedImage | None = None, use_responses: bool = False,
) -> None:
    try:
        outcome = await generate(
            api_key, user_id, prompt, settings, model=model, references=references,
            previous_image=previous_image, use_responses=use_responses,
        )
        if outcome.output_tokens is not None:
            ledger.settle(request_id, outcome.output_tokens, cancelled=not outcome.image and outcome.output_tokens == 0)
        balance = ledger.balance(user_id, get_limit())
        if outcome.image:
            used = (f"本次输出 {outcome.output_tokens:,} token" if outcome.output_tokens is not None
                    else f"本次用量待确认，已预留 {settings.reservation_tokens:,} token")
            if outcome.estimated:
                used = f"本次按估算扣除 {outcome.output_tokens:,} 图像 token"
            label = "Seedream 5.5" if model == SEEDREAM_MODEL else MODEL_LABELS[model]
            lines = [f"已画好 · {label} · {settings.label}", used, balance_line(balance)]
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
        await notify(Delivery("\n".join(lines), outcome.image,
                              seedream_url=outcome.seedream_url, openai_response_id=outcome.openai_response_id))
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
    seedream_key: str = "",
    require_reference: bool = False,
    previous_image: GeneratedImage | None = None, use_responses: bool = False,
    use_seedream: bool = False,
) -> ChatTool:
    request_id = hashlib.sha256(message_key.encode()).hexdigest()
    seedream = use_seedream
    models = [SEEDREAM_MODEL] if seedream else list(MODEL_LABELS)
    model_description = (
        "用户已通过本轮消息指定 Seedream 5.5。它支持单张静图和参考图。"
        if seedream else
        "常规文字生图和快速草稿优先 Flare。有参考图、替换物体、换背景、"
        "精确保留细节、复杂排版或精细构图时优先 Sunburst。用户指定 GPT Image 型号时使用指定型号。"
    )

    async def start(args: dict) -> str:
        if seedream and not seedream_key:
            return "Seedream 5.5 尚未配置火山方舟 API 密钥。"
        if not seedream and not api_key:
            return "GPT Image 尚未配置 API 密钥。"
        original_size = ((args["width"] + 8) // 16 * 16, (args["height"] + 8) // 16 * 16)
        try:
            settings_args = (*original_size, args["quality"], args["frame_count"], args["frame_duration_ms"])
            requested = (
                ImageSettings(*settings_args, seedream=True) if seedream
                else ImageSettings.from_request(*settings_args)
            )
        except ValueError as exc:
            return str(exc)
        prompt = args["prompt"].strip()
        if not prompt or len(prompt) > 4000:
            return "请提供 1 到 4000 字符的清晰画面描述。"
        indexes = args["reference_image_indexes"]
        if require_reference and not indexes:
            if len(images) == 1:
                indexes = [1]
            elif images:
                return "这次是改图，请选出要修改的底图编号，放在参考图列表第一位。"
            else:
                return "没找到要继续修改的图片。请引用龙娘上一版图片或重新上传后再改。"
        if any(index < 1 or index > len(images) for index in indexes):
            return "参考图编号应来自本轮附件，请对方发送或引用要处理的图片。"
        if len(indexes) != len(set(indexes)):
            return "请为每张参考图选一个编号。"
        references = tuple(images[index - 1] for index in indexes)
        model = SEEDREAM_MODEL if seedream else args["model"]
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
            logger.info(f"image_generation: edit={bool(references)} model={model} cooldown={reservation.retry_after}s")
            return f"还要等 {reservation.retry_after} 秒才能再次生图。"
        settings = reservation.settings
        if settings is None:
            logger.info(f"image_generation: edit={bool(references)} model={model} insufficient_quota")
            return f"余额不足以按该比例生成。{balance_line(reservation.balance)}。"

        try:
            await notify(Delivery(args["notice"], started=True))
        except (Exception, asyncio.CancelledError):
            ledger.settle(request_id, 0, cancelled=True)
            raise
        task = asyncio.create_task(
            _generate_and_deliver(
                ledger, request_id, user_id, seedream_key if seedream else api_key,
                prompt, settings, requested, get_limit, notify,
                original_size=original_size, model=model, references=references,
                previous_image=previous_image, use_responses=use_responses,
            )
        )
        logger.info(f"image_generation: started model={model} refs={len(references)} size={settings.size}")
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        return "图片任务已开始，程序已告知进度，完成会自动发图片和余额。继续简短回应，避免把图片说成已经生成。"

    return ChatTool(
        "根据本轮用户明确要求生成一张静图或 GIF 动图，也可以参考图片进行重绘、替换或修改。"
        "仅想看懂图片、找原图或听创意建议时，无需调用。程序会先估算用户配额，再告知开始、实际设置和生成结果。",
        {
            "prompt": {"type": "string", "description": "写清画面或改图要求。附参考图时，说明每张图的用途、要改的位置和需要保留的内容。动图还需描述动作和循环方式。"},
            "model": {"type": "string", "enum": models, "description": model_description},
            "reference_image_indexes": {"type": "array", "items": {"type": "integer"}, "description": (
                "选择本轮附件编号，从 1 开始。需要改原图时，底图放第一项，其他参考图按用途排列。"
                "纯文字生图填空数组。动图参考会使用首帧。"
            )},
            "width": {"type": "integer", "description": "宽度像素，选 16 的倍数。编辑图片时按底图比例或用户指定尺寸选择。新静图方形默认 1024，横图 1536，竖图 1024。GIF 方形默认 512。"},
            "height": {"type": "integer", "description": "高度像素，选 16 的倍数。编辑图片时按底图比例或用户指定尺寸选择。新静图方形默认 1024，横图 1024，竖图 1536。GIF 方形默认 512。"},
            "quality": {
                "type": "string", "enum": list(QUALITY_LEVELS),
                "description": (
                    "Seedream 通过像素尺寸控制分辨率，画质字段填 medium。普通方图默认 1024×1024，"
                    "高清选约 1536×1536 或 2048×2048，横竖图按比例选择，总像素须在 921600 到 4624220 之间。"
                    if seedream else
                    "按用户要求、细节复杂度和速度选择。普通图用 medium，快速试稿用 low，高清或细节丰富用 high，更精细时用 xhigh 或 max。"
                ),
            },
            "frame_count": {"type": "integer", "description": "静图用 1。用户明确要动图或 GIF 时用 2 到 16，未指定时用 4。"},
            "frame_duration_ms": {"type": "integer", "description": "GIF 每帧时长，20 到 2000 毫秒，默认 200。静图填 200。"},
        },
        start,
        defer_notice=True,
    )
