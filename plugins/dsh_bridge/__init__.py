"""DSH 远程操控插件。

通过 QQ 驱动一个独立的 DSH（DeepSeek Harness）工作会话：
  * 新建 / 列出 / 切换 / 关闭 会话与工作区
  * 发送指令并接收流式输出
  * 审批 DSH 的授权请求（同意 / 拒绝）
  * 切换模型、取消任务

底层走官方 ACP 协议（``dsh --profile acp``，JSON-RPC over stdio）。

配置（写在 .env 里）：
  DSH_BRIDGE_NODE       node.exe 绝对路径
  DSH_BRIDGE_REPO       DeepSeek-Harness 仓库路径
  DSH_BRIDGE_HOME       DSH_HOME（缺省用当前用户的 ~/.dsh）
  DSH_BRIDGE_WORKSPACE  默认为会话工作区
  DSH_BRIDGE_PROFILE    profile 名，默认 acp
  DSH_BRIDGE_ALLOWLIST  允许使用的 QQ 号，逗号分隔（留空=不限制，危险）
  DSH_BRIDGE_ENABLED    设为 0 可关闭插件
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from nonebot import get_bots, get_driver, logger, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.exception import NoneBotException
from nonebot.params import CommandArg
from nonebot.rule import Rule

from .acp_client import AcpError
from .ask_server import AskHttpServer
from .bridge import DshBridge
from .state_store import first_writable

driver = get_driver()

# ---------------------------------------------------------------------- 配置读取
def _env(name: str, default: str = "") -> str:
    """读配置：优先 NoneBot2 config（.env 经 pydantic 加载），回退到进程环境变量。"""
    raw = getattr(driver.config, name.lower(), None)
    if raw is None or raw == "":
        raw = os.environ.get(name, None)
    if raw is None:
        return default
    text = str(raw).strip()
    return text or default


NODE = _env("DSH_BRIDGE_NODE", r"D:\node-v24.19.0-win-x64\node.exe")
REPO = _env("DSH_BRIDGE_REPO", r"D:\DeepSeek-Harness")
HOME = _env("DSH_BRIDGE_HOME") or str(Path.home() / ".dsh")
WORKSPACE = _env("DSH_BRIDGE_WORKSPACE", r"D:\dshex")
PROFILE = _env("DSH_BRIDGE_PROFILE", "acp")
# 会话/工作区绑定的持久化文件。
# 优先放在机器人目录下的 state/；若该位置不可写（受限环境），自动降级到
# 其它候选位置，避免因权限问题丢掉「重启自动恢复」能力。
_BOT_DIR = Path(__file__).resolve().parents[2]
STATE_PATH = _env("DSH_BRIDGE_STATE") or str(
    first_writable(
        [
            _BOT_DIR / "state" / "dsh_sessions.json",
            _BOT_DIR / "dsh_sessions.json",
            Path(tempfile.gettempdir()) / "qqbot-dsh-sessions.json",
        ]
    )
)
ASK_HOST = _env("DSH_BRIDGE_ASK_HOST", "127.0.0.1")
try:
    ASK_PORT = int(_env("DSH_BRIDGE_ASK_PORT", "8090") or "8090")
except ValueError:
    ASK_PORT = 8090
ENABLED = _env("DSH_BRIDGE_ENABLED", "0") not in ("0", "false", "False", "")

_allow_raw = _env("DSH_BRIDGE_ALLOWLIST")
ALLOWLIST: set[str] = {x.strip() for x in _allow_raw.split(",") if x.strip()}

bridge: Optional[DshBridge] = None
ask_server: Optional[AskHttpServer] = None

HELP_TEXT = """🤖 DSH 远程操控
/dsh                 查看状态与帮助
/dsh new [工作区]     新建会话
/dsh ls [工作区]      列出可恢复会话
/dsh use <会话号> [工作区]  切换会话
/dsh close           关闭当前会话
/dsh model <名称>     切换模型
/dsh cancel          取消当前任务
/dsh yes | /dsh no   同意 / 拒绝 DSH 的授权请求
/dsh ans <答案>      回答 DSH 的选项提问
/dsh run <内容>       向 DSH 发送指令

