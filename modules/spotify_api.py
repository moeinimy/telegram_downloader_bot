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


def _get(url: str, params: dict | None = None) -> dict:
    from utils import http

    r = http.get(url, params=params, headers={"Authorization": f"Bearer {_access_token()}"})
    if r.status_code == 401:
        # Token rejected early - drop it and try once more.
        global _token_expires
        _token_expires = 0
        r = http.get(
            url, params=params, headers={"Authorization": f"Bearer {_access_token()}"}
        )
    r.raise_for_status()
    return r.json()


def fetch_playlist(playlist_id: str):
    """Full playlist with every page of tracks."""
    from modules.spotify import PlaylistMeta, TrackMeta

    head = _get(
        f"https://api.spotify.com/v1/playlists/{playlist_id}",
        {"fields": "name,owner(display_name),images,tracks(total)"},
    )
    images = head.get("images") or []
    total = ((head.get("tracks") or {}).get("total")) or 0

    tracks: list[TrackMeta] = []
    offset = 0
    while offset < min(total, MAX_TRACKS):
        page = _get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks",
            {
                "limit": 100,
                "offset": offset,
                "fields": "items(track(id,name,duration_ms,artists(name),"
                          "album(name,images)))",
            },
        )
        items = page.get("items") or []
        if not items:
            break
        for it in items:
            t = it.get("track") or {}
            if not t.get("id"):
                continue  # local files and removed tracks
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

    log.info("Spotify API: playlist %s -> %d/%d tracks", playlist_id, len(tracks), total)
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
    while True:
        page = _get(
            f"https://api.spotify.com/v1/albums/{album_id}/tracks",
            {"limit": 50, "offset": offset},
        )
        items = page.get("items") or []
        if not items:
            break
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
        if offset >= (page.get("total") or 0):
            break

    return AlbumMeta(
        id=album_id,
        name=name,
        artists=[a.get("name", "") for a in (head.get("artists") or [])] or ["Unknown"],
        cover_url=cover,
        tracks=tracks,
    )
