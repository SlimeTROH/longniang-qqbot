"""智谱 GLM 视觉模型识图：把图片转成详细的中文描述，供主模型理解。

为什么要单独做这一步：
    · 视觉模型按像素算 token（一张 200x200 的图就吃掉 140+ tokens），
      如果直接塞给豆包，图片历史会迅速把上下文撑爆、成本也高。
    · 先用免费/便宜的视觉模型把图「翻译」成文字，主模型只处理文字。
    · 历史里保存的是文字描述，不会随着轮次累积爆炸。

失败时返回一句兜底描述，绝不让整轮对话挂掉。
"""
import asyncio
import base64
import io
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from nonebot import logger

GLM_BASE = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
# 主力视觉模型（能力更强，但经常 429 限流）
DEFAULT_VISION_MODEL = "glm-4.6v-flash"
# 主力挂掉时自动降级到这里（免费、稳定）
FALLBACK_VISION_MODEL = "glm-4v-flash"

# 主力模型失败后，这段时间内直接用备用，不再每轮都去撞一次 429
_PRIMARY_COOLDOWN = 600.0
_primary_down_until = 0.0

# 压缩策略：**只在确实过大时才压，而且不压狠**。
#   · 长边 ≤ MAX_SIDE 且体积 ≤ MAX_BYTES → 原样返回（PNG/透明/动图都不动）
#   · 超过任一条才缩到长边 MAX_SIDE，JPEG 质量 90
# 之前是「无条件压到 768 + q80」，字和细节会糊；1600 + q90 对识图/OCR 都够清楚，
# 体积也还能压在 ~300KB 以内，不会把代理撑爆。
MAX_SIDE = 1600
JPEG_QUALITY = 90
MAX_BYTES = 1_500_000
# base64 后的安全上限（多数服务对请求体有 10MB 限制，留足余量）
MAX_B64_BYTES = 6 * 1024 * 1024

DESCRIBE_PROMPT = """请用中文详细描述这张图片，尽量把下面几点都说清楚：

1. 画面主体：是人还是物？人物的外貌、表情、动作、穿着、大致年龄
2. 场景与背景：在哪、什么环境、白天还是夜晚
3. 画面中的文字：如果有，请原样列出（包括标题、按钮、标注）
4. 风格：二次元/写实/摄影/插画？色调是明亮还是昏暗？氛围是温馨还是压抑
5. 其他值得注意的细节

要求：客观描述，不要抒情，不要推测图片之外的事。控制在 200 字以内。"""

CONTEXT_PROMPT = """对方发这张图的时候说：「{context}」

**你要做的是「回答他」，不是写图片说明。**

· 如果他问「这是谁 / 叫什么」，而你认得出来，**就直接说名字和出处**
  （例如「这是《XX》里的 YY」）。认不出就明说认不出，
  但把他能辨认身份的特征讲清楚：发色、瞳色、服装、配饰、标志性的角/耳/尾巴等。
· 如果他问「这是什么 / 什么梗」，就把画面内容和文字一起解释。
· 如果他问「好不好看 / 什么感受」，就给出判断和依据。
· **画面上的文字一定要原样念出来**——它往往正是理解这张图的关键。
· 与他的问题无关的部分，简单带过就行，不用面面俱到。

下面是通用的描述要求，作为补充：

{base}"""


def build_describe_prompt(context: str = "") -> str:
    """按有没有对话上下文，生成给视觉模型的提示词（保留兼容用）。"""
    ctx = (context or "").strip()
    if not ctx:
        return DESCRIBE_PROMPT
    return CONTEXT_PROMPT.format(context=ctx[:200], base=DESCRIBE_PROMPT)


