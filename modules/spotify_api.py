"""
Spotify Web API via client credentials - optional, for large playlists.

The keyless embed page (modules/spotify_scraper.py) is enough for tracks,
albums and small playlists, but it never returns more than ~100 entries. A
4000-track playlist simply is not reachable that way, so nothing past the
first hundred can be listed or downloaded.

With SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET set, playlists are paged
properly instead. Note that Spotify's late-2024 policy change blocks new
free apps from Spotify-OWNED editorial playlists (the 37i9... ones); normal
user playlists still read fine, which is what this is for.
"""

from __future__ import annotations

import base64
import logging
import threading
import time

from config import settings

log = logging.getLogger(__name__)

_token: str = ""
_token_expires: float = 0.0
_lock = threading.Lock()

MAX_TRACKS = 5000


def track_isrc(track_id: str) -> str:
    """The recording's ISRC, or "" when the Web API is not usable.

    The ISRC is the one identifier that means "this exact recording" rather
    than "something with this name and roughly this length". Spotify's embed
    page does not carry it and neither does Odesli, so this endpoint is the
    only route to it for a Spotify link - and it needs the app owner to hold
    Premium (see _get).
    """
    if not available():
        return ""
    try:
        d = _get(f"https://api.spotify.com/v1/tracks/{track_id}")
    except Exception:
        return ""
    return ((d.get("external_ids") or {}).get("isrc") or "").strip()


def available() -> bool:
    return bool(settings.spotify_client_id and settings.spotify_client_secret)


def _access_token() -> str:
    """Client-credentials token, cached until shortly before it expires."""
    global _token, _token_expires
    with _lock:
        if _token and time.time() < _token_expires:
            return _token

        from utils import http

        auth = base64.b64encode(
            f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()
        ).decode()
        r = http.client().post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {auth}"},
        )
        r.raise_for_status()
        data = r.json()
        _token = data["access_token"]
        _token_expires = time.time() + int(data.get("expires_in", 3600)) - 60
        return _token


# Set when Spotify answers 403 with its "owner must be Premium" message, so
# the reason can be reported once instead of surfacing as an opaque failure on
# every playlist. Cleared by a successful call.
_blocked_reason: str = ""


def blocked_reason() -> str:
    """Why the Web API is refusing us, or "" when it is working / untried."""
    return _blocked_reason


def _get(url: str, params: dict | None = None) -> dict:
    from utils import http

    global _token_expires, _blocked_reason

    r = http.get(url, params=params, headers={"Authorization": f"Bearer {_access_token()}"})
    if r.status_code == 401:
        # Token rejected early - drop it and try once more.
        _token_expires = 0
        r = http.get(
            url, params=params, headers={"Authorization": f"Bearer {_access_token()}"}
        )
    if r.status_code == 403:
        # Spotify now requires the *app owner* to hold an active Premium
        # subscription; without one every endpoint answers 403, credentials and
        # token both perfectly valid. Worth naming, because "playlists past 100
        # tracks stopped working" gives no hint of it whatsoever.
        detail = ""
        try:
            detail = ((r.json().get("error") or {}).get("message") or "").strip()
        except Exception:
            detail = (r.text or "").strip()[:200]
        _blocked_reason = detail or "403 from the Spotify Web API"
        if not _get._warned:
            log.warning("Spotify Web API refused (%s) - falling back to the "
                        "embed page, which caps playlists at ~100 tracks", _blocked_reason)
            _get._warned = True
        r.raise_for_status()
    r.raise_for_status()
    _blocked_reason = ""
    return r.json()


_get._warned = False


def fetch_playlist(playlist_id: str):
    """Full playlist with every page of tracks."""
    from modules.spotify import PlaylistMeta, TrackMeta

    head = _get(
        f"https://api.spotify.com/v1/playlists/{playlist_id}",
        # Nested values use dot notation. "tracks(total)" is not valid fields
        # syntax: Spotify silently omitted it, total came back 0, and the
        # paging loop never ran - an empty playlist from a 4000-track list.
        {"fields": "name,owner.display_name,images,tracks.total"},
    )
    images = head.get("images") or []
    total = ((head.get("tracks") or {}).get("total")) or 0

    tracks: list[TrackMeta] = []
    offset = 0
    # Driven by what the API actually returns rather than by `total`, so a
    # missing or wrong count cannot silently produce an empty playlist.
    while offset < MAX_TRACKS:
        page = _get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
            {"limit": 100, "offset": offset},
        )
        items = page.get("items") or []
        if not items:
            break
        total = page.get("total") or total

        for it in items:
            t = it.get("track") or {}
            # Local files, removed tracks and podcast episodes have no usable id.
            if not t.get("id") or t.get("type") not in (None, "track"):
                continue
            album = t.get("album") or {}
            art = (album.get("images") or [{}])[0].get("url", "")
            tracks.append(
                TrackMeta(
                    id=t["id"],
                    name=t.get("name") or "Unknown",
                    artists=[a.get("name", "") for a in (t.get("artists") or [])] or ["Unknown"],
                    album=album.get("name") or "",
                    duration_ms=int(t.get("duration_ms") or 0),
                    cover_url=art,
                    spotify_url=f"https://open.spotify.com/track/{t['id']}",
                )
            )

        offset += len(items)
        if offset >= total > 0:
            break

    log.info("Spotify API: playlist %s -> %d usable of %d", playlist_id, len(tracks), total)
    if not tracks:
        raise RuntimeError("Spotify API returned no tracks")

    return PlaylistMeta(
        id=playlist_id,
        name=head.get("name") or "Playlist",
        owner=((head.get("owner") or {}).get("display_name")) or "Spotify",
        cover_url=images[0].get("url", "") if images else "",
        tracks=tracks,
        truncated=total > len(tracks),
    )


def fetch_album(album_id: str):
    from modules.spotify import AlbumMeta, TrackMeta

    head = _get(f"https://api.spotify.com/v1/albums/{album_id}")
    images = head.get("images") or []
    cover = images[0].get("url", "") if images else ""
    name = head.get("name") or "Album"

    tracks: list[TrackMeta] = []
    offset = 0
    total = 0
    while offset < MAX_TRACKS:
        page = _get(
            f"https://api.spotify.com/v1/albums/{album_id}/tracks",
            {"limit": 50, "offset": offset},
        )
        items = page.get("items") or []
        if not items:
            break
        total = page.get("total") or total
        for t in items:
            if not t.get("id"):
                continue
            tracks.append(
                TrackMeta(
                    id=t["id"],
                    name=t.get("name") or "Unknown",
                    artists=[a.get("name", "") for a in (t.get("artists") or [])] or ["Unknown"],
                    album=name,
                    duration_ms=int(t.get("duration_ms") or 0),
                    cover_url=cover,
                    spotify_url=f"https://open.spotify.com/track/{t['id']}",
                )
            )
        offset += len(items)
        if offset >= total > 0:
            break

    if not tracks:
        raise RuntimeError("Spotify API returned no album tracks")

    return AlbumMeta(
        id=album_id,
        name=name,
        artists=[a.get("name", "") for a in (head.get("artists") or [])] or ["Unknown"],
        cover_url=cover,
        tracks=tracks,
    )