💡 直接发图片/文件给我，会自动存到工作区的 .qq-inbox/
💡 需要你同意/选择/任务完成时，我会戳一戳提醒你"""


def _user_key(event: MessageEvent) -> str:
    """把事件归一成稳定的用户键：群聊区分群，私聊用用户号。"""
    group_id = getattr(event, "group_id", None)
    if group_id:
        return f"g{group_id}:{event.user_id}"
    return f"p{event.user_id}"


def _is_allowed(user_key: str, event: MessageEvent) -> bool:
    if not ALLOWLIST:
        return True
    return str(event.user_id) in ALLOWLIST


async def _notify(user_key: str, text: str) -> None:
    """把文本推送到 QQ。"""
    bots = get_bots()
    if not bots:
        logger.warning("dsh_bridge: 当前没有已连接的 bot，无法推送")
        return
    bot: Bot = next(iter(bots.values()))
    if user_key.startswith("g") and ":" in user_key:
        group_id = int(user_key[1:].split(":", 1)[0])
        await bot.call_api("send_group_msg", group_id=group_id, message=text)
    else:
        user_id = int(user_key[1:])
        await bot.call_api("send_private_msg", user_id=user_id, message=text)


# 这些扩展名按「图片消息」发送（QQ 内联预览），其余按「文件上传」
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


async def _send_file(user_key: str, path: str) -> None:
    """把本地文件通过 QQ 发给用户。

    图片走 image 消息段（可在会话内直接预览），其它类型走 OneBot 的
    文件上传接口（upload_group_file / upload_private_file）。
    NapCat 与机器人同机，因此使用 file:// URI 指向本地绝对路径。
    """
    bots = get_bots()
    if not bots:
        raise RuntimeError("当前没有已连接的 bot，无法发送文件")
    bot: Bot = next(iter(bots.values()))
    file_path = Path(path).resolve()
    name = file_path.name
    uri = file_path.as_uri()
    is_image = file_path.suffix.lower() in IMAGE_EXTS

    if user_key.startswith("g") and ":" in user_key:
        group_id = int(user_key[1:].split(":", 1)[0])
        if is_image:
            await bot.call_api(
                "send_group_msg",
                group_id=group_id,
                message=[{"type": "image", "data": {"file": uri}}],
            )
        else:
            await bot.call_api("upload_group_file", group_id=group_id, file=uri, name=name)
    else:
        user_id = int(user_key[1:])
        if is_image:
            await bot.call_api(
                "send_private_msg",
                user_id=user_id,
                message=[{"type": "image", "data": {"file": uri}}],
            )
        else:
            await bot.call_api("upload_private_file", user_id=user_id, file=uri, name=name)

    await _notify(user_key, f"{'🖼️ 已发送图片' if is_image else '📎 已发送文件'}：{name}")


async def _poke(user_key: str) -> None:
    """在重要节点「戳一戳」用户，作为额外提醒。

    不同 OneBot 实现对戳一戳的接口名不一致（NapCat 群聊用 ``send_poke``、
    私聊用 ``friend_poke``），这里逐个尝试；全部失败会抛给 bridge 记日志，
    不影响主流程。
    """
    bots = get_bots()
    if not bots:
        return
    bot: Bot = next(iter(bots.values()))
    if user_key.startswith("g") and ":" in user_key:
        group_part, user_part = user_key[1:].split(":", 1)
        await bot.call_api("send_poke", group_id=int(group_part), user_id=int(user_part))
        return
    user_id = int(user_key[1:])
    last_error: Optional[Exception] = None
    for action in ("friend_poke", "send_poke"):
        try:
            await bot.call_api(action, user_id=user_id)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    if last_error is not None:
        raise last_error


# ---------------------------------------------------------------------- 接收用户发来的图片/文件
# 收件箱目录名（相对工作区）
INBOX_DIR = ".qq-inbox"


def _attachments_of(event: MessageEvent) -> list:
    """取出消息里的图片/文件消息段。"""
    return [seg for seg in event.get_message() if seg.type in ("image", "file")]


def _has_attachment(event: MessageEvent) -> bool:
    """Rule：只在消息确实带图片/文件时才触发接收逻辑。"""
    return bool(_attachments_of(event))


def _local_from_file_field(value: object) -> Optional[Path]:
    """把 OneBot 的 ``file`` 字段解析成本地路径（若它本来就是）。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("file://"):
        parsed = urllib.parse.urlparse(text)
        candidate = Path(urllib.request.url2pathname(parsed.path))
    else:
        candidate = Path(text)
    try:
        return candidate if candidate.is_file() else None
    except OSError:
        return None


