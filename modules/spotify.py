"""
Music module (Spotify metadata + multi-source search/download).

Metadata:
  - spotipy            -> tracks/albums/playlists/artists (falls back to
                          embed-page scraping when the API 403s).
  - iTunes Search API  -> free, keyless; used to replace raw YouTube titles
                          and thumbnails with the real song name, artist and
                          600x600 album art before downloading.

Search (free text), when Spotify API is unavailable:
  - YouTube  (ytsearch via yt-dlp)  -> ids prefixed "yt_"
  - SoundCloud (scsearch via yt-dlp) -> ids prefixed "sc_"

Download:
  - yt-dlp bestaudio -> 320kbps MP3, sharing the YouTube module's cookie +
    EJS-solver options. Album art + ID3 tags embedded via mutagen.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from config import settings
from utils.helpers import run_in_thread, safe_filename

log = logging.getLogger(__name__)


# ---------------- spotipy client ----------------

_sp: spotipy.Spotify | None = None


def _client() -> spotipy.Spotify:
    global _sp
    if _sp is None:
        auth = SpotifyClientCredentials(
            client_id=settings.spotify_client_id,
            client_secret=settings.spotify_client_secret,
        )
        _sp = spotipy.Spotify(auth_manager=auth, requests_timeout=15)
    return _sp


# ---------------- dataclasses ----------------

@dataclass
class TrackMeta:
    id: str
    name: str
    artists: list[str]
    album: str
    duration_ms: int
    cover_url: str
    spotify_url: str  # source URL (spotify page, youtube watch, soundcloud permalink)

    @property
    def display(self) -> str:
        return f"{', '.join(self.artists)} — {self.name}"

    @property
    def search_query(self) -> str:
        return f"{self.artists[0]} {self.name}"


@dataclass
class AlbumMeta:
    id: str
    name: str
    artists: list[str]
    cover_url: str
    tracks: list[TrackMeta] = field(default_factory=list)


@dataclass
class PlaylistMeta:
    id: str
    name: str
    owner: str
    tracks: list[TrackMeta] = field(default_factory=list)


# Cache for non-Spotify results ("yt_*" / "sc_*") so inline-button callbacks
# can resolve them later without re-searching.
_yt_cache: dict[str, TrackMeta] = {}


# ---------------- meta loaders ----------------

def _to_track(t: dict) -> TrackMeta:
    album = t.get("album") or {}
    images = album.get("images") or []
    return TrackMeta(
        id=t["id"],
        name=t["name"],
        artists=[a["name"] for a in t["artists"]],
        album=album.get("name", ""),
        duration_ms=t.get("duration_ms") or 0,
        cover_url=images[0]["url"] if images else "",
        spotify_url=t.get("external_urls", {}).get("spotify", ""),
    )


@run_in_thread
def get_track_meta(track_id: str) -> TrackMeta:
    if track_id.startswith(("yt_", "sc_")):
        cached = _yt_cache.get(track_id)
        if cached:
            return cached
        if track_id.startswith("yt_"):
            return _probe_source_track_sync(
                f"https://www.youtube.com/watch?v={track_id[3:]}", "yt"
            )
        # SoundCloud numeric ids cannot be turned back into URLs.
        raise RuntimeError("نتیجه جستجو منقضی شده؛ دوباره سرچ کن.")

    try:
        return _to_track(_client().track(track_id))
    except spotipy.exceptions.SpotifyException as e:
        # Spotify's late-2024 policy change blocks free-tier dev apps from
        # most endpoints with HTTP 403. Fall back to public embed scraper.
        log.warning("Spotify API rejected track lookup (%s) — using embed scraper.", e)
        from modules.spotify_scraper import fetch_track_meta
        return fetch_track_meta(track_id)


@run_in_thread
def get_album_tracks(album_id: str) -> AlbumMeta:
    sp = _client()
    album = sp.album(album_id)
    items = album["tracks"]["items"]
    # album.tracks doesn't include album field on each item — inject it
    for it in items:
        it["album"] = {"name": album["name"], "images": album["images"]}
    return AlbumMeta(
        id=album["id"],
        name=album["name"],
        artists=[a["name"] for a in album["artists"]],
        cover_url=album["images"][0]["url"] if album["images"] else "",
        tracks=[_to_track(t) for t in items],
    )


@run_in_thread
def get_playlist_tracks(playlist_id: str) -> PlaylistMeta:
    sp = _client()
    pl = sp.playlist(playlist_id)
    tracks: list[TrackMeta] = []
    results = pl["tracks"]
    while results:
        for item in results["items"]:
            t = item.get("track")
            if t and t.get("id"):
                tracks.append(_to_track(t))
        results = sp.next(results) if results.get("next") else None
    return PlaylistMeta(
        id=pl["id"],
        name=pl["name"],
        owner=pl["owner"]["display_name"],
        tracks=tracks,
    )


@run_in_thread
def get_artist_top_tracks(artist_id: str, market: str = "US") -> list[TrackMeta]:
    sp = _client()
    res = sp.artist_top_tracks(artist_id, country=market)
    return [_to_track(t) for t in res["tracks"][:10]]


@run_in_thread
def search_tracks(query: str, limit: int = 10) -> list[TrackMeta]:
    try:
        res = _client().search(q=query, type="track", limit=limit)
        return [_to_track(t) for t in res["tracks"]["items"]]
    except spotipy.exceptions.SpotifyException as e:
        log.warning("Spotify search rejected (%s) — using YouTube+SoundCloud.", e)
        return _fallback_search_tracks(query, limit)


@run_in_thread
def search_artist(name: str) -> str | None:
    """Return first artist id matching name, or None."""
    sp = _client()
    res = sp.search(q=name, type="artist", limit=1)
    items = res["artists"]["items"]
    return items[0]["id"] if items else None


# ---------------- yt-dlp based search / probing ----------------

def _flat_entries(search_url: str) -> list[dict]:
    from yt_dlp import YoutubeDL

    from modules.youtube import _base_opts

    opts = _base_opts() | {"extract_flat": True, "skip_download": True}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(search_url, download=False)
    return info.get("entries") or []


def _entry_to_track(e: dict, prefix: str) -> TrackMeta | None:
    vid = e.get("id")
    if not vid:
        return None
    url = e.get("url") or e.get("webpage_url") or ""
    if prefix == "yt" and not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={vid}"
    thumbs = e.get("thumbnails") or []
    meta = TrackMeta(
        id=f"{prefix}_{vid}",
        name=e.get("title") or "Unknown",
        artists=[e.get("channel") or e.get("uploader") or prefix],
        album="",
        duration_ms=int((e.get("duration") or 0) * 1000),
        cover_url=(thumbs[-1].get("url", "") if thumbs else e.get("thumbnail") or ""),
        spotify_url=url,
    )
    _yt_cache[meta.id] = meta
    return meta


def _fallback_search_tracks(query: str, limit: int) -> list[TrackMeta]:
    """Combined YouTube + SoundCloud search."""
    half = max(limit // 2, 4)
    tracks: list[TrackMeta] = []

    try:
        for e in _flat_entries(f"ytsearch{half}:{query}"):
            t = _entry_to_track(e, "yt")
            if t:
                tracks.append(t)
    except Exception as e:
        log.warning("YouTube search failed: %s", e)

    try:
        for e in _flat_entries(f"scsearch{half}:{query}"):
            t = _entry_to_track(e, "sc")
            if t:
                tracks.append(t)
    except Exception as e:
        log.warning("SoundCloud search failed: %s", e)

    return tracks[:limit]


def _probe_source_track_sync(url: str, prefix: str) -> TrackMeta:
    """Resolve full metadata for a single YouTube/SoundCloud URL."""
    from yt_dlp import YoutubeDL

    from modules.youtube import _base_opts

    with YoutubeDL(_base_opts() | {"skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    meta = TrackMeta(
        id=f"{prefix}_{info['id']}",
        name=info.get("title") or "Unknown",
        artists=[info.get("artist") or info.get("channel") or info.get("uploader") or prefix],
        album=info.get("album") or "",
        duration_ms=int((info.get("duration") or 0) * 1000),
        cover_url=info.get("thumbnail", ""),
        spotify_url=info.get("webpage_url") or url,
    )
    _yt_cache[meta.id] = meta
    return meta


@run_in_thread
def probe_source_track(url: str, prefix: str = "sc") -> TrackMeta:
    return _probe_source_track_sync(url, prefix)


@run_in_thread
def probe_soundcloud_set(url: str, limit: int = 20) -> list[TrackMeta]:
    """List the tracks of a SoundCloud playlist (set)."""
    tracks: list[TrackMeta] = []
    for e in _flat_entries(url)[:limit]:
        t = _entry_to_track(e, "sc")
        if t:
            tracks.append(t)
    return tracks


# ---------------- iTunes metadata enrichment ----------------

def _itunes_enrich(meta: TrackMeta) -> None:
    """
    For tracks sourced from YouTube/SoundCloud the 'metadata' is just the
    video title + channel name and the cover is a video thumbnail. Look the
    song up on the free iTunes Search API and, when the match is plausible,
    replace name/artist/album/cover with the real ones (600x600 album art).
    """
    import httpx

    try:
        r = httpx.get(
            "https://itunes.apple.com/search",
            params={"term": meta.name, "media": "music", "entity": "song", "limit": 3},
            timeout=10,
        )
        r.raise_for_status()
        results = r.json().get("results") or []
    except Exception as e:
        log.warning("iTunes lookup failed: %s", e)
        return

    haystack = meta.name.lower()
    for it in results:
        track_name = (it.get("trackName") or "").strip()
        artist_name = (it.get("artistName") or "").strip()
        if not track_name:
            continue
        # plausibility check: song title or artist must appear in the
        # original video title, otherwise we risk mislabeling the file.
        if track_name.lower() not in haystack and artist_name.lower() not in haystack:
            continue

        meta.name = track_name
        meta.artists = [artist_name] if artist_name else meta.artists
        meta.album = it.get("collectionName") or meta.album
        art = (it.get("artworkUrl100") or "").replace("100x100", "600x600")
        if art:
            meta.cover_url = art
        log.info("iTunes enriched: %s", meta.display)
        return


# ---------------- downloading ----------------

@run_in_thread
def download_track(meta: TrackMeta) -> Path:
    """
    Download best-quality audio (320kbps MP3) and embed album art + ID3
    tags. yt_/sc_ tracks download their exact source URL; Spotify tracks
    are located via a YouTube search.
    """
    from yt_dlp import YoutubeDL

    from modules.youtube import _base_opts

    # Fix metadata + cover before computing the filename.
    if meta.id.startswith(("yt_", "sc_")):
        _itunes_enrich(meta)

    out_dir = settings.download_dir / "spotify"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / f"{safe_filename(meta.display)}.mp3"
    if out_path.exists():
        return out_path

    if meta.id.startswith(("yt_", "sc_")) and meta.spotify_url.startswith("http"):
        target = meta.spotify_url
    elif meta.id.startswith("yt_"):
        target = f"https://www.youtube.com/watch?v={meta.id[3:]}"
    else:
        target = f"ytsearch1:{meta.search_query} audio"

    opts = _base_opts() | {
        "format": "bestaudio/best",
        "outtmpl": str(out_path.with_suffix(".%(ext)s")),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            },
            {"key": "FFmpegMetadata"},
        ],
    }
    with YoutubeDL(opts) as ydl:
        ydl.download([target])

    if not out_path.exists():
        raise RuntimeError(f"Download produced no file for: {meta.display}")

    _embed_cover_and_tags(out_path, meta)
    return out_path


def _embed_cover_and_tags(path: Path, meta: TrackMeta) -> None:
    """Embed album art (APIC) + title/artist/album ID3 tags via mutagen."""
    try:
        from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1

        try:
            tags = ID3(str(path))
        except Exception:
            tags = ID3()

        tags.delall("TIT2")
        tags.add(TIT2(encoding=3, text=meta.name))
        tags.delall("TPE1")
        tags.add(TPE1(encoding=3, text=", ".join(meta.artists)))
        if meta.album:
            tags.delall("TALB")
            tags.add(TALB(encoding=3, text=meta.album))

        if meta.cover_url:
            import httpx

            r = httpx.get(meta.cover_url, timeout=20, follow_redirects=True)
            r.raise_for_status()
            mime = r.headers.get("content-type", "image/jpeg").split(";")[0]
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=r.content))

        tags.save(str(path))
    except Exception as e:
        # Art is cosmetic — never fail the download over it.
        log.warning("Cover/tag embedding failed for %s: %s", meta.display, e)
