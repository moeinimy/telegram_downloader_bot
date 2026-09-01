"""
Spotify metadata without any API credentials.

Spotify's Web API now requires the dev-app owner to hold an active Premium
subscription for most endpoints (policy change late-2024); free-tier apps get
HTTP 403. The public `open.spotify.com/embed/<kind>/<id>` pages need no auth at
all and expose the full entity as JSON inside a <script id="__NEXT_DATA__"> tag.

Covers every entity the bot needs:
    track    -> name, artists, duration, cover art
    album    -> name, artists, cover, full track list
    playlist -> name, owner, full track list
    artist   -> name + top 10 tracks

Free-text search is NOT available here; the caller falls back to
YouTube + SoundCloud search (see modules/spotify.py).
"""

from __future__ import annotations

import json
import logging
import re
import time

import httpx

from utils.i18n import Localised

log = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)


def _fetch_entity(kind: str, resource_id: str) -> dict:
    """Return the `entity` object from an embed page, retrying on 5xx."""
    url = f"https://open.spotify.com/embed/{kind}/{resource_id}"
    last: Exception | None = None
    started = time.monotonic()

    # This is the first thing a pasted link does and nothing can be shown
    # until it returns, so its cost is the bot's response time. It used to
    # build a fresh httpx.Client per attempt - a DNS lookup and a full TLS
    # handshake each time, for the one request in the bot where latency is
    # most visible - and then wait 20s before deciding an attempt had failed,
    # with 1.5s/3s/4.5s of sleeping between four of them. A single slow
    # attempt could therefore cost most of a minute before the first reply.
    from utils import http

    for attempt in range(3):
        try:
            r = http.client().get(
                url,
                headers={"User-Agent": _UA, "Accept-Language": "en"},
                timeout=12,
                follow_redirects=True,
            )
            if r.status_code >= 500:
                raise RuntimeError(f"Spotify embed returned {r.status_code}")
            r.raise_for_status()

            m = _NEXT_DATA_RE.search(r.text)
            if not m:
                raise RuntimeError("Spotify embed page did not contain __NEXT_DATA__")
            data = json.loads(m.group(1))

            entity = (
                data.get("props", {})
                .get("pageProps", {})
                .get("state", {})
                .get("data", {})
                .get("entity")
            )
            if not entity:
                entity = _walk_for_entity(data, kind)
            if not entity:
                raise RuntimeError(f"Could not locate {kind} data in Spotify embed page")
            log.info("spotify embed %s/%s in %.1fs (attempt %d)",
                     kind, resource_id, time.monotonic() - started, attempt + 1)
            return entity
        except Exception as e:
            last = e
            log.warning("Spotify embed attempt %d for %s/%s failed after %.1fs: %s",
                        attempt + 1, kind, resource_id,
                        time.monotonic() - started, e)
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))

    raise Localised("Spotify صفحه رو نداد: {why}", why=str(last))


def _walk_for_entity(obj, want_type: str | None = None):
    """Recursive fallback in case Spotify moves the entity in the JSON tree."""
    if isinstance(obj, dict):
        t = obj.get("type")
        if want_type and isinstance(t, str) and t.lower() == want_type:
            return obj
        if not want_type and "name" in obj and ("artists" in obj or "trackList" in obj):
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


def _cover(entity: dict) -> str:
    """Largest available cover-art URL."""
    for key in ("visualIdentity", "coverArt"):
        v = entity.get(key)
        if isinstance(v, dict):
            images = v.get("image") or v.get("sources") or []
            if images:
                best = max(
                    images,
                    key=lambda i: i.get("maxHeight") or i.get("height") or 0,
                )
                return best.get("url", "")
    return ""


def _artists_of(entity: dict) -> list[str]:
    raw = entity.get("artists")
    if isinstance(raw, list):
        names = [a.get("name", "") for a in raw if isinstance(a, dict)]
        names = [n for n in names if n]
        if names:
            return names
    if entity.get("subtitle"):
        return [entity["subtitle"]]
    return ["Unknown"]


def _track_id_from_uri(uri: str) -> str:
    return uri.rsplit(":", 1)[-1] if uri else ""


def _tracklist_to_metas(entity: dict, fallback_cover: str, album_name: str = ""):
    """
    Convert an embed trackList into TrackMeta objects.

    The embed's per-track entries carry only title/subtitle/duration/uri - no
    artwork. `fallback_cover` is therefore correct for an album (every track
    really does share one cover) but wrong for a playlist, where it would stamp
    the playlist's image onto every song. Playlists pass "" and the real cover
    is looked up per track at download time (modules.spotify.ensure_cover).
    """
    from modules.spotify import TrackMeta  # avoid circular import at module load

    metas = []
    for item in entity.get("trackList") or []:
        tid = _track_id_from_uri(item.get("uri", ""))
        if not tid:
            continue
        metas.append(
            TrackMeta(
                id=tid,
                name=item.get("title") or "Unknown",
                artists=[item.get("subtitle")] if item.get("subtitle") else ["Unknown"],
                album=album_name,
                duration_ms=int(item.get("duration") or 0),
                cover_url=fallback_cover,
                spotify_url=f"https://open.spotify.com/track/{tid}",
            )
        )
    return metas


# ---------------- public API ----------------

def fetch_track_meta(track_id: str):
    from modules.spotify import TrackMeta

    entity = _fetch_entity("track", track_id)
    album = entity.get("album")
    return TrackMeta(
        id=track_id,
        name=entity.get("name") or entity.get("title") or "Unknown",
        artists=_artists_of(entity),
        album=album.get("name", "") if isinstance(album, dict) else "",
        duration_ms=int(entity.get("duration") or entity.get("duration_ms") or 0),
        cover_url=_cover(entity),
        spotify_url=f"https://open.spotify.com/track/{track_id}",
    )


def fetch_album(album_id: str):
    from modules.spotify import AlbumMeta

    entity = _fetch_entity("album", album_id)
    name = entity.get("name") or "Album"
    cover = _cover(entity)
    return AlbumMeta(
        id=album_id,
        name=name,
        artists=[entity["subtitle"]] if entity.get("subtitle") else _artists_of(entity),
        cover_url=cover,
        tracks=_tracklist_to_metas(entity, cover, album_name=name),
    )


def fetch_playlist(playlist_id: str):
    from modules.spotify import PlaylistMeta

    entity = _fetch_entity("playlist", playlist_id)
    # Deliberately no fallback cover on the tracks: each would otherwise show
    # the playlist artwork instead of its own album art. The playlist image is
    # kept on the container, for the single header photo.
    tracks = _tracklist_to_metas(entity, "")
    pl = PlaylistMeta(
        id=playlist_id,
        name=entity.get("name") or "Playlist",
        owner=entity.get("subtitle") or "Spotify",
        cover_url=_cover(entity),
        tracks=tracks,
    )
    # The keyless embed page serves at most 100 entries; tell the caller so it
    # can say so rather than silently presenting a truncated playlist.
    pl.truncated = len(tracks) >= 100
    return pl


def fetch_artist_top(artist_id: str):
    """Top tracks shown on an artist's embed page (usually 10)."""
    entity = _fetch_entity("artist", artist_id)
    return _tracklist_to_metas(entity, _cover(entity))
