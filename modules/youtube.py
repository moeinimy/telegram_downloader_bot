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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar

from yt_dlp import YoutubeDL

from config import settings
from utils import proxies
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
# Fast first, the slow one genuinely last, and nothing here that cannot do
# the job. Every rung works without cookies and without a PO token, checked
# against yt-dlp's own client table rather than assumed.
#
# Timed against a real track, one extraction each:
#
#     default       3.2s   4 audio formats, best 146kbps
#     android_vr    3.8s   4 audio formats, best 146kbps
#     tv_simply     4.1s   refused
#     mweb          4.8s   refused
#     ios           6.0s   refused
#     android       6.9s   SUCCEEDED with 0 audio formats
#     web_safari    9.0s   refused
#     tv           12.1s   refused, slowest of all
#
# android is left out because of what that line says: it does not fail. It
# returns a perfectly good extraction with no audio-only format in it, so it
# would be remembered as the winner and then have nothing to download. A rung
# that succeeds at the wrong thing is worse than one that refuses.
#
# tv is left out for costing twelve seconds to refuse.
#
# tv_simply stays, last. It was THIRD, and it is the client measured at 88.8s
# on the server against 7.8s for the others: reaching it early is how a
# refusal on the first two rungs turned into a minute and a half of waiting.
_CLIENT_LADDER: tuple[str, ...] = (
    "android_vr", "", "ios", "mweb", "web_embedded", "web_safari", "tv_simply",
)

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
    # Where the uploader's other videos live. Needed to offer them, and not
    # reconstructible from the name - a display name is not a handle.
    channel_url: str = ""
    # Approximate bytes per quality label, from the format list. The menu was
    # a set of unlabelled buttons, so picking one was a bet: "best" has no
    # ceiling and produced a 1640MB file from a video nobody expected to be
    # that size, and there was no way to see that coming before the upload.
    size_by_quality: dict[str, int] = field(default_factory=dict)



def probe_dimensions(path) -> tuple[int, int, int]:
    """(width, height, seconds) of a media file, or zeros when unreadable.

    Telegram is told the size of a video, or it guesses - and it guesses a
    default box, which is why a 16:9 upload came back stretched with its
    length shown as 00:00. The metadata we already hold describes the SOURCE;
    this describes the file that was actually produced, which is the one being
    sent and may be a different height entirely.
    """
    import json
    import subprocess

    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout
        data = json.loads(out or "{}")
        stream = (data.get("streams") or [{}])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        seconds = int(float((data.get("format") or {}).get("duration") or 0))
        return width, height, seconds
    except Exception as e:
        log.info("ffprobe could not read %s (%s)", path, e)
        return 0, 0, 0


# One connection is one throttle. aria2c opens several.
#
# YouTube shapes a download PER CONNECTION, so a single stream sits at
# whatever rate it decides to give - which is the difference between three
# minutes and under one on the same file and the same link. aria2c splits the
# file and fetches the parts at once, and the shaping applies to each part
# separately.
#
# concurrent_fragment_downloads already does this, but only for streams that
# ARE fragmented. A plain progressive mp4 - which is most of what a quality
# button resolves to - is one file, so that setting never applied to it.
#
# Looked up once per process: it is an apt package that either exists or does
# not, and shelling out to `which` on every download is a syscall for an
# answer that cannot change.
_aria2c: bool | None = None


def _have_aria2c() -> bool:
    global _aria2c

    if _aria2c is None:
        import shutil

        _aria2c = shutil.which("aria2c") is not None
        log.info("aria2c %s - downloads will be %s",
                 "found" if _aria2c else "not installed",
                 "split across connections" if _aria2c else "single-stream")
    return _aria2c


def _fast_download_opts() -> dict:
    """aria2c settings, or nothing when it is not installed.

    -x is connections per host, -s is pieces per file, -k is piece size. 16 is
    yt-dlp's own default pairing and the number the field has settled on;
    higher gets refused by some hosts for no extra speed.

    --file-allocation=none matters on a VPS: the default pre-allocates the
    whole file before the first byte arrives, which on a 710MB download is a
    visible pause where nothing appears to happen.
    """
    if not _have_aria2c():
        return {}
    return {
        "external_downloader": {"default": "aria2c"},
        "external_downloader_args": {
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M",
                       "--file-allocation=none", "--summary-interval=0"],
        },
    }

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
        # Retries of the TRANSFER, above, are worth having: a dropped fragment
        # is usually transient. Retries of the EXTRACTION are not - a client
        # YouTube is refusing gets refused again - and at three apiece they
        # tripled the cost of walking the ladder past every refusing client.
        # The ladder is the retry that helps here.
        "extractor_retries": 1,
        # YouTube requires a JS runtime (deno) + remote challenge-solver
        # scripts; allow yt-dlp to fetch the EJS solver from GitHub.
        "remote_components": ["ejs:github"],
        # Persist that solver (and signature caches) between runs instead of
        # re-fetching them on every single extraction.
        "cachedir": str(settings.download_dir / ".ytdlp-cache"),
        # Fragmented streams dominate the download time; fetch pieces at once.
        # Audio is usually one file and unaffected; HLS - which is what
        # SoundCloud and some YouTube streams serve - is fragmented, and
        # that is where the wall-clock time goes.
        "concurrent_fragment_downloads": 8,
    }
    if client:
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    if settings.yt_cookies_file:
        opts["cookiefile"] = settings.yt_cookies_file

    # The bot check is decided by where the request comes from, so a different
    # exit is the alternative to handing YouTube an account. yt-dlp rejects the
    # socks5h spelling exactly like every other library here.
    proxy = proxies.normalize(settings.yt_proxy)
    if proxy:
        opts["proxy"] = proxy
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