# 系统提示词：定角色 + 定优先级。用户的话单独作为一条 user 消息发过去。
SYSTEM_PROMPT = """你是负责「看图和回答」的助手。对方会发来一张图，并附上一句话。

**你的任务是回答他那句话，不是写图片说明。**

【最重要的一条：不确定就别给名字】

· **只有当你百分之百确定**这个角色/作品是什么，才可以说出名字和出处。
· 只要有一丝不确定——画风像、配色像、感觉像——**就说「我认不出这是谁」**，
  然后把能用来辨认的特征讲清楚：发色、瞳色、服装、配饰、角/耳/尾巴、
  画面上的水印和文字。让提问的人自己去判断。
· **绝对禁止**把猜测写成事实。不要写「这是 XX 画的」「这是《XX》里的 YY」，
  除非你真的确定。**说错了比说不出更糟。**
· 水印、签名这种小字尤其不可靠（压缩后会糊）。如果你要提，必须写成
  「水印好像是……但看不清」，不能说成「作者是……」。
· 宁可只说「蓝发、女仆装、鲸鱼耳朵、气鼓鼓」，也不要编一个名字。

【怎么回答】

· 他问「这是谁 / 叫什么」：确定就答，不确定就说认不出 + 讲特征。
· 他问「这是什么 / 什么梗 / 什么意思」：解释画面内容和文字的含义，这部分可以大胆说。
· 他问「好不好看 / 什么感受」：给判断和依据。
· 他只说「看看」：客观描述画面。
· **画面上的文字必须原样念出来**——它往往正是理解这张图的关键。
· 与他的问题无关的部分简单带过。

【通用描述要求】

1. 主体：是什么？人还是物？外貌、表情、动作、穿着
2. 场景：在哪、什么环境、光线如何
3. 文字：原样列出（标题、按钮、标注、水印都算）
4. 风格：二次元/写实/摄影/插画？色调明暗？氛围？
5. 不要抒情，不要推测画面之外的事。控制在 250 字以内（除非他明确要求更长）。"""

FALLBACK = "（对方发了一张图片，但没能看清内容）"


@dataclass
class VisionResult:
    description: str = ""
    ok: bool = False
    error: str = ""
    elapsed: float = 0.0
    bytes_in: int = 0
    bytes_out: int = 0
    mime: str = ""
    model_used: str = ""        # 实际用的哪个模型（可能降级过）


def _guess_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def compress_image(data: bytes, *, static: bool = False) -> tuple[bytes, str]:
    """按需压缩：只有「尺寸过大」或「体积过大」才处理。

    - 长边 ≤ MAX_SIDE 且 ≤ MAX_BYTES：原样返回（保留 PNG/GIF/透明通道）
    - 否则缩到长边 MAX_SIDE、JPEG 质量 90；若压完反而更大，仍用原图
    - 没有 Pillow 时原样返回，由调用方按字节数判断能否直接用
    """
    try:
        from PIL import Image
    except ImportError:
        return data, _guess_mime(data)

    try:
        img = Image.open(io.BytesIO(data))
        long_side = max(img.size)
        animated = static and getattr(img, "is_animated", False)
        if not animated and long_side <= MAX_SIDE and len(data) <= MAX_BYTES:
            return data, _guess_mime(data)

        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        if long_side > MAX_SIDE:
            img.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        out = buf.getvalue()
        # 压缩反而变大就用原图
        if out and (animated or len(out) < len(data)):
            return out, "image/jpeg"
        return data, _guess_mime(data)
    except Exception:  # noqa: BLE001
        return data, _guess_mime(data)


