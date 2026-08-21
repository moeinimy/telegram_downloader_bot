"""
Persistent Telegram file_id cache.

Telegram keeps every file a bot uploads and hands back a `file_id`. Sending
that id again costs one API call: no yt-dlp run, no ffmpeg pass, no upload.
That is the whole difference between "30 seconds of downloading" and an
instant reply for a song somebody already asked for.

Keys are canonical and source-based, e.g.:
    audio:yt_dQw4w9WgXcQ
    video:yt_dQw4w9WgXcQ:720p

The map is written atomically to downloads/file_ids.json so a crash midway
through a write cannot corrupt it.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_PATH: Path = settings.download_dir / "file_ids.json"
_cache: dict[str, str] | None = None


def _load() -> dict[str, str]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_PATH.read_text(encoding="utf-8"))
            log.info("file_id cache: %d entries loaded", len(_cache))
        except FileNotFoundError:
            _cache = {}
        except Exception as e:
            log.warning("file_id cache unreadable (%s) - starting empty", e)
            _cache = {}
    return _cache


def _flush(data: dict[str, str]) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_PATH)
    except Exception as e:
        log.warning("file_id cache write failed: %s", e)


def get(key: str) -> str | None:
    if not key:
        return None
    with _LOCK:
        return _load().get(key)


def put(key: str, file_id: str) -> None:
    if not key or not file_id:
        return
    with _LOCK:
        data = _load()
        if data.get(key) == file_id:
            return
        data[key] = file_id
        _flush(data)


def drop(key: str) -> None:
    """Forget an id Telegram no longer accepts, so the next request re-uploads."""
    with _LOCK:
        data = _load()
        if data.pop(key, None) is not None:
            _flush(data)


def snapshot() -> dict[str, str]:
    """A copy of every id held, for anything that needs to walk them.

    A copy rather than the live dict: the backfill that reads this takes
    minutes, and a download finishing halfway through would otherwise mutate
    what it is iterating.
    """
    with _LOCK:
        return dict(_load())
