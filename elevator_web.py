"""Serve the standalone elevator game next to the QQbot SQLite saves."""
from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import sqlite3
import time
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from elevator_web_auth import REMEMBERED_EXPIRES_AT, AuthError, AuthStore
from elevator_web_feed import GameFeed
from elevator_web_game import WebGame, rules
from elevator_web_sync import SnapshotCache

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web" / "elevator_web"
AUTH_DB = ROOT / "state" / "elevator_web_auth.sqlite3"
GAME_DB = ROOT / "state" / "endless_elevator.sqlite3"
SESSION_COOKIE = "elevator_session"
PREFIX = "/elevator"
API = PREFIX + "/api"
MAX_BODY = 4096
BROWSER_COOKIE_MAX_AGE = 400 * 24 * 60 * 60
STATIC_ASSETS = ("style.css", "inventory.css", "inventory.js", "station.js", "rtc.js", "app.js", "shaft.webp")


@lru_cache(maxsize=32)
def _asset_digest(path, modified, size):
    return sha256(path.read_bytes()).hexdigest()[:16]


def asset_version(name):
    path = WEB / name
    info = path.stat()
    return _asset_digest(path, info.st_mtime_ns, info.st_size)


class RequestError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def error(status: int, code: str, message: str, **extra):
    return JSONResponse({"error": {"code": code, "message": message}, **extra}, status)


def canonical_origin(value: str, *, local: bool = False) -> str:
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ValueError("无效的站点地址") from None
    schemes = {"http"} if local else {"https"}
    if parts.scheme not in schemes or not host or parts.username is not None or parts.path not in ("", "/"):
        raise ValueError("站点地址必须是完整的站点来源")
    if parts.query or parts.fragment or any(c.isspace() or c in "\\%@" for c in value):
        raise ValueError("站点地址包含无效字符")
    if local and host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("HTTP 测试模式仅支持本机地址")
    shown_host = f"[{host}]" if ":" in host else host
    default_port = 80 if local else 443
    return f"{parts.scheme}://{shown_host}" + (f":{port}" if port not in (None, default_port) else "")


async def read_json(request: Request, limit: int = MAX_BODY) -> dict:
    if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/json":
        raise RequestError(415, "json_required", "请使用 JSON 提交请求。")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise RequestError(413, "body_too_large", "提交内容超过长度限制。")
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeError, RecursionError):
        raise RequestError(400, "invalid_json", "提交内容无法读取。") from None
    if not isinstance(payload, dict):
        raise RequestError(400, "invalid_json", "提交内容必须是 JSON 对象。")
    return payload


def fields(payload, required, optional=()):
    if set(payload) - set((*required, *optional)) or any(k not in payload for k in required):
        raise RequestError(422, "invalid_fields", "请检查提交字段是否完整。")
    if any(not isinstance(v, str) for v in payload.values()):
        raise RequestError(422, "invalid_fields", "请求字段必须是文本。")
    return {key: payload[key] for key in (*required, *optional) if key in payload}


def public_account(account):
    return {"username": account["username"], "bound": bool(account.get("qq_id"))}


