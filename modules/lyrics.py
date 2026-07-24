"""
Lyrics lookup via lrclib.net - free, no API key required.

fetch_lyrics(artist, title) -> str | None
Tries an exact match first, then a fuzzy search.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

_BASE = "https://lrclib.net/api"
_HEADERS = {"User-Agent": "telegram-downloader-bot (personal use)"}


def _pick_text(item: dict) -> str | None:
    text = item.get("plainLyrics")
    if text and text.strip():
        return text.strip()
    synced = item.get("syncedLyrics")
    if synced and synced.strip():
        # strip [mm:ss.xx] timestamps from synced lyrics
        import re

        lines = []
        for line in synced.splitlines():
            cleaned = re.sub(r"^\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]\s*", "", line)
            lines.append(cleaned)
        out = "\n".join(lines).strip()
        return out or None
    return None


async def fetch_lyrics(artist: str, title: str) -> str | None:
    async with httpx.AsyncClient(timeout=15, headers=_HEADERS) as client:
        # 1) exact match
        try:
            r = await client.get(
                f"{_BASE}/get",
                params={"artist_name": artist, "track_name": title},
            )
            if r.status_code == 200:
                text = _pick_text(r.json())
                if text:
                    return text
        except Exception as e:
            log.warning("lrclib get failed: %s", e)

        # 2) fuzzy search
        try:
            r = await client.get(f"{_BASE}/search", params={"q": f"{artist} {title}"})
            if r.status_code == 200:
                for item in r.json():
                    text = _pick_text(item)
                    if text:
                        return text
        except Exception as e:
            log.warning("lrclib search failed: %s", e)

    return None
