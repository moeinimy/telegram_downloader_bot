"""
Lyrics lookup across several free sources, tried in priority order.

None of these needs an API key. They are ordered by what they are good at,
and the first usable answer wins:

  lrclib      Free, keyless, and the only one carrying time-synced lyrics.
              Excellent for anything in its database, thin on older or
              regional material.
  lyrics.ovh  Free, keyless, plain text. Different catalogue from lrclib, so
              it fills a lot of lrclib's gaps.
  genius      Best coverage by far, especially hip-hop and album cuts, but the
              lyrics live in the page HTML rather than the API, and its search
              happily returns a different song - so the match is verified
              against what was asked before the text is used.

Order is configurable with LYRICS_SOURCES. A source that errors is skipped
and the next one is tried, so one being down never costs the feature.

fetch_lyrics(artist, title) -> str | None
"""

from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote

from config import settings

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_MIN_CHARS = 80  # shorter than this is a stub, not lyrics


# ---------------- matching helpers ----------------

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9؀-ۿ]+", " ", (text or "").lower()).strip()


def _looks_like(candidate: str, artist: str, title: str) -> bool:
    """
    Guard against a search returning a different song.

    Genius in particular answers with its nearest guess rather than nothing,
    so asking for one track and printing another's words is a real risk.
    """
    hay = _norm(candidate)
    want_title = set(_norm(title).split())
    if not want_title:
        return False
    hit = len(want_title & set(hay.split())) / len(want_title)
    return hit >= 0.6


def _strip_timestamps(synced: str) -> str:
    lines = [
        re.sub(r"^\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]\s*", "", line)
        for line in synced.splitlines()
    ]
    return "\n".join(lines).strip()


def _clean(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text or "")
    return text.strip()


# ---------------- sources ----------------

def _from_lrclib(artist: str, title: str) -> str | None:
    from utils import http

    def pick(item: dict) -> str | None:
        plain = (item.get("plainLyrics") or "").strip()
        if plain:
            return plain
        synced = (item.get("syncedLyrics") or "").strip()
        return _strip_timestamps(synced) if synced else None

    r = http.get(
        "https://lrclib.net/api/get",
        params={"artist_name": artist, "track_name": title},
        headers={"User-Agent": "telegram-downloader-bot (personal use)"},
    )
    if r.status_code == 200:
        text = pick(r.json())
        if text:
            return text

    r = http.get(
        "https://lrclib.net/api/search",
        params={"q": f"{artist} {title}"},
        headers={"User-Agent": "telegram-downloader-bot (personal use)"},
    )
    if r.status_code == 200:
        for item in r.json() or []:
            if not _looks_like(item.get("trackName", ""), artist, title):
                continue
            text = pick(item)
            if text:
                return text
    return None


def _from_lyrics_ovh(artist: str, title: str) -> str | None:
    from utils import http

    r = http.get(f"https://api.lyrics.ovh/v1/{quote(artist)}/{quote(title)}")
    if r.status_code != 200:
        return None
    return (r.json().get("lyrics") or "").strip() or None


def _from_genius(artist: str, title: str) -> str | None:
    from utils import http

    r = http.get(
        "https://genius.com/api/search/multi",
        params={"q": f"{artist} {title}"},
        headers={"User-Agent": _UA},
    )
    if r.status_code != 200:
        return None

    for section in (r.json().get("response") or {}).get("sections") or []:
        for hit in section.get("hits") or []:
            res = hit.get("result") or {}
            url, full = res.get("url"), res.get("full_title", "")
            if not url:
                continue
            # Genius answers with its nearest guess, so verify before using it.
            if not _looks_like(full or res.get("title", ""), artist, title):
                continue

            page = http.get(url, headers={"User-Agent": _UA})
            if page.status_code != 200:
                continue
            blocks = re.findall(
                r'data-lyrics-container="true"[^>]*>(.*?)</div>', page.text, re.S
            )
            if not blocks:
                continue
            text = "\n".join(blocks)
            text = re.sub(r"<br\s*/?>", "\n", text)
            text = re.sub(r"<[^>]+>", "", text)
            text = html.unescape(text)
            # The container opens with "N ContributorsTranslations...<Song>
            # Lyrics"; drop everything up to that marker.
            text = re.sub(r"^.{0,800}?\bLyrics\b", "", text, count=1, flags=re.S)
            text = _clean(text)
            if len(text) >= _MIN_CHARS:
                return text
    return None


_SOURCES = {
    "lrclib": _from_lrclib,
    "lyricsovh": _from_lyrics_ovh,
    "genius": _from_genius,
}


def _order() -> list[str]:
    wanted = [s for s in settings.lyrics_sources if s in _SOURCES]
    return wanted or list(_SOURCES)


async def fetch_lyrics(artist: str, title: str) -> str | None:
    """First source with a usable answer wins; failures fall through."""
    import asyncio

    for name in _order():
        try:
            text = await asyncio.to_thread(_SOURCES[name], artist, title)
        except Exception as e:
            log.info("lyrics source %s failed for %s - %s: %s", name, artist, title, e)
            continue
        text = _clean(text or "")
        if len(text) >= _MIN_CHARS:
            log.info("lyrics: %s answered for %s - %s (%d chars)", name, artist, title, len(text))
            return text
    log.info("lyrics: nothing found for %s - %s", artist, title)
    return None
