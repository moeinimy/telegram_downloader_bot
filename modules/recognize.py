"""
Music recognition (Shazam) from a media file or URL.

The handler may call fetch_audio_snippet() with different offsets to sample
multiple windows of a long video (songs often start mid-video).
"""

from __future__ import annotations

import asyncio
import logging
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
    rate limiting."""
    global _shazam
    if _shazam is None:
        from shazamio import Shazam

        _shazam = Shazam()
    return _shazam


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
)


def _is_transient(e: Exception) -> bool:
    text = f"{type(e).__name__} {e}".lower()
    return any(m in text for m in _TRANSIENT_MARKERS)


# Windows fingerprinted at once. Three keeps the wall-clock down without
# looking like a burst to the endpoint.
_BATCH = 3


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

    if await sweep(normalise=True, label="n", points=offsets):
        ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
        return [(songs[k], n) for k, n in ranked]

    # Second pass without the loudness filter. dynaudnorm rescues quiet clips
    # but can smear an already-loud, compressed track enough to lose the
    # fingerprint. Only a couple of points this time - a full repeat doubled
    # the wait for the case that was already the slowest.
    if not votes:
        probe = offsets[:1] + offsets[len(offsets) // 2 : len(offsets) // 2 + 1]
        log.info("recognize: retrying %d unprocessed windows", len(probe))
        await sweep(normalise=False, label="r", points=probe)

    if votes:
        ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
        return [(songs[k], n) for k, n in ranked]

    # Last resort: hand over the whole file, which is what the original
    # implementation did and must never be beaten by the windowed version.
    log.info("recognize: no window matched - trying the whole file")
    song = await _recognize_once(path)
    if song:
        return [(song, 1)]

    # Shazam found nothing. Ask the other engines, whose failure modes differ
    # from its own - AcoustID in particular fingerprints the exact recording,
    # so it catches clean audio files that Shazam misses.
    return await _try_other_engines(path)


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
