"""Keep completed answers available for a short, requester-scoped download."""
from __future__ import annotations

import asyncio
import io
import re
import time
from dataclasses import dataclass
from itertools import count
from pathlib import PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

TTL_SECONDS = 600
OFFER = "需要源文件吗？发「源文件」领取，保留 10 分钟"
_REQUESTS = {"源文件", "要源文件", "需要源文件", "发源文件"}
_SHORT_REQUESTS = {"要", "需要"}
_ANSWER_NAME = "解答.txt"
_INVALID_NAME = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_RESERVED_NAME = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)", re.IGNORECASE)


@dataclass(frozen=True)
class CodeFile:
    name: str
    content: str


@dataclass
class SourceAnswer:
    number: int
    question: str
    answer: str
    files: tuple[CodeFile, ...]
    expires_at: float
    sending: bool = False


_answers: dict[str, dict[int, SourceAnswer]] = {}
_menus: dict[str, tuple[int, ...]] = {}
_ids = count(1)


def read_code_files(value: object) -> tuple[CodeFile, ...]:
    if not isinstance(value, list):
        return ()
    files = []
    used = {_ANSWER_NAME.casefold()}
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or not isinstance(content := item.get("content"), str) or not content.strip():
            continue
        raw_name = item.get("name")
        basename = raw_name.replace("\\", "/").rsplit("/", 1)[-1] if isinstance(raw_name, str) else ""
        name = _INVALID_NAME.sub("_", basename).strip(" .")[:100].rstrip(" .")
        if not name or _RESERVED_NAME.match(name):
            name = f"code-{index}.txt"
        path = PurePosixPath(name)
        version = 1
        while name.casefold() in used:
            version += 1
            name = f"{path.stem}-{version}{path.suffix}"
        used.add(name.casefold())
        files.append(CodeFile(name, content))
    return tuple(files)


def _expire(key: str, number: int) -> None:
    answers = _answers.get(key)
    if answers is None:
        return
    answers.pop(number, None)
    if not answers:
        _answers.pop(key, None)
        _menus.pop(key, None)


def remember(key: str, question: str, answer: str, files: tuple[CodeFile, ...]) -> None:
    loop = asyncio.get_running_loop()
    number = next(_ids)
    item = SourceAnswer(number, question, answer, files, time.monotonic() + TTL_SECONDS)
    _answers.setdefault(key, {})[number] = item
    loop.call_later(TTL_SECONDS, _expire, key, number)


def _available(key: str) -> dict[int, SourceAnswer]:
    answers = _answers.get(key, {})
    now = time.monotonic()
    for item in tuple(answers.values()):
        if now >= item.expires_at:
            _expire(key, item.number)
    return answers


def _number(text: str) -> str:
    command = text.split(maxsplit=1)
    if len(command) == 2 and command[0] == "源文件":
        return command[1] if command[1].isdecimal() else ""
    return text if text.isdecimal() else ""


def recognizes_reply(key: str, text: str, *, addressed: bool) -> bool:
    if text in _REQUESTS or (not text.isdecimal() and _number(text)):
        return True
    if not addressed or not _available(key):
        return False
    return text in _SHORT_REQUESTS or (text.isdecimal() and key in _menus)


def select(key: str, text: str) -> SourceAnswer | str:
    answers = _available(key)
    if not answers:
        return "当前没有可领取的源文件。每份解答发出后保留 10 分钟"
    if raw_number := _number(text):
        menu = _menus.get(key)
        if not menu:
            return "发「源文件」查看可领取的解答"
        try:
            index = int(raw_number) - 1
        except ValueError:
            index = -1
        if not 0 <= index < len(menu):
            return "请选列表里的编号"
        item = answers.get(menu[index])
        if item is None:
            return "这份源文件已过期。发「源文件」刷新列表"
        _menus.pop(key, None)
        return item
    if len(answers) == 1:
        _menus.pop(key, None)
        return next(iter(answers.values()))
    _menus[key] = tuple(answers)
    lines = ["选一道题领取源文件。回复编号，或发「源文件 2」"]
    for index, item in enumerate(answers.values(), 1):
        question = " ".join(item.question.split())
        label = question if len(question) <= 65 else question[:65] + "…"
        lines.append(f"{index}. {label}")
    return "\n".join(lines)


def attachment(item: SourceAnswer) -> tuple[str, bytes]:
    text = f"题目\n{item.question}\n\n解答\n{item.answer}\n".encode("utf-8")
    if not item.files:
        return f"解答-{item.number}.txt", text
    output = io.BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(_ANSWER_NAME, text)
        for code_file in item.files:
            archive.writestr(code_file.name, code_file.content.encode("utf-8"))
    return f"解答源文件-{item.number}.zip", output.getvalue()
