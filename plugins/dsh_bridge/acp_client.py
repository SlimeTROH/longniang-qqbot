"""DSH ACP 客户端：以 JSON-RPC over stdio 驱动 ``dsh --profile acp``。

协议：Agent Client Protocol (ACP) v1，外加 DSH 扩展方法
(``session/list`` / ``session/resume`` / ``session/close`` / ``session/set_config_option``)。

实现说明：Windows 上 ``asyncio.create_subprocess_exec`` 使用命名管道，
在受限环境下会被拒绝；这里改用同步 ``subprocess``（匿名管道）+ 读取线程，
再通过 ``call_soon_threadsafe`` 把消息投递回事件循环。

参考实现：
  D:\\DeepSeek-Harness\\packages\\acp\\acp\\README.md
  D:\\DeepSeek-Harness\\packages\\acp\\acp\\tests\\harness.ts
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import threading
from typing import Any, Awaitable, Callable, Optional

# ---- 客户端 -> 服务端 方法名 ----
M_INITIALIZE = "initialize"
M_SESSION_NEW = "session/new"
M_SESSION_LIST = "session/list"
M_SESSION_RESUME = "session/resume"
M_SESSION_CLOSE = "session/close"
M_SESSION_SET_CONFIG = "session/set_config_option"
M_SESSION_PROMPT = "session/prompt"
M_SESSION_CANCEL = "session/cancel"

# ---- 服务端 -> 客户端 ----
N_SESSION_UPDATE = "session/update"
R_REQUEST_PERMISSION = "session/request_permission"

# ACP 权限选项（见 packages/acp/acp/src/index.ts）
OPTION_ALLOW_ONCE = "allow-once"
OPTION_REJECT_ONCE = "reject-once"

PROTOCOL_VERSION = 1

SessionUpdateHandler = Callable[[dict], Awaitable[None]]
PermissionHandler = Callable[[dict], Awaitable[dict]]
StderrHandler = Callable[[str], None]


class AcpError(Exception):
    """ACP 调用或传输错误。"""


class AcpClient:
    """一个 ``dsh --profile acp`` 子进程的异步 JSON-RPC 客户端。"""

    def __init__(
        self,
        *,
        node: str,
        repo: str,
        dsh_home: str,
        profile: str = "acp",
        on_session_update: Optional[SessionUpdateHandler] = None,
        on_permission: Optional[PermissionHandler] = None,
        on_stderr: Optional[StderrHandler] = None,
    ) -> None:
        self._node = node
        self._repo = repo
        self._dsh_home = dsh_home
        self._profile = profile
        self._on_session_update = on_session_update
        self._on_permission = on_permission
        self._on_stderr = on_stderr

        self._proc: Optional[subprocess.Popen] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._threads: list[threading.Thread] = []
        self._write_lock = threading.Lock()
        self._closed = False
        # sessionId -> configOptions（session/new、resume、set_config 的响应）
        self._config_options: dict[str, list] = {}

    # ------------------------------------------------------------------ 生命周期
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    async def start(self) -> None:
        if self._proc is not None:
            return
        self._loop = asyncio.get_running_loop()
        env = os.environ.copy()
        env["DSH_HOME"] = self._dsh_home
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            [self._node, "apps/cli/lib/bin.js", "--profile", self._profile],
            cwd=self._repo,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=creationflags,
        )
        self._closed = False
        for target in (self._read_loop_thread, self._stderr_loop_thread):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    async def stop(self) -> None:
        self._closed = True
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                await asyncio.to_thread(proc.wait, 5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._fail_pending(AcpError("ACP 客户端已停止"))

    async def __aenter__(self) -> "AcpClient":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ------------------------------------------------------------------ 读取线程
    def _read_loop_thread(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for raw in iter(proc.stdout.readline, b""):
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    if self._on_stderr is not None:
                        self._on_stderr(f"[acp] 非 JSON 输出: {text[:200]}")
                    continue
                loop = self._loop
                if loop is not None and not loop.is_closed():
                    loop.call_soon_threadsafe(self._spawn_dispatch, msg)
        except Exception as exc:  # pragma: no cover
            if self._on_stderr is not None:
                self._on_stderr(f"[acp] 读取线程异常: {exc!r}")
        finally:
            loop = self._loop
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self._fail_pending, AcpError("ACP 连接已关闭"))

    def _spawn_dispatch(self, msg: dict) -> None:
        """在事件循环线程里为一条消息创建处理任务。"""
        asyncio.ensure_future(self._dispatch(msg))

    def _stderr_loop_thread(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            for raw in iter(proc.stderr.readline, b""):
                if self._on_stderr is not None:
                    self._on_stderr(raw.decode("utf-8", "replace").rstrip())
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ 传输层
    async def _send(self, obj: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AcpError("ACP 进程未启动")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

        def _write() -> None:
            with self._write_lock:
                stdin = proc.stdin
                if stdin is None:
                    raise AcpError("ACP stdin 不可用")
                stdin.write(data)
                stdin.flush()

        await asyncio.to_thread(_write)

    async def _dispatch(self, msg: dict) -> None:
        # 1) 对请求的响应
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if fut is not None and not fut.done():
                if "error" in msg:
                    err = msg.get("error") or {}
                    fut.set_exception(AcpError(f"{err.get('code')}: {err.get('message')}"))
                else:
                    fut.set_result(msg["result"])
            return

        method = msg.get("method")

        # 2) 服务端通知：会话更新流
        if method == N_SESSION_UPDATE:
            if self._on_session_update is not None:
                try:
                    maybe = self._on_session_update(msg.get("params", {}))
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception as exc:  # 通知处理失败不影响协议
                    if self._on_stderr is not None:
                        self._on_stderr(f"[acp] update 处理失败: {exc!r}")
            return

        # 3) 服务端请求（带 id）：如权限审批
        if "id" in msg:
            await self._handle_server_request(msg)
            return

        # 4) 其余通知：忽略

    async def _handle_server_request(self, msg: dict) -> None:
        method = msg.get("method")
        try:
            if method == R_REQUEST_PERMISSION:
                if self._on_permission is None:
                    result = {"outcome": {"outcome": "cancelled"}}
                else:
                    maybe = self._on_permission(msg.get("params", {}))
                    result = await maybe if inspect.isawaitable(maybe) else maybe
            else:
                raise AcpError(f"未支持的 ACP 请求: {method}")
            await self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except Exception as exc:
            if self._on_stderr is not None:
                self._on_stderr(f"[acp] 请求 {method} 处理失败: {exc!r}")
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32603, "message": f"client handler failed: {exc}"},
                }
            )

    async def _request(
        self,
        method: str,
        params: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        if self._closed:
            raise AcpError("ACP 客户端已关闭")
        if self._proc is None:
            raise AcpError("ACP 进程未启动")
        req_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)
        if timeout is None:
            return await fut
        return await asyncio.wait_for(fut, timeout=timeout)

    # ------------------------------------------------------------------ 高层 API
    async def initialize(self, client_capabilities: Optional[dict] = None) -> dict:
        """握手。返回 agentInfo / agentCapabilities。"""
        return await self._request(
            M_INITIALIZE,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": client_capabilities or {},
            },
            timeout=60,
        )

    async def new_session(self, cwd: str, mcp_servers: Optional[list] = None) -> str:
        """新建会话，返回 sessionId。``cwd`` 即工作区绝对路径。"""
        res = await self._request(
            M_SESSION_NEW,
            {"cwd": cwd, "mcpServers": mcp_servers or []},
            timeout=180,
        )
        session_id = res["sessionId"]
        self._config_options[session_id] = res.get("configOptions", []) or []
        return session_id

    async def list_sessions(self, cwd: Optional[str] = None) -> list:
        """列出持久化的可恢复会话（最新在前）。"""
        params: dict[str, Any] = {}
        if cwd:
            params["cwd"] = cwd
        res = await self._request(M_SESSION_LIST, params, timeout=60)
        return res.get("sessions", [])

    async def resume_session(
        self, session_id: str, cwd: str, mcp_servers: Optional[list] = None
    ) -> str:
        res = await self._request(
            M_SESSION_RESUME,
            {"sessionId": session_id, "cwd": cwd, "mcpServers": mcp_servers or []},
            timeout=180,
        )
        resumed = res.get("sessionId", session_id)
        if "configOptions" in res:
            self._config_options[resumed] = res.get("configOptions") or []
        return resumed

    async def close_session(self, session_id: str) -> None:
        await self._request(M_SESSION_CLOSE, {"sessionId": session_id}, timeout=60)
        self._config_options.pop(session_id, None)

    async def set_config_option(self, session_id: str, config_id: str, value: str) -> dict:
        """切换 model / reasoning_effort 等配置项。``value`` 必须是公布选项的 value。"""
        res = await self._request(
            M_SESSION_SET_CONFIG,
            {"sessionId": session_id, "configId": config_id, "value": value},
            timeout=60,
        )
        if "configOptions" in res:
            self._config_options[session_id] = res.get("configOptions") or []
        return res

    # ------------------------------------------------------------------ 配置项解析
    def config_options(self, session_id: str) -> list:
        return self._config_options.get(session_id, [])

    def model_options(self, session_id: str) -> list[dict]:
        """把 model 配置项扁平化成 ``[{"name":..., "value":...}, ...]``。"""
        for option in self._config_options.get(session_id, []):
            if option.get("id") != "model":
                continue
            flat: list[dict] = []

            def walk(items: list) -> None:
                for item in items:
                    if isinstance(item, dict) and "options" in item:
                        walk(item["options"] or [])
                    elif isinstance(item, dict) and "value" in item:
                        flat.append({"name": item.get("name") or item["value"], "value": item["value"]})

            walk(option.get("options") or [])
            return flat
        return []

    def resolve_model_value(self, session_id: str, query: str) -> Optional[str]:
        """把用户输入的模型名/片段解析成合法的 option value。"""
        text = (query or "").strip().lower()
        if not text:
            return None
        options = self.model_options(session_id)
        for opt in options:
            if opt["value"] == query:
                return opt["value"]
        for opt in options:
            if (opt["name"] or "").lower() == text:
                return opt["value"]
        for opt in options:
            if text in (opt["name"] or "").lower() or text in opt["value"].lower():
                return opt["value"]
        return None

    async def prompt(self, session_id: str, text: str) -> dict:
        """发送一条用户指令；返回 ``{"stopReason": ...}``。"""
        return await self._request(
            M_SESSION_PROMPT,
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            },
        )

    async def cancel(self, session_id: str) -> None:
        """取消当前 prompt（通知，无响应）。"""
        await self._send(
            {
                "jsonrpc": "2.0",
                "method": M_SESSION_CANCEL,
                "params": {"sessionId": session_id},
            }
        )


def permission_response(allow: bool) -> dict:
    """构造 ``session/request_permission`` 的响应体。"""
    return {
        "outcome": {
            "outcome": "selected",
            "optionId": OPTION_ALLOW_ONCE if allow else OPTION_REJECT_ONCE,
        }
    }


def permission_cancelled() -> dict:
    return {"outcome": {"outcome": "cancelled"}}