async def _describe_once(
    image_bytes: bytes,
    api_key: str,
    model: str,
    timeout: float = 60.0,
    context: str = "",
    base_url: str = GLM_BASE,
    history: list[dict] | None = None,
    system_prompt: str = "",
    json_mode: bool = False,
) -> VisionResult:
    """把图片转成中文描述。失败时 description 是兜底文案。

    context 是发图的人说的话——带上它，视觉模型才会「针对他问的东西」看，
    而不是泛泛地描述整张图。
    history 是最近的对话（只文字，不含旧图），用来解「那这张呢」这种指代。
    base_url 走 OpenAI 兼容协议，所以 GLM 和 Gemini 中转都能用同一个函数。
    system_prompt 为空时用默认描述提示词；识别（identify）会传自己的。
    """
    started = time.time()
    if not image_bytes:
        return VisionResult(description=FALLBACK, ok=False, error="空图片")

    payload_bytes, mime = compress_image(image_bytes)
    b64 = base64.b64encode(payload_bytes).decode("ascii")
    if len(b64) > MAX_B64_BYTES:
        return VisionResult(
            description=FALLBACK,
            ok=False,
            error=f"图片太大（base64 {len(b64) // 1024}KB）",
            elapsed=time.time() - started,
            bytes_in=len(image_bytes),
            bytes_out=len(payload_bytes),
            mime=mime,
        )

    # 真正的「用户发图提问」结构：
    #   system —— 你是个帮我读图的人，要回答而不是写说明
    #   history —— 最近几轮对话（只文字），用来解指代
    #   user   —— 图 + 他说的原话（一字不改）
    question = (context or "").strip() or "描述这张图"
    msgs: list[dict] = [{"role": "system", "content": system_prompt or SYSTEM_PROMPT}]
    if history:
        msgs.extend(history)
    msgs.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                },
                {"type": "text", "text": question},
            ],
        }
    )
    payload = {
        "model": model,
        "messages": msgs,
    }
    if model.rsplit("/", 1)[-1].lower().startswith("gemini-4"):
        payload["reasoning_effort"] = "low"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(base_url, headers=headers, json=payload)
    except Exception as exc:  # noqa: BLE001
        return VisionResult(
            description=FALLBACK,
            ok=False,
            error=f"请求失败：{type(exc).__name__}: {exc}",
            elapsed=time.time() - started,
            bytes_in=len(image_bytes),
            bytes_out=len(payload_bytes),
            mime=mime,
        )

    if resp.status_code != 200:
        return VisionResult(
            description=FALLBACK,
            ok=False,
            error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            elapsed=time.time() - started,
            bytes_in=len(image_bytes),
            bytes_out=len(payload_bytes),
            mime=mime,
        )

    try:
        content = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        return VisionResult(
            description=FALLBACK,
            ok=False,
            error=f"返回结构异常：{exc}",
            elapsed=time.time() - started,
            bytes_in=len(image_bytes),
            bytes_out=len(payload_bytes),
            mime=mime,
        )

    if not content:
        return VisionResult(
            description=FALLBACK,
            ok=False,
            error="模型返回空内容",
            elapsed=time.time() - started,
            bytes_in=len(image_bytes),
            bytes_out=len(payload_bytes),
            mime=mime,
        )

    return VisionResult(
        description=content,
        ok=True,
        elapsed=time.time() - started,
        bytes_in=len(image_bytes),
        bytes_out=len(payload_bytes),
        mime=mime,
    )


def wrap_for_main_model(description: str) -> str:
    """把图片描述包装成主模型能理解的一段文本。"""
    return f"【对方发来一张图片，画面内容是】\n{description}"


# 「主模型没看清 / 认不出」的典型说法。命中就再去问一次百度识图。
_UNSURE_MARKERS = (
    "认不出",
    "认不出来",
    "不认识",
    "不确定",
    "无法确定",
    "无法辨认",
    "看不清",
    "看不清楚",
    "无法识别",
    "不能识别",
    "没能看清",
    "不知道这是",
)


def looks_unsure(description: str) -> bool:
    """判断视觉模型的描述是不是「没认出来」。"""
    text = (description or "").strip()
    if not text or text == FALLBACK:
        return True
    return any(marker in text for marker in _UNSURE_MARKERS)


# ---------------------------------------------------------------- 主体识别（溯源第一步）
# 注意：要认的不只是「某部作品里的角色」——真人、地点、建筑、商品品牌、
# 影视/游戏作品、动植物、梗图来源都算。提示词和字段都要保持通用。
IDENTIFY_SYSTEM = """你是图像识别器，判断图里的**主体**是什么。

主体可能是：
- 人物：动漫/游戏角色、真人（演员、运动员、公众人物）、coser
- 地点：地标、建筑、城市、景点、店铺、房间
- 物体：商品、品牌/logo、车型、机型、食物、动植物
- 作品：影视、番剧、游戏、漫画的截图或海报
- 其他：梗图出处、新闻现场、艺术作品

规则：
1. 只有百分百确定才给名字；格式尽量写成「主体名（所属作品/品牌/地点）」；
2. 只是风格像、配色像、感觉像 —— 一律算不确定，name 留空；
3. 严禁猜测，严禁编造出处。

只输出 JSON，不要任何解释：
{"name": "<主体名字，不确定就留空>",
 "category": "character|person|place|object|brand|work|plant|animal|other|unknown",
 "confidence": "high|medium|low|none"}

confidence：high=确定；medium=很像但没把握；low=只有模糊印象；none=不知道。"""

IDENTIFY_QUESTION = "图里的主体是什么？如果是人物/地点/商品/作品，说出它的名字或出处。"


@dataclass
class IdentifyResult:
    ok: bool = False
    name: str = ""
    category: str = "unknown"
    confidence: str = "none"
    raw: str = ""
    error: str = ""
    model_used: str = ""
    elapsed: float = 0.0

    @property
    def confident(self) -> bool:
        return self.ok and self.confidence == "high" and bool(self.name)


