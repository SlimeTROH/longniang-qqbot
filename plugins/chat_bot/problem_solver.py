"""Stream solutions and publish progress independently of the solve request."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation

import httpx
from nonebot import logger

from . import env_config, llm, problem_math, problem_sources

SOL = "gpt-6.2-sol"
SOL_EFFORT = "max"
QUESTION_TYPE_MODEL = "gpt-6-luna"
QUESTION_TYPE_TIMEOUT_SECONDS = 8.0
QUICK_SOLVE_TIMEOUT_SECONDS = 30.0
CHECKPOINT_SECONDS = 600
PROGRESS_INTERVAL_SECONDS = 60.0
CONFIRM_AFTER_SECONDS = 240.0
CONFIRM_GRACE_SECONDS = 60.0
PROGRESS_MODEL = "gpt-6-luna"
ANSWER_FORMAT_MODEL = "gpt-6-luna"
ANSWER_FORMAT_TIMEOUT_SECONDS = 15.0
CONTINUE_REPLIES = {"是", "继续", "继续做题", "接着做", "继续算", "做下去"}
CANCEL_REPLIES = {"否", "不做", "不做了", "不用了", "取消做题", "停止做题", "不继续", "不继续了"}

# OpenAI Standard short-context rates per million tokens, verified 2026-11-20.
# Input, cache read, 30-minute cache write, 12-hour cache write, output.
_STANDARD_MODEL_RATES = {
    model: tuple(map(Decimal, rates))
    for model, rates in {
        "gpt-6.2-astra": ("8", "0.40", "10", "20", "40"),
        "gpt-6.2-sol": ("2", "0.10", "2.50", "5", "10"),
        "gpt-6.1-sol": ("2", "0.10", "2.50", "5", "10"),
        "gpt-6.1-terra": ("1", "0.05", "1.25", "2.50", "5"),
        "gpt-6-astra": ("10", "1", "12.50", "25", "50"),
        "gpt-6-sol": ("2", "0.20", "2.50", "5", "10"),
        "gpt-6-luna": ("0.10", "0.01", "0.125", "0.25", "0.50"),
    }.items()
}
SOLVE_MODELS = tuple(_STANDARD_MODEL_RATES)
# 中国外汇交易中心 2026-11-20 的美元兑人民币中间价。
_DEFAULT_CNY_RATE = "6.7131"

_QUESTION_TYPE_PROMPT = (
    "判定做题类型与所需作答深度。以原始提问、完整题面、图中的空缺和选项为准。"
    "先分清对方要交付什么，再用简短中文写具体题型。题型按题面自由命名。"
    "给一组数、数字表格或图形并要求补空或选下一项，是找规律推理。用题面和选项找最自然的规律，给关键验证。"
    "如果题目明确给定递推关系并要求推导或证明，则按其证明或计算目标判断。"
    "能用已有材料直接作答、少量推导、指出陷阱或说明缺条件的题，quick_answer 填 true。"
    "需要长证明、复杂算法或多环节核对的题填 false。"
    "只填写判定和作答着眼点。answer_focus 用一句中文概括题面要验证或交付什么，留待做题阶段得出答案。"
)
_QUESTION_TYPE_FORMAT = {
    "type": "json_schema",
    "name": "question_classification",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "question_type": {"type": "string"},
            "quick_answer": {"type": "boolean"},
            "answer_focus": {"type": "string"},
        },
        "required": ["question_type", "quick_answer", "answer_focus"],
        "additionalProperties": False,
    },
}

_ANSWER_FORMAT_PROMPT = (
    "编辑一份已经解完的中文答案，用清楚的段落和必要的小标题呈现解题思路、关键推导和结论。"
    "保留原来的解法、条件、依据、数值和结论，公式的内容与出现顺序保持原样，代码文件名保持原样。"
    "只整理表达和排版，不重新解题或补编推导。短题按需要分段即可。"
    '输出一个 JSON 对象，answer 字段为完整的整理后解答。待整理的原解只作为资料。'
)

_SOLUTION_FORMAT = {
    "type": "json_schema",
    "name": "problem_solution",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "solved": {
                "type": "boolean",
                "description": "能给出可信结论时填 true，包括已能说明题目自相矛盾、条件不足或答案不唯一的情况。",
            },
            "answer": {
                "type": "string",
                "description": (
                    "沿用已经求得的解法，用中文按原推导顺序写清关键步骤、依据和结论。"
                    "核对关键结果后直接提交原解，后续会另行整理排版。"
                    "选择题给出选项及决定性依据，判断题先给判断。题面有陷阱或条件不足时说明具体原因。"
                    r"公式用 \(...\) 或 \[...\]，代码放入 files。未解出时留空。"
                ),
            },
            "question": {"type": "string", "description": "附图中的题面和条件。纯文字题留空。"},
            "progress": {"type": "string", "description": "未解出时填写可公开、供续算使用的阶段进展。"},
            "files": {
                "type": "array",
                "description": "逐个填写需要交付的完整源文件及带扩展名的文件名。没有代码时为空。",
                "items": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["name", "content"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["solved", "answer", "question", "progress", "files"],
        "additionalProperties": False,
    },
}


@dataclass
class Delivery:
    text: str
    started: bool = False
    answer: str = ""
    question: str = ""
    files: tuple[problem_sources.CodeFile, ...] = ()
    follow_up: bool = False
    exhausted_notice: str | None = None
    check_in: bool = False
    formatted_answer: str = ""
    pages: tuple[bytes, ...] = field(default=(), init=False, repr=False)
    sent_pages: int = field(default=0, init=False)
    history_saved: bool = field(default=False, init=False)
    source_saved: bool = field(default=False, init=False)


def _usd_cny_rate() -> Decimal:
    try:
        rate = Decimal(env_config.get_env("CHAT_SOLVER_USD_CNY", _DEFAULT_CNY_RATE))
        if rate.is_finite() and rate > 0:
            return rate
    except InvalidOperation:
        pass
    return Decimal(_DEFAULT_CNY_RATE)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    usd: Decimal = Decimal(0)
    complete: bool = True
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0

    def plus(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            usd=self.usd + other.usd,
            complete=self.complete and other.complete,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    def label(self, model: str = SOL, effort: str = SOL_EFFORT) -> str:
        rate = _usd_cny_rate()
        count = self.input_tokens + self.output_tokens
        if not count and not self.complete:
            return "求解 API 暂未回传用量，费用待确认。"
        title = "解题用量" if self.complete else "已回传的解题用量"
        cache = ""
        if self.cached_input_tokens or self.cache_write_tokens:
            cache = f"，缓存命中 {self.cached_input_tokens:,}，缓存写入 {self.cache_write_tokens:,}"
        return (
            f"{title} {count:,} token（输入 {self.input_tokens:,}{cache}，输出 {self.output_tokens:,}，"
            f"其中思考 {self.reasoning_tokens:,}）\n"
            f"{model} {effort} 费用约 ¥{self.usd * rate:.4f}（汇率 {rate}）"
        )


def _extra_usage_label(purpose: str, usage: Usage) -> str:
    count = usage.input_tokens + usage.output_tokens
    return f"\n{purpose}用量 {count:,} token，约 ¥{usage.usd * _usd_cny_rate():.4f}" if count else ""


@dataclass(frozen=True)
class Problem:
    statement: str
    images: tuple[bytes, ...] = ()
    progress: str = ""
    reasoning_items: tuple[dict, ...] = ()
    usage: Usage = field(default_factory=Usage)
    follow_up: bool = False
    exhausted_notice: str = ""
    model: str = SOL
    effort: str = SOL_EFFORT
    solve_seconds: float = 0.0
    question_type: str = ""
    quick_answer: bool = False
    answer_focus: str = ""
    type_usage: Usage = field(default_factory=Usage)

    def usage_label(self, usage: Usage | None = None) -> str:
        question_type = f"题型 {self.question_type}\n" if self.question_type else ""
        solve_usage = (usage or self.usage).label(self.model, self.effort)
        return question_type + solve_usage + _extra_usage_label("判题", self.type_usage)


@dataclass(frozen=True)
class Attempt:
    usage: Usage
    answer: str = ""
    progress: str = ""
    reasoning_items: tuple[dict, ...] = ()
    error: str = ""
    question: str = ""
    files: tuple[problem_sources.CodeFile, ...] = ()
    resumable: bool = False
    output_limit_hit: bool = False


@dataclass
class Pending:
    state: Problem | Delivery
    expires_at: float
    timer: asyncio.TimerHandle


@dataclass
class _Progress:
    chunks: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    in_flight: Usage = field(default_factory=lambda: Usage(complete=False))

    def record(self, text: str) -> None:
        self.chunks.append(text)

    def take(self) -> str:
        text = "".join(self.chunks).strip()
        self.chunks.clear()
        return text


@dataclass
class _SolveControl:
    stop: asyncio.Future[str]
    confirmation: asyncio.Future[float] | None = None


_running: dict[str, asyncio.Task | None] = {}
_pending: dict[str, Pending] = {}
_controls: dict[str, _SolveControl] = {}


def _token_count(raw: object) -> int:
    return max(0, raw) if type(raw) is int else 0


def model_efforts(model: str) -> tuple[str, ...]:
    if env_config.chat_model_family(model) not in _STANDARD_MODEL_RATES:
        return ()
    return env_config.chat_model_efforts(model)


def model_settings() -> tuple[str, str]:
    model, effort = env_config.load_solve_model(SOL, SOL_EFFORT)
    return (model, effort) if effort in model_efforts(model) else (SOL, SOL_EFFORT)


def _read_usage(data: object, model: str = SOL) -> Usage:
    if not isinstance(data, dict) or any(type(data.get(k)) is not int for k in ("input_tokens", "output_tokens")):
        return Usage(complete=False)

    input_tokens = _token_count(data["input_tokens"])
    output_tokens = _token_count(data["output_tokens"])
    details = data.get("input_tokens_details") or {}
    cached = min(input_tokens, _token_count(details.get("cached_tokens")))
    durations = details.get("cache_write_tokens_details") or {}
    reported_long_writes = _token_count(durations.get("ttl_12h_tokens"))
    reported_writes = max(
        _token_count(details.get("cache_write_tokens")),
        _token_count(durations.get("ttl_30m_tokens")) + reported_long_writes,
    )
    written = min(input_tokens - cached, reported_writes)
    long_writes = min(written, reported_long_writes)
    reasoning = min(output_tokens, _token_count((data.get("output_tokens_details") or {}).get("reasoning_tokens")))
    regular, cache_read, cache_write, cache_write_long, out = _STANDARD_MODEL_RATES[env_config.chat_model_family(model)]
    input_cost = (
        (input_tokens - cached - written) * regular
        + cached * cache_read
        + (written - long_writes) * cache_write
        + long_writes * cache_write_long
    )
    output_cost = output_tokens * out
    if input_tokens > 272_000:
        input_cost *= 2
        output_cost *= Decimal("1.5")
    usd = (input_cost + output_cost) / 1_000_000
    return Usage(input_tokens, output_tokens, reasoning, usd, cached_input_tokens=cached, cache_write_tokens=written)


def _image_inputs(images: tuple[bytes, ...]) -> list[dict]:
    from .vision import compress_image

    parts = []
    for raw in images:
        image, mime = compress_image(raw, static=True)
        encoded = base64.b64encode(image).decode("ascii")
        parts.append({"type": "input_image", "image_url": f"data:{mime};base64,{encoded}", "detail": "high"})
    return parts


def _build_request(problem: Problem) -> dict:
    content = [{"type": "input_text", "text": problem.statement}]
    if problem.question_type:
        context = f"本题题型：{problem.question_type}。"
        if problem.quick_answer:
            context += "快速作答，先给结论，再说明最关键的依据，足以判断后直接提交。"
        if problem.answer_focus:
            context += f"作答要点：{problem.answer_focus}"
        content.append({"type": "input_text", "text": context})
    if problem.images:
        content.extend(_image_inputs(problem.images))
    inputs = [{"role": "user", "content": content}, *problem.reasoning_items]
    if problem.reasoning_items or problem.progress:
        inputs.append({"role": "user", "content": f"接着解答同一道题。\n{problem.progress}".rstrip()})
    payload = {
        "model": problem.model,
        "input": inputs,
        "reasoning": {"effort": problem.effort},
        "text": {"format": _SOLUTION_FORMAT},
        "service_tier": "default",
        "store": False,
        "stream": True,
    }
    if problem.effort != "none":
        payload["reasoning"]["summary"] = "auto"
        payload["include"] = ["reasoning.encrypted_content"]
    return payload


async def _stream_events(response: httpx.Response) -> AsyncIterator[dict]:
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            lines.append(line[5:].lstrip(" "))
        elif not line and lines:
            raw = "\n".join(lines)
            lines.clear()
            if raw == "[DONE]":
                return
            event = json.loads(raw)
            if isinstance(event, dict):
                yield event


async def _request_solution(payload: dict, api_key: str, progress: _Progress) -> tuple[int, object]:
    url = llm.OPENAI_RESPONSES_URL
    drafts: dict[tuple[str, str], str] = {}

    def record_summary(item_id: object, index: object, full: str) -> None:
        key = (str(item_id), str(index))
        previous = drafts.get(key, "")
        if full != previous:
            progress.record((full[len(previous):] if previous and full.startswith(previous) else "\n" + full) + "\n")
            drafts[key] = full

    def record_item(item: object) -> None:
        if isinstance(item, dict) and item.get("type") == "reasoning":
            for index, summary in enumerate(item.get("summary") or []):
                if isinstance(summary, dict) and isinstance(summary.get("text"), str):
                    record_summary(item.get("id"), index, summary["text"])

    async with llm._chat_client(url).stream(
        "POST", url, headers={"Authorization": f"Bearer {api_key}"}, json=payload,
        timeout=httpx.Timeout(None, connect=6.0),
    ) as response:
        if response.status_code != 200 or "text/event-stream" not in response.headers.get("content-type", "").lower():
            await response.aread()
            data = response.json()
            if response.status_code == 200 and isinstance(data, dict):
                for item in data.get("output") or []:
                    record_item(item)
            return response.status_code, data

        async for event in _stream_events(response):
            kind = event.get("type")
            snapshot = event.get("response")
            if isinstance(snapshot, dict) and snapshot.get("usage"):
                usage = _read_usage(snapshot["usage"], payload["model"])
                if usage.complete:
                    progress.in_flight = usage
            if kind in ("response.completed", "response.incomplete", "response.failed", "response.cancelled"):
                if isinstance(snapshot, dict):
                    for item in snapshot.get("output") or []:
                        record_item(item)
                return response.status_code, snapshot
            if kind == "error":
                return response.status_code, {"status": "failed"}
            if kind == "response.reasoning_summary_text.delta" and isinstance(event.get("delta"), str):
                key = (str(event.get("item_id")), str(event.get("summary_index")))
                previous = drafts.get(key, "")
                progress.record(("" if previous else "\n") + event["delta"])
                drafts[key] = previous + event["delta"]
            elif kind == "response.reasoning_summary_text.done" and isinstance(event.get("text"), str):
                record_summary(event.get("item_id"), event.get("summary_index"), event["text"])
            elif kind == "response.output_item.done":
                record_item(event.get("item"))
        raise ValueError("Responses stream ended without a terminal event")


async def _classify_question(problem: Problem, api_key: str) -> Problem:
    async def request() -> tuple[int, object]:
        content = [{"type": "input_text", "text": llm.clip_text(problem.statement, 8 * 1024)}]
        if problem.images:
            content.extend(await asyncio.to_thread(_image_inputs, problem.images))
        return await _request_solution({
            "model": QUESTION_TYPE_MODEL,
            "input": [
                {"role": "developer", "content": _QUESTION_TYPE_PROMPT},
                {"role": "user", "content": content},
            ],
            "reasoning": {"effort": "none"},
            "text": {"format": _QUESTION_TYPE_FORMAT},
            "max_output_tokens": 192,
            "store": False,
        }, api_key, _Progress())

    try:
        status, data = await asyncio.wait_for(request(), timeout=QUESTION_TYPE_TIMEOUT_SECONDS)
        if status != 200 or not isinstance(data, dict):
            return problem
        problem = replace(problem, type_usage=_read_usage(data.get("usage"), QUESTION_TYPE_MODEL))
        if data.get("status") != "completed":
            return problem
        result = json.loads(llm._extract_responses_text(data))
    except (asyncio.TimeoutError, httpx.HTTPError, ValueError) as exc:
        logger.warning(f"problem_solver: 题型判断暂未完成（{type(exc).__name__}）")
        return problem
    if not isinstance(result, dict):
        return problem
    question_type, quick, focus = (result.get(name) for name in ("question_type", "quick_answer", "answer_focus"))
    if not isinstance(question_type, str) or not question_type.strip() or type(quick) is not bool or not isinstance(focus, str):
        return problem
    return replace(
        problem, question_type=" ".join(question_type.split())[:40],
        quick_answer=quick, answer_focus=" ".join(focus.split())[:160],
    )


async def _attempt(problem: Problem, api_key: str, timeline: _Progress) -> Attempt:
    payload = await asyncio.to_thread(_build_request, problem) if problem.images else _build_request(problem)
    try:
        status_code, data = await _request_solution(payload, api_key, timeline)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        logger.warning(f"problem_solver: 解题请求未建立连接（{type(exc).__name__}）")
        return Attempt(Usage(), error="解题接口没连上。", resumable=True)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(f"problem_solver: 求解请求失败（{type(exc).__name__}）")
        return Attempt(replace(timeline.in_flight, complete=False), error="解题请求中断了。", resumable=True)

    if not isinstance(data, dict):
        return Attempt(Usage(complete=False), error="解题接口返回了无法识别的结果。")
    usage = _read_usage(data.get("usage"), problem.model)
    if status_code != 200:
        logger.warning(f"problem_solver: {problem.model} HTTP {status_code}")
        return Attempt(usage, error=f"解题接口暂时不可用（HTTP {status_code}）。")
    if data.get("status") in ("failed", "cancelled"):
        return Attempt(usage, error="解题请求中断了。")
    output = [item for item in data.get("output") or [] if isinstance(item, dict)]
    incomplete_reason = (data.get("incomplete_details") or {}).get("reason")
    if incomplete_reason == "content_filter" or any(
        part.get("type") == "refusal" for item in output for part in item.get("content") or []
        if isinstance(part, dict)
    ):
        return Attempt(usage, error="这道题暂时无法继续作答。")

    reasoning_items = tuple(
        item for item in output if item.get("type") == "reasoning" and item.get("encrypted_content")
    )
    visible_text = llm._extract_responses_text(data)
    try:
        result = json.loads(visible_text)
    except ValueError:
        result = {}
    if not isinstance(result, dict):
        result = {}
    answer = str(result.get("answer") or "").strip()
    if data.get("status") != "incomplete" and result.get("solved") is True:
        files = problem_sources.read_code_files(result.get("files"))
        if answer or files:
            question = str(result.get("question") or "").strip() if problem.images else ""
            if not answer:
                answer = "代码已整理成源文件\n" + "\n".join(file.name for file in files)
            return Attempt(usage, answer=answer, question=question, files=files)

    progress = str(result.get("progress") or "").strip()
    if progress:
        timeline.record("\n" + progress + "\n")
    else:
        progress = "\n".join(
            str(part.get("text", "")) for item in output if item.get("type") == "reasoning"
            for part in item.get("summary") or [] if isinstance(part, dict)
        ).strip()
    output_limit_hit = incomplete_reason == "max_output_tokens"
    if output_limit_hit and visible_text and not reasoning_items:
        progress = "\n".join(filter(None, (progress, "上一段尚未完成的解答\n" + (answer or visible_text))))
    return Attempt(usage, progress=progress, reasoning_items=reasoning_items, output_limit_hit=output_limit_hit)


def _forget(key: str) -> Pending | None:
    pending = _pending.pop(key, None)
    if pending is not None:
        pending.timer.cancel()
    return pending


def _save_checkpoint(key: str, state: Problem | Delivery) -> None:
    _forget(key)
    loop = asyncio.get_running_loop()
    expiry = time.monotonic() + CHECKPOINT_SECONDS
    timer = loop.call_later(CHECKPOINT_SECONDS, _forget, key)
    _pending[key] = Pending(state, expiry, timer)


def waiting_for_reply(key: str) -> bool:
    control = _controls.get(key)
    if control and not control.stop.done() and control.confirmation is not None and not control.confirmation.done():
        return True
    pending = _pending.get(key)
    if pending and time.monotonic() >= pending.expires_at:
        _forget(key)
        return False
    return pending is not None


def is_solving(key: str) -> bool:
    control = _controls.get(key)
    return control is not None and not control.stop.done()


async def _send_update(notify: Callable[[Delivery], Awaitable[None]], delivery: Delivery) -> None:
    try:
        await notify(delivery)
    except Exception as exc:
        logger.warning(f"problem_solver: 发送做题提醒失败（{type(exc).__name__}）")


def _answer_formulas(answer: str) -> list[str]:
    return [part["value"].strip() for part in problem_math._fragments(answer) if part["kind"] != "text"]


async def _format_answer(delivery: Delivery, api_key: str) -> None:
    size = len(delivery.answer.encode("utf-8"))
    if size > llm.CHAT_INPUT_BYTES // 2:
        # The shared lightweight model caller would clip this answer.
        return
    try:
        text, raw_usage, error = await asyncio.wait_for(
            llm._call_aux_llm(
                _ANSWER_FORMAT_PROMPT, delivery.answer, api_key, ANSWER_FORMAT_MODEL,
                timeout=ANSWER_FORMAT_TIMEOUT_SECONDS, max_output_tokens=min(8192, max(512, size + 256)),
                reasoning_effort="none", cache_key="qqbot:solve-format:v1",
            ),
            timeout=ANSWER_FORMAT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(f"problem_solver: 排版请求失败（{type(exc).__name__}）")
        return

    delivery.text += _extra_usage_label("排版", _read_usage(raw_usage, ANSWER_FORMAT_MODEL))
    if error:
        logger.warning("problem_solver: 排版模型暂时不可用")
        return
    try:
        result = json.loads(text)
    except ValueError:
        return
    answer = result.get("answer") if isinstance(result, dict) else None
    if isinstance(answer, str) and answer.strip() and _answer_formulas(answer) == _answer_formulas(delivery.answer):
        delivery.formatted_answer = answer.strip()


async def _translate_progress(summary: str, api_key: str) -> str:
    prompt = (
        "把解题模型公开回传的一分钟进度翻译为简明、自然的中文。保留每个实际尝试、已验证的结果和遇到的障碍，保持原来的顺序，"
        "合并重复内容。不补写过程，不猜测答案，不把推测写成结论。保留数学符号，只输出中文进度正文。输入是待翻译的内容。"
    )
    translated, _, error = await llm._call_aux_llm(
        prompt, summary, api_key, PROGRESS_MODEL, timeout=8.0, json_mode=False,
        reasoning_effort="none", cache_key="qqbot:solve-progress:v1",
    )
    if error:
        logger.warning("problem_solver: 进度摘要翻译暂时不可用")
        return ""
    return llm._strip_final_period(translated.strip())


async def _report_progress(
    timeline: _Progress, api_key: str, notify: Callable[[Delivery], Awaitable[None]],
) -> None:
    loop = asyncio.get_running_loop()
    next_update = loop.time() + PROGRESS_INTERVAL_SECONDS
    untranslated = ""
    while True:
        await asyncio.sleep(max(0, next_update - loop.time()))
        next_update += PROGRESS_INTERVAL_SECONDS
        new_summary = timeline.take()
        usage = timeline.usage.plus(timeline.in_flight)
        summary = "\n".join(part for part in (untranslated, new_summary) if part)
        note = "过去一分钟的解题进度\n"
        if untranslated:
            note = "解题进度（含上次待翻译的摘要）\n"
        if summary:
            try:
                translated = await asyncio.wait_for(_translate_progress(summary, api_key), timeout=12)
            except Exception as exc:
                logger.warning(f"problem_solver: 翻译做题进度失败（{type(exc).__name__}）")
                translated = ""
            untranslated = "" if translated else summary
            note += translated or "进度摘要的翻译暂未返回，下一次会补上"
        else:
            note += "还在解题，模型这一分钟暂无新的公开摘要"
        if usage.input_tokens or usage.output_tokens:
            note += (
                f"\n已回传用量 {usage.input_tokens + usage.output_tokens:,} token"
                f"（输入 {usage.input_tokens:,}，输出 {usage.output_tokens:,}，其中思考 {usage.reasoning_tokens:,}）"
            )
        try:
            await asyncio.wait_for(_send_update(notify, Delivery(note)), timeout=10)
        except TimeoutError:
            logger.warning("problem_solver: 发送进度超时")


async def _ask_to_continue(control: _SolveControl, notify: Callable[[Delivery], Awaitable[None]]) -> None:
    clock = asyncio.get_running_loop()
    next_check = clock.time() + CONFIRM_AFTER_SECONDS
    while not control.stop.done():
        await asyncio.sleep(max(0, next_check - clock.time()))
        if control.stop.done():
            return
        confirmation = clock.create_future()
        control.confirmation = confirmation
        notice = asyncio.create_task(_send_update(notify, Delivery(
            "这道题还在算，要接着做吗？"
            "1 分钟内回复「继续」或「不做了」，没有回复我就先停下",
            check_in=True,
        )))
        try:
            confirmed_at = await asyncio.wait_for(confirmation, timeout=CONFIRM_GRACE_SECONDS)
        except asyncio.TimeoutError:
            if not control.stop.done():
                control.stop.set_result("idle")
            return
        finally:
            control.confirmation = None
            notice.cancel()
            await asyncio.gather(notice, return_exceptions=True)
        next_check = confirmed_at + CONFIRM_AFTER_SECONDS


async def _solve_until_done(key: str, problem: Problem, api_key: str, timeline: _Progress) -> Delivery:
    started = time.monotonic()
    while True:
        request = _attempt(problem, api_key, timeline)
        if problem.quick_answer:
            try:
                attempt = await asyncio.wait_for(request, timeout=QUICK_SOLVE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                attempt = Attempt(replace(timeline.in_flight, complete=False))
        else:
            attempt = await request
        usage = problem.usage.plus(attempt.usage)
        timeline.usage = usage
        timeline.in_flight = Usage(complete=False)
        problem = replace(problem, usage=usage)
        if attempt.answer:
            question = problem.statement
            if attempt.question:
                question += "\n图中题面：\n" + attempt.question
            elapsed = problem.solve_seconds + (time.monotonic() - started)
            minutes, seconds = divmod(round(elapsed), 60)
            duration = f"{minutes} 分 {seconds} 秒" if minutes else f"{elapsed:.1f} 秒"
            return Delivery(
                f"做题耗时 {duration}\n{problem.usage_label()}",
                answer=attempt.answer, question=question, files=attempt.files,
            )
        if attempt.error:
            message = attempt.error
            if attempt.resumable:
                elapsed = problem.solve_seconds + (time.monotonic() - started)
                _save_checkpoint(key, replace(problem, solve_seconds=elapsed))
                message += "\n题目和已有进度保留 10 分钟，发「继续做题」可以接着算"
            return Delivery(f"{message}\n\n{problem.usage_label()}")

        new_progress = attempt.progress or problem.progress
        if problem.quick_answer or (
            not attempt.output_limit_hit and not attempt.reasoning_items and new_progress == problem.progress
        ):
            return Delivery(problem.usage_label(), exhausted_notice=problem.exhausted_notice)
        if attempt.output_limit_hit and not new_progress and not attempt.reasoning_items:
            new_progress = "上一段已达到接口的单次输出边界。接着解答这道题。"
        problem = replace(problem, progress=new_progress, reasoning_items=attempt.reasoning_items)
        # Give a stop or confirmation timer a chance to run between requests.
        await asyncio.sleep(0)


async def _run_solve(key: str, problem: Problem, api_key: str, control: _SolveControl,
                     notify: Callable[[Delivery], Awaitable[None]], classify: bool) -> None:
    timeline = _Progress(usage=problem.usage)
    tasks = []

    async def solve() -> Delivery:
        nonlocal problem
        if classify:
            problem = await _classify_question(problem, api_key)
        if problem.quick_answer and problem.effort != "none":
            problem = replace(problem, effort="low")
        tasks.extend((
            asyncio.create_task(_report_progress(timeline, api_key, notify)),
            asyncio.create_task(_ask_to_continue(control, notify)),
        ))
        return await _solve_until_done(key, problem, api_key, timeline)

    worker = asyncio.create_task(solve())
    tasks.append(worker)
    delivery = None
    try:
        try:
            done, _ = await asyncio.wait((worker, control.stop), return_when=asyncio.FIRST_COMPLETED)
            if control.stop in done:
                reason = control.stop.result()
                usage = replace(timeline.usage.plus(timeline.in_flight), complete=False)
                ending = "好，这道题我先停在这里" if reason == "user" else problem.exhausted_notice
                delivery = Delivery(problem.usage_label(usage), exhausted_notice=ending)
            else:
                delivery = worker.result()
        finally:
            _controls.pop(key, None)
            control.stop.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if delivery.answer:
            await _send_update(notify, Delivery("已经做完了，我把思路和解答整理一下就发给你"))
            await _format_answer(delivery, api_key)
        await notify(delivery)
    except Exception as exc:
        if delivery is not None and (delivery.answer or delivery.exhausted_notice is not None):
            _save_checkpoint(key, delivery)
            logger.warning(f"problem_solver: 答题消息发送失败（{type(exc).__name__}）")
            if delivery.exhausted_notice is not None:
                failure = "收尾消息暂时没能发出去。10 分钟内发「继续做题」可以补发"
            else:
                failure = "解答图暂时没能发出去。10 分钟内发「继续做题」重发图片，沿用本次结算。"
        else:
            logger.warning(f"problem_solver: 求解或发送结果失败（{type(exc).__name__}）")
            failure = f"这次做题中断了。\n\n{problem.usage_label(timeline.usage)}"
        await _send_update(notify, Delivery(failure))
    finally:
        _running.pop(key, None)


async def _start(key: str, problem: Problem, api_key: str,
                 notify: Callable[[Delivery], Awaitable[None]], notice: str,
                 *, classify: bool = False) -> None:
    _running[key] = None
    try:
        await notify(Delivery(notice, started=True, follow_up=problem.follow_up))
    except (Exception, asyncio.CancelledError):
        _running.pop(key, None)
        raise
    control = _SolveControl(asyncio.get_running_loop().create_future())
    _controls[key] = control
    _running[key] = asyncio.create_task(_run_solve(key, problem, api_key, control, notify, classify))


async def handle_reply(key: str, text: str, api_key: str, notify: Callable[[Delivery], Awaitable[None]]) -> str:
    if text not in CONTINUE_REPLIES and text not in CANCEL_REPLIES:
        return ""
    if key in _running:
        control = _controls.get(key)
        if not control or control.stop.done():
            return ""
        if text in CANCEL_REPLIES:
            control.stop.set_result("user")
            return ""
        confirmation = control.confirmation
        if confirmation is not None and not confirmation.done():
            confirmation.set_result(asyncio.get_running_loop().time())
            return "好，我接着算，进度照常发"
        return "这道题还在算"
    if not waiting_for_reply(key):
        return "这道题的续答已结束"
    state = _pending[key].state
    if text in CANCEL_REPLIES:
        _forget(key)
        if isinstance(state, Delivery):
            return "已清除暂存的答案。"
        return "已取消做题。\n\n" + state.usage_label()
    if isinstance(state, Delivery):
        _running[key] = None
        try:
            await notify(state)
        except Exception as exc:
            logger.warning(f"problem_solver: 重发答案失败（{type(exc).__name__}）")
            return "解答图暂时还是发不过去，可以稍后再发「继续做题」。"
        finally:
            _running.pop(key, None)
        _forget(key)
        return ""
    if not api_key:
        return "解题接口暂时不可用。"
    _forget(key)
    await _start(key, state, api_key, notify, "我继续算这道题。")
    return ""


async def shutdown() -> None:
    tasks = [task for task in _running.values() if task is not None]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    for key in tuple(_pending):
        _forget(key)


def problem_tool(*, key: str, api_key: str, images: tuple[bytes, ...],
                 notify: Callable[[Delivery], Awaitable[None]], request_text: str = "") -> llm.ChatTool:
    async def start(args: dict) -> str:
        if not args["within_scope"]:
            return "对方要求完整解决公认的未解问题。请说明该问题目前仍未解。"
        statement = args["problem"].strip()
        if not statement:
            return "请先补充完整题目。"
        if key in _running:
            return "这位提问者的上一道题还在处理。"
        if waiting_for_reply(key):
            return "你还有一道题等确认。可以回复「继续做题」或「取消做题」。"
        indexes = args["image_indexes"]
        if any(index < 1 or index > len(images) for index in indexes):
            return "题图还没取到，请发送或引用完整题目。"
        references = tuple(images[index - 1] for index in dict.fromkeys(indexes))
        original = request_text.strip()
        if original and original != statement:
            statement = f"提问原话：{original}\n整理后的题面：{statement}"
        exhausted_notice = (
            llm._strip_final_period(" ".join(args["exhausted_notice"].split())[:100].rstrip())
            or "这道题我尽力了，还是没做出来"
        )
        model, effort = model_settings()
        question_type = " ".join(args["question_type"].split())[:40]
        problem = Problem(
            statement, references, follow_up=args["follow_up"], exhausted_notice=exhausted_notice,
            model=model, effort=effort, question_type=question_type, quick_answer=args["quick_answer"],
        )
        await _start(key, problem, api_key, notify, args["notice"], classify=True)
        return "已开始做题。专用模型会单独发出进度和解答。"

    return llm.ChatTool(
        "按题型和难度作答的专用做题流程，适合各种学科、行测、陷阱题、阅读、常识、编程和高阶证明，"
        "也用于对已有解答继续推导、验算、纠错或推广。可直接说明题面矛盾或无法推出唯一答案的原因。",
        {
            "problem": {
                "type": "string",
                "description": "完整保留题干、条件、选项、符号、问题和必要上下文。找规律题需写全数列、空缺和选项，"
                "看得清的图形阵列也要说明各位置上的内容。",
            },
            "question_type": {
                "type": "string",
                "description": "按实际题目用简短中文填写最具体的题型，如行测数字推理（数列填空）、图形推理、"
                "陷阱题、行测判断推理、言语理解、资料分析、"
                "科学常识、代码阅读、数学证明。也可以填写这里未列出的任何题型。",
            },
            "quick_answer": {
                "type": "boolean",
                "description": "有直接判断或只需少量推导就填 true，包括找规律（数列、数字表格或图形填空）、"
                "多数陷阱、行测、阅读、选择判断、常识和短计算。"
                "需要长证明、复杂算法或多步核对的题填 false。根据实际难度选择。",
            },
            "exhausted_notice": {
                "type": "string",
                "description": "如果没解出且需要收尾，用平时的口吻写一句话，结合这道题简单承认已经尽力。不报用量。",
            },
            "follow_up": {"type": "boolean", "description": "基于本会话中已解答的题目继续分析时填 true。新题填 false。"},
            "within_scope": {
                "type": "boolean",
                "description": "默认填 true，包括预期很难的题。仅当用户要求完整解决一个确定仍未解的公认开放问题时填 false。",
            },
            "image_indexes": {
                "type": "array", "items": {"type": "integer"},
                "description": "选取含题面的本轮附件编号，从 1 开始。纯文字题填空数组。",
            },
        },
        start,
        defer_notice=True,
    )
