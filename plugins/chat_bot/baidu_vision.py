"""百度图像识别：通用物体和场景识别（advanced_general）。

定位：
    主视觉模型（Gemini / GLM）认不出、或者整条识图链失败时，补一层
    「图里到底有什么」的客观标签（物体 / 场景 / 置信度），交给主模型当线索。
    它不会替你回答「这是谁」，但能给出「动漫人物 0.97」「夜景 0.88」这种
    确定性信息，避免她对着图瞎猜。

接口（官方文档 ai.baidu.com/ai-doc/IMAGERECOGNITION）：
    token : POST https://aip.baidubce.com/oauth/2.0/token
    识别  : POST https://aip.baidubce.com/rest/2.0/image-classify/v2/advanced_general
    文字  : POST https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic
    access_token 有效期 30 天，缓存到 qqbot/state/baidu_token.json。

免费额度（2025-11 文档）：
    通用物体和场景识别：实名认证后共 10,000 次（2 QPS）
    通用文字识别：每月 1,000 次
    成功和失败调用都计次，所以这里对「没配置 / 图片过大」直接短路，不发请求。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from nonebot import logger

TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
ADVANCED_GENERAL_URL = (
    "https://aip.baidubce.com/rest/2.0/image-classify/v2/advanced_general"
)
OCR_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic"

# 千帆（AppBuilder）的 API Key 走 OpenAI 兼容协议，可以直接调多模态模型。
# 这条路用的是 .env 里已有的 CHAT_BAIDU_SEARCH_KEY（bce-v3/ALTAK-…），
# 不需要再去申请图像识别的 AK/SK；实测 internvl3-38b 可用（约 2.8s/张）。
QIANFAN_URL = "https://qianfan.baidubce.com/v2/chat/completions"
DEFAULT_QIANFAN_MODEL = "internvl3-38b"

# 百度只做「客观提取」，不做身份判断/出处猜测/引申义。
QIANFAN_PROMPTS = {
    "objects": """只做客观提取，不要理解也不要猜测：
- 列出画面里能直接看到的东西：人物数量、物体、场景、颜色、明显动作；
- 不判断身份、不猜出处、不评价好坏、不引申含义；
- 不要编名字。控制在 120 字内。""",
    "ocr": """只逐行输出图片里出现的文字，完全按原样：