CATEGORY_LABELS = {
    "character": "动漫/游戏角色",
    "person": "真人",
    "place": "地点",
    "object": "物体",
    "brand": "品牌/商品",
    "work": "作品",
    "plant": "植物",
    "animal": "动物",
    "other": "其他",
    "unknown": "",
}


def _parse_identify(text: str) -> tuple[str, str, str]:
    """从模型输出里抠出 (name, confidence, category)。"""
    import json as _json
    import re as _re

    t = (text or "").strip()
    if not t:
        return "", "none", "unknown"
    if t.startswith("```"):
        t = _re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = _re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = _json.loads(t[start : end + 1])
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            name = str(obj.get("name") or "").strip()
            conf = str(obj.get("confidence") or "").strip().lower()
            cat = str(obj.get("category") or "").strip().lower() or "unknown"
            if conf not in ("high", "medium", "low", "none"):
                conf = "medium" if name else "none"
            if cat not in CATEGORY_LABELS:
                cat = "other"
            return name, (conf if name else "none"), (cat if name else "unknown")
    # 不是 JSON：短句且不是「不知道」就当中等置信的名字
    if len(t) <= 40 and not any(k in t for k in ("不确定", "不知道", "认不出", "无法")):
        return t, "medium", "unknown"
    return "", "none", "unknown"


# 视觉中转（8317）最近经常 502。挂了就先冷 5 分钟，别再每张图都白等一次。
_relay_down_until = 0.0


async def identify_image(
    image_bytes: bytes,
    api_key: str,
    model: str = DEFAULT_VISION_MODEL,
    gemini_key: str = "",
    gemini_model: str = "",
    gemini_base: str = "",
    fallback_model: str = FALLBACK_VISION_MODEL,
    timeout: float = 15.0,
    budget: float = 25.0,
    context: str = "",
) -> IdentifyResult:
    """先让普通视觉模型自己认，作为溯源链的第一层。

    顺序：Gemini 中转 → GLM 主力 → GLM 备用；
    整体最多花 budget 秒，超了就直接返回（交给搜索/Google 兜底），别拖住聊天。
    返回 name/confidence；confidence=high 才认为「拿得准」。
    """
    started = time.time()
    if not image_bytes:
        return IdentifyResult(ok=False, error="空图片")

    global _relay_down_until
    layers: list[tuple[str, str, str]] = []
    if (
        gemini_key
        and gemini_model
        and gemini_base
        and time.time() >= _relay_down_until
    ):
        layers.append((gemini_model, gemini_key, gemini_base))
    if api_key:
        layers.append((model, api_key, GLM_BASE))
        if fallback_model and fallback_model != model:
            layers.append((fallback_model, api_key, GLM_BASE))

    last_err = ""
    for layer_model, layer_key, layer_base in layers:
        left = budget - (time.time() - started)
        if left <= 2.0:
            last_err = last_err or "识别超时"
            break
        res = await _describe_once(
            image_bytes,
            layer_key,
            layer_model,
            timeout=min(timeout, left),
            context=context or IDENTIFY_QUESTION,
            base_url=layer_base,
            system_prompt=IDENTIFY_SYSTEM,
            json_mode=True,
        )
        if not res.ok:
            last_err = res.error
            if layer_base == gemini_base and ("502" in res.error or "500" in res.error):
                _relay_down_until = time.time() + 300
                logger.warning("vision: 视觉中转 5xx，暂停 5 分钟，改用 GLM")
            continue
        name, conf, cat = _parse_identify(res.description)
        if not name and conf == "none":
            # 这层明确说不知道，换下一层再试（不同模型见过的东西不一样）
            last_err = "模型表示不认识"
            continue
        return IdentifyResult(
            ok=bool(name),
            name=name,
            category=cat,
            confidence=conf,
            raw=res.description[:300],
            model_used=layer_model,
            elapsed=time.time() - started,
        )

    return IdentifyResult(
        ok=False, error=last_err or "没有可用的视觉模型", elapsed=time.time() - started
    )


