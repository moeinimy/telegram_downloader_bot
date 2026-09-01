"""Cut a piece out of a track or a video.

Send the media, then say `3:28-4:53`. Everything outside that range goes.

Two decisions worth writing down.

WHY STREAM COPY FIRST. Re-encoding a five minute video to keep forty
seconds of it costs more CPU than the whole download did, and it throws
away a generation of quality for a job that is really just deleting bytes.
`-c copy` does no decoding at all: it is close to instant and the output is
bit-identical to the source inside the range.

WHY IT IS NOT ALWAYS ENOUGH. Video can only be copied from a keyframe, and
keyframes are seconds apart. Asking for 3:28 on a stream whose nearest
keyframe is at 3:25 gets three extra seconds at the front - the clip is
right but it does not start where it was asked to. Audio has no such
problem; its frames are milliseconds long.

So: copy, then measure. If the result is longer than asked for by more than
a moment, the cut was keyframe-snapped and it is redone properly with an
encode. Most cuts never pay for that, and the ones that would have been
visibly wrong do.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# How far the copied result may drift from the requested length before it is
# worth re-encoding. One second is under a keyframe interval on most sources
# and over the rounding noise of container timestamps.
_DRIFT_TOLERANCE = 1.0

# A cut may not exceed this. Not a policy about length - a guard against a
# typo like "1-9999" quietly asking for a three hour encode.
_MAX_SECONDS = 3 * 60 * 60

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# 3:28-4:53 · 1:02:30 - 1:05:00 · 28-53 · ۳:۲۸ تا ۴:۵۳
_SEPARATOR = r"(?:\s*(?:-|–|—|to|until|تا)\s*)"
_STAMP = r"\d{1,2}(?::\d{1,2}){0,2}(?:[.,]\d{1,3})?"
_RANGE_RE = re.compile(rf"^{_SEPARATOR.join(('(' + _STAMP + ')',) * 2)}$")


def _seconds(stamp: str) -> float | None:
    """SS, MM:SS or HH:MM:SS to seconds."""
    stamp = stamp.replace(",", ".")
    parts = stamp.split(":")
    if len(parts) > 3:
        return None
    total = 0.0
    for part in parts:
        try:
            value = float(part)
        except ValueError:
            return None
        # Only the first component may exceed 59: "90" alone is a minute and a
        # half, but "3:90" is not a time anybody means.
        if part is not parts[0] and value >= 60:
            return None
        total = total * 60 + value
    return total


def parse_range(text: str) -> tuple[float, float] | None:
    """(start, end) in seconds, or None when this is not a cut request.

    Returning None rather than raising is what lets this be tried against
    every incoming message: anything that is not a time range is somebody
    else's to handle.
    """
    cleaned = (text or "").strip().translate(_DIGITS)
    # Persian text arrives with direction marks that are invisible and are
    # not whitespace, so they survive .strip() and break the match.
    cleaned = re.sub(r"[‌‎‏‪-‮]", "", cleaned)
    match = _RANGE_RE.match(cleaned)
    if not match:
        return None
    start, end = _seconds(match.group(1)), _seconds(match.group(2))
    if start is None or end is None:
        return None
    if end <= start:
        return None
    if end - start > _MAX_SECONDS:
        return None
    return start, end


def format_stamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def media_seconds(path: Path) -> float:
    """Length of a media file, or 0.0 when ffprobe will not say."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float((result.stdout or "").strip())
    except Exception:
        return 0.0


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "ffmpeg failed").strip()[:200])


def _has_video(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return "video" in (result.stdout or "")
    except Exception:
        return False


def cut(src: Path, start: float, end: float, dest: Path) -> Path:
    """Write the [start, end) slice of `src` to `dest`.

    `-ss` goes BEFORE `-i` so ffmpeg seeks rather than decoding and
    discarding everything up to the start - on a long video that difference
    is minutes. `-t` goes after, because a duration is unambiguous where a
    second absolute timestamp is not: the meaning of `-to` before an input
    has changed between ffmpeg releases.
    """
    duration = end - start
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)

    _run(["ffmpeg", "-y", "-loglevel", "error",
          "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
          "-c", "copy",
          # Copied packets keep their original timestamps, which start at
          # `start` rather than zero. Players that respect that show a clip
          # which begins several minutes in and seek to the wrong place.
          "-avoid_negative_ts", "make_zero",
          str(dest)])

    produced = media_seconds(dest)
    if produced and abs(produced - duration) <= _DRIFT_TOLERANCE:
        return dest

    # Keyframe-snapped. Redo it with an encode, which can start anywhere.
    log.info("cut: copy gave %.1fs for a %.1fs request - re-encoding",
             produced, duration)
    codecs = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-c:a", "aac", "-b:a", "192k"]
              if _has_video(src) else
              ["-c:a", "libmp3lame", "-q:a", "2"])
    dest.unlink(missing_ok=True)
    _run(["ffmpeg", "-y", "-loglevel", "error",
          "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
          *codecs, str(dest)])
    return dest
