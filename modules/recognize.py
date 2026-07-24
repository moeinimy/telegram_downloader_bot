"""
Music recognition (Shazam) from a media file or URL.

The handler may call fetch_audio_snippet() with different offsets to sample
multiple windows of a long video (songs often start mid-video).
"""

from __future__ import annotations

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


async def recognize_file(path: Path) -> RecognizedSong | None:
    """Run Shazam recognition on a local audio/video file."""
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
    log.info("Shazam matched: %s - %s", artist, title)
    return RecognizedSong(title=title, artist=artist)


@run_in_thread
def fetch_audio_snippet(url: str, seconds: int = 45, offset: int = 0) -> Path:
    """
    Download a short audio snippet [offset, offset+seconds] from a video URL
    for fingerprinting. Reuses the YouTube base options (cookies + EJS
    solver), which are harmless for other sites.
    """
    from yt_dlp import YoutubeDL

    try:
        from yt_dlp.utils import download_range_func
    except Exception:
        download_range_func = None

    from modules.youtube import _base_opts

    out_dir = settings.download_dir / "recognize"
    out_dir.mkdir(parents=True, exist_ok=True)
    outtmpl = str(out_dir / f"%(id)s_snip{offset}.%(ext)s")

    opts = _base_opts() | {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "overwrites": True,
    }
    # Clip to the requested window so long videos stay fast.
    if download_range_func:
        opts["download_ranges"] = download_range_func(
            None, [(offset, offset + seconds)]
        )
        opts["force_keyframes_at_cuts"] = True

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))

    if not path.exists():
        candidates = list(out_dir.glob(f"{info['id']}_snip{offset}.*"))
        if candidates:
            path = candidates[0]
    if not path.exists():
        raise RuntimeError("Snippet download produced no file")
    return path