def create_app(game_path=GAME_DB, auth_path=AUTH_DB, *, public_origin="", local_http=False, rtc_port=None) -> FastAPI:
    if rtc_port is not None and (not local_http or not 1 <= rtc_port <= 65535):
        raise ValueError("本机 WebRTC 测试需要回环 HTTP 和有效的控制端口")
    fixed = canonical_origin(public_origin, local=local_http) if public_origin else ""
    auth = AuthStore(auth_path)
    game = WebGame(game_path, on_action=GameFeed(game_path))
    sync = SnapshotCache()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=3)

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):
        if request.method == "POST" and request.url.path.startswith(API + "/"):
            try:
                scheme = "http" if local_http else "https"
                host_origin = canonical_origin(f"{scheme}://{request.headers.get('host', '')}", local=local_http)
                origin = canonical_origin(request.headers.get("origin", ""), local=local_http)
                if origin != host_origin or (fixed and origin != fixed):
                    raise ValueError("origin mismatch")
                if request.headers.get("sec-fetch-site", "") in {"cross-site", "same-site"}:
                    raise ValueError("external fetch")
            except ValueError:
                return error(403, "origin_rejected", "请在游戏页面中发起操作。")
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith(PREFIX + "/static/") and response.status_code in (200, 304):
            name = request.url.path.removeprefix(PREFIX + "/static/")
            version = request.query_params.get("v")
            response.headers["Cache-Control"] = "public, no-cache"
            if name in STATIC_ASSETS and version and version == asset_version(name):
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(AuthError)
    async def auth_error(_request, exc):
        return error(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestError)
    async def invalid_request(_request, exc):
        return error(exc.status, exc.code, exc.message)

    @app.exception_handler(sqlite3.OperationalError)
    async def sqlite_error(_request, _exc):
        if isinstance(_exc, rules.GameMaintenance):
            return error(503, "maintenance", str(_exc))
        return error(503, "store_unavailable", "存档暂时忙，请稍后重试。")

    def source(request):
        return request.client.host if request.client is not None else "local"

    def token(request):
        return request.cookies.get(SESSION_COOKIE, "")

    def set_session_cookie(response, session_token, expires_at=None):
        max_age = None if expires_at is None else min(BROWSER_COOKIE_MAX_AGE, max(0, expires_at - int(time.time())))
        response.set_cookie(SESSION_COOKIE, session_token, max_age=max_age,
                            path=PREFIX, secure=not local_http, httponly=True, samesite="strict")

    def session_response(result, *, persistent=True):
        data = {"account": public_account(result["account"]), "expires_at": result["expires_at"]}
        if "challenge" in result:
            data["challenge"] = result["challenge"]
        response = JSONResponse(data)
        set_session_cookie(response, result["session"], result["expires_at"] if persistent else None)
        return response

    @app.get("/")
    @app.get(PREFIX)
    async def go_to_game():
        return RedirectResponse(PREFIX + "/", status_code=307)

    @app.get(PREFIX + "/")
    async def index():
        if await asyncio.to_thread(game.game.store.is_paused):
            return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                                '<meta http-equiv="refresh" content="30"><title>无尽电梯维护中</title><main style="max-width:32rem;margin:15vh auto;padding:1rem;font:1rem/1.7 system-ui">'
                                '<h1>无尽电梯维护中</h1><p>探险进度已保存，计时已暂停。开放后可以继续本局。</p>'
                                '<a href="/elevator/">刷新状态</a></main></html>', status_code=503)
        document = (WEB/"index.html").read_text(encoding="utf-8")
        if rtc_port is not None:
            document = document.replace("<!-- elevator-rtc -->", '<script src="/elevator/static/rtc.js" defer></script>')
        for name in STATIC_ASSETS:
            url = PREFIX + "/static/" + name
            if url in document:
                document = document.replace(url+'"', url+"?v="+asset_version(name)+'"')
        return HTMLResponse(document)

    @app.get(API + "/maintenance")
    async def maintenance():
        return {"paused": await asyncio.to_thread(game.game.store.is_paused)}

    @app.get(API + "/health")
    async def health():
        return {"service": "endless-elevator", "status": "ok"}

    @app.get(API + "/auth/session")
    async def get_session(request: Request, game_state: bool = False):
        session_token = token(request)
        try:
            result = await asyncio.to_thread(auth.get_session_info, session_token)
        except AuthError as exc:
            if exc.status_code != 401:
                raise
            return {"account": None}
        account = result["account"]
        payload = {"account": public_account(account)}
        if game_state and account.get("qq_id") and account.get("qq_group"):
            owner = (account["qq_id"], account["qq_group"])
            try:
                current = await asyncio.to_thread(game.state, *owner, account.get("nickname") or "乘客")
                payload.update(sync.pack(owner, current))
            except rules.GameMaintenance:
                payload["maintenance"] = True
        response = JSONResponse(payload)
        if result["expires_at"] == REMEMBERED_EXPIRES_AT:
            set_session_cookie(response, session_token, result["expires_at"])
        return response

    @app.post(API + "/auth/register", status_code=201)
    async def register(request: Request):
        args = fields(await read_json(request), ("username", "password", "qq_id"))
        result = await asyncio.to_thread(auth.register, **args, source=source(request))
        response = session_response(result)
        response.status_code = 201
        return response

    @app.post(API + "/auth/login")
    async def login(request: Request):
        payload = await read_json(request)
        legacy_cookie = "remember" not in payload
        remember = payload.pop("remember", False)
        if not isinstance(remember, bool):
            raise RequestError(422, "invalid_fields", "记住设备选项必须为布尔值。")
        args = fields(payload, ("username", "password"), ("login_type",))
        result = await asyncio.to_thread(auth.login, **args, source=source(request), remember=remember)
        return session_response(result, persistent=legacy_cookie or remember)

    @app.post(API + "/auth/logout")
    async def logout(request: Request):
        fields(await read_json(request), ())
        await asyncio.to_thread(auth.logout, token(request))
        response = JSONResponse({"account": None})
        response.delete_cookie(SESSION_COOKIE, path=PREFIX, secure=not local_http, httponly=True, samesite="strict")
        return response

    async def challenge(request, method: Callable, required, *, session=False):
        args = fields(await read_json(request), required)
        positional = (token(request),) if session else ()
        return {"challenge": await asyncio.to_thread(method, *positional, **args, source=source(request))}

    @app.post(API + "/auth/bind")
    async def bind(request: Request):
        return await challenge(request, auth.issue_bind, ("qq_id",), session=True)

    @app.post(API + "/auth/change")
    async def change(request: Request):
        return await challenge(request, auth.issue_change, ("current_password", "new_password"), session=True)

    @app.post(API + "/auth/reset")
    async def reset(request: Request):
        return await challenge(request, auth.issue_reset, ("username", "new_password"))

    async def bound_player(request: Request):
        account = await asyncio.to_thread(auth.get_session, token(request))
        if not account.get("qq_id") or not account.get("qq_group"):
            raise RequestError(403, "qq_unbound", "请先在 QQ 群完成账号绑定。")
        return account["qq_id"], account["qq_group"], account.get("nickname") or "乘客"

    if rtc_port is not None:
        def negotiate(sdp: str, session: str) -> dict:
            control = http.client.HTTPConnection("127.0.0.1", rtc_port, timeout=6)
            try:
                body = json.dumps({"sdp": sdp, "session": session}).encode()
                control.request("POST", "/offer", body, {"Content-Type": "application/json"})
                reply = control.getresponse()
                if reply.status != 200:
                    raise RequestError(503, "rtc_unavailable", "游戏直连暂不可用。")
                answer = json.loads(reply.read(48 * 1024))
                if answer.get("type") != "answer" or not isinstance(answer.get("sdp"), str):
                    raise ValueError("invalid answer")
                return {"type": "answer", "sdp": answer["sdp"]}
            except (OSError, http.client.HTTPException, ValueError, AttributeError):
                raise RequestError(503, "rtc_unavailable", "游戏直连暂不可用。") from None
            finally:
                control.close()

        @app.post(API + "/rtc/offer")
        async def rtc_offer(request: Request):
            await bound_player(request)
            offer = fields(await read_json(request, 40 * 1024), ("type", "sdp"))
            if offer["type"] != "offer" or len(offer["sdp"]) > 32 * 1024:
                raise RequestError(422, "invalid_offer", "无效的游戏连接请求。")
            return await asyncio.to_thread(negotiate, offer["sdp"], token(request))

    @app.get(API + "/game/state")
    async def state(request: Request):
        uid, group, nickname = await bound_player(request)
        current = await asyncio.to_thread(game.state, uid, group, nickname)
        return sync.pack((uid, group), current) if request.headers.get("x-elevator-sync") == "1" else current

    def game_response(result, request, owner, revision):
        data = {"state": result["state"], "reply": result.get("reply")}
        if request.headers.get("x-elevator-sync") == "1":
            base = "" if result.get("error") else request.headers.get("x-elevator-base", "")
            data = sync.pack(owner, result["state"], base, revision)
            if "result" not in result or result.get("error"):
                data["reply"] = result.get("reply")
        if "result" in result:
            data["result"] = result["result"]
        if result.get("error"):
            code = result["code"]
            status = {"stale": 409, "cooldown": 429}.get(code, 422)
            return error(status, code, result["error"], **data)
        return data

    @app.post(API + "/game/action")
    async def action(request: Request):
        args = fields(await read_json(request), ("action", "revision"), ("argument",))
        uid, group, nickname = await bound_player(request)
        try:
            result = await asyncio.to_thread(game.act, uid, group, args["action"], args.get("argument", ""),
                                             args["revision"], nickname)
        except ValueError as exc:
            raise RequestError(422, "invalid_action", "操作无效，请刷新后重新选择。") from exc
        return game_response(result, request, (uid, group), args["revision"])

    @app.post(API + "/game/command")
    async def command(request: Request):
        body = await read_json(request)
        params = body.pop("params", {})
        if not isinstance(params, dict):
            raise RequestError(422, "invalid_fields", "操作参数必须是 JSON 对象。")
        args = fields(body, ("command", "revision"))
        uid, group, nickname = await bound_player(request)
        try:
            result = await asyncio.to_thread(
                game.command, uid, group, args["command"], params, args["revision"], nickname,
            )
        except ValueError as exc:
            raise RequestError(422, "invalid_command", str(exc)) from exc
        return game_response(result, request, (uid, group), args["revision"])

    app.mount(PREFIX + "/static", StaticFiles(directory=WEB, check_dir=False), name="elevator-static")
    return app


def main():
    import uvicorn

    cli = argparse.ArgumentParser(description="无尽电梯网页服务")
    cli.add_argument("--port", type=int, default=8092)
    cli.add_argument("--public-origin", default="", help="固定 HTTPS 域名，例如 https://elevator.example.com")
    cli.add_argument("--local-http", action="store_true", help="允许回环 HTTP 开发测试")
    cli.add_argument("--local-rtc-port", type=int, help="回环测试实例的 WebRTC 控制端口")
    cli.add_argument("--game-db", type=Path, default=GAME_DB)
    cli.add_argument("--auth-db", type=Path, default=AUTH_DB)
    args = cli.parse_args()
    if args.game_db.resolve() == args.auth_db.resolve():
        cli.error("游戏与账号存档需要使用两个数据库")
    app = create_app(args.game_db, args.auth_db, public_origin=args.public_origin, local_http=args.local_http,
                     rtc_port=args.local_rtc_port)
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=not args.local_http,
                forwarded_allow_ips="127.0.0.1", limit_concurrency=64, access_log=False, server_header=False)


if __name__ == "__main__":
    main()
