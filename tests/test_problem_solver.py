"""Regression coverage for the problem solver, with no billed API calls."""
from __future__ import annotations

import asyncio
import base64
import importlib
import io
import json
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.request import url2pathname
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

import httpx
from nonebot.adapters.onebot.v11 import ActionFailed, MessageSegment
from PIL import Image, ImageChops

from test_chat_group_settings import load_handlers

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
package = types.ModuleType("problem_solver_test")
package.__path__ = [str(ROOT / "plugins" / "chat_bot")]
sys.modules[package.__name__] = package
solver = importlib.import_module(f"{package.__name__}.problem_solver")
math_renderer = importlib.import_module(f"{package.__name__}.problem_math")
sources = importlib.import_module(f"{package.__name__}.problem_sources")


def response(*, output_tokens, solution=None, summary="", encrypted=False):
    output = []
    if solution is not None:
        output.append({"type": "message", "content": [{"type": "output_text", "text": json.dumps(solution)}]})
    if summary or encrypted:
        output.append({
            "type": "reasoning",
            "encrypted_content": "opaque-state",
            "summary": [{"type": "summary_text", "text": summary}] if summary else [],
        })
    return httpx.Response(200, json={
        "status": "completed" if solution is not None else "incomplete",
        "incomplete_details": None if solution is not None else {"reason": "max_output_tokens"},
        "output": output,
        "usage": {"input_tokens": 250, "output_tokens": output_tokens,
                  "output_tokens_details": {"reasoning_tokens": output_tokens - 100}},
    })


def sse_event(kind, **fields):
    return ("data: " + json.dumps({"type": kind, **fields}, ensure_ascii=False) + "\n\n").encode("utf-8")


class DelayedStream(httpx.AsyncByteStream):
    def __init__(self, events, finish, terminal):
        self.events = events
        self.finish = finish
        self.terminal = terminal
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        for event in self.events:
            yield event
        await self.finish.wait()
        yield self.terminal

    async def aclose(self):
        self.closed.set()


class SolverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.notify = AsyncMock()
        self.key = "bot:group_room_123"
        self.requests = []
        self.classification_requests = []
        self.request_timeouts = []
        exchange = patch.object(solver.env_config, "get_env", return_value="6.7131")
        exchange.start()
        self.addCleanup(exchange.stop)
        self.addCleanup(solver._forget, self.key)
        self.progress_model = AsyncMock(return_value=("", {}, "无公开进度"))
        self.format_model = AsyncMock(return_value=("", {}, ""))

        async def call_aux(*args, **kwargs):
            model = self.format_model if kwargs["cache_key"] == "qqbot:solve-format:v1" else self.progress_model
            return await model(*args, **kwargs)

        aux = patch.object(solver.llm, "_call_aux_llm", side_effect=call_aux)
        aux.start()
        self.addCleanup(aux.stop)

    async def connect_mock(self, *responses, classifications=()):
        def exchange(request):
            payload = json.loads(request.content)
            if payload["text"]["format"]["name"] == "question_classification":
                self.classification_requests.append(payload)
                return next(judgments, httpx.Response(503, json={}))
            self.requests.append(payload)
            self.request_timeouts.append(request.extensions.get("timeout", {}))
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        judgments = iter(classifications)
        replies = iter(responses)
        client = httpx.AsyncClient(transport=httpx.MockTransport(exchange))
        self.addAsyncCleanup(client.aclose)
        mock_client = patch.object(solver.llm, "_chat_client", return_value=client)
        mock_client.start()
        self.addCleanup(mock_client.stop)

    async def start(
        self, *, in_scope=True, follow_up=False, problem="求 x^2 在 [0, 1] 上的积分。",
        question_type="数学计算题", quick_answer=False, request_text="", images=(),
    ):
        tool = solver.problem_tool(key=self.key, api_key="mock", images=images, notify=self.notify, request_text=request_text)
        result = await solver.llm._execute_chat_tool(
            {
                "name": "solve_problem",
                "arguments": json.dumps({
                    "problem": problem,
                    "question_type": question_type,
                    "quick_answer": quick_answer,
                    "notice": "我来算一下。",
                    "exhausted_notice": "这道积分我尽力了，还是没做出来。",
                    "image_indexes": list(range(1, len(images) + 1)),
                    "within_scope": in_scope,
                    "follow_up": follow_up,
                }),
            },
            {"solve_problem": tool}, None, {},
        )
        task = solver._running.get(self.key)
        if task:
            await task
        return result

    async def test_first_request_has_no_application_cap_or_solver_prompt(self):
        await self.connect_mock(
            response(output_tokens=650, solution={"solved": True, "answer": r"积分是 \(\frac{1}{3}\)。", "progress": ""}),
        )
        self.assertIn("已开始", await self.start(follow_up=True))
        request, = self.requests
        self.assertEqual(request["model"], solver.SOL)
        self.assertEqual(request["reasoning"]["effort"], "max")
        self.assertNotIn("max_output_tokens", request)
        self.assertFalse(any(item.get("role") == "developer" for item in request["input"]))
        self.assertNotIn("instructions", request)
        first, ready, last = [call.args[0] for call in self.notify.await_args_list]
        self.assertTrue(first.started)
        self.assertTrue(first.follow_up)
        self.assertIn("已经做完了", ready.text)
        self.assertFalse(ready.answer)
        self.assertIn("积分", last.question)
        self.assertEqual(last.answer, r"积分是 \(\frac{1}{3}\)。")
        self.assertIn("900 token", last.text)
        self.assertIn("其中思考 550", last.text)
        self.assertIn("gpt-6.2-sol max 费用约 ¥0.0470", last.text)
        self.assertFalse(solver.waiting_for_reply(self.key))

    async def test_type_review_corrects_a_wrong_math_label_from_text_or_picture(self):
        with Image.new("RGB", (80, 80), "white") as image, io.BytesIO() as buffer:
            image.save(buffer, format="PNG")
            question_image = buffer.getvalue()
        cases = (
            ("2，6，12，20，（ ）", (), "行测数字推理", "从相邻差归纳规律并填数"),
            ("帮我做一下", (question_image,), "行测图形推理", "观察图形排列，给出空缺处的选项"),
            ("这段社评的主要论点是哪句？", (), "论点定位", "用题面原句说明主旨"),
        )
        await self.connect_mock(
            *(response(output_tokens=250, solution={"solved": True, "answer": "解答。", "progress": ""})
              for _ in cases),
            classifications=(
                response(output_tokens=100, solution={"question_type": label, "quick_answer": True, "answer_focus": focus})
                for _, _, label, focus in cases
            ),
        )
        for index, (original, images, label, focus) in enumerate(cases):
            with self.subTest(question_type=label):
                await self.start(
                    problem="根据题面完成作答。", request_text=original, images=images,
                    question_type="数学证明", quick_answer=False,
                )
                review = self.classification_requests[index]
                self.assertEqual(review["model"], "gpt-6-luna")
                self.assertEqual(review["reasoning"]["effort"], "none")
                content = review["input"][-1]["content"]
                self.assertIn(original, content[0]["text"])
                self.assertNotIn("数学证明", content[0]["text"])
                self.assertEqual(sum(part["type"] == "input_image" for part in content), len(images))
                solve = self.requests[index]
                self.assertEqual(solve["reasoning"]["effort"], "low")
                self.assertIn(original, solve["input"][0]["content"][0]["text"])
                instructions = solve["input"][0]["content"][1]["text"]
                self.assertIn(label, instructions)
                self.assertIn(focus, instructions)
                self.assertIn(f"题型 {label}", self.notify.await_args.args[0].text)
                self.assertIn("判题用量 350 token", self.notify.await_args.args[0].text)

    async def test_type_review_restores_configured_effort_for_a_proof(self):
        await self.connect_mock(
            response(output_tokens=500, solution={"solved": True, "answer": "证明完毕。", "progress": ""}),
            classifications=(response(output_tokens=120, solution={
                "question_type": "数列证明", "quick_answer": False, "answer_focus": "证明给定递推数列的极限",
            }),),
        )
        await self.start(
            problem="已知 a_1=1、a_{n+1}=sqrt(2+a_n)，证明数列收敛并求极限。",
            question_type="数字推理", quick_answer=True,
        )
        request, = self.requests
        self.assertEqual(request["reasoning"]["effort"], "max")
        self.assertIn("数列证明", request["input"][0]["content"][1]["text"])

    async def test_quick_questions_use_an_open_ended_type_and_return_an_answer(self):
        questions = (
            ("陷阱题", "公鸡在斜屋顶下的蛋会往哪边滚？", "公鸡不会下蛋，题目的前提不成立。"),
            ("行测判断推理", "所有甲都是乙，乙都不是丙。A. 有些甲是丙 B. 甲都不是丙。", "选 B。甲属于乙，已知乙与丙没有交集。"),
            ("化学实验现象", "少量二氧化碳通入澄清石灰水，会看到什么？", "石灰水变浑浊，因为生成了碳酸钙沉淀。"),
        )
        await self.connect_mock(*(response(
            output_tokens=300, solution={"solved": True, "answer": answer, "progress": ""},
        ) for _, _, answer in questions))
        for index, (question_type, question, answer) in enumerate(questions):
            with self.subTest(question_type=question_type):
                self.assertIn("已开始", await self.start(
                    problem=question, question_type=question_type, quick_answer=True,
                ))
                request = self.requests[index]
                self.assertEqual(request["reasoning"]["effort"], "low")
                self.assertNotIn("max_output_tokens", request)
                parts = request["input"][0]["content"]
                self.assertEqual(parts[0]["text"], question)
                self.assertIn(question_type, parts[1]["text"])
                self.assertEqual(self.notify.await_args.args[0].answer, answer)
        self.assertEqual(len(self.requests), len(questions))

    async def test_quick_question_does_not_restart_an_incomplete_response(self):
        await self.connect_mock(response(output_tokens=500, summary="仍在检查选项", encrypted=True))
        await self.start(question_type="行测资料分析", quick_answer=True)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.notify.await_args.args[0].exhausted_notice)
        self.format_model.assert_not_awaited()

    async def test_quick_question_times_out_and_closes_the_active_request(self):
        stream = DelayedStream([], asyncio.Event(), b"")
        await self.connect_mock(httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream))
        self.assertEqual(solver.QUICK_SOLVE_TIMEOUT_SECONDS, 30)
        with patch.object(solver, "QUICK_SOLVE_TIMEOUT_SECONDS", 0.04):
            await asyncio.wait_for(self.start(question_type="短篇阅读理解", quick_answer=True), timeout=2)
        self.assertTrue(stream.closed.is_set())
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.notify.await_args.args[0].exhausted_notice)
        self.assertFalse(solver.is_solving(self.key))

    async def test_solver_ends_and_announces_completion_before_formatting(self):
        raw = r"用幂函数积分，得到 \(\frac{1}{3}\)。"
        formatted = "### 解题思路\n" + raw
        finish = asyncio.Event()
        finish.set()
        solved = response(output_tokens=500, solution={"solved": True, "answer": raw, "progress": ""})
        stream = DelayedStream([], finish, sse_event("response.completed", response=solved.json()))
        await self.connect_mock(httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream))
        at_format_start = []

        async def edit(*args, **kwargs):
            at_format_start.append((self.notify.await_args.args[0].text, stream.closed.is_set(), solver.is_solving(self.key)))
            return json.dumps({"answer": formatted}), {"input_tokens": 100, "output_tokens": 60}, ""

        self.format_model.side_effect = edit
        await self.start()
        notice, stream_closed, still_solving = at_format_start.pop()
        self.assertIn("已经做完了", notice)
        self.assertTrue(stream_closed)
        self.assertFalse(still_solving)
        self.format_model.assert_awaited_once()
        self.assertEqual(self.format_model.await_args.args[1:4], (raw, "mock", "gpt-6-luna"))
        self.assertEqual(self.format_model.await_args.kwargs["reasoning_effort"], "none")
        self.assertEqual(len(self.requests), 1)
        delivered = self.notify.await_args.args[0]
        self.assertEqual(delivered.answer, raw)
        self.assertEqual(delivered.formatted_answer, formatted)
        self.assertIn("排版用量 160 token，约 ¥0.0003", delivered.text)

    async def test_formatting_keeps_the_original_when_a_formula_changes(self):
        raw = r"积分为 \(\frac{1}{3}\)。"
        await self.connect_mock(response(output_tokens=500, solution={"solved": True, "answer": raw, "progress": ""}))
        self.format_model.return_value = (json.dumps({"answer": r"### 结论\n积分为 \(\frac{1}{2}\)。"}), {}, "")
        await self.start()
        delivered = self.notify.await_args.args[0]
        self.assertEqual(delivered.answer, raw)
        self.assertFalse(delivered.formatted_answer)

    async def test_completed_code_is_kept_separate_from_answer_text(self):
        code = 'print("答案")\n'
        await self.connect_mock(response(output_tokens=650, solution={
            "solved": True, "answer": "运行 main.py 查看结果。", "progress": "",
            "files": [{"name": "main.py", "content": code}],
        }))
        await self.start()
        final = self.notify.await_args.args[0]
        self.assertEqual(final.files, (sources.CodeFile("main.py", code),))
        self.assertNotIn(code, final.answer)
        schema = self.requests[0]["text"]["format"]["schema"]
        self.assertIn("files", schema["required"])

    async def test_provider_boundary_continues_automatically_with_opaque_state(self):
        await self.connect_mock(
            httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=sse_event(
                "response.incomplete", response=response(output_tokens=10_000, summary="对 x 的幂函数积分。").json(),
            )),
            response(output_tokens=500, solution={"solved": True, "answer": "三分之一。", "progress": ""}),
        )
        await self.start()
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(solver.waiting_for_reply(self.key))
        self.assertEqual(self.requests[1]["model"], solver.SOL)
        self.assertTrue(all("max_output_tokens" not in req for req in self.requests))
        self.assertIn("对 x 的幂函数积分。", json.dumps(self.requests[1]["input"], ensure_ascii=False))
        reasoning, = [item for item in self.requests[1]["input"] if item.get("type") == "reasoning"]
        self.assertEqual(reasoning["encrypted_content"], "opaque-state")
        self.assertIn("做题耗时", self.notify.await_args.args[0].text)
        self.assertIn("11,000 token", self.notify.await_args.args[0].text)
        self.assertEqual(len(self.notify.await_args_list), 3)

    async def test_provider_boundary_continues_without_opaque_state_even_if_no_output_is_received(self):
        incomplete = response(output_tokens=10_000).json()
        partial = '{"solved":true,"answer":"先验证边界条件'
        incomplete["output"] = [{"type": "message", "content": [{"type": "output_text", "text": partial}]}]
        await self.connect_mock(
            response(output_tokens=10_000),
            httpx.Response(200, json=incomplete),
            response(output_tokens=500, solution={"solved": True, "answer": "证明完毕。", "progress": ""}),
        )
        await self.start()
        self.assertEqual(len(self.requests), 3)
        self.assertIn("先验证边界条件", json.dumps(self.requests[2]["input"], ensure_ascii=False))
        self.assertEqual(self.notify.await_args.args[0].answer, "证明完毕。")

    async def test_progress_model_translates_only_public_summaries_before_the_answer(self):
        finish = asyncio.Event()
        final = response(output_tokens=500, solution={"solved": True, "answer": "三分之一。", "progress": ""})
        stream = DelayedStream([
            sse_event("response.in_progress", response={"usage": {
                "input_tokens": 150, "output_tokens": 240, "output_tokens_details": {"reasoning_tokens": 180},
            }}),
            sse_event("response.reasoning_text.delta", delta="internal reasoning"),
            sse_event("response.reasoning_summary_text.delta", item_id="item", summary_index=0, delta="Checked the boundary"),
            sse_event("response.reasoning_summary_text.done", item_id="item", summary_index=0, text="Checked the boundaries."),
            sse_event("response.reasoning_summary_text.delta", item_id="item", summary_index=1, delta="Now applying the power rule."),
        ], finish, sse_event("response.completed", response=final.json()))
        await self.connect_mock(httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream))
        self.progress_model.return_value = ("已核对边界，正在用幂函数积分", {}, "")

        async def notify(delivery):
            if "已核对边界，正在用幂函数积分" in delivery.text:
                finish.set()

        self.notify.side_effect = notify
        self.assertEqual(solver.PROGRESS_INTERVAL_SECONDS, 60)
        with patch.object(solver, "PROGRESS_INTERVAL_SECONDS", 0.01):
            await asyncio.wait_for(self.start(), timeout=2)
        deliveries = [call.args[0] for call in self.notify.await_args_list]
        opening, ready, answer = deliveries[0], deliveries[-2], deliveries[-1]
        interim = next(delivery for delivery in deliveries if "已核对边界，正在用幂函数积分" in delivery.text)
        self.assertTrue(opening.started)
        self.assertIn("已核对边界，正在用幂函数积分", interim.text)
        self.assertIn("已回传用量 390 token", interim.text)
        self.assertIn("其中思考 180", interim.text)
        self.assertNotIn("internal reasoning", interim.text)
        self.progress_model.assert_awaited_once()
        call = str(self.progress_model.await_args)
        self.assertIn("gpt-6-luna", call)
        self.assertIn("Checked the boundaries.", call)
        self.assertIn("Now applying the power rule.", call)
        self.assertNotIn("internal reasoning", call)
        self.assertIn("已经做完了", ready.text)
        self.assertEqual(answer.answer, "三分之一。")
        self.assertIn("750 token", answer.text)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0]["stream"])

    async def test_progress_without_a_summary_reports_the_solve_stage_and_stops(self):
        finish = asyncio.Event()
        final = response(output_tokens=500, solution={"solved": True, "answer": "三分之一。", "progress": ""})
        stream = DelayedStream([], finish, sse_event("response.completed", response=final.json()))
        await self.connect_mock(httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream))

        notices = []

        async def notify(delivery):
            if not delivery.started and not delivery.answer and not delivery.text.startswith("已经做完了"):
                notices.append(delivery.text)
                if len(notices) == 2:
                    finish.set()

        self.notify.side_effect = notify
        with patch.object(solver, "PROGRESS_INTERVAL_SECONDS", 0.01):
            await asyncio.wait_for(self.start(), timeout=2)
            await asyncio.sleep(0.03)
        self.assertEqual(len(notices), 2)
        self.assertTrue(all(note.strip() for note in notices))
        self.progress_model.assert_not_awaited()
        self.assertEqual(self.notify.await_args.args[0].answer, "三分之一。")
        self.assertEqual([r["model"] for r in self.requests], [solver.SOL])

    async def test_sol_max_reuses_opaque_state_without_public_summary(self):
        await self.connect_mock(
            response(output_tokens=10_000, encrypted=True),
            response(output_tokens=500, solution={"solved": True, "answer": "三分之一。", "progress": ""}),
        )
        await self.start()
        self.assertEqual([r["model"] for r in self.requests], [solver.SOL, solver.SOL])
        self.assertTrue(any(item.get("encrypted_content") == "opaque-state" for item in self.requests[1]["input"]))
        self.assertTrue(all(r["reasoning"]["effort"] == "max" for r in self.requests))

    async def test_model_setting_applies_to_new_questions_and_keeps_running_question_on_original_model(self):
        finish = asyncio.Event()
        stream = DelayedStream([], finish, sse_event(
            "response.incomplete", response=response(output_tokens=10_000, encrypted=True).json(),
        ))
        await self.connect_mock(
            httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream),
            response(output_tokens=500, solution={"solved": True, "answer": "续答完成。", "progress": ""}),
            response(output_tokens=500, solution={"solved": True, "answer": "新题完成。", "progress": ""}),
        )
        with TemporaryDirectory() as temp, patch.object(solver.env_config, "SOLVE_MODEL_PATH", Path(temp) / "solve.json"):
            solver.env_config.save_solve_model("gpt-6.1-terra", "low")
            running = asyncio.create_task(self.start())
            await asyncio.wait_for(stream.started.wait(), timeout=2)
            solver.env_config.save_solve_model("gpt-6.2-astra", "high")
            finish.set()
            await asyncio.wait_for(running, timeout=2)
            self.assertIn("gpt-6.1-terra low 费用约 ¥0.3558", self.notify.await_args.args[0].text)
            self.assertTrue(any(item.get("encrypted_content") == "opaque-state" for item in self.requests[1]["input"]))
            await self.start()
        self.assertEqual(
            [(r["model"], r["reasoning"]["effort"]) for r in self.requests],
            [("gpt-6.1-terra", "low"), ("gpt-6.1-terra", "low"), ("gpt-6.2-astra", "high")],
        )
        self.assertIn("gpt-6.2-astra high 费用约 ¥0.1477", self.notify.await_args.args[0].text)

    async def test_solve_can_go_beyond_five_requests_and_fifty_thousand_output_tokens(self):
        await self.connect_mock(
            *(response(output_tokens=10_000, summary=f"阶段 {i} 的发现") for i in range(6)),
            response(output_tokens=500, solution={"solved": True, "answer": "证明完毕。", "progress": ""}),
        )
        await self.start()
        self.assertEqual(len(self.requests), 7)
        self.assertTrue(all("max_output_tokens" not in req for req in self.requests))
        self.assertEqual(self.notify.await_args.args[0].answer, "证明完毕。")
        self.assertIn("输出 60,500", self.notify.await_args.args[0].text)
        self.assertFalse(solver.waiting_for_reply(self.key))

    async def test_unsolved_result_preserves_verified_progress_and_keeps_working(self):
        await self.connect_mock(
            response(output_tokens=500, solution={"solved": False, "answer": "", "progress": "已证函数为偶函数"}),
            response(output_tokens=500, solution={"solved": True, "answer": "证明完毕。", "progress": ""}),
        )
        await self.start()
        self.assertEqual(len(self.requests), 2)
        self.assertIn("已证函数为偶函数", json.dumps(self.requests[1]["input"], ensure_ascii=False))
        self.assertEqual(self.notify.await_args.args[0].answer, "证明完毕。")

    async def slow_solution(self):
        finish = asyncio.Event()
        final = response(output_tokens=500, solution={"solved": True, "answer": "证明完毕。", "progress": ""})
        stream = DelayedStream([], finish, sse_event("response.completed", response=final.json()))
        await self.connect_mock(httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream))
        confirmations = asyncio.Queue()

        async def notify(delivery):
            if delivery.check_in:
                confirmations.put_nowait(delivery)

        self.notify.side_effect = notify
        return stream, finish, confirmations

    async def test_confirmation_preserves_the_in_flight_request_and_continue_clears_deadline(self):
        stream, finish, confirmations = await self.slow_solution()
        self.assertEqual(solver.CONFIRM_AFTER_SECONDS, 240)
        self.assertEqual(solver.CONFIRM_GRACE_SECONDS, 60)
        with patch.object(solver, "CONFIRM_AFTER_SECONDS", 0.035), patch.object(solver, "CONFIRM_GRACE_SECONDS", 0.3):
            started = asyncio.create_task(self.start())
            await asyncio.wait_for(confirmations.get(), timeout=2)
            self.assertTrue(solver.waiting_for_reply(self.key))
            self.assertFalse(solver.waiting_for_reply("bot:group_room_456"))
            await solver.handle_reply("bot:group_room_456", "不做了", "mock", self.notify)
            task = solver._running[self.key]
            self.assertFalse(task.done())
            self.assertFalse(stream.closed.is_set())
            self.assertIsNone(self.request_timeouts[0]["read"])
            await solver.handle_reply(self.key, "继续", "mock", self.notify)
            self.assertFalse(solver.waiting_for_reply(self.key))
            await asyncio.wait_for(confirmations.get(), timeout=2)
            self.assertTrue(solver.waiting_for_reply(self.key))
            self.assertIs(solver._running[self.key], task)
            self.assertFalse(stream.closed.is_set())
            finish.set()
            await asyncio.wait_for(started, timeout=2)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.notify.await_args.args[0].answer, "证明完毕。")

    async def test_repeated_confirms_stop_after_an_unanswered_check_in(self):
        stream, _, confirmations = await self.slow_solution()
        with patch.object(solver, "CONFIRM_AFTER_SECONDS", 0.035), patch.object(solver, "CONFIRM_GRACE_SECONDS", 0.3):
            started = asyncio.create_task(self.start())
            for reply in ("继续", "继续做题"):
                await asyncio.wait_for(confirmations.get(), timeout=2)
                self.assertTrue(solver.waiting_for_reply(self.key))
                self.assertFalse(stream.closed.is_set())
                await solver.handle_reply(self.key, reply, "mock", self.notify)
                self.assertFalse(solver.waiting_for_reply(self.key))
            await asyncio.wait_for(confirmations.get(), timeout=2)
            self.assertTrue(solver.waiting_for_reply(self.key))
            self.assertFalse(stream.closed.is_set())
            await asyncio.wait_for(started, timeout=2)
        self.assertTrue(stream.closed.is_set())
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.notify.await_args.args[0].exhausted_notice, "这道积分我尽力了，还是没做出来")
        self.assertFalse(solver.waiting_for_reply(self.key))

    async def test_requester_can_stop_running_solve_and_get_sticker(self):
        stream, _, confirmations = await self.slow_solution()
        with patch.object(solver, "CONFIRM_AFTER_SECONDS", 0.035), patch.object(solver, "CONFIRM_GRACE_SECONDS", 0.3):
            started = asyncio.create_task(self.start())
            await asyncio.wait_for(confirmations.get(), timeout=2)
            await solver.handle_reply(self.key, "不做了", "mock", self.notify)
            await asyncio.wait_for(asyncio.gather(started, return_exceptions=True), timeout=2)
        self.assertTrue(stream.closed.is_set())
        self.assertTrue(self.notify.await_args.args[0].exhausted_notice)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(solver.waiting_for_reply(self.key))
        self.assertNotIn(self.key, solver._running)

    async def test_confirmation_expires_and_cancels_with_ending_and_sticker(self):
        stream, _, confirmations = await self.slow_solution()
        with patch.object(solver, "CONFIRM_AFTER_SECONDS", 0.035), patch.object(solver, "CONFIRM_GRACE_SECONDS", 0.08):
            started = asyncio.create_task(self.start())
            await asyncio.wait_for(confirmations.get(), timeout=2)
            self.assertFalse(stream.closed.is_set())
            await asyncio.wait_for(asyncio.gather(started, return_exceptions=True), timeout=2)
        ending = self.notify.await_args.args[0]
        self.assertTrue(stream.closed.is_set())
        self.assertEqual(ending.exhausted_notice, "这道积分我尽力了，还是没做出来")
        self.assertFalse(solver.waiting_for_reply(self.key))
        self.assertNotIn(self.key, solver._running)

    async def test_connection_failure_can_resume_from_a_short_checkpoint(self):
        await self.connect_mock(httpx.ConnectTimeout("mock"), response(
            output_tokens=500, solution={"solved": True, "answer": "三分之一。", "progress": ""},
        ))
        await self.start()
        pending = solver._pending[self.key]
        self.assertAlmostEqual(pending.timer.when() - asyncio.get_running_loop().time(), 600, delta=1)
        self.assertEqual(pending.state.usage.output_tokens, 0)
        self.assertEqual(await solver.handle_reply(self.key, "继续做题", "mock", self.notify), "")
        await solver._running[self.key]
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all("max_output_tokens" not in req for req in self.requests))
        self.assertEqual(self.notify.await_args.args[0].answer, "三分之一。")

    async def test_interrupted_continuation_keeps_the_previous_checkpoint_without_retrying(self):
        await self.connect_mock(
            response(output_tokens=10_000, summary="先计算幂函数的积分"),
            httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=b""),
            response(output_tokens=500, solution={"solved": True, "answer": "三分之一。", "progress": ""}),
        )
        await self.start()

        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(timeout["read"] is None for timeout in self.request_timeouts))
        checkpoint = solver._pending[self.key].state
        self.assertIn("先计算幂函数的积分", checkpoint.progress)
        self.assertTrue(any(item.get("encrypted_content") == "opaque-state" for item in checkpoint.reasoning_items))
        self.assertEqual(checkpoint.usage.output_tokens, 10_000)
        self.assertFalse(checkpoint.usage.complete)
        notice = self.notify.await_args.args[0].text
        self.assertIn("题目和已有进度保留", notice)
        self.assertNotIn("已预留", notice)

        self.assertEqual(await solver.handle_reply(self.key, "继续做题", "mock", self.notify), "")
        await solver._running[self.key]
        self.assertNotIn("max_output_tokens", self.requests[2])
        self.assertTrue(any(item.get("encrypted_content") == "opaque-state" for item in self.requests[2]["input"]))
        self.assertEqual(self.notify.await_args.args[0].answer, "三分之一。")

    async def test_checkpoint_expiry_removes_progress(self):
        solver._save_checkpoint(self.key, solver.Problem("题目", progress="进度"))
        pending = solver._pending[self.key]
        pending.expires_at = 0
        self.assertIn("续答已结束", await solver.handle_reply(self.key, "继续做题", "mock", self.notify))
        self.assertNotIn(self.key, solver._pending)
        self.assertTrue(pending.timer.cancelled())
        self.notify.assert_not_awaited()

    async def test_send_failure_retains_answer_for_retry_without_another_model_request(self):
        await self.connect_mock(
            response(output_tokens=240, solution={"solved": True, "answer": "答案是三分之一。", "progress": ""}),
        )
        rejected = ActionFailed(retcode=1200)
        self.notify.side_effect = [None, None, rejected, None, rejected, None]
        await self.start()
        self.assertTrue(solver.waiting_for_reply(self.key))
        self.assertIn("重发图片", self.notify.await_args.args[0].text)
        solution = solver._pending[self.key].state
        self.assertIn("暂时还是发不过去", await solver.handle_reply(self.key, "继续做题", "", self.notify))
        self.assertTrue(solver.waiting_for_reply(self.key))
        self.assertEqual(await solver.handle_reply(self.key, "继续做题", "", self.notify), "")
        final = self.notify.await_args.args[0]
        self.assertIs(final, solution)
        self.assertEqual(final.answer, "答案是三分之一。")
        self.assertIn("490 token", final.text)
        self.assertEqual(len(self.requests), 1)
        self.format_model.assert_awaited_once()
        self.assertFalse(solver.waiting_for_reply(self.key))

    async def test_open_problem_rejection_does_not_start_a_solve(self):
        self.assertIn("公认的未解", await self.start(in_scope=False))
        self.assertNotIn(self.key, solver._running)
        self.notify.assert_not_awaited()

    def test_pricing_uses_cache_categories_and_counts_reasoning_inside_output(self):
        usage = solver._read_usage({
            "input_tokens": 1000,
            "input_tokens_details": {
                "cached_tokens": 500, "cache_write_tokens": 200, "cache_write_tokens_details": {"ttl_12h_tokens": 100},
            },
            "output_tokens": 500,
            "output_tokens_details": {"reasoning_tokens": 400},
        })
        self.assertEqual(usage.usd, Decimal("0.0064"))
        self.assertEqual(usage.cached_input_tokens, 500)
        self.assertEqual(usage.cache_write_tokens, 200)
        self.assertEqual(usage.reasoning_tokens, 400)


class SourcesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.key = "bot:group_first_requester"
        self.addCleanup(sources._answers.pop, self.key, None)
        self.addCleanup(sources._menus.pop, self.key, None)

    async def test_multiple_answers_use_a_stable_menu_for_the_requester(self):
        sources.remember(self.key, "第一题", "甲", ())
        sources.remember(self.key, "第二题", "乙", ())
        listing = sources.select(self.key, "源文件")
        self.assertIn("1. 第一题\n2. 第二题", listing)
        self.assertTrue(sources.recognizes_reply(self.key, "2", addressed=True))
        self.assertFalse(sources.recognizes_reply(self.key, "2", addressed=False))
        sources.remember(self.key, "后来完成的第三题", "丙", ())
        other = sources.select("bot:group_other_user", "源文件 2")
        self.assertIn("没有可领取", other)
        selected = sources.select(self.key, "源文件 2")
        self.assertEqual(selected.question, "第二题")
        self.assertFalse(sources.recognizes_reply(self.key, "2", addressed=True))

    async def test_source_cache_expires_without_an_incoming_message(self):
        with patch.object(sources, "TTL_SECONDS", 0.01):
            sources.remember(self.key, "题目", "解答", ())
        self.assertIn(self.key, sources._answers)
        await asyncio.sleep(0.04)
        self.assertNotIn(self.key, sources._answers)

    async def test_attachment_contains_answer_and_safe_code_names(self):
        code = "def f():\n    return '答案'\n"
        files = sources.read_code_files([{"name": "../../main.py", "content": code}])
        sources.remember(self.key, "写一个函数", "运行 main.py", files)
        name, data = sources.attachment(sources.select(self.key, "源文件"))
        self.assertTrue(name.endswith(".zip"))
        with ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(archive.namelist(), ["解答.txt", "main.py"])
            self.assertIn("运行 main.py", archive.read("解答.txt").decode("utf-8"))
            self.assertEqual(archive.read("main.py"), code.encode("utf-8"))