async def describe_image(
    image_bytes: bytes,
    api_key: str,
    model: str = DEFAULT_VISION_MODEL,
    timeout: float = 60.0,
    context: str = "",
    fallback_model: str = FALLBACK_VISION_MODEL,
    gemini_key: str = "",
    gemini_model: str = "",
    gemini_base: str = "",
    history: list[dict] | None = None,
    baidu_ak: str = "",
    baidu_sk: str = "",
    baidu_ocr: bool = False,
    baidu_qianfan_key: str = "",
    baidu_qianfan_model: str = "",
    baidu_mode: str = "",
) -> VisionResult:
    """识图。降级链：Gemini → GLM 主模型 → GLM 备用 → 百度图像识别。

    每一层都用 OpenAI 兼容协议，区别只在 base_url / key / model。
    Gemini 那层如果没配（gemini_key 为空）就跳过，完全退回 GLM。
    最后如果全挂了、或者描述里明说「认不出」，再用百度通用物体识别补线索。
    """
    global _primary_down_until

    started = time.time()

    async def _finish(res: VisionResult) -> VisionResult:
        """收尾：**只有客观提取请求**才用百度补一层。

        调用方通过 baidu_mode 明确表态：
          "ocr"     —— 用户只要图里的文字；
          "objects" —— 用户只问图里有什么（客观清单）；
          ""        —— 需要理解/判断/引申，百度一律不参与。
        """
        res.elapsed = time.time() - started
        if not baidu_mode:
            return res
        if not ((baidu_ak and baidu_sk) or baidu_qianfan_key):
            return res
        # 客观清单模式下，主视觉模型已经给出明确描述就不必再问百度
        if baidu_mode == "objects" and res.ok and not looks_unsure(res.description):
            return res
        try:
            from . import baidu_vision

            hint = await baidu_vision.describe(
                image_bytes,
                baidu_ak,
                baidu_sk,
                want_ocr=baidu_ocr,
                timeout=12.0,
                qianfan_key=baidu_qianfan_key,
                qianfan_model=baidu_qianfan_model or baidu_vision.DEFAULT_QIANFAN_MODEL,
                question=context,
                mode=baidu_mode,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"vision: 百度图像识别异常 {exc!r}")
            return res
        if not hint.ok:
            logger.info(f"vision: 百度图像识别未命中（{hint.error[:120]}）")
            return res
        logger.info(
            f"vision: 百度图像识别补充 ok={res.ok} kind={hint.kind} "
            f"{hint.elapsed:.1f}s >>> {hint.text[:200]}"
        )
        if res.ok:
            res.description = f"{res.description}\n\n（百度识图补充：{hint.text}）"
            res.model_used = f"{res.model_used}+baidu" if res.model_used else "baidu"
        else:
            res.description = f"【百度图像识别】\n{hint.text}"
            res.ok = True
            res.model_used = "baidu-general"
        res.elapsed = time.time() - started
        return res

    # ① 首选：Gemini 中转（质量最好）
    # Argon 服务端偶发 500，重试一次通常就过；两次都不行才降级。
    if gemini_key and gemini_model and gemini_base:
        last_err = ""
        for attempt in range(2):
            res = await _describe_once(
                image_bytes, gemini_key, gemini_model, timeout, context, gemini_base, history
            )
            res.model_used = gemini_model
            if res.ok:
                return await _finish(res)
            last_err = res.error
            if attempt == 0:
                logger.warning(
                    f"vision: Gemini {gemini_model} 第1次失败（{last_err[:120]}），重试一次"
                )
                await asyncio.sleep(1.0)
        logger.warning(
            f"vision: Gemini {gemini_model} 两次都失败（{last_err[:120]}），降级到 GLM"
        )

    # ② 次选：GLM 主模型（主力刚挂过就直接跳备用）
    use_model = (
        fallback_model
        if (fallback_model and time.time() < _primary_down_until)
        else model
    )
    res = await _describe_once(
        image_bytes, api_key, use_model, timeout, context, GLM_BASE, history
    )
    res.model_used = use_model
    if res.ok:
        return await _finish(res)

    # ③ 最后：GLM 备用
    if fallback_model and fallback_model != use_model:
        logger.warning(
            f"vision: {use_model} 失败（{res.error[:120]}），"
            f"降级到 {fallback_model}，接下来 {int(_PRIMARY_COOLDOWN / 60)} 分钟都用备用"
        )
        _primary_down_until = time.time() + _PRIMARY_COOLDOWN
        alt = await _describe_once(
            image_bytes, api_key, fallback_model, timeout, context, GLM_BASE, history
        )
        alt.model_used = fallback_model
        alt.elapsed = time.time() - started
        if alt.ok:
            return await _finish(alt)
        alt.error = f"{use_model} 失败：{res.error[:100]} / {fallback_model} 也失败：{alt.error[:100]}"
        return await _finish(alt)

    return await _finish(res)
