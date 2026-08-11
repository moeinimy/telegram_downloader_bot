"""
Speech to text for the "video subtitles" button.

Two backends behind one call, chosen by what is configured:

  api     an OpenAI-compatible /audio/transcriptions endpoint (Groq, OpenAI,
          anything that speaks the same shape). Preferred, and by a wide
          margin: it runs whisper-large-v3, which is the model that actually
          gets Persian right, and it costs this server nothing at all.
  local   faster-whisper on the CPU. No account needed, but on a shared VPS
          core it is slow and it competes with every download the bot is
          doing at the same time.

Why the default moved. The first local attempt used "base" and produced
Persian that was invented - real words, right rhythm, no meaning. Sizing up
to large-v3-turbo with beam search fixed the text and made one clip take five
minutes while pinning the box, which is not a trade worth making for a
subtitle button. The API does both properly, so local is now the fallback
rather than the plan.

Local is capped rather than left to take what it likes:

  * one transcription at a time, process-wide
  * half the cores by default, so downloads and uploads keep running
  * a hard timeout, so a pathological file cannot occupy a core forever

Neither backend is in requirements.txt. faster-whisper drags in ctranslate2
and a model download, and a failure there must not be able to break a working
venv on update.
"""

from __future__ import annotations

import asyncio
import logging
import os

from config import settings
from utils.helpers import run_in_thread

log = logging.getLogger(__name__)

_model = None
_MAX_SECONDS = 600          # 10 minutes of audio; longer than any reel
_LOCAL_TIMEOUT = 900        # and a wall clock limit on top of it
_MAX_UPLOAD_BYTES = 24 * 1024 * 1024   # Groq's free-tier request cap

# One at a time. Whisper is CPU-bound and re-entrant use just makes every
# caller slower while starving the download pool of cores.
_local_gate = asyncio.Semaphore(1)


def api_configured() -> bool:
    return bool(settings.whisper_api_key and settings.whisper_api_url)


def local_available() -> bool:
    try:
        import faster_whisper  # noqa: F401

        return True
    except Exception:
        return False


def backend() -> str:
    if api_configured():
        return "api"
    if local_available():
        return "local"
    return "none"


# --------------------------------------------------------------------------
# API backend
# --------------------------------------------------------------------------

@run_in_thread
def _via_api(path) -> tuple[str, str]:
    import httpx

    size = path.stat().st_size
    if size > _MAX_UPLOAD_BYTES:
        raise RuntimeError(f"audio is {size // 1024 // 1024}MB, over the API limit")

    url = settings.whisper_api_url.rstrip("/") + "/audio/transcriptions"
    with path.open("rb") as handle:
        # verbose_json so the detected language comes back; plain json omits it.
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {settings.whisper_api_key}"},
            files={"file": (path.name, handle, "audio/m4a")},
            data={"model": settings.whisper_api_model, "response_format": "verbose_json"},
            timeout=120,
        )

    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")

    payload = response.json()
    return (payload.get("text") or "").strip(), payload.get("language") or ""


# --------------------------------------------------------------------------
# Local backend
# --------------------------------------------------------------------------

def _cpu_threads() -> int:
    configured = settings.whisper_cpu_threads
    if configured > 0:
        return configured
    # Half the box, at least one. Left at the library default it takes every
    # core, and the bot spends the whole transcription unable to download.
    return max(1, (os.cpu_count() or 2) // 2)


def _load():
    """Built once and kept. Loading the weights is the expensive part, and
    doing it per request would cost more than most transcriptions."""
    global _model

    if _model is None:
        from faster_whisper import WhisperModel

        cache = settings.download_dir / "whisper"
        cache.mkdir(parents=True, exist_ok=True)
        # HF_HOME rather than the library argument: some versions read the
        # environment for the tokenizer download regardless. The library's own
        # default is under $HOME, which ProtectHome=true makes unwritable.
        os.environ.setdefault("HF_HOME", str(cache))

        threads = _cpu_threads()
        log.info(
            "whisper: loading %s on %d thread(s) (first run downloads it)",
            settings.whisper_model, threads,
        )
        _model = WhisperModel(
            settings.whisper_model,
            device="cpu",
            compute_type="int8",   # ~4x faster on CPU, negligible quality cost
            cpu_threads=threads,
            num_workers=1,
            download_root=str(cache),
        )
    return _model


# Whisper writes what it hears in the style the prompt establishes. Without
# one it drifts toward Arabic orthography and Finglish for Persian; a couple
# of correctly-spelled sentences anchor it.
_PROMPTS = {
    "fa": "این یک ویدیوی فارسی است. گفتار روزمره، با نگارش درست فارسی و نقطه‌گذاری.",
    "ar": "هذا مقطع فيديو باللغة العربية، مع علامات الترقيم الصحيحة.",
    "en": "The following is a clear English transcript, with punctuation.",
}


def _detect(model, path) -> str:
    """Language of the first window. Cheap - one token, not the audio."""
    try:
        language, probability, _ = model.detect_language(str(path))
        log.info("whisper: detected %s (%.0f%%)", language, probability * 100)
        return language or ""
    except Exception as e:
        log.info("whisper: language detection unavailable (%s)", e)
        return ""


@run_in_thread(heavy=True)
def _via_local(path) -> tuple[str, str]:
    model = _load()
    language = _detect(model, path)

    segments, info = model.transcribe(
        str(path),
        language=language or None,
        initial_prompt=_PROMPTS.get(language),
        # Beam search, but narrow. beam_size=1 let a wrong word early drag the
        # rest of the sentence after it; 5 with best_of=5 was most of why one
        # clip took five minutes. Two is the useful part of the difference.
        beam_size=2,
        # Whisper's fallback ladder, shortened. It retries a window hotter
        # when the output comes out degenerate - the repeated "گگگهل" failure -
        # but every extra rung is another full decode of that window.
        temperature=[0.0, 0.4, 0.8],
        compression_ratio_threshold=2.4,
        no_speech_threshold=0.6,
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


# --------------------------------------------------------------------------

async def transcribe(path) -> tuple[str, str]:
    """(text, detected language). Empty text when there is nothing to hear."""
    if api_configured():
        try:
            return await _via_api(path)
        except Exception as e:
            log.warning("whisper: API failed (%s)", e)
            if not local_available():
                raise
            log.info("whisper: falling back to the local model")

    async with _local_gate:
        return await asyncio.wait_for(_via_local(path), timeout=_LOCAL_TIMEOUT)


def available() -> bool:
    return backend() != "none"
