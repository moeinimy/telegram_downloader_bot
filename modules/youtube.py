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

import json
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
    # A downloader that fell over is worth another client, not a dead end:
    # without this the ladder stopped on the first candidate and reported
    # "all 1 candidates failed".
    "aria2c exited",
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


# One connection is one throttle. aria2c opens several - where that is
# allowed.
#
# OFF by default, on the evidence. Measured on the live server, every single
# attempt ended:
#
#     ERROR: aria2c exited with code 22
#     retrying natively
#     android_vr refused audio after 14.2s (HTTP Error 403: Forbidden)
#
# YouTube refuses the parallel range requests that make aria2c fast, so the
# 403 the native retry then gets is the same refusal - and the six seconds
# spent discovering that were added to every download. An optimisation that
# never lands is a tax.
#
# It stays available because it is genuinely faster on hosts that allow it,
# and because a different address may be treated differently. YT_USE_ARIA2C=1
# switches it on.
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

        installed = shutil.which("aria2c") is not None
        _aria2c = installed and settings.yt_use_aria2c
        if installed and not settings.yt_use_aria2c:
            log.info("aria2c is installed but switched off (YT_USE_ARIA2C) - "
                     "single-stream downloads")
        else:
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
        # Keyed on "http" and NOT on "default".
        #
        # aria2c cannot download HLS. SoundCloud serves hls_aac_160k, and
        # "default" handed those to it too - which is
        #
        #     ERROR: aria2c exited with code 22
        #
        # (an unexpected HTTP header: it fetched a playlist and expected
        # media). With this key, plain http and https go to aria2c and every
        # fragmented protocol falls through to yt-dlp's own downloader, which
        # is what concurrent_fragment_downloads is already parallelising.
        #
        # yt-dlp simplifies https -> http when it looks this up, so one key
        # covers both.
        "external_downloader": {"http": "aria2c"},
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
# Which client last worked, kept on disk.
#
# In the journal, every restart pays this again:
#
#   android_vr refused audio after 7.4s (HTTP Error 403: Forbidden)
#   default    served  audio in 11.2s
#
# The winner was already remembered - in memory, so a restart threw it away
# and the first download of every session bought the same 403 at full price.
# Seven and a half seconds is most of a fast download, spent re-learning
# something the process before it already knew.
_PREFERRED_PATH = settings.download_dir / "yt_clients.json"


def _load_preferred() -> None:
    try:
        stored = json.loads(_PREFERRED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(stored, dict):
        return
    for kind, client in (stored.get("preferred") or {}).items():
        if isinstance(kind, str) and isinstance(client, str) and client in _CLIENT_LADDER:
            _preferred[kind] = client

    # Resumed as skipped, not as one-attempt-from-skipped.
    #
    # The cooldown is what re-tries a client, and it already does: _is_cold
    # clears an entry after fifteen minutes and the ladder picks it up again.
    # Restoring one attempt short of the threshold instead would buy a fresh
    # 14-second 403 on every restart - and during a day of deploys that is the
    # difference the whole exercise is about.
    now = time.monotonic()
    for key, count in (stored.get("refusals") or {}).items():
        kind, _, client = str(key).partition("|")
        if client in _CLIENT_LADDER and isinstance(count, int) and count >= _REFUSALS_BEFORE_SKIP:
            _refusals[(kind, client)] = (count, now)

    if _preferred or _refusals:
        log.info("yt-dlp: resuming with %s, %d client(s) known to refuse",
                 dict(_preferred), len(_refusals))


def _save_preferred() -> None:
    try:
        _PREFERRED_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PREFERRED_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "preferred": _preferred,
            # The refusals too. android_vr answers a probe fine and then 403s
            # every media request from this address; without this, each
            # restart re-learns that at 8-14 seconds a download, three times
            # over before the ladder gives up on it.
            "refusals": {f"{k[0]}|{k[1]}": v[0] for k, v in _refusals.items()},
        }), encoding="utf-8")
        tmp.replace(_PREFERRED_PATH)
    except Exception:
        pass          # a cache that cannot be written is not a failure



