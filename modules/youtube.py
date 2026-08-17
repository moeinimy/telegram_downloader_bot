"""
YouTube module.

Flow:
  1) probe_video(url) -> VideoInfo (title, duration, thumbnail, available formats)
  2) Bot shows thumbnail + inline keyboard of quality choices + "Audio (MP3)".
  3) On callback: download_video(url, format_id) or download_audio(url).

Cookie-free operation
---------------------
On datacenter/VPS IPs YouTube answers the default web client with
"Sign in to confirm you're not a bot". Instead of requiring an exported
cookies.txt, every extraction runs through a ladder of alternative player
clients (tv_simply, android_vr, web_embedded, ios). Those endpoints are not
gated behind the bot check and need no account. A cookies file is still used
when YT_COOKIES_FILE is set, but it is entirely optional.

ytdlp_run() is shared by the spotify / recognize / soundcloud modules so every
extraction in the bot gets the same retry ladder.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from yt_dlp import YoutubeDL

from config import settings
from utils.helpers import run_in_thread, safe_filename

log = logging.getLogger(__name__)

T = TypeVar("T")

# Quality buckets we expose to the user. yt-dlp will pick the best fitting format.
QUALITY_CHOICES: dict[str, str] = {
    "360p": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    "480p": "bestvideo[height<=480]+bestaudio/best[height<=480]",
    "720p": "bestvideo[height<=720]+bestaudio/best[height<=720]",
    "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "best": "bestvideo+bestaudio/best",
}

# Player clients tried in order when the previous one is refused. "" = yt-dlp
# defaults. android_vr leads because it is the one client that serves the full
# format list without a PO token or a signed-in cookie jar - that is what makes
# account-free operation on a datacenter IP possible. The default client is
# second: it is faster, but on a VPS it is the one that hits the bot check.
_CLIENT_LADDER: tuple[str, ...] = ("android_vr", "", "tv_simply", "web_embedded", "ios")

# Errors that mean "this client was refused" rather than "this video is gone".
_RETRYABLE = (
    "sign in to confirm",
    "confirm you're not a bot",
    "not a bot",
    "requested format is not available",
    "unable to extract",
    "failed to extract any player response",
    "please sign in",
    "http error 403",
    "content isn't available",
    "this content isn't available",
    "nsig extraction failed",
    "video unavailable",
)


@dataclass
class VideoInfo:
    id: str
    title: str
    duration: int
    thumbnail: str
    uploader: str
    available_heights: set[int]


# ---------- yt-dlp plumbing ----------

def _base_opts(client: str = "") -> dict:
    """Common yt-dlp options. `client` selects an alternative player client."""
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "noprogress": True,
        # Hung connections must not freeze a worker thread forever.
        "socket_timeout": 30,
        "retries": 3,
        # YouTube requires a JS runtime (deno) + remote challenge-solver
        # scripts; allow yt-dlp to fetch the EJS solver from GitHub.
        "remote_components": ["ejs:github"],
        # Persist that solver (and signature caches) between runs instead of
        # re-fetching them on every single extraction.
        "cachedir": str(settings.download_dir / ".ytdlp-cache"),
        # Fragmented streams dominate the download time; fetch pieces at once.
        "concurrent_fragment_downloads": 4,
    }
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    if settings.yt_cookies_file:
        opts["cookiefile"] = settings.yt_cookies_file
    return opts


# Which client last served each kind of request. Measured, from one download:
#
#   android_vr served it in 4.5s                        <- the probe
#   android_vr refused after 7.8s (HTTP Error 403)      <- the media
#   default    refused after 7.8s (HTTP Error 403)
#   tv_simply  served it in 88.8s
#
# The same client answers metadata and then refuses the media for the very
# same video, so there is no single best client - there is a best client per
# kind of request. Remembering the winner per kind skips the 15.6s of 403s
# that were being paid before the first byte of every download.
#
# Deliberately not persisted: which client YouTube accepts changes without
# notice, and a stale winner on disk would cost a startup penalty rather than
# save one.
_preferred: dict[str, str] = {}


def _ladder(kind: str = "") -> tuple[str, ...]:
    # With a cookie jar the default client becomes the strongest option, so
    # promote it to the front; otherwise keep the account-free order.
    if settings.yt_cookies_file:
        base = ("",) + tuple(c for c in _CLIENT_LADDER if c)
    else:
        base = _CLIENT_LADDER

    # "" is a real client here (yt-dlp's default), so this tests for presence
    # rather than truthiness.
    winner = _preferred.get(kind)
    if winner is not None and winner in base:
        return (winner,) + tuple(c for c in base if c != winner)
    return base


def _is_retryable(e: Exception) -> bool:
    s = str(e).lower()
    return any(marker in s for marker in _RETRYABLE)


def _friendly(e: Exception | None) -> RuntimeError:
    s = str(e or "").lower()
    if "private" in s or "members-only" in s:
        return RuntimeError("این ویدیو خصوصیه و قابل دانلود نیست.")
    if "removed" in s or "unavailable" in s or "not a bot" in s or "sign in" in s:
        return RuntimeError(
            "یوتیوب این ویدیو رو نداد. ممکنه حذف/محدود شده باشه یا موقتا "
            "درخواست‌های سرور رو رد کنه. چند دقیقه بعد دوباره امتحان کن."
        )
    return RuntimeError(f"YouTube: {e}")


def ytdlp_run(extra: dict, fn: Callable[[YoutubeDL], T], kind: str = "") -> T:
    """Run `fn` against a YoutubeDL instance, retrying down the client ladder.

    Shared by every module that extracts media so the account-free fallbacks
    apply bot-wide, not just to /youtube links.

    `kind` groups requests that behave alike - a metadata probe and a media
    download are refused by different clients, so they remember different
    winners.
    """
    last: Exception | None = None
    for client in _ladder(kind):
        opts = _base_opts(client) | extra
        started = time.monotonic()
        try:
            with YoutubeDL(opts) as ydl:
                result = fn(ydl)
            _preferred[kind] = client
            log.info("yt-dlp client '%s' served %s in %.1fs", client or "default",
                     kind or "it", time.monotonic() - started)
            return result
        except Exception as e:
            if not _is_retryable(e):
                raise
            # It led the ladder because it worked last time and it has just
            # stopped, so drop it rather than paying for the same refusal on
            # every subsequent request.
            if _preferred.get(kind) == client:
                _preferred.pop(kind, None)
            log.warning("yt-dlp client '%s' refused %s after %.1fs (%s) — trying next.",
                        client or "default", kind or "it",
                        time.monotonic() - started, e)
            last = e
    raise _friendly(last)


# ---------- probe ----------

@run_in_thread
def probe_video(url: str) -> VideoInfo:
    info = ytdlp_run(
        {"skip_download": True},
        lambda ydl: ydl.extract_info(url, download=False),
        kind="probe",
    )

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


@run_in_thread(heavy=True)
def download_video(
    url: str,
    info: VideoInfo,
    quality: str,
    progress_hook: Callable[[dict], None] | None = None,
) -> Path:
    extra = {
        "format": QUALITY_CHOICES.get(quality, QUALITY_CHOICES["best"]),
        "outtmpl": _make_outtmpl(info),
        "merge_output_format": "mp4",
        "progress_hooks": [progress_hook] if progress_hook else [],
    }

    def _run(ydl: YoutubeDL) -> Path:
        result = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(result)).with_suffix(".mp4")

    return ytdlp_run(extra, _run, kind="video")


@run_in_thread(heavy=True)
def download_audio(
    url: str,
    info: VideoInfo,
    progress_hook: Callable[[dict], None] | None = None,
) -> Path:
    """Audio-only download as 320kbps MP3 with embedded thumbnail + metadata."""
    extra = {
        "format": "bestaudio/best",
        "outtmpl": _make_outtmpl(info),
        "writethumbnail": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            },
            {"key": "EmbedThumbnail"},
            {"key": "FFmpegMetadata"},
        ],
        "progress_hooks": [progress_hook] if progress_hook else [],
    }

    def _run(ydl: YoutubeDL) -> Path:
        result = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(result)).with_suffix(".mp3")

    return ytdlp_run(extra, _run, kind="audio")
