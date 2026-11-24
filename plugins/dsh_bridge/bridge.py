"""DSH 桥接会话管理器。

职责：把「QQ 用户」与「DSH ACP 会话」绑定起来，并把 ACP 侧的
流式更新 / 权限审批请求转换成适合 QQ 发送的文本。

输出策略（QQ 消息无法原地编辑）：
  * agent 文本增量 -> 累积到缓冲区，按时间片节流 flush；
  * 工具调用 -> 立即单独推送一条简报；
  * 权限请求 -> 立即询问，并挂起一个 future 等待用户回复；
  * 一轮结束 -> flush 缓冲并推送收尾。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .acp_client import AcpClient, AcpError, permission_response
from .state_store import load_state, save_state

# 一次 flush 最多发送的字符数，避免 QQ 消息过长
MAX_CHUNK = 1500
# 汇合窗口：agent 文本静默这么久就 flush 一次
FLUSH_INTERVAL = 2.5

Notifier = Callable[[str, str], Awaitable[None]]
# (user_key, 本地文件绝对路径) -> None，由 NoneBot2 侧用 OneBot API 真正发送
FileSender = Callable[[str, str], Awaitable[None]]
# (user_key) -> None，在重要节点「戳一戳」提醒用户（失败不影响主流程）
Poker = Callable[[str], Awaitable[None]]
# outbox 目录名（相对工作区），agent 把要发给用户的文件放这里
OUTBOX_DIR = ".qq-outbox"
# outbox 扫描间隔（秒）
OUTBOX_INTERVAL = 3.0


@dataclass
class PendingApproval:
    """一个已发出、等待用户在 QQ 里回答的审批请求。"""

    params: dict
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)


@dataclass
class PendingQuestion:
    """一个已发到 QQ、等待用户回答的 elicitation 提问。"""

    questions: list
    future: asyncio.Future


@dataclass
class UserSession:
    """一个 QQ 用户（或群）的远程会话状态。"""

    session_id: Optional[str] = None
    cwd: Optional[str] = None
    busy: bool = False
    text_buffer: str = ""
    last_flush: float = field(default_factory=time.time)
    pending: Optional[PendingApproval] = None
    # 最近一次 flush 后是否还有未发送内容
    dirty: bool = False
    # toolCallId -> 工具标题，用于把审批请求翻译成人话
    tool_titles: dict[str, str] = field(default_factory=dict)
    # 待回答的 elicitation 提问
    pending_question: Optional[PendingQuestion] = None
    # 最近活跃时间（用于把没有 sessionId 的提问路由到用户）
    last_active: float = field(default_factory=time.time)
    # 已发送过的 outbox 文件标识（文件名:修改时间:大小），避免重复发送
    sent_files: set[str] = field(default_factory=set)
    # 当前 ACP 进程里该会话是否已激活（机器人重启后为 False，首次发指令时再 resume）
    active: bool = False

    @property
    def has_session(self) -> bool:
        return self.session_id is not None


def format_questions(questions: list) -> str:
    """把 elicitation 提问格式化成 QQ 消息。"""
    lines = ["❓ DSH 需要你的决定：", ""]
    multi = len(questions) > 1
    for qi, q in enumerate(questions, 1):
        prefix = f"【{qi}】" if multi else ""
        lines.append(f"{prefix}{q.get('question', '')}")
        if q.get("header"):
            lines.append(f"（{q['header']}）")
        if q.get("detail"):
            detail = str(q["detail"])
            lines.append("  详情：" + (detail[:800] + "…" if len(detail) > 800 else detail))
        options = q.get("options") or []
        for oi, opt in enumerate(options, 1):
            desc = f" — {opt.get('description')}" if opt.get("description") else ""
            lines.append(f"  {oi}) {opt.get('label', '')}{desc}")
        if q.get("multiSelect"):
            lines.append("  （可多选）")
        lines.append("")
    lines.append("回复：/dsh ans <答案>")
    lines.append("单选填选项号（如 1）；多个问题用逗号分隔（如 1,2）；也可直接填自定义文本。")
    return "\n".join(lines)


def parse_answers(questions: list, raw: str) -> list:
    """把 QQ 文本答案解析成 user-questions 的 answers 结构。"""
    tokens = [t.strip() for t in raw.replace("，", ",").split(",") if t.strip()]
    answers: list = []
    for idx, q in enumerate(questions):
        options = q.get("options") or []
        token = tokens[idx] if idx < len(tokens) else ""
        selected: list = []
        custom: Optional[str] = None
        if token.isdigit():
            num = int(token)
            if 1 <= num <= len(options):
                selected = [str(options[num - 1].get("label", ""))]
        elif token:
            custom = token
        item: dict = {"id": q.get("id"), "selected": selected}
        if custom:
            item["custom"] = custom
        answers.append(item)
    return answers


def _extract_text(update: dict) -> str:
    """从 ACP session/update 里取出可读文本（若有）。"""
    kind = update.get("sessionUpdate")
    if kind == "agent_message_chunk":
        content = update.get("content") or {}
        if isinstance(content, dict):
            if content.get("type") == "text":
                return content.get("text") or ""
            # 兼容 resource_link 等
            if content.get("type") == "resource_link":
                return f"[{content.get('name') or content.get('uri')}]"
        return ""
    # 思考增量（agent_thought_chunk）默认不推送到 QQ，避免刷屏；
    # 若想查看 DSH 的推理过程，把下面改为提取 content.text 并加 💭 前缀即可。
    return ""


def _summarize_tool_call(update: dict) -> Optional[str]:
    """把工具调用生命周期转成一条人类可读简报。"""
    kind = update.get("sessionUpdate")
    if kind not in ("tool_call", "tool_call_update"):
        return None
    title = update.get("title") or update.get("kind") or "工具"
    status = update.get("status") or ""
    tool_id = update.get("toolCallId") or ""
    if kind == "tool_call":
        return f"🔧 {title}" + (f" ({status})" if status else "")
    # tool_call_update：只在有意义的终态提示，避免刷屏
    if status in ("completed", "failed"):
        icon = "✅" if status == "completed" else "❌"
        return f"{icon} {title} [{status}]"
    return None


class DshBridge:
    """QQ <-> DSH(ACP) 的桥接核心。"""

    def __init__(
        self,
        *,
        node: str,
        repo: str,
        dsh_home: str,
        default_workspace: str,
        profile: str = "acp",
        allowlist: Optional[set[str]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        state_path: Optional[str] = None,
    ) -> None:
        self._default_workspace = default_workspace
        self._state_path = Path(state_path) if state_path else None
        self._restored = 0
        self._allowlist = allowlist or set()
        self._on_log = on_log or (lambda _msg: None)
        self._users: dict[str, UserSession] = {}
        self._session_owner: dict[str, str] = {}
        self._notifier: Optional[Notifier] = None
        self._file_sender: Optional[FileSender] = None
        self._poker: Optional[Poker] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._outbox_task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()

        self.acp = AcpClient(
            node=node,
            repo=repo,
            dsh_home=dsh_home,
            profile=profile,
            on_session_update=self._on_update,
            on_permission=self._on_permission,
            on_stderr=lambda line: self._on_log(f"[dsh-acp] {line}"),
        )

    # ------------------------------------------------------------------ 权限
    def allowed(self, user_key: str) -> bool:
        """allowlist 为空表示不限制；否则只放行名单内的用户。"""
        if not self._allowlist:
            return True
        return user_key in self._allowlist

    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def set_file_sender(self, sender: FileSender) -> None:
        """注入真正把本地文件发到 QQ 的回调。"""
        self._file_sender = sender

    def set_poker(self, poker: Poker) -> None:
        """注入「戳一戳」回调，用于在重要节点提醒用户。"""
        self._poker = poker

    async def poke(self, user_key: str) -> None:
        """戳一戳用户（尽力而为，失败只记日志）。"""
        if self._poker is None:
            return
        try:
            await self._poker(user_key)
        except Exception as exc:  # noqa: BLE001
            self._on_log(f"戳一戳失败: {exc!r}")

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        self.load_persisted()
        await self.acp.start()
        await self.acp.initialize()
        self._flush_task = asyncio.create_task(self._flush_loop())
        self._outbox_task = asyncio.create_task(self._outbox_loop())
        self._on_log("DSH ACP 已连接")

    async def stop(self) -> None:
        for task in (self._flush_task, self._outbox_task):
            if task is not None:
                task.cancel()
        self._flush_task = None
        self._outbox_task = None
        await self.acp.stop()

    async def _notify(self, user_key: str, text: str) -> None:
        if self._notifier is None or not text:
            return
        # 超长文本分片；单个分片推送失败不应影响协议处理
        remaining = text
        while remaining:
            piece, remaining = remaining[:MAX_CHUNK], remaining[MAX_CHUNK:]
            try:
                await self._notifier(user_key, piece)
            except Exception as exc:  # noqa: BLE001
                self._on_log(f"推送失败: {exc!r}")

    # ------------------------------------------------------------------ 用户状态
    def _user(self, user_key: str) -> UserSession:
        state = self._users.get(user_key)
        if state is None:
            state = UserSession(cwd=self._default_workspace)
            self._users[user_key] = state
        return state

    def state_of(self, user_key: str) -> UserSession:
        return self._user(user_key)

    # ------------------------------------------------------------------ 持久化
    def load_persisted(self) -> int:
        """从磁盘恢复「用户 -> 会话/工作区」绑定，返回恢复条数。"""
        if self._state_path is None:
            return 0
        saved = load_state(self._state_path)
        for key, item in saved.items():
            state = self._user(key)
            state.session_id = item.get("session_id")
            if item.get("cwd"):
                state.cwd = item["cwd"]
            # 机器人刚启动，ACP 是全新进程：会话存在但未激活，首次发指令时 resume
            state.active = False
        self._session_owner = {
            item["session_id"]: key for key, item in saved.items() if item.get("session_id")
        }
        self._restored = len(saved)
        if saved:
            self._on_log(f"已从磁盘恢复 {len(saved)} 个用户的会话绑定")
        return len(saved)

    @property
    def restored_count(self) -> int:
        """本次启动从磁盘恢复的绑定条数。"""
        return self._restored

    def _persist(self) -> None:
        """把当前绑定写入磁盘（尽力而为，失败只记日志）。"""
        if self._state_path is None:
            return
        users = {
            key: {
                "session_id": state.session_id,
                "cwd": state.cwd,
                "updated_at": time.time(),
            }
            for key, state in self._users.items()
            if state.session_id
        }
        try:
            save_state(self._state_path, users)
        except Exception as exc:  # noqa: BLE001
            self._on_log(f"保存会话绑定失败: {exc!r}")

    # ------------------------------------------------------------------ 会话操作
    async def new_session(self, user_key: str, cwd: Optional[str] = None) -> str:
        target = cwd or self._user(user_key).cwd or self._default_workspace
        session_id = await self.acp.new_session(target)
        state = self._user(user_key)
        # 切换前关闭旧会话（尽力而为）
        if state.session_id and state.session_id in self._session_owner:
            self._session_owner.pop(state.session_id, None)
            try:
                await self.acp.close_session(state.session_id)
            except Exception:
                pass
        state.session_id = session_id
        state.cwd = target
        state.text_buffer = ""
        state.dirty = False
        state.active = True
        self._session_owner[session_id] = user_key
        self._persist()
        return session_id

    async def list_sessions(self, cwd: Optional[str] = None) -> list:
        return await self.acp.list_sessions(cwd)

    async def switch_session(self, user_key: str, session_id: str, cwd: str) -> None:
        await self.acp.resume_session(session_id, cwd)
        state = self._user(user_key)
        state.session_id = session_id
        state.cwd = cwd
        state.text_buffer = ""
        state.dirty = False
        state.active = True
        self._session_owner[session_id] = user_key
        self._persist()

    async def _reactivate(self, user_key: str) -> bool:
        """机器人重启后，把磁盘上记住的会话重新激活（resume）。

        成功返回 True；会话已不存在等失败情况会清掉绑定并返回 False，
        由调用方决定是否新建会话。
        """
        state = self._user(user_key)
        if state.session_id is None:
            return False
        sid = state.session_id
        try:
            await self.acp.resume_session(sid, state.cwd or self._default_workspace)
        except Exception as exc:  # noqa: BLE001
            self._on_log(f"恢复会话 {sid} 失败: {exc!r}")
            self._session_owner.pop(sid, None)
            state.session_id = None
            state.active = False
            self._persist()
            await self._notify(user_key, "⚠️ 上次的会话已无法恢复，将新建一个会话。")
            return False
        state.active = True
        self._session_owner[sid] = user_key
        await self._notify(user_key, f"♻️ 已自动恢复上次的会话\n工作区：{state.cwd}")
        return True

    async def close_session(self, user_key: str) -> None:
        state = self._user(user_key)
        if state.session_id is None:
            return
        self._session_owner.pop(state.session_id, None)
        try:
            await self.acp.close_session(state.session_id)
        finally:
            state.session_id = None
            state.active = False
            self._persist()

    async def set_model(self, user_key: str, model: str) -> dict:
        state = self._user(user_key)
        if state.session_id is None:
            raise AcpError("当前没有活动会话")
        value = self.acp.resolve_model_value(state.session_id, model)
        if value is None:
            names = ", ".join(o["name"] for o in self.acp.model_options(state.session_id))
            raise AcpError(f"未知模型「{model}」。可用：{names or '（无）'}")
        return await self.acp.set_config_option(state.session_id, "model", value)

    # ------------------------------------------------------------------ 发送指令
    async def send_prompt(self, user_key: str, text: str) -> None:
        state = self._user(user_key)
        if state.session_id is None:
            await self.new_session(user_key)
            state = self._user(user_key)
        elif not state.active:
            # 机器人重启后会话尚未激活：先按磁盘记录 resume，失败再新建
            if not await self._reactivate(user_key):
                await self.new_session(user_key, state.cwd)
            state = self._user(user_key)
        if state.busy:
            raise AcpError("上一轮还在进行中，请先 /dsh cancel 或等待完成")
        state.busy = True
        state.last_flush = time.time()
        try:
            result = await self.acp.prompt(state.session_id, text)
            await self._flush_user(user_key, force=True)
            state.busy = False
            reason = (result or {}).get("stopReason") or "end_turn"
            await self._notify(user_key, f"— 本轮结束（{reason}）—")
            # 任务「完成」——戳一戳提醒
            await self.poke(user_key)
        except Exception as exc:
            state.busy = False
            await self._notify(user_key, f"⚠️ 执行失败：{exc}")

    async def cancel(self, user_key: str) -> None:
        state = self._user(user_key)
        if state.session_id is not None:
            await self.acp.cancel(state.session_id)

    # ------------------------------------------------------------------ 审批
    async def _on_permission(self, params: dict) -> dict:
        """ACP 服务端请求：需要用户同意/拒绝。"""
        session_id = params.get("sessionId")
        user_key = self._session_owner.get(session_id)
        if user_key is None:
            return {"outcome": {"outcome": "cancelled"}}
        state = self._user(user_key)
        # 同一用户同时只挂一个审批
        if state.pending is not None and not state.pending.future.done():
            state.pending.future.set_result(False)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        state.pending = PendingApproval(params=params, future=fut)

        tool_call = params.get("toolCall") or {}
        call_id = str(tool_call.get("toolCallId") or "")
        tool = state.tool_titles.get(call_id) or tool_call.get("title") or call_id or "未知操作"
        options = params.get("options") or []
        await self._notify(
            user_key,
            "🔐 DSH 请求授权：\n"
            f"操作：{tool}\n"
            f"选项：{', '.join(o.get('name') or o.get('optionId', '') for o in options)}\n"
            "回复 /dsh yes 同意，/dsh no 拒绝",
        )
        # 需要用户「同意」——戳一戳提醒
        await self.poke(user_key)
        try:
            allow = await asyncio.wait_for(fut, timeout=300)
        except asyncio.TimeoutError:
            allow = False
        finally:
            state.pending = None
        await self._notify(user_key, "✅ 已同意" if allow else "⛔ 已拒绝")
        return permission_response(bool(allow))

    async def respond_permission(self, user_key: str, allow: bool) -> bool:
        """用户在 QQ 里回复 yes/no。"""
        state = self._user(user_key)
        pending = state.pending
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(allow)
        return True

    # ------------------------------------------------------------------ 选项提问
    async def ask_user_questions(self, user_key: str, questions: list) -> list:
        """把 DSH 的 elicitation 提问发到 QQ，等用户回答后返回 answers。"""
        if not questions:
            return []
        state = self._user(user_key)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        state.pending_question = PendingQuestion(questions=questions, future=fut)
        await self._notify(user_key, format_questions(questions))
        # 需要用户「选择」——戳一戳提醒
        await self.poke(user_key)
        try:
            answers = await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            answers = None
            await self._notify(user_key, "⌛ 提问等待超时，DSH 将收到「无人回答」。")
        finally:
            state.pending_question = None
        return answers or []

    async def answer_question(self, user_key: str, raw: str) -> bool:
        """用户在 QQ 里回答 elicitation 提问，返回是否命中待答提问。"""
        state = self._user(user_key)
        pending = state.pending_question
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(parse_answers(pending.questions, raw))
        return True

    def touch(self, user_key: str) -> None:
        self._user(user_key).last_active = time.time()

    def resolve_user_for_session(self, session_id: Optional[str]) -> Optional[str]:
        """把 DSH 会话映射到 QQ 用户；映射不到时回退到最近活跃用户。"""
        if session_id:
            owner = self._session_owner.get(session_id)
            if owner:
                return owner
        if not self._users:
            return None
        return max(self._users.items(), key=lambda kv: kv[1].last_active)[0]

    # ------------------------------------------------------------------ 更新分发
    async def _on_update(self, params: dict) -> None:
        session_id = params.get("sessionId")
        user_key = self._session_owner.get(session_id)
        if user_key is None:
            return
        update = params.get("update") or {}
        state = self._user(user_key)

        # 先登记工具标题，审批请求只给 callId，需要用这里补出人话
        kind = update.get("sessionUpdate")
        if kind in ("tool_call", "tool_call_update"):
            tool_id = update.get("toolCallId")
            title = update.get("title") or update.get("kind")
            if tool_id and title:
                state.tool_titles[str(tool_id)] = str(title)

        text = _extract_text(update)
        if text:
            state.text_buffer += text
            state.dirty = True
            self._wake.set()
            return

        brief = _summarize_tool_call(update)
        if brief:
            await self._flush_user(user_key, force=True)
            await self._notify(user_key, brief)

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=FLUSH_INTERVAL)
                self._wake.clear()
            except asyncio.TimeoutError:
                pass
            for user_key in list(self._users):
                await self._flush_user(user_key)

    # ------------------------------------------------------------------ outbox 文件外发
    async def _outbox_loop(self) -> None:
        while True:
            await asyncio.sleep(OUTBOX_INTERVAL)
            try:
                await self.scan_outbox()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._on_log(f"outbox 扫描失败: {exc!r}")

    async def scan_outbox(self) -> int:
        """扫描各活跃工作区的 outbox 目录，把新文件发给对应用户。

        扫描节奏：agent 只要把文件放进 ``<工作区>/.qq-outbox/``，最多几秒后
        就会通过 QQ 发出；发送成功或失败都会记录，避免重复刷屏。
        """
        if self._file_sender is None:
            return 0
        count = 0
        for user_key, state in list(self._users.items()):
            if not state.cwd:
                continue
            outbox = Path(state.cwd) / OUTBOX_DIR
            if not outbox.is_dir():
                continue
            for entry in sorted(outbox.iterdir()):
                if entry.is_dir() or not entry.is_file():
                    continue
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                marker = f"{entry.name}:{stat.st_mtime_ns}:{stat.st_size}"
                if marker in state.sent_files:
                    continue
                try:
                    await self._file_sender(user_key, str(entry))
                except Exception as exc:  # noqa: BLE001
                    self._on_log(f"发送文件失败 {entry.name}: {exc!r}")
                    state.sent_files.add(marker)
                    continue
                state.sent_files.add(marker)
                count += 1
                # 归档到 .sent/，保持 outbox 干净
                try:
                    archive = outbox / ".sent"
                    archive.mkdir(exist_ok=True)
                    target = archive / entry.name
                    if target.exists():
                        target = archive / f"{int(time.time())}-{entry.name}"
                    entry.replace(target)
                except OSError:
                    pass
        return count

    async def _flush_user(self, user_key: str, force: bool = False) -> None:
        state = self._users.get(user_key)
        if state is None or not state.text_buffer:
            return
        now = time.time()
        if not force and now - state.last_flush < FLUSH_INTERVAL:
            return
        buffered = state.text_buffer
        state.text_buffer = ""
        state.last_flush = now
        state.dirty = False
        await self._notify(user_key, buffered)
