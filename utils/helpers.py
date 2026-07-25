"""Misc helpers: byte/time formatting, file size guards, async wrappers."""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


def sizeof_fmt(num: float, suffix: str = "B") -> str:
    for unit in ("", "K", "M", "G", "T"):
        if abs(num) < 1024.0:
            return f"{num:3.1f}{unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f}P{suffix}"


def fmt_duration(seconds: int | float | None) -> str:
    if not seconds:
        return "?"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe_filename(name: str, max_len: int = 120) -> str:
    bad = '<>:"/\\|?*\n\r\t'
    cleaned = "".join("_" if c in bad else c for c in name).strip(" .")
    return cleaned[:max_len] or "file"


def file_too_big(path: Path, limit_mb: int) -> bool:
    return path.exists() and path.stat().st_size > limit_mb * 1024 * 1024


def run_in_thread(func=None, *, heavy: bool = False):
    """
    Decorator: run a blocking function in a bounded thread pool.

    `heavy=True` marks work that spawns yt-dlp/ffmpeg. Those go to a small
    separate pool so a queue of downloads cannot delay a search: on the shared
    default executor, a 200ms metadata lookup ended up waiting behind several
    30-second downloads and the bot felt frozen for everyone.
    """

    def decorate(fn: Callable[..., T]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            from utils.limits import heavy_pool, light_pool

            loop = asyncio.get_running_loop()
            pool = heavy_pool() if heavy else light_pool()
            return await loop.run_in_executor(
                pool, functools.partial(fn, *args, **kwargs)
            )

        return wrapper

    return decorate(func) if func is not None else decorate


@run_in_thread
def prepare_telegram_thumb(url: str, dest: Path) -> Path | None:
    """
    Download an image and shrink it to Telegram's audio/video thumbnail
    limits (JPEG, max 320x320, <200KB). iOS and Desktop clients only show
    thumbnails passed via the API parameter, not embedded ID3 art.
    Returns None on any failure (thumbnails are cosmetic).
    """
    try:
        from io import BytesIO

        from PIL import Image

        from utils import http

        got = http.get_bytes(url)
        if not got:
            return None
        img = Image.open(BytesIO(got[0])).convert("RGB")
        img.thumbnail((320, 320))
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, "JPEG", quality=85)
        return dest
    except Exception:
        return None
