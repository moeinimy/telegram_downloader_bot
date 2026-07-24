"""
Fallback Spotify metadata fetcher.

Spotify's Web API now requires the dev-app owner to have an active Premium
subscription for most track/album endpoints (policy change late-2024). Free-tier
apps get HTTP 403 with "Active premium subscription required for the owner of
the app." This module scrapes the public `open.spotify.com/embed/...` page,
which is unauthenticated and returns the track metadata as JSON inside a
<script id="__NEXT_DATA__"> tag.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

log = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


def _fetch_embed_json(kind: str, resource_id: str) -> dict:
    url = f"https://open.spotify.com/embed/{kind}/{resource_id}"
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        r = client.get(url, headers={"User-Agent": _UA, "Accept-Language": "en"})
        r.raise_for_status()
    m = _NEXT_DATA_RE.search(r.text)
    if not m:
        raise RuntimeError("Spotify embed page did not contain __NEXT_DATA__")
    return json.loads(m.group(1))


def _walk_for_entity(obj, want_type: str | None = None):
    """Recursively search for a dict that looks like a Spotify entity."""
    if isinstance(obj, dict):
        t = obj.get("type")
        if want_type and isinstance(t, str) and t.lower() == want_type:
            return obj
        if not want_type and "name" in obj and ("artists" in obj or "subtitle" in obj):
            return obj
        for v in obj.values():
            r = _walk_for_entity(v, want_type)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for item in obj:
            r = _walk_for_entity(item, want_type)
            if r is not None:
                return r
    return None


def _extract_cover(entity: dict) -> str:
    for key in ("visualIdentity", "coverArt"):
        v = entity.get(key)
        if isinstance(v, dict):
            images = v.get("image") or v.get("sources") or []
            if images:
                # pick largest available
                images_sorted = sorted(
                    images,
                    key=lambda i: i.get("maxHeight") or i.get("height") or 0,
                    reverse=True,
                )
                return images_sorted[0].get("url", "")
    return ""


def fetch_track_meta(track_id: str):
    """Returns a TrackMeta built from the public embed page (no auth required)."""
    from modules.spotify import TrackMeta  # avoid circular import

    data = _fetch_embed_json("track", track_id)
    entity = _walk_for_entity(data, want_type="track") or _walk_for_entity(data)
    if not entity:
        raise RuntimeError("Could not locate track entity in Spotify embed page")

    name = entity.get("name") or entity.get("title") or "Unknown"

    artists: list[str] = []
    if isinstance(entity.get("artists"), list):
        artists = [
            a.get("name", "") for a in entity["artists"] if isinstance(a, dict)
        ]
        artists = [a for a in artists if a]
    if not artists and entity.get("subtitle"):
        artists = [entity["subtitle"]]
    if not artists:
        artists = ["Unknown"]

    duration_ms = (
        entity.get("duration")
        or entity.get("duration_ms")
        or 0
    )

    return TrackMeta(
        id=track_id,
        name=name,
        artists=artists,
        album=entity.get("album", {}).get("name", "") if isinstance(entity.get("album"), dict) else "",
        duration_ms=int(duration_ms),
        cover_url=_extract_cover(entity),
        spotify_url=f"https://open.spotify.com/track/{track_id}",
    )