_preferred_cost: dict[str, float] = {}
_preferred_at: dict[str, float] = {}

_SLOW_SECONDS = 20.0
_REPROBE_AFTER = 600.0


# Clients that have just refused, so the ladder stops paying for them.
#
# ytdlp_run walks the WHOLE ladder on every call, and download_track calls it
# once per candidate - up to six of them. Seven clients times six candidates is
# forty-two attempts, and on a given address most of the seven never return an
# audio format at all. Each costs a couple of seconds to say no, which is most
# of a two-minute download spent on refusals that were entirely predictable by
# the second candidate.
#
# So a refusal is remembered. Three in a row and the client is skipped for a
# while; one success clears it. Nothing here hardcodes WHICH clients are good,
# because that differs per address and changes without notice - the ladder just
# stops asking the ones currently saying no.
_REFUSALS_BEFORE_SKIP = 3
_REFUSAL_COOLDOWN = 900.0
_refusals: dict[tuple[str, str], tuple[int, float]] = {}

# Never skip everything. If every rung has gone cold, trying them is the
# shortest way back to a working one.
_MIN_RUNGS = 2


def _note_refusal(kind: str, client: str) -> None:
    count, _ = _refusals.get((kind, client), (0, 0.0))
    _refusals[(kind, client)] = (count + 1, time.monotonic())
    if count + 1 >= _REFUSALS_BEFORE_SKIP:
        _save_preferred()      # worth surviving a restart at this point


def _note_success(kind: str, client: str) -> None:
    _refusals.pop((kind, client), None)


def _is_cold(kind: str, client: str) -> bool:
    count, when = _refusals.get((kind, client), (0, 0.0))
    if count < _REFUSALS_BEFORE_SKIP:
        return False
    if time.monotonic() - when > _REFUSAL_COOLDOWN:
        _refusals.pop((kind, client), None)      # long enough; try it again
        return False
    return True


# Everything it reads now exists: the ladder, _preferred and _refusals.
_load_preferred()


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
        base = (winner,) + tuple(c for c in base if c != winner)

    # Drop the rungs that are currently only costing time. Never all of them:
    # if every one is cold, trying them is the shortest way back to a working
    # client.
    warm = tuple(c for c in base if not _is_cold(kind, c))
    if len(warm) >= _MIN_RUNGS:
        return warm
    return base


# An external downloader falling over is not the same as the site refusing.
#
# aria2c reports every HTTP problem as an exit code - 22 for an unexpected
# header, which is what a 403 or a redirect it dislikes looks like from
# outside. That is not a reason to give up on the track: aria2c is only here
# to make a download faster, and yt-dlp's own downloader handles the headers
# and the redirects it does not.
#
# So this is a signal to retry the SAME url natively, not to move on.
_EXTERNAL_DL_FAILURE = ("aria2c exited", "external downloader",
                        "downloader exited with code")


def _is_external_downloader_failure(e: Exception) -> bool:
    text = str(e).lower()
    return any(marker in text for marker in _EXTERNAL_DL_FAILURE)


def _without_external_downloader(opts: dict) -> dict:
    return {k: v for k, v in opts.items()
            if not k.startswith("external_downloader")}


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


