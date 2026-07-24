"""
YouTube module.
Flow:
  1) probe_video(url) -> VideoInfo (title, duration, thumbnail, available formats)
  2) Bot shows thumbnail + inline keyboard of quality choices + "Audio (MP3)".
  3) On callback: download_video(url, format_id) or download_audio(url).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from yt_dlp import YoutubeDL

from config import settings
from utils.helpers import run_in_thread, safe_filename

log = logging.getLogger(__name__)

# Quality buckets we expose to the user. yt-dlp will pick the best fitting format.
QUALITY_CHOICES: dict[str, str] = {
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "best": "bestvideo+bestaudio/best",
}


@dataclass
class VideoInfo:
    id: str
    title: str
    duration: int
    thumbnail: str
    uploader: str
    available_heights: set[int]


# ---------- probe ----------

def _base_opts() -> dict:
    """Common yt-dlp options; injects cookies + EJS challenge solver."""
    opts: dict = {
        "quiet": True,
        "noplaylist": True,
        # Hung connections must not freeze a worker thread forever.
        "socket_timeout": 30,
        # YouTube requires a JS runtime (deno) + remote challenge-solver
        # scripts; allow yt-dlp to fetch the EJS solver from GitHub.
        "remote_components": ["ejs:github"],
    }
    if settings.yt_cookies_file:
        opts["cookiefile"] = settings.yt_cookies_file
    return opts


@run_in_thread
def probe_video(url: str) -> VideoInfo:
    opts = _base_opts() | {"skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    heights = {
        f.get("height")
        for f in (info.get("formats") or [])
        if f.get("vcodec") != "none" and f.get("height")
    }
    return VideoInfo(
        id=info["id"],
        title=info.get("title", "video"),
        duration=info.get("duration") or 0,
        thumbnail=info.get("thumbnail", ""),
        uploader=info.get("uploader", ""),
        available_heights=heights,
    )


def quality_options_for(info: VideoInfo) -> list[str]:
    """Filter QUALITY_CHOICES to those actually available + always include 'best'."""
    out: list[str] = []
    for label in ("360p", "480p", "720p", "1080p"):
        h = int(label.rstrip("p"))
        if any(av and av <= h for av in info.available_heights if av):
            out.append(label)
    out.append("best")
    return out


# ---------- download ----------

def _make_outtmpl(info: VideoInfo) -> str:
    name = safe_filename(info.title)
    return str(settings.download_dir / f"{info.id}_{name}.%(ext)s")


@run_in_thread
def download_video(
    url: str,
    info: VideoInfo,
    quality: str,
    progress_hook: Callable[[dict], None] | None = None,
) -> Path:
    format_str = QUALITY_CHOICES.get(quality, QUALITY_CHOICES["best"])
    opts = _base_opts() | {
        "format": format_str,
        "outtmpl": _make_outtmpl(info),
        "merge_output_format": "mp4",
        "progress_hooks": [progress_hook] if progress_hook else [],
    }
    with YoutubeDL(opts) as ydl:
        result = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(result)).with_suffix(".mp4")
    return path


@run_in_thread
def download_audio(
    url: str,
    info: VideoInfo,
    progress_hook: Callable[[dict], None] | None = None,
) -> Path:
    """Audio-only download as MP3 with embedded thumbnail + metadata."""
    opts = _base_opts() | {
        "format": "bestaudio/best",
        "outtmpl": _make_outtmpl(info),
        "writethumbnail": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            },
            {"key": "EmbedThumbnail"},
            {"key": "FFmpegMetadata"},
        ],
        "progress_hooks": [progress_hook] if progress_hook else [],
    }
    with YoutubeDL(opts) as ydl:
        result = ydl.extract_info(url, download=True)
        base = Path(ydl.prepare_filename(result))
    return base.with_suffix(".mp3")
