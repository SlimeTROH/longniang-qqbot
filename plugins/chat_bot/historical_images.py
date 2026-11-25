"""Keep recent image bytes outside the text history, scoped to one conversation."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import re
import time

_HISTORICAL_IMAGE = re.compile(
    r"(?:这|那|前|原|上|刚才|刚刚|之前|同一).{0,5}(?:图|截图|表情包)|"
    r"上一张|图[里中上]|画面[里中上]|(?:左|右|上|下)(?:边|半边|半部分|侧).{0,10}(?:是|谁|啥|什么|哪)"
)
_QUOTED_RECHECK = re.compile(r"再看(?:看|一下|一眼)|重看|仔细看|看清(?:楚)?")
_IMAGE_EDIT = re.compile(
    r"(?:改图|修图|重绘|图生图)(?!工具|软件|教程|功能|能力|是什么意思|是怎么)|"
    r"(?:这|那|上一|上一版|刚才|刚刚|前面|原).{0,5}(?:图|画面).{0,15}(?:改|修|换|调整)|"
    r"(?:背景|配色|发色|衣服|尺寸|比例|画风).{0,4}(?:改成|换成|调成)|"
    r"(?:继续|接着|再).{0,12}(?:改一下|改一改|改成|改得|改下|修改|调整一下|换个背景)"
)


def refers_to_image(text: str, *, quoted: bool) -> bool:
    return bool(_HISTORICAL_IMAGE.search(text) or (quoted and _QUOTED_RECHECK.search(text)))


def requests_image_edit(text: str) -> bool:
    return bool(_IMAGE_EDIT.search(text))


def _sent_message_id(result: object) -> str:
    value = result.get("message_id") if isinstance(result, dict) else result
    return str(value) if type(value) in (int, str) else ""


@dataclass
class _TurnImages:
    speaker: str
    at: float
    images: tuple[bytes, ...]
    size: int
    reply_id: str = ""
    generated: bool = False
    source: GeneratedImage | None = None


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    seedream_url: str = ""
    openai_response_id: str = ""


class ImageHistory:
    def __init__(self, *, max_bytes: int = 64 * 1024**2, max_turns: int = 256,
                 ttl: float = 2700, recent_ttl: float = 600, clock=time.monotonic):
        self._turns: OrderedDict[tuple[tuple[str, str], str], _TurnImages] = OrderedDict()
        self._size = 0
        self._max_bytes, self._max_turns = max_bytes, max_turns
        self._ttl, self._recent_ttl, self._clock = ttl, recent_ttl, clock

    def _prune(self, now: float) -> None:
        while self._turns:
            first = next(iter(self._turns.values()))
            if (now - first.at <= self._ttl and self._size <= self._max_bytes
                    and len(self._turns) <= self._max_turns):
                break
            _, item = self._turns.popitem(last=False)
            self._size -= item.size

    def remember(self, scope: tuple[str, str], speaker: str, message_id: str, images: list[bytes],
                 *, source: GeneratedImage | None = None) -> None:
        size = sum(map(len, images))
        if not message_id or not 0 < size <= self._max_bytes:
            return
        now = self._clock()
        key = scope, message_id
        if previous := self._turns.pop(key, None):
            self._size -= previous.size
        self._turns[key] = _TurnImages(speaker, now, tuple(images), size, generated=source is not None, source=source)
        self._size += size
        self._prune(now)

    def remember_generated(self, scope: tuple[str, str], speaker: str, result: object, image: bytes,
                           *, seedream_url: str = "", openai_response_id: str = "") -> None:
        source = GeneratedImage(image, seedream_url, openai_response_id)
        self.remember(scope, speaker, _sent_message_id(result), [image], source=source)

    def record_reply(self, scope: tuple[str, str], message_id: str, result: object) -> None:
        sent_id = _sent_message_id(result)
        if not sent_id:
            return
        self._prune(self._clock())
        if turn := self._turns.get((scope, message_id)):
            turn.reply_id = sent_id

    def recover(self, scope: tuple[str, str], speaker: str, quote_id: str | None, *,
                generated_only: bool = False) -> tuple[bytes, ...]:
        turn = self._recover_turn(scope, speaker, quote_id, generated_only)
        return turn.images if turn else ()

    def generated_source(self, scope: tuple[str, str], speaker: str, quote_id: str | None) -> GeneratedImage | None:
        turn = self._recover_turn(scope, speaker, quote_id, True)
        if not turn or not turn.source:
            return None
        if turn.speaker != speaker:
            return GeneratedImage(turn.source.data, seedream_url=turn.source.seedream_url)
        return turn.source

    def _recover_turn(self, scope: tuple[str, str], speaker: str, quote_id: str | None,
                      generated_only: bool) -> _TurnImages | None:
        now = self._clock()
        self._prune(now)
        for (stored_scope, message_id), turn in reversed(self._turns.items()):
            if stored_scope != scope or (generated_only and not turn.generated):
                continue
            if quote_id:
                if quote_id in (message_id, turn.reply_id):
                    return turn
            elif turn.speaker == speaker and now - turn.at <= (
                self._ttl if generated_only else self._recent_ttl
            ):
                return turn
        return None

    def forget(self, scope: tuple[str, str]) -> None:
        for key in [key for key in self._turns if key[0] == scope]:
            self._size -= self._turns.pop(key).size
