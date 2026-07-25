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


async def _recognize_once(path: Path) -> RecognizedSong | None:
    from shazamio import Shazam

    shazam = Shazam()
    try:
        out = await shazam.recognize(str(path))
    except AttributeError:
        # older shazamio versions used recognize_song()
        out = await shazam.recognize_song(str(path))
    except Exception as e:
        log.warning("Shazam recognition error: %s", e)
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
    """Cut a mono 16kHz wav window - the format Shazam fingerprints best."""
    import subprocess

    try:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(offset), "-t", str(seconds), "-i", str(src),
                "-vn", "-ac", "1", "-ar", "16000", str(dest),
            ],
            capture_output=True, timeout=120,
        )
    except Exception as e:
        log.warning("window extraction failed at %ds: %s", offset, e)
        return None
    # A window past the end of the file produces a near-empty wav.
    return dest if dest.exists() and dest.stat().st_size > 8000 else None


async def recognize_file(path: Path) -> RecognizedSong | None:
    """
    Identify the music in a local audio/video file.

    A single sample is unreliable: speech, effects or an intro over the music
    makes Shazam answer with something unrelated (a clip of "Sicko Mode" came
    back as "Big Poppa"). Several windows across the file are fingerprinted
    instead and the answer that repeats wins; an answer seen only once, with
    others disagreeing, is discarded rather than reported as fact.
    """
    from collections import Counter

    duration = _media_duration(path)
    window = 12
    if duration <= 0:
        offsets = [0]
    else:
        # Skip the very start (intros/silence) and spread over the file.
        span = max(duration - window, 0)
        offsets = sorted({int(span * f) for f in (0.05, 0.3, 0.55, 0.8)})

    tmp_dir = path.parent
    votes: Counter = Counter()
    first: RecognizedSong | None = None

    for i, off in enumerate(offsets):
        clip = tmp_dir / f"{path.stem}_w{i}.wav"
        made = await asyncio.to_thread(_extract_window, path, off, window, clip)

        if made is None:
            # Past the end of the file, or ffmpeg could not read it. On the
            # very first window fall back to handing Shazam the whole file.
            if i == 0:
                song = await _recognize_once(path)
                if song:
                    return song
            continue

        try:
            song = await _recognize_once(made)
        finally:
            clip.unlink(missing_ok=True)

        if not song:
            continue
        first = first or song
        key = (song.artist.lower().strip(), song.title.lower().strip())
        votes[key] += 1
        log.info("Shazam window @%ds: %s - %s", off, song.artist, song.title)
        # Two windows agreeing is enough; stop paying for more.
        if votes[key] >= 2:
            return song

    if not votes:
        return None
    (artist, title), count = votes.most_common(1)[0]
    if count == 1 and len(votes) > 1:
        log.info("Shazam windows disagreed (%s) - reporting no match", list(votes))
        return None
    if first and (first.artist.lower().strip(), first.title.lower().strip()) == (artist, title):
        return first
    return RecognizedSong(title=title, artist=artist)


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