class FormulaTests(unittest.TestCase):
    def test_markdown_headings_preserve_inline_math_and_body_hashes(self):
        from playwright.sync_api import sync_playwright

        title = "一、短回文串能填哪些和？"
        body = "正文中的 C# 和 # 仍然可见"
        answer = f"### {title}\n" + r"### \(k=3\) ###" + f"\n{body}"
        assets = math_renderer._ASSETS
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=math_renderer._browser_executable(), headless=True)
            try:
                page = browser.new_page(offline=True)
                page.set_content((assets / "page.html").read_text(encoding="utf-8"))
                page.add_script_tag(path=assets / "katex.min.js")
                page.evaluate("parts => layoutSolution(parts)", math_renderer._fragments(answer))
                headings = page.locator("#content > p.heading")
                self.assertEqual(headings.count(), 2)
                self.assertEqual(headings.nth(0).text_content(), title)
                self.assertEqual(headings.nth(1).locator(".katex").count(), 1)
                self.assertNotIn("#", headings.nth(1).text_content())
                self.assertEqual(page.locator("#content > p:not(.heading)").all_text_contents(), [body])
            finally:
                browser.close()

    def test_control_character_in_inequality_does_not_break_rendering(self):
        answer = r"**1. \(M\) 非空、闭，且 \(c>0\)。** 从而 \(A(u)" + "\x07" + r"\ge C^{-4}\)。"
        formulas = [part["value"] for part in math_renderer._fragments(answer) if part["kind"] == "inline"]
        self.assertEqual(formulas, ["M", "c>0", r"A(u)\ge C^{-4}"])
        page, = math_renderer.render_answer(answer)
        with Image.open(io.BytesIO(page)) as image:
            self.assertFalse(any(r > 175 and g < 85 and b < 85 for r, g, b in image.convert("RGB").getdata()))

    def test_fourteen_equations_share_one_page(self):
        answer = "设 " + "，".join(fr"\(x_{{{index}}}=1\)" for index in range(14)) + "。"
        self.assertEqual(sum(part["kind"] == "inline" for part in math_renderer._fragments(answer)), 14)
        page, = math_renderer.render_answer(answer)
        with Image.open(io.BytesIO(page)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertGreater(image.height, 72)
            image.verify()

    def test_long_paragraph_and_wide_formula_keep_the_final_line(self):
        wide = r"\[" + " + ".join(fr"\frac{{x_{{{index}}}}}{{{index + 1}}}" for index in range(22)) + r"\]"
        paragraph = "按题目条件核对每条等式的依据。" * 145
        final = r"\[\begin{aligned}u&=1\\v&=\textcolor{red}{\mathrm{END}}\end{aligned}\]"
        answer = wide + "\n" + paragraph + "\n" + final
        pages = math_renderer.render_answer(answer)
        self.assertEqual(len(pages), 2)
        with Image.open(io.BytesIO(pages[0])) as first:
            self.assertGreater(first.height, 1600)
        for page in pages:
            with Image.open(io.BytesIO(page)) as source:
                image = source.convert("RGB")
            self.assertEqual(image.width, 800)
            self.assertLessEqual(image.height, 3600)
            bounds = ImageChops.difference(image, Image.new("RGB", image.size, "white")).getbbox()
            self.assertIsNotNone(bounds)
            self.assertGreaterEqual(bounds[0], 30)
            self.assertLessEqual(bounds[2], image.width - 30)
            self.assertLessEqual(bounds[3], image.height - 30)
        self.assertTrue(any(red > 175 and green < 85 and blue < 85 for red, green, blue in image.getdata()))


class OneBotDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.event = SimpleNamespace(message_id=42)
        self.env = {
            "ActionFailed": ActionFailed, "MessageSegment": MessageSegment, "asyncio": asyncio,
            "problem_math": math_renderer, "problem_solver": solver, "logger": solver.logger,
            "problem_sources": sources, "TemporaryDirectory": TemporaryDirectory, "Path": Path,
            "HERE": ROOT / "plugins" / "chat_bot",
            "_is_private": lambda event: event.message_type == "private",
            "_reply_to": lambda event, text: MessageSegment.reply(event.message_id) + MessageSegment.text(text),
        }
        load_handlers(
            "plugins/chat_bot/__init__.py",
            {"_image_reply", "_send_problem_delivery", "_problem_session_key", "_send_problem_source",
             "_problem_reply_text", "_is_problem_reply"},
            self.env,
        )

    def test_unaddressed_solve_decisions_are_scoped_to_requester_and_group(self):
        owner = "1:group_23_42"
        pending = {owner}
        self.env.update(
            _group_allowed=lambda event: event.group_id == 23,
            message_io=SimpleNamespace(addressed_to_bot=lambda event: event.addressed),
            problem_solver=SimpleNamespace(
                CONTINUE_REPLIES=solver.CONTINUE_REPLIES, CANCEL_REPLIES=solver.CANCEL_REPLIES,
                waiting_for_reply=pending.__contains__, is_solving=lambda key: key == owner,
            ),
        )
        matches = self.env["_is_problem_reply"]

        def event(text, *, user=42, group=23, addressed=False):
            return SimpleNamespace(self_id=1, message_type="group", user_id=user, group_id=group,
                                   get_session_id=lambda: f"group_{group}_{user}",
                                   get_plaintext=lambda: text, addressed=addressed)

        self.assertTrue(matches(event("继续！")))
        self.assertTrue(matches(event("不做了。")))
        self.assertFalse(matches(event("不做了。", user=17)))
        self.assertFalse(matches(event("继续", group=18)))
        self.assertFalse(matches(event("是")))
        self.assertTrue(matches(event("是", addressed=True)))
        pending.clear()
        self.assertFalse(matches(event("继续")))
        self.assertTrue(matches(event("不做了")))

    async def test_finished_answer_offers_source_without_uploading_it(self):
        event = SimpleNamespace(message_id=42, self_id=1, get_session_id=lambda: "minutely_test_group")
        key = "1:minutely_test_group"
        self.addCleanup(sources._answers.pop, key, None)
        bot = SimpleNamespace(send=AsyncMock(), call_api=AsyncMock())
        delivery = solver.Delivery("费用", answer="证明完毕", question="原题", formatted_answer="### 结论\n证明完毕")
        with patch.object(math_renderer, "render_answer", return_value=(b"page",)) as render:
            await self.env["_send_problem_delivery"](bot, event, delivery)
        render.assert_called_once_with(delivery.formatted_answer)
        bot.call_api.assert_not_awaited()
        footer = bot.send.await_args.args[1][-1].data["text"]
        self.assertIn(sources.OFFER, footer)
        self.assertEqual(sources.select(key, "源文件").answer, delivery.formatted_answer)

    async def test_exhausted_answer_sends_the_given_sticker_with_the_model_notice(self):
        bot = SimpleNamespace(send=AsyncMock())
        delivery = solver.Delivery("解题用量", exhausted_notice="这题我尽力了")
        await self.env["_send_problem_delivery"](bot, self.event, delivery)
        outgoing = bot.send.await_args.args[1]
        self.assertEqual([part.type for part in outgoing], ["reply", "text", "image", "text"])
        self.assertEqual(outgoing[1].data["text"], delivery.exhausted_notice)
        expected = (ROOT / "assets" / "problem-exhausted-sticker.png").read_bytes()
        self.assertEqual(base64.b64decode(outgoing[2].data["file"].removeprefix("base64://")), expected)
        self.assertEqual(outgoing[3].data["text"], "\n解题用量")

    async def test_requested_attachment_uploads_to_the_original_conversation(self):
        item = sources.SourceAnswer(1, "原题", "解答", (), 1000)
        paths = []

        async def upload(api, **kwargs):
            path = Path(url2pathname(urlsplit(kwargs["file"]).path))
            self.assertEqual(path.read_text(encoding="utf-8"), "题目\n原题\n\n解答\n解答\n")
            self.assertTrue(kwargs["name"].endswith(".txt"))
            paths.append(path)

        bot = SimpleNamespace(call_api=AsyncMock(side_effect=upload))
        for kind, target in (("group", {"group_id": 23}), ("private", {"user_id": 45})):
            event = SimpleNamespace(message_type=kind, **target)
            self.assertEqual(await self.env["_send_problem_source"](bot, event, item), "")
            call = bot.call_api.await_args
            self.assertEqual(call.args, (f"upload_{kind}_file",))
            self.assertEqual({name: call.kwargs[name] for name in target}, target)
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_group_send_preserves_one_image_and_does_not_fall_back_to_text(self):
        bot = SimpleNamespace(send=AsyncMock(side_effect=ActionFailed(retcode=1200)))
        answer = "，".join(fr"\(x_{index}=1\)" for index in range(14))
        delivery = solver.Delivery("费用约 ¥0.0021", answer=answer)
        with self.assertRaises(ActionFailed):
            await self.env["_send_problem_delivery"](bot, self.event, delivery)
        bot.send.assert_awaited_once()
        outgoing = bot.send.await_args.args[1]
        self.assertEqual([part.type for part in outgoing], ["reply", "text", "image", "text"])
        self.assertEqual(outgoing[1].data["text"], "解答见图")
        self.assertEqual(outgoing[3].data["text"], f"\n{delivery.text}")
        encoded = outgoing[2].data["file"]
        self.assertTrue(encoded.startswith("base64://"))
        with Image.open(io.BytesIO(base64.b64decode(encoded.removeprefix("base64://"), validate=True))) as image:
            self.assertEqual(image.format, "PNG")
            image.verify()
        self.assertEqual(delivery.sent_pages, 0)

    async def test_retry_only_sends_pages_without_an_acknowledgment(self):
        bot = SimpleNamespace(send=AsyncMock(side_effect=[None, ActionFailed(retcode=1200), None]))
        delivery = solver.Delivery("费用约 ¥0.0021", answer="解答")
        send = self.env["_send_problem_delivery"]
        with patch.object(math_renderer, "render_answer", return_value=(b"first", b"second")) as render:
            with self.assertRaises(ActionFailed):
                await send(bot, self.event, delivery)
            self.assertEqual(delivery.sent_pages, 1)
            await send(bot, self.event, delivery)
        render.assert_called_once()
        outgoing = [call.args[1] for call in bot.send.await_args_list]
        self.assertEqual([msg[2].data["file"] for msg in outgoing],
                         ["base64://Zmlyc3Q=", "base64://c2Vjb25k", "base64://c2Vjb25k"])
        self.assertEqual([part.type for part in outgoing[0]], ["reply", "text", "image"])
        self.assertEqual(outgoing[-1][1].data["text"], "解答 2/2")
        self.assertEqual(outgoing[-1][3].data["text"], f"\n{delivery.text}")
        self.assertEqual(delivery.sent_pages, 2)
