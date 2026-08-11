"""
Speech to text for the "video subtitles" button.

faster-whisper, running on the CPU. Deliberately NOT in requirements.txt, for
the same reason instagrapi is not: it drags in ctranslate2 and a model
download, and a failure there must not be able to break a working venv on
every update. Install it on its own:

    botctl whisper

Without it the button says so and names the command, rather than erroring.

Model choice is the whole story for Persian.

The first version defaulted to "base" and produced Persian that was almost
entirely invented - recognisable words in the right rhythm, meaning nothing.
That is not a bug, it is what the small multilingual models do with Persian:
they were trained overwhelmingly on English, and English is the only language
they hold up in at that size. So the default is now large-v3-turbo, which is
a distilled decoder on top of large-v3 - close to large quality at roughly
four times the speed.

    tiny / base      English only, in practice
    small            usable English, Persian still poor
    medium           Persian becomes readable
    large-v3-turbo   default; Persian is actually correct
    large-v3         marginally better, several times slower

The cost is memory: large-v3-turbo needs roughly 1.5-2GB of RAM. `botctl
whisper` checks what the box has before offering it.

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


# Whisper writes what it hears in the style the prompt establishes. Without
# one it drifts toward Arabic orthography and Finglish for Persian; a couple
# of correctly-spelled Persian sentences anchor it. The prompt has to match
# the spoken language, which is why the language is detected first.
_PROMPTS = {
    "fa": "این یک ویدیوی فارسی است. گفتار روزمره، با نگارش درست فارسی و نقطه‌گذاری.",
    "ar": "هذا مقطع فيديو باللغة العربية، مع علامات الترقيم الصحيحة.",
    "en": "The following is a clear English transcript, with punctuation.",
}


def _detect(model, path) -> str:
    """Language of the first window. Cheap - it decodes one token, not the
    audio - and worth it because the prompt below depends on the answer."""
    try:
        language, probability, _ = model.detect_language(str(path))
        log.info("whisper: detected %s (%.0f%%)", language, probability * 100)
        return language or ""
    except Exception as e:
        # Older faster-whisper has no detect_language; transcribing without a
        # prompt is still fine, just slightly worse.
        log.info("whisper: language detection unavailable (%s)", e)
        return ""


@run_in_thread(heavy=True)
def transcribe(path) -> tuple[str, str]:
    """(text, detected language). Empty text when there is nothing to hear."""
    model = _load()
    language = _detect(model, path)

    segments, info = model.transcribe(
        str(path),
        language=language or None,
        initial_prompt=_PROMPTS.get(language),
        # Beam search, not greedy. beam_size=1 was chosen for speed in the
        # first version and it showed: on Persian the greedy path commits to
        # a wrong word early and the rest of the sentence follows it.
        beam_size=5,
        best_of=5,
        # Whisper's own fallback ladder. When a window comes out with low
        # confidence or degenerate repetition it retries hotter instead of
        # emitting the garbage - which is exactly the "گگگهل" failure.
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        compression_ratio_threshold=2.4,
        no_speech_threshold=0.6,
        # Also switched back on. Turning it off stops a bad guess cascading
        # but costs every sentence its context, and context is most of what
        # makes an inflected language come out right.
        condition_on_previous_text=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 700, "speech_pad_ms": 200},
    )

    parts: list[str] = []
    for segment in segments:
        if segment.start > _MAX_SECONDS:
            break
        text = segment.text.strip()
        if text:
            parts.append(text)

    return " ".join(parts).strip(), (language or getattr(info, "language", "") or "")
