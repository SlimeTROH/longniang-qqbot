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


def refers_to_image(text: str, *, quoted: bool) -> bool:
    return bool(_HISTORICAL_IMAGE.search(text) or (quoted and _QUOTED_RECHECK.search(text)))


@dataclass
class _TurnImages:
    speaker: str
    at: float
    images: tuple[bytes, ...]
    size: int
    reply_id: str = ""


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

    def remember(self, scope: tuple[str, str], speaker: str, message_id: str, images: list[bytes]) -> None:
        size = sum(map(len, images))
        if not message_id or not 0 < size <= self._max_bytes:
            return
        now = self._clock()
        key = scope, message_id
        if previous := self._turns.pop(key, None):
            self._size -= previous.size
        self._turns[key] = _TurnImages(speaker, now, tuple(images), size)
        self._size += size
        self._prune(now)

    def record_reply(self, scope: tuple[str, str], message_id: str, result: object) -> None:
        sent_id = result.get("message_id") if isinstance(result, dict) else result
        if type(sent_id) not in (int, str) or not str(sent_id):
            return
        self._prune(self._clock())
        if turn := self._turns.get((scope, message_id)):
            turn.reply_id = str(sent_id)

    def recover(self, scope: tuple[str, str], speaker: str, quote_id: str | None) -> tuple[bytes, ...]:
        now = self._clock()
        self._prune(now)
        for (stored_scope, message_id), turn in reversed(self._turns.items()):
            if stored_scope != scope:
                continue
            if quote_id:
                if quote_id in (message_id, turn.reply_id):
                    return turn.images
            elif turn.speaker == speaker and now - turn.at <= self._recent_ttl:
                return turn.images
        return ()

    def forget(self, scope: tuple[str, str]) -> None:
        for key in [key for key in self._turns if key[0] == scope]:
            self._size -= self._turns.pop(key).size