def _guess_ext(data: bytes) -> str:
    """按文件头（magic bytes）判断真实类型，比 URL 后缀可靠。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    if data[:4] == b"%PDF":
        return ".pdf"
    if data[:2] == b"PK":
        return ".zip"
    if data[:4] == b"\x1f\x8b\x08\x00" or data[:2] == b"\x1f\x8b":
        return ".gz"
    return ".bin"


async def _save_attachment(bot: Bot, segment, inbox: Path) -> Optional[Path]:
    """把图片/文件落到本地收件箱，返回保存路径。

    优先用本地路径（NapCat 与机器人同机），其次按 URL 下载。
    """
    data = dict(segment.data or {})
    inbox.mkdir(parents=True, exist_ok=True)

    local = _local_from_file_field(data.get("file"))
    if local is not None:
        if local.parent == inbox:
            # 已经在收件箱里，直接用，避免 SameFileError
            return local
        target = inbox / local.name
        if target.exists():
            # 同名不覆盖，加时间戳前缀
            target = inbox / f"{int(time.time() * 1000)}-{local.name}"
        try:
            shutil.copy2(local, target)
            return target
        except shutil.SameFileError:
            return local
        except OSError as exc:
            logger.warning(f"dsh_bridge: 复制附件失败 {exc!r}")

    url = str(data.get("url") or "").strip()
    if url.startswith("http"):
        # 先用 URL 后缀占位，下载后再按文件头纠正
        suffix = Path(urllib.parse.urlparse(url).path).suffix or ".bin"
        target = inbox / f"{int(time.time() * 1000)}{suffix}"

        def _download() -> bytes:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return resp.read()

        try:
            payload = await asyncio.to_thread(_download)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"dsh_bridge: 下载附件失败 {exc!r}")
            return None

        # 按真实文件头修正后缀（QQ 的图片常常给不出正确后缀）
        real_ext = _guess_ext(payload)
        if real_ext != suffix:
            target = target.with_suffix(real_ext)
        try:
            target.write_bytes(payload)
            return target
        except OSError as exc:
            logger.warning(f"dsh_bridge: 写入附件失败 {exc!r}")
    return None


def _accept_files_mode() -> str:
    """附件接收策略。

    none    —— 完全不接收
    private —— 只在私聊接收（默认，最安全）
    all     —— 私聊和群聊都接收（不建议，群里会被刷屏且泄露路径）
    """
    return _env("DSH_BRIDGE_ACCEPT_FILES", "private").lower()


def _should_accept_files(event: MessageEvent) -> bool:
    """判断这条消息的附件是否该被接收。"""
    mode = _accept_files_mode()
    if mode == "none":
        return False
    if mode == "all":
        return True
    # 默认：只认私聊
    return getattr(event, "message_type", "") == "private"


incoming = on_message(rule=Rule(_has_attachment), priority=50, block=False)


@incoming.handle()
async def _handle_incoming(bot: Bot, event: MessageEvent) -> None:
    """接收私聊发来的图片/文件，保存到当前工作区的收件箱。

    安全策略（重要）：
      - 默认【只在私聊】接收；群里发来的附件一律静默忽略，不保存、不回复。
      - 只有在白名单里的用户才会被处理。
      - 群聊里绝不回复，避免刷屏和泄露本地路径。
    """
    if bridge is None:
        return
    # 群里不收：直接静默返回（不调用 finish，免得在群里冒泡）
    if not _should_accept_files(event):
        return
    user_key = _user_key(event)
    if not _is_allowed(user_key, event):
        return

    bridge.touch(user_key)
    state = bridge.state_of(user_key)
    inbox = Path(state.cwd or WORKSPACE) / INBOX_DIR

    saved: list[str] = []
    for segment in _attachments_of(event):
        try:
            target = await _save_attachment(bot, segment, inbox)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"dsh_bridge: 保存附件失败 {exc!r}")
            continue
        if target is not None:
            saved.append(target.name)

    # 这里是私聊，回复里带路径是安全的（只有你自己看得到）
    if saved:
        listing = "\n".join(f"• {name}" for name in saved)
        await incoming.finish(
            f"📥 已接收并保存：\n{listing}\n目录：{inbox}\n"
            "需要我处理时，直接说（例如：把 .qq-inbox 里的图片转成 PNG）"
        )
    else:
        await incoming.finish("⚠️ 收到附件但没能保存到本地，请查看机器人日志。")


# ---------------------------------------------------------------------- 生命周期
@driver.on_startup
async def _bridge_startup() -> None:
    global bridge
    if not ENABLED:
        logger.info("dsh_bridge: 已在配置中禁用（DSH_BRIDGE_ENABLED=0）")
        return
    bridge = DshBridge(
        node=NODE,
        repo=REPO,
        dsh_home=HOME,
        default_workspace=WORKSPACE,
        profile=PROFILE,
        allowlist=ALLOWLIST,
        on_log=lambda msg: logger.debug(f"dsh_bridge: {msg}"),
        state_path=STATE_PATH,
    )
    bridge.set_notifier(_notify)
    bridge.set_file_sender(_send_file)
    bridge.set_poker(_poke)
    try:
        await bridge.start()
        restored = bridge.restored_count
        logger.success(
            f"dsh_bridge: DSH ACP 已就绪（profile={PROFILE}, home={HOME}）"
            + (f"，已恢复 {restored} 个会话绑定" if restored else "")
        )
        if restored:
            logger.info(f"dsh_bridge: 会话绑定文件 {STATE_PATH}")
    except Exception as exc:
        logger.error(f"dsh_bridge: DSH ACP 启动失败：{exc!r}")
        bridge = None
        return
    # 启动提问接收端点（供 DSH 的 dsh-qq-bridge 插件回调）
    global ask_server
    try:
        ask_server = AskHttpServer(ASK_HOST, ASK_PORT, bridge.ask_user_questions)
        ask_server.set_user_resolver(bridge.resolve_user_for_session)
        await ask_server.start()
        logger.success(f"dsh_bridge: 提问端点 http://{ASK_HOST}:{ASK_PORT}/ask")
    except Exception as exc:
        logger.error(f"dsh_bridge: 提问端点启动失败：{exc!r}")
        ask_server = None


@driver.on_shutdown
async def _bridge_shutdown() -> None:
    global bridge, ask_server
    if ask_server is not None:
        await ask_server.stop()
        ask_server = None
    if bridge is not None:
        await bridge.stop()
        bridge = None


# ---------------------------------------------------------------------- 命令
dsh_cmd = on_command("dsh", priority=5, block=True)


@dsh_cmd.handle()
async def _handle_dsh(event: MessageEvent, arg: Message = CommandArg()) -> None:
    if bridge is None:
        await dsh_cmd.finish("⚠️ DSH 桥接未启用或初始化失败，请查看机器人日志。")
    user_key = _user_key(event)
    if not _is_allowed(user_key, event):
        await dsh_cmd.finish("⛔ 你没有使用 DSH 远程操控的权限。")
    bridge.touch(user_key)

    parts = arg.extract_plain_text().strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    try:
        if sub in ("", "help", "状态"):
            await _cmd_status(user_key)
        elif sub == "new":
            await _cmd_new(user_key, rest)
        elif sub == "ls":
            await _cmd_list(user_key, rest)
        elif sub == "use":
            await _cmd_use(user_key, rest)
        elif sub == "close":
            await _cmd_close(user_key)
        elif sub == "model":
            await _cmd_model(user_key, rest)
        elif sub == "cancel":
            await bridge.cancel(user_key)
            await dsh_cmd.finish("🛑 已发送取消请求。")
        elif sub in ("yes", "y", "同意"):
            ok = await bridge.respond_permission(user_key, True)
            await dsh_cmd.finish("✅ 已同意。" if ok else "ℹ️ 当前没有待处理的授权请求。")
        elif sub in ("no", "n", "拒绝"):
            ok = await bridge.respond_permission(user_key, False)
            await dsh_cmd.finish("⛔ 已拒绝。" if ok else "ℹ️ 当前没有待处理的授权请求。")
        elif sub in ("ans", "answer", "答"):
            if not rest:
                await dsh_cmd.finish("用法：/dsh ans <答案>（选项号，多个用逗号分隔）")
            ok = await bridge.answer_question(user_key, rest)
            await dsh_cmd.finish("✅ 已提交你的选择。" if ok else "ℹ️ 当前没有待回答的提问。")
        elif sub in ("run", "ask"):
            if not rest:
                await dsh_cmd.finish("用法：/dsh run <要交给 DSH 的任务>")
            await dsh_cmd.send(f"🚀 已交给 DSH：{rest}")
            await bridge.send_prompt(user_key, rest)
        else:
            # 未识别的子命令：整段当作指令发给 DSH
            await dsh_cmd.send("🚀 已交给 DSH（未识别为子命令，按指令处理）")
            await bridge.send_prompt(user_key, arg.extract_plain_text().strip())
    except NoneBotException:
        # finish/reject/pause/stop_propagation 都是 NoneBot 的控制流异常，
        # 必须原样抛出，否则会被下面的兜底 except 误当成错误。
        raise
    except AcpError as exc:
        await dsh_cmd.finish(f"⚠️ DSH 错误：{exc}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("dsh_bridge: 命令处理失败")
        await dsh_cmd.finish(f"⚠️ 处理失败：{exc}")


async def _cmd_status(user_key: str) -> None:
    assert bridge is not None
    state = bridge.state_of(user_key)
    lines = [HELP_TEXT, "", "— 当前状态 —"]
    lines.append(f"会话：{state.session_id or '（未创建）'}")
    lines.append(f"工作区：{state.cwd or WORKSPACE}")
    if state.session_id and not state.active:
        lines.append("状态：🕓 待恢复（下次发指令时自动 resume）")
    else:
        lines.append(f"运行中：{'是' if state.busy else '否'}")
    await dsh_cmd.finish("\n".join(lines))


async def _cmd_new(user_key: str, rest: str) -> None:
    assert bridge is not None
    cwd = rest or None
    sid = await bridge.new_session(user_key, cwd)
    await dsh_cmd.finish(
        f"🆕 已新建会话\n会话号：{sid}\n工作区：{bridge.state_of(user_key).cwd}\n"
        "现在可以 /dsh run <任务>"
    )


async def _cmd_list(user_key: str, rest: str) -> None:
    assert bridge is not None
    sessions = await bridge.list_sessions(rest or None)
    if not sessions:
        await dsh_cmd.finish("（没有可恢复的会话）")
    lines = ["📋 可恢复会话："]
    for i, item in enumerate(sessions[:20], 1):
        lines.append(f"{i}. {item.get('sessionId')}\n   工作区：{item.get('cwd')}")
    lines.append("\n用 /dsh use <会话号> 切换")
    await dsh_cmd.finish("\n".join(lines))


async def _cmd_use(user_key: str, rest: str) -> None:
    assert bridge is not None
    if not rest:
        await dsh_cmd.finish("用法：/dsh use <会话号> [工作区]")
    token, _, cwd_part = rest.partition(" ")
    sessions = await bridge.list_sessions()
    match = None
    for item in sessions:
        if str(item.get("sessionId", "")).startswith(token):
            match = item
            break
    if match is None:
        await dsh_cmd.finish(f"❓ 找不到以「{token}」开头的会话，先 /dsh ls 看看。")
    target_cwd = cwd_part.strip() or match.get("cwd")
    await bridge.switch_session(user_key, match["sessionId"], target_cwd)
    await dsh_cmd.finish(f"🔁 已切换到会话\n{match['sessionId']}\n工作区：{target_cwd}")


async def _cmd_close(user_key: str) -> None:
    assert bridge is not None
    await bridge.close_session(user_key)
    await dsh_cmd.finish("🗑️ 已关闭当前会话。")


async def _cmd_model(user_key: str, rest: str) -> None:
    assert bridge is not None
    if not rest:
        await dsh_cmd.finish("用法：/dsh model <模型名>，如 deepseek-v4-pro")
    result = await bridge.set_model(user_key, rest)
    await dsh_cmd.finish(f"🧠 模型已切换：{rest}")