- 不要翻译、不要解释、不要改错别字、不要加原文没有的标点；
- 没有文字就只输出四个字：没有文字。""",
}

# 旧的全能提示词（保留给人工调试），日常不用
QIANFAN_PROMPT = QIANFAN_PROMPTS["objects"]

# 接口限制：base64 后 ≤ 4M，短边 ≥15px，长边 ≤4096px
MAX_B64_BYTES = 4 * 1024 * 1024
MAX_SIDE = 1600
JPEG_QUALITY = 90

# token 失效的错误码：重新取一次 token 再试
_TOKEN_INVALID_CODES = {110, 111}

_STATE_FILE = Path(__file__).resolve().parents[2] / "state" / "baidu_token.json"
_mem_token: dict[str, tuple[str, float]] = {}


@dataclass
class BaiduResult:
    ok: bool = False
    text: str = ""
    tags: list[tuple[str, float]] = field(default_factory=list)
    ocr_text: str = ""
    error: str = ""
    elapsed: float = 0.0
    kind: str = ""          # general / ocr / general+ocr


def configured(ak: str, sk: str) -> bool:
    return bool((ak or "").strip() and (sk or "").strip())


def _ak_key(ak: str) -> str:
    return hashlib.sha256(ak.strip().encode("utf-8")).hexdigest()[:16]


def _load_token_cache() -> dict[str, dict]:
    try:
        raw = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_token_cache(ak: str, token: str, expires_at: float) -> None:
    try:
        data = _load_token_cache()
        data[_ak_key(ak)] = {"token": token, "expires_at": expires_at}
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except OSError as exc:  # 缓存失败不影响主流程
        logger.debug(f"baidu_vision: 写 token 缓存失败 {exc!r}")


async def _fetch_token(ak: str, sk: str, timeout: float) -> tuple[str, str]:
    """换 access_token，返回 (token, error)。"""
    params = {
        "grant_type": "client_credentials",
        "client_id": ak.strip(),
        "client_secret": sk.strip(),
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(TOKEN_URL, params=params)
    except Exception as exc:  # noqa: BLE001
        return "", f"取 token 失败：{type(exc).__name__}: {exc}"
    try:
        data = resp.json()
    except ValueError:
        return "", f"取 token 返回非 JSON（HTTP {resp.status_code}）"
    if data.get("access_token"):
        return str(data["access_token"]), ""
    detail = data.get("error_description") or data.get("error") or str(data)[:160]
    return "", f"AK/SK 无效或未开通：{detail}"


async def _access_token(ak: str, sk: str, timeout: float) -> tuple[str, str]:
    """带缓存的 token 获取。"""
    key = _ak_key(ak)
    now = time.time()
    cached = _mem_token.get(key)
    if cached and cached[1] > now + 60:
        return cached[0], ""
    disk = _load_token_cache().get(key) or {}
    token = str(disk.get("token") or "")
    expires_at = float(disk.get("expires_at") or 0)
    if token and expires_at > now + 60:
        _mem_token[key] = (token, expires_at)
        return token, ""

    token, err = await _fetch_token(ak, sk, timeout)
    if not token:
        return "", err
    expires_at = now + 25 * 24 * 3600  # 官方 30 天，保守按 25 天
    _mem_token[key] = (token, expires_at)
    _save_token_cache(ak, token, expires_at)
    return token, ""


def _guess_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


def compress_for_baidu(data: bytes) -> bytes:
    """按需压缩：只有长边 >1600 或体积 >1.5MB 才处理。

    百度只是对 base64 有 4MB 上限，小图没必要动；大图压到 1600/q90，
    文字和细节都能保住，同时稳稳落在 4MB 以内。
    """
    try:
        from PIL import Image
    except ImportError:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        if max(img.size) <= MAX_SIDE and len(data) <= 1_500_000:
            return data
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > MAX_SIDE:
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        out = buf.getvalue()
        return out if out and len(out) < len(data) else data
    except Exception:  # noqa: BLE001
        return data


def _error_text(data: dict) -> str:
    code = data.get("error_code")
    msg = data.get("error_msg") or ""
    if code is not None:
        return f"百度接口错误 {code}: {msg[:160]}"
    return "百度接口返回异常"


async def _post_form(
    url: str, token: str, form: dict[str, str], timeout: float
) -> tuple[dict, str]:
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(
                url,
                params={"access_token": token},
                data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:  # noqa: BLE001
        return {}, f"请求失败：{type(exc).__name__}: {exc}"
    try:
        data = resp.json()
    except ValueError:
        return {}, f"返回非 JSON（HTTP {resp.status_code}）"
    if resp.status_code != 200:
        return {}, f"HTTP {resp.status_code}: {str(data)[:160]}"
    return data, ""


async def recognize(
    image_bytes: bytes,
    ak: str,
    sk: str,
    baike_num: int = 0,
    timeout: float = 12.0,
) -> BaiduResult:
    """通用物体和场景识别。失败返回 ok=False，不抛异常。"""
    started = time.time()
    if not configured(ak, sk):
        return BaiduResult(ok=False, error="未配置 CHAT_BAIDU_AK/SK")
    if not image_bytes:
        return BaiduResult(ok=False, error="空图片")

    payload = compress_for_baidu(image_bytes)
    b64 = base64.b64encode(payload).decode("ascii")
    if len(b64) > MAX_B64_BYTES:
        return BaiduResult(
            ok=False,
            error=f"图片太大（base64 {len(b64) // 1024}KB > 4MB）",
            elapsed=time.time() - started,
        )

    token, err = await _access_token(ak, sk, timeout)
    if not token:
        return BaiduResult(ok=False, error=err, elapsed=time.time() - started)

    form = {"image": b64}
    if baike_num:
        form["baike_num"] = str(baike_num)

    data, err = await _post_form(ADVANCED_GENERAL_URL, token, form, timeout)
    # token 过期：清掉缓存再试一次（只重试这类错误）
    if data.get("error_code") in _TOKEN_INVALID_CODES:
        _mem_token.pop(_ak_key(ak), None)
        token, err2 = await _access_token(ak, sk, timeout)
        if token:
            data, err = await _post_form(ADVANCED_GENERAL_URL, token, form, timeout)
            err = err or err2
    if err:
        return BaiduResult(ok=False, error=err, elapsed=time.time() - started)
    if "error_code" in data:
        return BaiduResult(
            ok=False, error=_error_text(data), elapsed=time.time() - started
        )

    tags: list[tuple[str, float]] = []
    for item in data.get("result") or []:
        keyword = str(item.get("keyword") or "").strip()
        if not keyword:
            continue
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        tags.append((keyword, score))

    if not tags:
        return BaiduResult(
            ok=False, error="没识别出任何标签", elapsed=time.time() - started
        )

    return BaiduResult(
        ok=True,
        text="、".join(f"{k}（{s:.2f}）" for k, s in tags[:6]),
        tags=tags,
        elapsed=time.time() - started,
        kind="general",
    )


async def ocr(image_bytes: bytes, ak: str, sk: str, timeout: float = 12.0) -> BaiduResult:
    """通用文字识别（CHN_ENG）。"""
    started = time.time()
    if not configured(ak, sk):
        return BaiduResult(ok=False, error="未配置 CHAT_BAIDU_AK/SK")
    if not image_bytes:
        return BaiduResult(ok=False, error="空图片")

    payload = compress_for_baidu(image_bytes)
    b64 = base64.b64encode(payload).decode("ascii")
    if len(b64) > MAX_B64_BYTES:
        return BaiduResult(
            ok=False,
            error=f"图片太大（base64 {len(b64) // 1024}KB > 4MB）",
            elapsed=time.time() - started,
        )

    token, err = await _access_token(ak, sk, timeout)
    if not token:
        return BaiduResult(ok=False, error=err, elapsed=time.time() - started)

    form = {"image": b64, "language_type": "CHN_ENG"}
    data, err = await _post_form(OCR_URL, token, form, timeout)
    if data.get("error_code") in _TOKEN_INVALID_CODES:
        _mem_token.pop(_ak_key(ak), None)
        token, err2 = await _access_token(ak, sk, timeout)
        if token:
            data, err = await _post_form(OCR_URL, token, form, timeout)
            err = err or err2
    if err:
        return BaiduResult(ok=False, error=err, elapsed=time.time() - started)
    if "error_code" in data:
        return BaiduResult(
            ok=False, error=_error_text(data), elapsed=time.time() - started
        )

    lines = [
        str(item.get("words") or "").strip()
        for item in (data.get("words_result") or [])
    ]
    text = "\n".join(x for x in lines if x)
    if not text:
        return BaiduResult(
            ok=False, error="图中没有文字", elapsed=time.time() - started
        )
    return BaiduResult(
        ok=True, ocr_text=text, elapsed=time.time() - started, kind="ocr"
    )


async def qianfan_describe(
    image_bytes: bytes,
    api_key: str,
    model: str = DEFAULT_QIANFAN_MODEL,
    question: str = "",
    timeout: float = 20.0,
    mode: str = "objects",
) -> BaiduResult:
    """用千帆（AppBuilder）API Key 调多模态模型，做「客观提取」。

    mode：
      · objects —— 只列看得见的东西（数量/物体/场景/颜色），不判断身份；
      · ocr     —— 只逐行抄图里的文字，不翻译不解释。

    用的是 .env 里已有的 CHAT_BAIDU_SEARCH_KEY，不需要 AIP 的 AK/SK。
    明确不做「这是谁/什么梗/好不好看」这类理解与猜测——那是主模型的事。
    """
    started = time.time()
    if not (api_key or "").strip():
        return BaiduResult(ok=False, error="未配置千帆 API Key")
    if not image_bytes:
        return BaiduResult(ok=False, error="空图片")

    raw = compress_for_baidu(image_bytes)
    b64 = base64.b64encode(raw).decode("ascii")
    mime = _guess_mime(raw)
    prompt = QIANFAN_PROMPTS.get(mode) or QIANFAN_PROMPTS["objects"]
    q = (question or "").strip()
    if q and mode == "objects":
        # 只用来缩小观察范围（比如「看左下角」），明确禁止借题发挥
        prompt += f"\n\n对方只要求客观回答：「{q[:100]}」。仍然不许判断身份或猜测出处。"

    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                ],
            }
        ],
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(
                QIANFAN_URL,
                headers={
                    "Authorization": f"Bearer {api_key.strip()}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except Exception as exc:  # noqa: BLE001
        return BaiduResult(
            ok=False,
            error=f"千帆请求失败：{type(exc).__name__}: {exc}",
            elapsed=time.time() - started,
        )

    try:
        data = json.loads(resp.content.decode("utf-8", "replace"))
    except ValueError:
        return BaiduResult(
            ok=False,
            error=f"千帆返回非 JSON（HTTP {resp.status_code}）",
            elapsed=time.time() - started,
        )

    if resp.status_code != 200:
        err = data.get("error") or {}
        detail = err.get("code") or err.get("message") or str(data)[:160]
        return BaiduResult(
            ok=False,
            error=f"千帆 HTTP {resp.status_code}: {detail}",
            elapsed=time.time() - started,
        )

    try:
        text = str(data["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        return BaiduResult(
            ok=False,
            error=f"千帆返回结构异常：{exc}",
            elapsed=time.time() - started,
        )
    if not text:
        return BaiduResult(
            ok=False, error="千帆返回空内容", elapsed=time.time() - started
        )
    return BaiduResult(
        ok=True,
        text=text,
        elapsed=time.time() - started,
        kind=f"qianfan:{model}:{mode}",
    )


async def describe(
    image_bytes: bytes,
    ak: str,
    sk: str,
    want_ocr: bool = False,
    timeout: float = 12.0,
    qianfan_key: str = "",
    qianfan_model: str = DEFAULT_QIANFAN_MODEL,
    question: str = "",
    mode: str = "objects",
) -> BaiduResult:
    """只做「客观提取」：要么抄文字（ocr），要么列可见内容（objects）。

    这一点是硬约束：调用方只有在请求不需要理解、不需要身份判断、
    不需要猜测时才应该走到这里（见 __init__._baidu_mode）。

    两条路：
      · 配了 AIP 的 AK/SK → 通用物体识别 / 通用文字识别（快、便宜）；
      · 没配但千帆 Key 有 → 调 internvl3-38b，用严格提示词约束成客观输出。
    """
    started = time.time()
    mode = "ocr" if mode == "ocr" else "objects"
    if not configured(ak, sk) and not (qianfan_key or "").strip():
        return BaiduResult(ok=False, error="未配置百度识图（AK/SK 或千帆 Key）")
    if not image_bytes:
        return BaiduResult(ok=False, error="空图片")

    # ① AIP：文字识别 / 物体识别
    aip_err = ""
    if configured(ak, sk):
        if mode == "ocr":
            only = await ocr(image_bytes, ak, sk, timeout=timeout)
            if only.ok:
                text = only.ocr_text.strip()
                if len(text) > 500:
                    text = text[:500] + "…"
                return BaiduResult(
                    ok=True,
                    text=text,
                    ocr_text=only.ocr_text,
                    elapsed=time.time() - started,
                    kind="ocr",
                )
            aip_err = only.error
        else:
            general, ocr_res = await asyncio.gather(
                recognize(image_bytes, ak, sk, timeout=timeout),
                ocr(image_bytes, ak, sk, timeout=timeout) if want_ocr else _none(),
            )
            parts: list[str] = []
            if general.ok:
                parts.append(f"物体/场景：{general.text}")
            if ocr_res.ok and ocr_res.ocr_text.strip():
                text = ocr_res.ocr_text.strip()
                if len(text) > 300:
                    text = text[:300] + "…"
                parts.append(f"图中文字：{text}")
            if parts:
                return BaiduResult(
                    ok=True,
                    text="\n".join(parts),
                    tags=general.tags,
                    ocr_text=ocr_res.ocr_text if ocr_res.ok else "",
                    elapsed=time.time() - started,
                    kind="general" + ("+ocr" if ocr_res.ok else ""),
                )
            aip_err = general.error or (ocr_res.error if want_ocr else "")

    # ② 千帆：严格提示词下的客观提取
    if (qianfan_key or "").strip():
        qf = await qianfan_describe(
            image_bytes,
            qianfan_key,
            qianfan_model,
            question,
            timeout=max(timeout, 20.0),
            mode=mode,
        )
        if qf.ok:
            return qf
        return BaiduResult(ok=False, error=qf.error, elapsed=time.time() - started)

    return BaiduResult(
        ok=False,
        error=aip_err or "没有可用的百度识图通道",
        elapsed=time.time() - started,
    )


async def _none() -> BaiduResult:
    return BaiduResult(ok=False, error="未启用")
