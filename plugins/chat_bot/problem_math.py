"""Render solve answers as local browser PNGs using bundled KaTeX assets."""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import threading
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops

_ASSETS = Path(__file__).resolve().parents[2] / "assets" / "solution-renderer"
_MATH = re.compile(
    r"(?<!\\)(?:\$\$(?P<display>.+?)\$\$|\\\[(?P<bracket>.+?)\\\]"
    r"|\\\((?P<paren>.+?)\\\)|\$(?P<inline>[^$\n]+?)\$)",
    re.DOTALL,
)
_INVISIBLE_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WOFF2_SOURCE = re.compile(r"""url\(["']?fonts/([\w.-]+\.woff2)["']?\)\s*format\(["']woff2["']\)[^;{}]*""")
_RENDER_LOCK = threading.Lock()


def _fragments(answer: str) -> list[dict]:
    answer = _INVISIBLE_CONTROLS.sub("", answer)
    fragments = []
    start = 0
    for match in _MATH.finditer(answer):
        if match.start() > start:
            fragments.append({"kind": "text", "value": answer[start:match.start()]})
        fragments.append({
            "kind": "display" if match.lastgroup in ("display", "bracket") else "inline",
            "value": match.group(match.lastgroup),
        })
        start = match.end()
    if start < len(answer):
        fragments.append({"kind": "text", "value": answer[start:]})
    return fragments


@lru_cache(maxsize=1)
def _katex_css() -> str:
    def inline_font(match: re.Match) -> str:
        font = (_ASSETS / "fonts" / match.group(1)).read_bytes()
        encoded = base64.b64encode(font).decode("ascii")
        return f'url(data:font/woff2;base64,{encoded}) format("woff2")'

    css = (_ASSETS / "katex.min.css").read_text(encoding="utf-8")
    return _WOFF2_SOURCE.sub(inline_font, css)


def _browser_executable() -> str | None:
    for command in ("msedge", "chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(command)
        if found:
            return found
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if not base:
            continue
        for name in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
            candidate = Path(base) / name
            if candidate.is_file():
                return str(candidate)
    return None


def _trim_blank_bottom(png: bytes) -> bytes:
    with Image.open(io.BytesIO(png)) as source:
        page = source.convert("RGB")
    ink = ImageChops.difference(page, Image.new("RGB", page.size, "white")).getbbox()
    bottom = ink[3] + 36 if ink is not None else 0
    if not bottom or bottom >= page.height:
        return png
    output = io.BytesIO()
    page.crop((0, 0, page.width, bottom)).save(output, format="PNG")
    return output.getvalue()


def render_answer(answer: str) -> tuple[bytes, ...]:
    """Lay out and capture measured pages in an offline browser."""
    from playwright.sync_api import sync_playwright

    if not answer.strip():
        return ()
    with _RENDER_LOCK, sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=_browser_executable(), headless=True)
        try:
            page = browser.new_page(offline=True, viewport={"width": 860, "height": 1600})
            page.set_content((_ASSETS / "page.html").read_text(encoding="utf-8"))
            page.add_style_tag(content=_katex_css())
            page.add_script_tag(path=_ASSETS / "katex.min.js")
            count = page.evaluate("parts => layoutSolution(parts)", _fragments(answer))
            frame = page.locator("#frame")
            pages = []
            for index in range(count):
                page.evaluate("index => showSolutionPage(index)", index)
                pages.append(_trim_blank_bottom(frame.screenshot(type="png", scale="css")))
            return tuple(pages)
        finally:
            browser.close()