def ytdlp_run(extra: dict, fn: Callable[[YoutubeDL], T], kind: str = "",
              accept: Callable[[T], bool] | None = None) -> T:
    """Run `fn` against a YoutubeDL instance, retrying down the client ladder.

    Shared by every module that extracts media so the account-free fallbacks
    apply bot-wide, not just to /youtube links.

    `kind` groups requests that behave alike - a metadata probe and a media
    download are refused by different clients, so they remember different
    winners.

    `accept` is for the failure that does not raise. Some clients answer a
    probe with a clean extraction containing one video format, and a quality
    menu built from that offers 360p for an hour-long video that has eight
    resolutions. Nothing errored; the answer was simply not usable. A client
    whose result is rejected is walked past like one that refused, but the
    rejected result is kept - if no client does better, a thin answer still
    beats no answer.
    """
    thin: T | None = None
    thin_seen = False
    last: Exception | None = None
    for client in _ladder(kind):
        opts = _base_opts(client) | extra
        started = time.monotonic()
        try:
            try:
                with YoutubeDL(opts) as ydl:
                    result = fn(ydl)
            except Exception as first:
                # aria2c is an optimisation. When it fails, the download is
                # still perfectly possible - just not that way.
                if not (_is_external_downloader_failure(first)
                        and "external_downloader" in opts):
                    raise
                log.warning("yt-dlp: the external downloader failed (%s) - "
                            "retrying natively", str(first)[:80])
                with YoutubeDL(_without_external_downloader(opts)) as ydl:
                    result = fn(ydl)
            took = time.monotonic() - started

            if accept is not None and not accept(result):
                if not thin_seen:
                    thin, thin_seen = result, True
                log.info("yt-dlp client '%s' answered %s in %.1fs but the "
                         "result is too thin to use - trying the next",
                         client or "default", kind or "it", took)
                _note_refusal(kind, client)
                if _preferred.get(kind) == client:
                    _preferred.pop(kind, None)
                continue

            if _preferred.get(kind) != client:
                _preferred[kind] = client
                _save_preferred()
            _preferred[kind] = client
            _preferred_cost[kind] = took
            _preferred_at[kind] = time.monotonic()
            _note_success(kind, client)
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
                _save_preferred()
            _note_refusal(kind, client)
            log.warning("yt-dlp client '%s' refused %s after %.1fs (%s) — trying next.",
                        client or "default", kind or "it",
                        time.monotonic() - started, e)
            last = e

    # Every client either refused or answered thinly. A thin answer is still
    # an answer - a video that genuinely has one resolution looks exactly like
    # this, and erroring would be wrong for it.
    if thin_seen:
        log.warning("yt-dlp: no client gave a full answer for %s - using the "
                    "thin one", kind or "it")
        return thin

    raise _friendly(last)


# ---------- probe ----------

def _usable_probe(info) -> bool:
    """Whether a probe is rich enough to build a quality menu from.

    Some clients answer with one progressive stream and nothing else. That is
    a clean, successful extraction - and an hour-long video with eight
    resolutions then arrived as a single "360p" button, because one stream is
    all the menu had to offer.

    A video that genuinely has one resolution is indistinguishable from this
    at the probe, so a thin answer is not an error; it is a reason to ask a
    different client first and keep this one only if nobody does better.
    """
    formats = (info or {}).get("formats") or []
    heights = {f.get("height") for f in formats
               if f.get("vcodec") not in (None, "none") and f.get("height")}
    audio_only = [f for f in formats
                  if f.get("vcodec") in (None, "none")
                  and f.get("acodec") not in (None, "none")]
    # Two heights, or one height with a separate audio track: either shape
    # means the client is showing the adaptive ladder rather than a single
    # muxed file.
    return len(heights) >= 2 or bool(audio_only)


# A probe, kept for a few minutes.
#
# Nothing about a video changes minute to minute, and the probe is the slowest
# thing between pasting a link and seeing the menu - one extraction, and more
# than one when the first client answers thinly. Pasting the same link twice,
# or two people sending the same video, paid for it twice.
#
# Short on purpose. This is not a store, it is a way of not asking the same
# question twice in the span of one conversation.
# A probe costs a full yt-dlp extraction - measured at 4.5 to 4.9 seconds
# against this server, and there is no way to make it cheaper. Checked: the
# obvious candidate, player_skip=webpage, does not speed it up at all - it
# makes YouTube demand a bot check and the extraction fails outright.
#
# So the only thing left is to do it less often. Ten minutes and in memory
# meant every restart re-probed everything, and a popular link pasted by three
# different people cost three extractions. Six hours and on disk: format
# ladders do not change through the day, and if one does the worst case is a
# size estimate that is slightly off on a menu that already says "approximate".
_PROBE_TTL = 6 * 3600.0
_PROBE_CACHE_PATH = settings.download_dir / "yt_probes.json"
_probe_cache: dict[str, tuple[float, "VideoInfo"]] = {}