# What that winner cost, and when it won.
#
# Remembering only WHO won turns a one-off fallback into a permanent tax. The
# measurement in the comment above says it plainly: android_vr refused in
# 7.8s, the default refused in 7.8s, and tv_simply served it in 88.8s. Once
# tv_simply is the remembered winner it leads the ladder on every later
# request, so every download starts by paying eighty-eight seconds - and the
# fast clients are never tried again while it keeps working.
#
# A slow client is a fallback, not a preference. It keeps the lead only until
# the next re-probe, and then the fast ones get another chance: whether
# YouTube refuses this address changes with cookies, with a proxy, and on its
# own.
_preferred_cost: dict[str, float] = {}
_preferred_at: dict[str, float] = {}

_SLOW_SECONDS = 20.0
_REPROBE_AFTER = 600.0


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
    if winner is not None and _preferred_cost.get(kind, 0.0) >= _SLOW_SECONDS:
        if time.monotonic() - _preferred_at.get(kind, 0.0) >= _REPROBE_AFTER:
            log.info("yt-dlp: '%s' is a slow winner (%.0fs) - re-probing the "
                     "fast clients", winner or "default", _preferred_cost[kind])
            winner = None
    if winner is not None and winner in base:
        return (winner,) + tuple(c for c in base if c != winner)
    return base


def _is_retryable(e: Exception) -> bool:
    s = str(e).lower()
    return any(marker in s for marker in _RETRYABLE)


# YouTube declining to serve this address anonymously, as opposed to a video
# being gone. Searching callers need the difference: with the search refused,
# "nothing matched" is a statement about the server, not about the track, and
# reporting it as the latter told users a song did not exist.
_BOT_CHECK = ("sign in to confirm", "confirm you're not a bot", "not a bot",
              "use --cookies")
_last_bot_check = 0.0


def bot_checked_recently(within: float = 600.0) -> bool:
    return bool(_last_bot_check and time.monotonic() - _last_bot_check < within)


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
            took = time.monotonic() - started
            _preferred[kind] = client
            _preferred_cost[kind] = took
            _preferred_at[kind] = time.monotonic()
            log.info("yt-dlp client '%s' served %s in %.1fs", client or "default",
                     kind or "it", took)
            return result
        except Exception as e:
            if not _is_retryable(e):
                raise

            if any(marker in str(e).lower() for marker in _BOT_CHECK):
                global _last_bot_check

                _last_bot_check = time.monotonic()

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

    formats = info.get("formats") or []
    heights = {
        f.get("height")
        for f in formats
        if f.get("vcodec") != "none" and f.get("height")
    }
    return VideoInfo(
        id=info["id"],
        title=info.get("title", "video"),
        duration=info.get("duration") or 0,
        thumbnail=info.get("thumbnail", ""),
        uploader=info.get("uploader", ""),
        available_heights=heights,
        size_by_quality=_sizes_by_quality(formats),
        channel_url=info.get("channel_url") or info.get("uploader_url") or "",
    )


@dataclass
class ChannelVideo:
    id: str
    title: str
    duration: int
    views: int


@run_in_thread
def popular_from_channel(channel_url: str, limit: int = 8) -> list[ChannelVideo]:
    """The uploader's most-watched videos, best first.

    YouTube's own "sort by popular" is a UI control backed by a query
    parameter that has changed spelling more than once and is not part of any
    documented interface. Rather than depend on it, the videos tab is listed
    flat - which is cheap, one request, no per-video extraction - and sorted
    here on the view counts that listing already carries.

    Entries without a view count sort last rather than being dropped: a
    missing number is not a video nobody watched, and dropping them would
    quietly shorten the list.
    """
    if not channel_url:
        return []

    url = channel_url.rstrip("/")
    if not url.endswith("/videos"):
        url += "/videos"

    info = ytdlp_run(
        {
            "extract_flat": "in_playlist",
            "skip_download": True,
            # Enough to rank meaningfully without walking an entire channel;
            # a flat page is cheap but a 5000-video channel is not.
            "playlistend": 60,
        },
        lambda ydl: ydl.extract_info(url, download=False),
        kind="channel",
    )

    out: list[ChannelVideo] = []
    for e in info.get("entries") or []:
        if not isinstance(e, dict) or not e.get("id"):
            continue
        out.append(ChannelVideo(
            id=e["id"],
            title=e.get("title") or "video",
            duration=int(e.get("duration") or 0),
            views=int(e.get("view_count") or 0),
        ))

    out.sort(key=lambda v: v.views, reverse=True)
    return out[:limit]


