"""
Music recognition (Shazam) from a media file or URL.

The handler may call fetch_audio_snippet() with different offsets to sample
multiple windows of a long video (songs often start mid-video).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from config import settings
from utils.helpers import run_in_thread

log = logging.getLogger(__name__)


@dataclass
class RecognizedSong:
    title: str
    artist: str

    @property
    def query(self) -> str:
        return f"{self.artist} {self.title}".strip()


_shazam = None


def _client():
    """One Shazam client for the process: a fresh instance per window opened a
    new HTTP session each time, which is both slower and more likely to trip
    rate limiting.

    SHAZAM_PROXY exists because the endpoint refuses some datacenter
    addresses outright - it answers with an HTML block page, shazamio cannot
    parse it, and every window fails. No amount of retrying fixes an IP the
    other end has decided about; the request has to leave from somewhere else.
    """
    global _shazam
    if _shazam is None:
        from shazamio import Shazam

        if settings.shazam_proxy:
            _install_proxy(settings.shazam_proxy)
        _shazam = Shazam()
    return _shazam


_proxy_installed = False


def _install_proxy(proxy: str) -> None:
    """Force every aiohttp request through the proxy.

    Shazam(proxy=...) only exists in some shazamio versions. On the one
    installed here it raised TypeError, the old code caught it, logged
    "this shazamio version ignores SHAZAM_PROXY" and carried on WITHOUT a
    proxy - so a correctly configured proxy changed nothing and Shazam kept
    answering 403 to the server's own address.

    Setting it at the aiohttp layer works whatever shazamio does with its
    constructor, because that is the layer the request actually leaves from.
    Scoped by the guard below to one install, and only when a proxy is set.

    aiohttp is used in this process by shazamio alone - python-telegram-bot
    is on httpx, and web/webhook.py is a server rather than a client - so
    nothing else is redirected.

    SOCKS is handled through aiohttp-socks by replacing the session's
    connector. aiohttp has no native SOCKS support, and the alternative -
    bridging it to http with privoxy - is one more service to install, start
    and get wrong. It was, immediately: privoxy refused to start because the
    setup appended a listen-address the packaged config already had, and
    every request then failed with connection refused on the bridge.
    """
    global _proxy_installed

    if _proxy_installed:
        return

    import aiohttp

    if proxy.lower().startswith("socks"):
        try:
            from aiohttp_socks import ProxyConnector
        except ImportError:
            log.error(
                "SHAZAM_PROXY is SOCKS but aiohttp-socks is not installed. "
                "Run: botctl proxy  (it installs it), or use an http:// proxy."
            )
            return

        # aiohttp-socks does not know the "h" suffix and raises
        #     ValueError: Invalid scheme component: socks5h
        # before any request leaves. curl and requests use socks5h to mean
        # "resolve DNS at the proxy"; aiohttp-socks does that by default, so
        # the suffix is simply dropped rather than translated.
        url = proxy
        for suffix, plain in (("socks5h://", "socks5://"), ("socks4a://", "socks4://")):
            if url.lower().startswith(suffix):
                url = plain + url[len(suffix):]
                break

        original_init = aiohttp.ClientSession.__init__

        def __init__(self, *args, **kwargs):
            # Only when the caller did not bring its own connector.
            if not kwargs.get("connector"):
                kwargs["connector"] = ProxyConnector.from_url(url)
            original_init(self, *args, **kwargs)

        aiohttp.ClientSession.__init__ = __init__
    else:
        original_request = aiohttp.ClientSession._request

        async def _request(self, method, url, **kwargs):
            # setdefault, so an explicit per-call proxy still wins.
            kwargs.setdefault("proxy", proxy)
            return await original_request(self, method, url, **kwargs)

        aiohttp.ClientSession._request = _request

    _proxy_installed = True
    log.info("shazam: routing aiohttp through %s", proxy.split("@")[-1])


def reset_client() -> None:
    """Drop the cached client so a settings change takes effect."""
    global _shazam
    _shazam = None


class RecognitionUnavailable(RuntimeError):
    """The recognition service could not be reached or refused us.

    Deliberately distinct from "no match": one means try again shortly, the
    other means the audio genuinely is not in the catalogue. Collapsing both
    into None told users "no music found" whenever Shazam was briefly
    unreachable or throttling us, which looks exactly like poor accuracy.
    """


_TRANSIENT_MARKERS = (
    "cannot connect", "connection", "timeout", "timed out", "temporarily",
    "too many requests", "429", "502", "503", "504", "reset by peer",
    "ssl", "dns", "unreachable",
    # Shazam answering with something that is not JSON. Observed in
    # production as:
    #
    #     Shazam error on 00.mp4: FailedDecodeJson: Failed to decode json
    #
    # It means an HTML challenge, a block page or an empty body came back
    # where the result should be - a datacenter IP being refused, not a track
    # that is missing from the catalogue. Unclassified it fell to the generic
    # branch, returned None, and reached the user as "no music found", which
    # is why this looked like an accuracy problem for days.
    "faileddecodejson", "failed to decode", "forbidden", "403", "captcha",
    "blocked", "cloudflare", "expecting value",
)

# The last thing Shazam did wrong, for /recstatus. A silent outage that
# presents as poor accuracy needs somewhere to be visible.
last_error: str = ""
last_error_at: float = 0.0


def _is_transient(e: Exception) -> bool:
    text = f"{type(e).__name__} {e}".lower()
    return any(m in text for m in _TRANSIENT_MARKERS)


def _note_error(e: Exception) -> None:
    global last_error, last_error_at

    last_error = f"{type(e).__name__}: {e}"[:160]
    last_error_at = time.time()


# Windows fingerprinted at once.
#
# Three was chosen when every request left from this server directly. Through
# a proxy each round trip costs several times more, so the batch size is what
# decides the wall clock: a five-window plan at three-per-batch is two
# sequential rounds, at five it is one.
def _int_env(name: str, default: int) -> int:
    import os

    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


_BATCH = _int_env("RECOGNIZE_BATCH", 5)

# Where the wall clock actually goes, per phase. Guessing at latency has been
# wrong every time this session; each phase reports itself instead.
last_timing: dict[str, float] = {}


def _phase(name: str, started: float) -> None:
    last_timing[name] = round(time.monotonic() - started, 2)


async def _recognize_once(path: Path, attempts: int = 2) -> RecognizedSong | None:
    """One window against Shazam. None means no match; a transient failure
    raises RecognitionUnavailable after the retries are exhausted."""
    shazam = _client()
    last: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            if hasattr(shazam, "recognize"):
                out = await shazam.recognize(str(path))
            else:
                # older shazamio versions used recognize_song()
                out = await shazam.recognize_song(str(path))
        except Exception as e:
            last = e
            if _is_transient(e) and attempt < attempts:
                log.info(
                    "Shazam transient error (%d/%d) on %s: %s - retrying",
                    attempt, attempts, path.name, type(e).__name__,
                )
                await asyncio.sleep(0.8 * attempt)
                continue
            _note_error(e)
            if _is_transient(e):
                log.warning("Shazam unreachable after %d tries: %s", attempts, e)
                raise RecognitionUnavailable(str(e)) from e
            log.warning("Shazam error on %s: %s: %s", path.name, type(e).__name__, e)
            return None

        track = (out or {}).get("track") or {}
        title = (track.get("title") or "").strip()
        artist = (track.get("subtitle") or "").strip()
        if not title:
            return None
        return RecognizedSong(title=title, artist=artist)

    raise RecognitionUnavailable(str(last))


def _media_duration(path: Path) -> float:
    """Length in seconds via ffprobe; 0 when it cannot be determined."""
    import subprocess

    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, timeout=20,
        )
        return float(out.stdout.strip() or 0)
    except Exception:
        return 0.0


_SILENCE_DB = -45.0  # below this a window has nothing to fingerprint


def _extract_window(
    src: Path, offset: int, seconds: int, dest: Path, *, normalise: bool = True
) -> tuple[Path, float] | None:
    """
    Cut one window of audio and report its loudness.

    Returns (path, mean_volume_dB) or None when the window is unusable.
    volumedetect sits first in the chain so the figure describes the ORIGINAL
    audio, before any levelling - that is what tells us whether the window
    contains anything worth sending.

    44.1kHz mono MP3 rather than a 16kHz WAV: the decoder shazamio uses is
    happiest with ordinary compressed audio, and downsampling before its own
    resampling only discards detail the fingerprint needs.
    """
    import re
    import subprocess

    chain = "volumedetect"
    if normalise:
        # Phone clips are often quiet with the music under speech; levelling
        # gives the fingerprint more to work with. Nothing is removed.
        chain += ",dynaudnorm=f=200:g=5"

    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "info", "-y",
                "-ss", str(offset), "-t", str(seconds), "-i", str(src),
                "-vn", "-af", chain,
                "-ac", "1", "-ar", "44100", "-b:a", "128k", str(dest),
            ],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0:
            log.warning(
                "ffmpeg window @%ds failed: %s",
                offset, proc.stderr.decode("utf-8", "replace")[-200:],
            )
            return None
    except Exception as e:
        log.warning("window extraction failed at %ds: %s", offset, e)
        return None

    # A window past the end of the file produces a near-empty file.
    if not dest.exists() or dest.stat().st_size <= 4000:
        return None

    stderr = proc.stderr.decode("utf-8", "replace")
    m = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", stderr)
    return dest, (float(m.group(1)) if m else 0.0)


def _sample_plan(duration: float) -> tuple[int, list[int]]:
    """
    Choose the window length and where to sample.

    A fixed 15s window meant anything shorter than that got exactly one
    sample, so the whole point of cross-checking was lost precisely where it
    matters most: short clips, which is what people actually send. Windows
    are sized to the clip and overlapped so even a 14-second video is
    fingerprinted three times.

    Returns (window_seconds, offsets).
    """
    if duration <= 0:
        return 12, [0]

    # Shazam matches comfortably on 5-12 seconds.
    window = 12 if duration >= 20 else max(5, int(duration * 0.6))
    last = max(int(duration - window), 0)
    if last == 0:
        return window, [0]

    count = min(5, max(3, last // max(window // 2, 3) + 1))
    offsets = sorted({int(last * i / (count - 1)) for i in range(count)})
    return window, offsets


# Five 12-second mp3 windows plus the source is well under this; the margin
# is so a nearly-full disk fails here rather than halfway through.
_MIN_FREE_MB = 150


def _require_workspace(folder: Path) -> None:
    """Fail loudly when there is nowhere to cut the windows.

    Every window goes through ffmpeg writing an mp3. When that write fails,
    _extract_window returns None, every window returns None, and the caller
    reports "I couldn't identify any music" - so a full disk arrives at the
    user as a recognition result rather than as a disk problem. This is the
    difference between a five-minute fix and a week of blaming Shazam.
    """
    import shutil

    try:
        free_mb = shutil.disk_usage(folder).free // (1024 * 1024)
    except OSError as e:
        log.warning("recognize: could not check free space on %s: %s", folder, e)
        return

    if free_mb < _MIN_FREE_MB:
        log.error("recognize: only %dMB free on %s - cannot cut windows", free_mb, folder)
        raise RuntimeError(
            f"فضای دیسک سرور پره (فقط {free_mb} مگ آزاده).\n\n"
            "ادمین: «botctl clearcache» یا گزینه ۱۲ منو."
        )


async def recognize_candidates(path: Path) -> list[tuple[RecognizedSong, int]]:
    """
    Fingerprint several windows of a file and return every distinct answer
    with its vote count, best first.

    A single sample is unreliable: speech, effects or an intro over the music
    makes Shazam answer with something unrelated (a clip of "Sicko Mode" came
    back as "Big Poppa"). Returning all candidates lets the caller act on a
    clear winner and ask the user when the windows disagree, instead of
    presenting one unverified guess as fact.
    """
    _require_workspace(path.parent)

    duration = _media_duration(path)
    window, offsets = _sample_plan(duration)

    tmp_dir = path.parent
    votes: dict[tuple[str, str], int] = {}
    songs: dict[tuple[str, str], RecognizedSong] = {}

    def _key(s: RecognizedSong) -> tuple[str, str]:
        return (s.artist.lower().strip(), s.title.lower().strip())

    log.info(
        "recognize: %s (%.0fs) - sampling %d windows at %s",
        path.name, duration, len(offsets), offsets,
    )

    async def one_window(i: int, off: int, normalise: bool, label: str):
        """Cut and fingerprint a single window. Returns the song or None."""
        clip = tmp_dir / f"{path.stem}_{label}{i}.mp3"
        made = await asyncio.to_thread(
            _extract_window, path, off, window, clip, normalise=normalise
        )
        if made is None:
            return None
        clip_path, level = made

        # A near-silent window has nothing to identify; sending it wastes a
        # request and returns a no-match that looks like a failure.
        if level <= _SILENCE_DB:
            log.info("window @%ds skipped: silent (%.0f dB)", off, level)
            clip_path.unlink(missing_ok=True)
            return None
        try:
            return await _recognize_once(clip_path)
        finally:
            clip_path.unlink(missing_ok=True)

    async def sweep(normalise: bool, label: str, points: list[int]) -> bool:
        """
        One pass over `points`. True once a match is confirmed twice.

        Windows go out in small concurrent batches rather than one at a time:
        sequentially this was one round trip per window plus a deliberate
        pause between each, which dominated the wait on anything it could not
        identify immediately. Batching keeps the early exit - a batch is only
        started if the previous one did not already settle it.
        """
        for start in range(0, len(points), _BATCH):
            batch = points[start : start + _BATCH]
            results = await asyncio.gather(
                *[
                    one_window(start + n, off, normalise, label)
                    for n, off in enumerate(batch)
                ],
                return_exceptions=True,
            )
            for off, song in zip(batch, results):
                if isinstance(song, RecognitionUnavailable):
                    raise song
                if isinstance(song, BaseException) or not song:
                    if not isinstance(song, BaseException):
                        log.info("Shazam %s window @%ds: no match", label, off)
                    continue
                k = _key(song)
                songs.setdefault(k, song)
                votes[k] = votes.get(k, 0) + 1
                log.info("Shazam %s window @%ds: %s - %s", label, off, song.artist, song.title)
                if votes[k] >= 2:
                    return True
        return False

    # Shazam being down must not end the attempt. The other engines have
    # completely different failure modes - a different company, a different
    # network path, a different catalogue - so an outage at Shazam is exactly
    # when they are worth asking. Aborting here meant a blocked IP took the
    # working engines down with it.
    down: Exception | None = None
    started = time.monotonic()
    last_timing.clear()

    try:
        if await sweep(normalise=True, label="n", points=offsets):
            _phase("shazam", started)
            log.info("recognize: matched in %.1fs (%d windows, batch %d)",
                     last_timing["shazam"], len(offsets), _BATCH)
            ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
            return [(songs[k], n) for k, n in ranked]

        # Second pass without the loudness filter. dynaudnorm rescues quiet
        # clips but can smear an already-loud, compressed track enough to lose
        # the fingerprint. Only a couple of points this time - a full repeat
        # doubled the wait for the case that was already the slowest.
        if not votes:
            probe = offsets[:1] + offsets[len(offsets) // 2 : len(offsets) // 2 + 1]
            log.info("recognize: retrying %d unprocessed windows", len(probe))
            await sweep(normalise=False, label="r", points=probe)

        if votes:
            ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
            return [(songs[k], n) for k, n in ranked]

        # Last resort for Shazam: hand over the whole file, which is what the
        # original implementation did and must never be beaten by windowing.
        log.info("recognize: no window matched - trying the whole file")
        song = await _recognize_once(path)
        if song:
            return [(song, 1)]
    except RecognitionUnavailable as e:
        down = e
        log.warning("recognize: Shazam unavailable (%s) - falling through to the others", e)

    _phase("shazam", started)

    # AcoustID in particular fingerprints the exact recording, so it catches
    # clean audio Shazam misses - and it is free and keyless-cheap, which
    # makes it the one to have configured when Shazam blocks a datacenter IP.
    engines_started = time.monotonic()
    others = await _try_other_engines(path)
    _phase("other_engines", engines_started)
    log.info(
        "recognize: shazam %.1fs, other engines %.1fs (%d windows, batch %d)",
        last_timing.get("shazam", 0), last_timing.get("other_engines", 0),
        len(offsets), _BATCH,
    )
    if others:
        return others

    # Nothing answered. If Shazam never got to speak, say so rather than
    # claiming the audio is not in any catalogue.
    if down is not None:
        raise down
    return []


async def _try_other_engines(path: Path) -> list[tuple[RecognizedSong, int]]:
    """Configured extra engines, in priority order, first answer wins."""
    from modules import engines

    for name in engines.active_engines():
        try:
            res = await asyncio.to_thread(engines.recognize_with, name, path)
        except Exception as e:
            log.info("engine %s raised: %s", name, e)
            continue
        if res and res.title:
            log.info("engine %s matched: %s - %s", name, res.artist, res.title)
            # Another engine agreeing where Shazam found nothing is the best
            # evidence available, so treat it as confirmed.
            return [(RecognizedSong(title=res.title, artist=res.artist), 2)]
    return []


def service_reachable() -> tuple[bool, str]:
    """Cheap connectivity check for the recognition endpoint, so 'it never
    recognises anything' can be told apart from a blocked host."""
    import socket

    try:
        socket.create_connection(("amp.shazam.com", 443), timeout=6).close()
        return True, "amp.shazam.com در دسترسه"
    except Exception as e:
        return False, f"amp.shazam.com در دسترس نیست ({type(e).__name__})"