def _probe_to_dict(info: "VideoInfo") -> dict:
    return {
        "id": info.id, "title": info.title, "duration": info.duration,
        "thumbnail": info.thumbnail, "uploader": info.uploader,
        "available_heights": sorted(info.available_heights or []),
        "channel_url": info.channel_url,
        "size_by_quality": info.size_by_quality,
    }


def _probe_from_dict(d: dict) -> "VideoInfo":
    return VideoInfo(
        id=d.get("id", ""), title=d.get("title", ""),
        duration=int(d.get("duration") or 0), thumbnail=d.get("thumbnail", ""),
        uploader=d.get("uploader", ""),
        available_heights=set(d.get("available_heights") or []),
        channel_url=d.get("channel_url", ""),
        size_by_quality=d.get("size_by_quality") or {},
    )


def _load_probes() -> None:
    try:
        stored = json.loads(_PROBE_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    now = time.time()
    kept = 0
    for key, entry in (stored or {}).items():
        try:
            when = float(entry["at"])
            if now - when >= _PROBE_TTL:
                continue
            # Stored as wall clock, compared as monotonic: an entry written
            # before the last reboot has no meaningful monotonic age, so its
            # remaining life is recomputed from how long ago it was written.
            _probe_cache[key] = (time.monotonic() - (now - when),
                                 _probe_from_dict(entry["info"]))
            kept += 1
        except Exception:
            continue
    if kept:
        log.info("probe cache: %d entries still fresh", kept)


def _save_probes() -> None:
    try:
        now_mono, now_wall = time.monotonic(), time.time()
        data = {
            key: {"at": now_wall - (now_mono - at), "info": _probe_to_dict(info)}
            for key, (at, info) in _probe_cache.items()
            if now_mono - at < _PROBE_TTL
        }
        _PROBE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PROBE_CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(_PROBE_CACHE_PATH)
    except Exception:
        pass


def _probe_key(url: str) -> str:
    """The video id where one can be found, so youtu.be and a watch url with
    tracking parameters are recognised as the same video."""
    import re

    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else url.strip()


# Everything it needs is defined by here: VideoInfo and both helpers.
_load_probes()


@run_in_thread
def probe_video(url: str) -> VideoInfo:
    key = _probe_key(url)
    hit = _probe_cache.get(key)
    if hit and time.monotonic() - hit[0] < _PROBE_TTL:
        log.info("probe: reusing the one from %.0fs ago",
                 time.monotonic() - hit[0])
        return hit[1]

    info = ytdlp_run(
        {"skip_download": True},
        lambda ydl: ydl.extract_info(url, download=False),
        kind="probe",
        accept=_usable_probe,
    )

    formats = info.get("formats") or []
    heights = {
        f.get("height")
        for f in formats
        if f.get("vcodec") != "none" and f.get("height")
    }
    probed = VideoInfo(
        id=info["id"],
        title=info.get("title", "video"),
        duration=info.get("duration") or 0,
        thumbnail=info.get("thumbnail", ""),
        uploader=info.get("uploader", ""),
        available_heights=heights,
        size_by_quality=_sizes_by_quality(formats, info.get("duration") or 0),
        channel_url=info.get("channel_url") or info.get("uploader_url") or "",
    )

    # Keyed on the id from the response, not the url that was pasted, so every
    # spelling of the same video shares one entry.
    _probe_cache[probed.id] = (time.monotonic(), probed)
    _probe_cache[key] = (time.monotonic(), probed)
    _save_probes()
    if len(_probe_cache) > 200:
        cutoff = time.monotonic() - _PROBE_TTL
        for k, (seen, _) in list(_probe_cache.items()):
            if seen < cutoff:
                _probe_cache.pop(k, None)
    return probed


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


def _size_of(f: dict, seconds: float = 0) -> int:
    """Bytes this format will cost, estimated from the bitrate when it has to.

    Some clients report neither filesize nor filesize_approx - HLS variants
    especially - and returning 0 there dropped the number off the button
    entirely. tbr is in kbit/s and is almost always present, so the estimate
    exists whenever the runtime does.
    """
    known = int(f.get("filesize") or f.get("filesize_approx") or 0)
    if known:
        return known
    tbr = f.get("tbr") or 0
    return int(tbr * 1000 / 8 * seconds) if tbr and seconds else 0


def _sizes_by_quality(formats: list[dict], seconds: float = 0) -> dict[str, int]:
    """Roughly what each button will cost, mirroring how QUALITY_CHOICES picks.

    Approximate on purpose: yt-dlp reports filesize_approx for most streams and
    an estimate that is in the right order of magnitude is the whole point. The
    alternative on offer was no number at all.
    """
    video = [f for f in formats if f.get("vcodec") != "none" and f.get("height")]
    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
    if not video:
        return {}

    best_audio = max((_size_of(f, seconds) for f in audio), default=0)

    # Which FORMAT each label resolves to, not just how big it is. When a
    # client offers one video stream, every label picks it - and the menu
    # then offered 360p, 480p, 720p, 1080p and best as five buttons showing
    # 516MB each, which is one file wearing five names. Recording the pick is
    # what lets the duplicates be dropped instead of displayed.
    sizes: dict[str, int] = {}
    picked: dict[str, str] = {}
    for label in ("360p", "480p", "720p", "1080p"):
        cap = int(label.rstrip("p"))
        fitting = [f for f in video if (f.get("height") or 0) <= cap]
        if fitting:
            pick = max(fitting,
                       key=lambda f: (f.get("height") or 0, _size_of(f, seconds)))
            if _size_of(pick, seconds):
                sizes[label] = _size_of(pick, seconds) + best_audio
                picked[label] = str(pick.get("format_id") or pick.get("height"))

    # "best" has no ceiling in QUALITY_CHOICES, which is exactly why it needs a
    # number next to it more than any of the others do.
    top = max(video, key=lambda f: (f.get("height") or 0, _size_of(f, seconds)))
    if _size_of(top, seconds):
        sizes["best"] = _size_of(top, seconds) + best_audio
        picked["best"] = str(top.get("format_id") or top.get("height"))

    # A label that lands on the same stream as a lower one is not a choice.
    seen: dict[str, str] = {}
    for label in ("360p", "480p", "720p", "1080p", "best"):
        fmt = picked.get(label)
        if fmt is None:
            continue
        if fmt in seen.values():
            sizes.pop(label, None)
        else:
            seen[label] = fmt
    return sizes


def quality_options_for(info: VideoInfo) -> list[str]:
    """The qualities that are genuinely different files.

    Offering a label that resolves to the same stream as another is worse than
    offering fewer: it reads as a choice, and picking it changes nothing. When
    the sizes are known they decide, because two labels sharing one stream is
    exactly what _sizes_by_quality dropped.
    """
    sizes = info.size_by_quality
    out: list[str] = []
    for label in ("360p", "480p", "720p", "1080p"):
        h = int(label.rstrip("p"))
        if not any(av and av <= h for av in info.available_heights if av):
            continue
        if sizes and label not in sizes:
            continue
        out.append(label)
    if not sizes or "best" in sizes:
        out.append("best")
    return out or ["best"]


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
