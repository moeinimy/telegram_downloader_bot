"""
Speech to text for the "video subtitles" button.

faster-whisper, running on the CPU. Deliberately NOT in requirements.txt, for
the same reason instagrapi is not: it drags in ctranslate2 and a model
download, and a failure there must not be able to break a working venv on
every update. Install it on its own:

    botctl whisper

Without it the button says so and names the command, rather than erroring.

Sized for what this is actually used on - reels, a few seconds to a minute.
The default model is "base": on a plain VPS core it transcribes a 15-second
clip in a handful of seconds and handles Persian and English. "small" is
noticeably better and roughly three times slower; "tiny" is fast and only
worth it for clear English speech.

Model weights land in the downloads directory, which is the one path the
systemd unit grants write access to - the library's default cache is under
$HOME, which ProtectHome=true makes unwritable for this service.
"""

from __future__ import annotations

import logging
import os

from config import settings
from utils.helpers import run_in_thread

log = logging.getLogger(__name__)

_model = None
_MAX_SECONDS = 600  # a 10-minute cap; longer than any reel and it protects the CPU


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def _load():
    """Built once and kept. Loading the weights is the expensive part, and
    doing it per request would make every subtitle cost several seconds more
    than the transcription itself."""
    global _model

    if _model is None:
        from faster_whisper import WhisperModel

        cache = settings.download_dir / "whisper"
        cache.mkdir(parents=True, exist_ok=True)
        # HF_HOME rather than the library argument: some versions read the
        # environment for the tokenizer download regardless.
        os.environ.setdefault("HF_HOME", str(cache))

        log.info("whisper: loading model %s (first run downloads it)", settings.whisper_model)
        _model = WhisperModel(
            settings.whisper_model,
            device="cpu",
            compute_type="int8",  # ~4x faster on CPU, negligible quality cost
            download_root=str(cache),
        )
    return _model


@run_in_thread(heavy=True)
def transcribe(path) -> tuple[str, str]:
    """(text, detected language). Empty text when there is nothing to hear."""
    model = _load()

    segments, info = model.transcribe(
        str(path),
        beam_size=1,          # greedy: the quality difference is small, the speed is not
        vad_filter=True,      # skip silence and music-only stretches
        condition_on_previous_text=False,  # stops a bad guess cascading
    )

    parts: list[str] = []
    for segment in segments:
        if segment.start > _MAX_SECONDS:
            break
        text = segment.text.strip()
        if text:
            parts.append(text)

    return " ".join(parts).strip(), getattr(info, "language", "") or ""
