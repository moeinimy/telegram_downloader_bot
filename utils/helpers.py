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


def run_in_thread(func: Callable[..., T]) -> Callable[..., Awaitable[T]]:
    """Decorator: run a blocking function in the default executor."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, functools.partial(func, *args, **kwargs)
        )

    return wrapper


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