def _size_of(f: dict) -> int:
    return int(f.get("filesize") or f.get("filesize_approx") or 0)


def _sizes_by_quality(formats: list[dict]) -> dict[str, int]:
    """Roughly what each button will cost, mirroring how QUALITY_CHOICES picks.

    Approximate on purpose: yt-dlp reports filesize_approx for most streams and
    an estimate that is in the right order of magnitude is the whole point. The
    alternative on offer was no number at all.
    """
    video = [f for f in formats if f.get("vcodec") != "none" and f.get("height")]
    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    if not video:
        return {}

    best_audio = max((_size_of(f) for f in audio), default=0)

    sizes: dict[str, int] = {}
    for label in ("360p", "480p", "720p", "1080p"):
        cap = int(label.rstrip("p"))
        fitting = [f for f in video if (f.get("height") or 0) <= cap]
        if fitting:
            pick = max(fitting, key=lambda f: (f.get("height") or 0, _size_of(f)))
            if _size_of(pick):
                sizes[label] = _size_of(pick) + best_audio

    # "best" has no ceiling in QUALITY_CHOICES, which is exactly why it needs a
    # number next to it more than any of the others do.
    top = max(video, key=lambda f: (f.get("height") or 0, _size_of(f)))
    if _size_of(top):
        sizes["best"] = _size_of(top) + best_audio
    return sizes


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

def _make_outtmpl(info: VideoInfo, quality: str = "") -> str:
    name = safe_filename(info.title)
    # The quality belongs in the name. Without it every quality of a video
    # wrote to one path, and yt-dlp does not re-download a file that is
    # already there - so after a 1640MB "best" was left on disk by an upload
    # that never finished, asking for 360p returned that same 1640MB file:
    #
    #     yt-dlp client 'android_vr' served video in 1.3s
    #
    # for a 37-minute video, which is the tell. Nothing was downloaded at all.
    tag = f"_{quality}" if quality else ""
    return str(settings.download_dir / f"{info.id}{tag}_{name}.%(ext)s")


@run_in_thread(heavy=True)
def download_video(
    url: str,
    info: VideoInfo,
    quality: str,
    progress_hook: Callable[[dict], None] | None = None,
) -> Path:
    extra = {
        "format": QUALITY_CHOICES.get(quality, QUALITY_CHOICES["best"]),
        # Prefer the codecs that go into an .mp4 without being re-encoded.
        #
        # merge_output_format below asks for mp4, and YouTube's best streams
        # are AV1 video with Opus audio - neither of which mp4 will simply
        # hold. So ffmpeg re-encoded the whole thing, every time, on top of
        # the download. On a 710MB video that is minutes of CPU nobody asked
        # for.
        #
        # A preference rather than a filter, deliberately: [vcodec^=avc1]
        # fails outright on a video that has no H.264 rendition, and this
        # cannot fail - it reorders what is there. Measured on one track:
        #
        #     as shipped   av01 + opus   7.9MB   re-encode
        #     with sort    avc1 + mp4a   7.4MB   remux
        #
        # Smaller as well as faster, at the same height, for every rung of the
        # quality menu.
        "format_sort": ["vcodec:h264", "acodec:m4a"],
        "outtmpl": _make_outtmpl(info, quality),
        "merge_output_format": "mp4",
        **_fast_download_opts(),
        # A file left behind by an upload that died mid-flight is not a
        # finished download, and yt-dlp cannot tell the difference. Say so
        # rather than inheriting whatever is on disk.
        "overwrites": True,
        "progress_hooks": [progress_hook] if progress_hook else [],
    }

    def _run(ydl: YoutubeDL) -> Path:
        result = ydl.extract_info(url, download=True)
        # What the selector actually resolved to. A quality that quietly
        # resolves to something enormous is indistinguishable from a slow
        # upload once the file exists on disk.
        log.info("yt format for %s: id=%s height=%s ext=%s",
                 quality, result.get("format_id"), result.get("height"),
                 result.get("ext"))
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
        "outtmpl": _make_outtmpl(info, "audio"),
        "overwrites": True,
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