async def recognize_file(path: Path) -> RecognizedSong | None:
    """Best single answer, or None when nothing was recognised."""
    candidates = await recognize_candidates(path)
    return candidates[0][0] if candidates else None


@run_in_thread(heavy=True)
def fetch_audio_snippet(url: str, seconds: int = 45, offset: int = 0) -> Path:
    """
    Download a short audio snippet [offset, offset+seconds] from a video URL
    for fingerprinting. Goes through the YouTube module's client-fallback
    ladder so it works without cookies on datacenter IPs.
    """
    try:
        from yt_dlp.utils import download_range_func
    except Exception:
        download_range_func = None

    from modules.youtube import ytdlp_run

    out_dir = settings.download_dir / "recognize"
    out_dir.mkdir(parents=True, exist_ok=True)

    extra = {
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / f"%(id)s_snip{offset}.%(ext)s"),
        "overwrites": True,
    }
    # Clip to the requested window so long videos stay fast.
    if download_range_func:
        extra["download_ranges"] = download_range_func(None, [(offset, offset + seconds)])
        extra["force_keyframes_at_cuts"] = True

    def _run(ydl):
        info = ydl.extract_info(url, download=True)
        return info, Path(ydl.prepare_filename(info))

    info, path = ytdlp_run(extra, _run)

    if not path.exists():
        candidates = list(out_dir.glob(f"{info['id']}_snip{offset}.*"))
        if candidates:
            path = candidates[0]
    if not path.exists():
        raise RuntimeError("Snippet download produced no file")
    return path
