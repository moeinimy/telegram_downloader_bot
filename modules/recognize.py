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


async def _recognize_once(path: Path) -> RecognizedSong | None:
    shazam = _client()
    try:
        if hasattr(shazam, "recognize"):
            out = await shazam.recognize(str(path))
        else:
            # older shazamio versions used recognize_song()
            out = await shazam.recognize_song(str(path))
    except Exception as e:
        log.warning("Shazam error on %s: %s: %s", path.name, type(e).__name__, e)
        return None

    track = (out or {}).get("track") or {}
    title = (track.get("title") or "").strip()
    artist = (track.get("subtitle") or "").strip()
    if not title:
        return None
    return RecognizedSong(title=title, artist=artist)


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


def _extract_window(src: Path, offset: int, seconds: int, dest: Path) -> Path | None:
    """
    Cut one window of audio for fingerprinting.

    Kept at 44.1kHz mono MP3 rather than a 16kHz WAV: the decoder shazamio
    uses is happiest with ordinary compressed audio, and downsampling before
    it does its own resampling only throws away detail the fingerprint needs.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(offset), "-t", str(seconds), "-i", str(src),
                # Phone clips are often quiet with the music well under
                # speech; levelling the audio gives the fingerprint more to
                # work with. Nothing is filtered out, only evened up.
                "-vn", "-af", "dynaudnorm=f=200:g=5",
                "-ac", "1", "-ar", "44100", "-b:a", "128k", str(dest),
            ],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0:
            log.warning(
                "ffmpeg window @%ds failed: %s",
                offset, proc.stderr.decode("utf-8", "replace")[:200],
            )
            return None
    except Exception as e:
        log.warning("window extraction failed at %ds: %s", offset, e)
        return None

    # A window past the end of the file produces a near-empty file.
    if dest.exists() and dest.stat().st_size > 4000:
        return dest
    return None


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

    for i, off in enumerate(offsets):
        clip = tmp_dir / f"{path.stem}_w{i}.mp3"
        made = await asyncio.to_thread(_extract_window, path, off, window, clip)
        if made is None:
            continue

        if i:
            await asyncio.sleep(0.4)  # don't machine-gun the Shazam endpoint
        try:
            song = await _recognize_once(made)
        finally:
            clip.unlink(missing_ok=True)

        if not song:
            log.info("Shazam window @%ds: no match", off)
            continue

        k = _key(song)
        songs.setdefault(k, song)
        votes[k] = votes.get(k, 0) + 1
        log.info("Shazam window @%ds: %s - %s", off, song.artist, song.title)

        # Two windows agreeing is a confident match; stop sampling.
        if votes[k] >= 2:
            break

    if votes:
        ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
        return [(songs[k], n) for k, n in ranked]

    # Windowing must never do worse than the original behaviour: if every
    # window came back empty (ffmpeg unavailable, an unreadable container, or
    # music that only surfaces where we did not sample), hand Shazam the whole
    # file exactly as before.
    log.info("recognize: no window matched - retrying with the whole file")
    song = await _recognize_once(path)
    return [(song, 1)] if song else []


async def recognize_file(path: Path) -> RecognizedSong | None:
    """Best single answer, or None when nothing was recognised."""
    candidates = await recognize_candidates(path)
    return candidates[0][0] if candidates else None


@run_in_thread
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
