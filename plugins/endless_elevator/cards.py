"""Fast cached local rendering for Endless Elevator panels, shops, black market, and guides."""
from __future__ import annotations

import atexit
import hashlib
import io
import os
import queue
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable
from itertools import groupby
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_RENDER_VERSION = "v2"
_CACHE_DIR = Path(__file__).resolve().parents[2] / "state" / "elevator_cards_cache"
_RAM_LIMIT = 256
_DISK_LIMIT = 400

_ram_cache: OrderedDict[str, bytes] = OrderedDict()
_cache_lock = threading.Lock()


_TEXT_CARD_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<style>
  * { box-sizing: border-box; }
  body { margin: 0; background: #101824; }
  article {
    width: 820px;
    padding: 26px 32px 28px;
    border-top: 7px solid #e6ba61;
    background: linear-gradient(180deg, #213047 0%, #192538 100%);
    color: #edf3fa;
    font: 25px/1.62 "Microsoft YaHei", "Noto Sans CJK SC", "Segoe UI Emoji", sans-serif;
    overflow-wrap: anywhere;
  }
  .title {
    color: #fff2cc;
    font-size: 29px;
    font-weight: 700;
    padding-bottom: 12px;
    margin-bottom: 12px;
    border-bottom: 1px solid rgba(230, 186, 97, 0.28);
  }
  .section {
    color: #f4cb73;
    font-weight: 700;
    margin-top: 10px;
    margin-bottom: 4px;
  }
  .line {
    margin: 3px 0;
    white-space: pre-wrap;
  }
  .sub {
    color: #b7cae2;
    font-size: 23px;
    margin: 0 0 6px 0;
    white-space: pre-wrap;
  }
  .tip {
    color: #cfe0f5;
    font-size: 23px;
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px dashed rgba(145, 173, 212, 0.25);
    white-space: pre-wrap;
  }
  .footer {
    margin-top: 14px;
    padding: 11px 16px;
    border-radius: 10px;
    border-left: 4px solid #e6ba61;
    background: rgba(13, 22, 34, 0.62);
    color: #ffebc0;
    font-size: 23px;
    font-weight: 600;
    white-space: pre-wrap;
  }
</style>
<article id="card"></article>
<script>
window.renderCardText = (text) => {
  const root = document.getElementById("card");
  root.replaceChildren();
  const lines = String(text || "").split(/\r?\n/);
  lines.forEach((raw, idx) => {
    const el = document.createElement("div");
    if (idx === 0) {
      el.className = "title";
    } else if (raw.startsWith("👉")) {
      el.className = "footer";
    } else if (raw.startsWith("【") || raw.startsWith("▸ ")) {
      el.className = "section";
    } else if (raw.startsWith("   ") || raw.startsWith("  ")) {
      el.className = "sub";
    } else if (raw.startsWith("💡") || raw.startsWith("发「") || raw.startsWith("买完进仓库")) {
      el.className = "tip";
    } else {
      el.className = "line";
    }
    el.textContent = raw;
    root.appendChild(el);
  });
};
</script>
"""


def _cache_key(text: str) -> str:
    return hashlib.sha256(f"{_RENDER_VERSION}\0{text}".encode("utf-8")).hexdigest()[:24]


def _get_cached(key: str) -> bytes | None:
    with _cache_lock:
        hit = _ram_cache.get(key)
        if hit is not None:
            _ram_cache.move_to_end(key)
            return hit
    disk_file = _CACHE_DIR / f"{key}.png"
    if disk_file.is_file():
        try:
            data = disk_file.read_bytes()
        except OSError:
            return None
        with _cache_lock:
            _ram_cache[key] = data
            _ram_cache.move_to_end(key)
            while len(_ram_cache) > _RAM_LIMIT:
                _ram_cache.popitem(last=False)
        return data
    return None


def _put_cached(key: str, data: bytes) -> None:
    with _cache_lock:
        _ram_cache[key] = data
        _ram_cache.move_to_end(key)
        while len(_ram_cache) > _RAM_LIMIT:
            _ram_cache.popitem(last=False)
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        target = _CACHE_DIR / f"{key}.png"
        tmp = _CACHE_DIR / f".{key}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_bytes(data)
        tmp.replace(target)
        _prune_disk_cache()
    except OSError:
        pass


def _prune_disk_cache() -> None:
    try:
        files = list(_CACHE_DIR.glob("*.png"))
        if len(files) <= _DISK_LIMIT:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for old in files[: len(files) - _DISK_LIMIT]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass


class _WarmRenderer:
    """Dedicated single-thread Playwright Chromium/Edge worker that reuses one warm page."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, threading.Event, list[bytes | BaseException]] | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._queue = queue.Queue()
            self._thread = threading.Thread(target=self._loop, name="elevator-card-renderer", daemon=True)
            self._thread.start()

    def _launch(self):
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        chromium = pw.chromium
        options = {}
        if os.name == "nt" and not Path(chromium.executable_path).is_file():
            options["channel"] = "msedge"
        browser = chromium.launch(headless=True, **options)
        page = browser.new_page(offline=True, viewport={"width": 820, "height": 900})
        page.set_content(_TEXT_CARD_PAGE)
        return pw, browser, page

    @staticmethod
    def _teardown(pw, browser, page) -> None:
        for obj in (page, browser):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass

    def _loop(self) -> None:
        pw = browser = page = None
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                text, done, box = item
                try:
                    for attempt in range(2):
                        try:
                            if page is None:
                                pw, browser, page = self._launch()
                            page.evaluate("text => window.renderCardText(text)", text)
                            png = page.locator("#card").screenshot(type="png", scale="css")
                            box.append(png)
                            break
                        except Exception as exc:
                            self._teardown(pw, browser, page)
                            pw = browser = page = None
                            if attempt == 1:
                                box.append(OSError("物品介绍图片生成失败"))
                                box[-1].__cause__ = exc
                finally:
                    done.set()
        finally:
            self._teardown(pw, browser, page)

    def render(self, text: str) -> bytes:
        self._ensure_started()
        done = threading.Event()
        box: list[bytes | BaseException] = []
        self._queue.put((text, done, box))
        if not done.wait(timeout=25.0):
            raise OSError("物品介绍图片生成超时")
        if not box:
            raise OSError("物品介绍图片生成失败")
        outcome = box[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
            q = self._queue
        if thread is not None and thread.is_alive():
            q.put(None)
            thread.join(timeout=4.0)


_renderer = _WarmRenderer()
atexit.register(_renderer.close)


def close_renderer() -> None:
    _renderer.close()


def render_text_card(text: str) -> bytes:
    """Render the complete QQ reply with RAM + disk content-addressed cache and warm browser reuse."""
    key = _cache_key(text)
    cached = _get_cached(key)
    if cached is not None:
        return cached
    png = _renderer.render(text)
    _put_cached(key, png)
    return png


def prewarm_text_cards(texts: Iterable[str]) -> int:
    """Pre-render a batch of card texts into RAM + disk cache. Returns count newly rendered."""
    rendered = 0
    seen: set[str] = set()
    for text in texts:
        if not text:
            continue
        key = _cache_key(text)
        if key in seen:
            continue
        seen.add(key)
        if _get_cached(key) is not None:
            continue
        try:
            png = _renderer.render(text)
            _put_cached(key, png)
            rendered += 1
        except OSError:
            continue
    return rendered


def _font(size: int) -> ImageFont.FreeTypeFont:
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    choices = (
        fonts / "msyh.ttc", fonts / "simhei.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    )
    for path in choices:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    raise OSError("找不到可渲染中文的字体")


def _text(value: str) -> str:
    return re.sub(r"[\U0001F000-\U0001FAFF\uFE0F]", "", value).strip()


def _wrap(value: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines, current = [], ""
    for char in _text(value):
        if current and font.getlength(current + char) > width:
            lines.append(current)
            current = ""
        current += char
    if current:
        lines.append(current)
    return lines


def render_card(title: str, entries: tuple[str, ...]) -> bytes:
    cache_id = _cache_key("PIL-ordered:" + title + "\n" + "\n".join(entries))
    cached = _get_cached(cache_id)
    if cached is not None:
        return cached
    width, gutter = 800, 36
    heading, font, small = _font(40), _font(27), _font(23)
    blocks = []
    height = 124 + gutter
    for grid, entries_group in groupby(entries, key=lambda s: s.startswith(("○ ", "● "))):
        lines = list(entries_group) if grid else [
            line for entry in entries_group for line in _wrap(entry, font, width - 2 * gutter - 34)
        ]
        blocks.append((grid, lines))
        height += 86 * ((len(lines) + 1) // 2) + 24 if grid else 43 * len(lines)
    image = Image.new("RGB", (width, height), "#141d2a")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 7), fill="#e6ba61")
    draw.text((gutter, 34), "无尽电梯 · " + title, font=heading, fill="#fff1cb")
    draw.rounded_rectangle((gutter, 107, width - gutter, height - gutter),
                           radius=18, fill="#213047", outline="#41506b", width=1)
    y = 122
    for grid, lines in blocks:
        if not grid:
            for line in lines:
                draw.text((gutter + 18, y), line, font=font, fill="#edf3fa")
                y += 43
            continue
        y += 12
        half = (width - 2 * gutter - 52) // 2
        for index, stamp in enumerate(lines):
            lit = stamp.startswith("●")
            x, top = gutter + 17 + (index % 2) * (half + 13), y + index // 2 * 86
            draw.rounded_rectangle((x, top, x + half, top + 72), radius=9,
                                   fill="#3f3541" if lit else "#192639",
                                   outline="#d8a857" if lit else "#3b4c67")
            draw.ellipse((x + 12, top + 15, x + 22, top + 25), fill="#f0bf59" if lit else "#7084a4")
            for row, part in enumerate(_wrap(stamp[2:], small, half - 47)[:2]):
                draw.text((x + 32, top + 8 + row * 27), part, font=small,
                          fill="#ffebc0" if lit else "#b3c2d8")
        y += 86 * ((len(lines) + 1) // 2) + 12
    with io.BytesIO() as output:
        image.save(output, format="PNG", optimize=True)
        png = output.getvalue()
    _put_cached(cache_id, png)
    return png
