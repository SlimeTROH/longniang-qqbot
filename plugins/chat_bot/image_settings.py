"""GPT Image 2.6 raster output-token calculation and quota fitting."""
from __future__ import annotations

from dataclasses import dataclass, replace

# OpenAI's image generation calculator: https://developers.openai.com/api/docs/guides/image-generation#calculating-costs
# Each pair is the long-side token grid and the billable multiplier in tenths.
_QUALITY_RATES = {
    "low": (16, 42),
    "medium": (24, 33),
    "high": (48, 10),
    "xhigh": (64, 10),
    "max": (96, 10),
}
QUALITY_LEVELS = tuple(_QUALITY_RATES)
QUALITY_LABELS = {"low": "低", "medium": "中", "high": "高", "xhigh": "精细", "max": "最高"}
MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400
MAX_EDGE = 3840
SIZE_STEP = 16
MAX_GIF_FRAMES = 16


def _valid_dimensions(width: int, height: int) -> bool:
    return (
        type(width) is int and type(height) is int
        and 0 < width <= MAX_EDGE and 0 < height <= MAX_EDGE
        and width % SIZE_STEP == height % SIZE_STEP == 0
        and max(width, height) <= 3 * min(width, height)
    )


def _valid_size(width: int, height: int) -> bool:
    return _valid_dimensions(width, height) and MIN_PIXELS <= width * height <= MAX_PIXELS


def _render_sizes(width: int, height: int, frames: int) -> tuple[tuple[int, int], ...]:
    if not _valid_dimensions(width, height):
        return ()
    if frames == 1:
        return ((width, height),) if _valid_size(width, height) else ()

    # GIF renders a sheet with cells twice the output frame's width and height.
    sizes = []
    for columns in range(1, frames + 1):
        rows = (frames + columns - 1) // columns
        sheet = (2 * width * columns, 2 * height * rows)
        if _valid_size(*sheet):
            sizes.append(sheet)
    return tuple(sizes)


def _smaller_sizes(width: int, height: int):
    landscape = width >= height
    long_edge, short_edge = max(width, height), min(width, height)
    for long_side in range(long_edge - SIZE_STEP, 0, -SIZE_STEP):
        short_side = round(short_edge * long_side / (long_edge * SIZE_STEP)) * SIZE_STEP
        yield (long_side, short_side) if landscape else (short_side, long_side)


def _render_token_tenths(width: int, height: int, quality: str) -> int:
    grid_long, multiplier = _QUALITY_RATES[quality]
    long_edge, short_edge = max(width, height), min(width, height)
    grid_short = round(grid_long * short_edge / long_edge)
    pixels = width * height
    raw = (grid_long * grid_short * (2_000_000 + pixels) + 3_999_999) // 4_000_000
    return raw * multiplier


@dataclass(frozen=True)
class ImageSettings:
    width: int
    height: int
    quality: str
    frame_count: int = 1
    frame_duration_ms: int = 200

    def __post_init__(self) -> None:
        if self.quality not in _QUALITY_RATES:
            raise ValueError("画质应为 low、medium、high、xhigh 或 max")
        if type(self.frame_count) is not int or not 1 <= self.frame_count <= MAX_GIF_FRAMES:
            raise ValueError("静图用 1 帧，GIF 支持 2 到 16 帧。")
        if self.animated and (type(self.frame_duration_ms) is not int or not 20 <= self.frame_duration_ms <= 2000):
            raise ValueError("GIF 每帧时长应在 20 到 2000 毫秒之间。")
        if not _render_sizes(self.width, self.height, self.frame_count):
            if self.animated:
                raise ValueError("这个尺寸和帧数无法生成 GIF，请调整尺寸或帧数。")
            raise ValueError("请选择有效尺寸，例如 1024×1024、1536×1024 或 1024×1536")

    @classmethod
    def from_request(cls, width: int, height: int, quality: str, frames: int, duration_ms: int) -> ImageSettings:
        if type(frames) is int and 2 <= frames <= MAX_GIF_FRAMES and _valid_dimensions(width, height):
            if not _render_sizes(width, height, frames):
                for smaller in _smaller_sizes(width, height):
                    if _render_sizes(*smaller, frames):
                        width, height = smaller
                        break
        return cls(width, height, quality, frames, duration_ms)

    @property
    def animated(self) -> bool:
        return self.frame_count > 1

    @property
    def output_format(self) -> str:
        return "gif" if self.animated else "png"

    @property
    def size(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def _output_token_tenths(self) -> int:
        # The API chooses its own sheet layout. Reserve for the most expensive fitting one.
        return max(_render_token_tenths(w, h, self.quality) for w, h in
                   _render_sizes(self.width, self.height, self.frame_count))

    @property
    def output_tokens(self) -> int:
        return (self._output_token_tenths + 5) // 10

    @property
    def reservation_tokens(self) -> int:
        # The API can round the billed result up where the calculator rounds down.
        return (self._output_token_tenths + 9) // 10

    @property
    def label(self) -> str:
        label = f"{self.width}×{self.height} · {QUALITY_LABELS[self.quality]}画质"
        return f"{label} · GIF {self.frame_count} 帧" if self.animated else label


def fit_image(requested: ImageSettings, remaining: int) -> ImageSettings | None:
    """Keep dimensions when lowering quality suffices, then shrink at the same aspect ratio."""
    if requested.reservation_tokens <= remaining:
        return requested
    requested_rank = QUALITY_LEVELS.index(requested.quality)
    for quality in reversed(QUALITY_LEVELS[:requested_rank]):
        candidate = replace(requested, quality=quality)
        if candidate.reservation_tokens <= remaining:
            return candidate

    for width, height in _smaller_sizes(requested.width, requested.height):
        if _render_sizes(width, height, requested.frame_count):
            candidate = replace(requested, width=width, height=height, quality="low")
            if candidate.reservation_tokens <= remaining:
                return candidate
    return None
