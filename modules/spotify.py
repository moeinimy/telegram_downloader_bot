"""
Music module (metadata + multi-source search/download). No API keys required.

Metadata:
  - Spotify embed pages -> tracks/albums/playlists/artist top-10, no auth.
  - iTunes Search API   -> free, keyless; replaces raw YouTube titles and
                           thumbnails with the real song name, artist and
                           600x600 album art before downloading.

Search (free text):
  - YouTube    (ytsearch via yt-dlp)  -> ids prefixed "yt_"
  - SoundCloud (scsearch via yt-dlp)  -> ids prefixed "sc_"

Download:
  - yt-dlp bestaudio -> 320kbps MP3 through the YouTube module's client-fallback
    ladder. Album art + ID3 tags embedded via mutagen.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from config import settings
from utils.helpers import run_in_thread, safe_filename

log = logging.getLogger(__name__)


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
    itunes_url: str = ""  # filled by _itunes_enrich when a match is confident

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


# ---------------- meta loaders (Spotify embed, keyless) ----------------

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

    from modules.spotify_scraper import fetch_track_meta
    return fetch_track_meta(track_id)


@run_in_thread
def get_album_tracks(album_id: str) -> AlbumMeta:
    from modules.spotify_scraper import fetch_album
    return fetch_album(album_id)


@run_in_thread
def get_playlist_tracks(playlist_id: str) -> PlaylistMeta:
    from modules.spotify_scraper import fetch_playlist
    return fetch_playlist(playlist_id)


@run_in_thread
def get_artist_top_tracks(artist_id: str, market: str = "US") -> list[TrackMeta]:
    from modules.spotify_scraper import fetch_artist_top
    return fetch_artist_top(artist_id)


@run_in_thread
def search_tracks(query: str, limit: int = 10) -> list[TrackMeta]:
    """Spotify has no keyless search endpoint, so free text is resolved
    against YouTube + SoundCloud."""
    return _fallback_search_tracks(query, limit)


# ---------------- yt-dlp based search / probing ----------------

def _flat_entries(search_url: str) -> list[dict]:
    from modules.youtube import ytdlp_run

    info = ytdlp_run(
        {"extract_flat": True, "skip_download": True},
        lambda ydl: ydl.extract_info(search_url, download=False),
    )
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


def _search_one(search_url: str, prefix: str) -> list[TrackMeta]:
    try:
        return [t for t in (_entry_to_track(e, prefix) for e in _flat_entries(search_url)) if t]
    except Exception as e:
        log.warning("%s search failed: %s", prefix, e)
        return []


def _fallback_search_tracks(query: str, limit: int) -> list[TrackMeta]:
    """Combined YouTube + SoundCloud search, run concurrently - sequentially
    this waited out two full yt-dlp round trips before showing anything."""
    from concurrent.futures import ThreadPoolExecutor

    half = max(limit // 2, 4)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_yt = pool.submit(_search_one, f"ytsearch{half}:{query}", "yt")
        f_sc = pool.submit(_search_one, f"scsearch{half}:{query}", "sc")
        tracks = f_yt.result() + f_sc.result()

    return tracks[:limit]


def _probe_source_track_sync(url: str, prefix: str) -> TrackMeta:
    """Resolve full metadata for a single YouTube/SoundCloud URL."""
    from modules.youtube import ytdlp_run

    info = ytdlp_run(
        {"skip_download": True},
        lambda ydl: ydl.extract_info(url, download=False),
    )
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

_NOISE = (
    "official music video", "official video", "official audio", "official lyric video",
    "lyric video", "lyrics", "official", "audio only", "audio", "video", "hd", "hq",
    "4k", "8k", "mv", "m/v", "visualizer", "explicit", "clean", "remaster",
    "remastered", "music video", "full song", "with lyrics", "free download",
)


def _strip_noise(raw: str) -> str:
    """Turn 'Artist - Song (Official Music Video) [4K] | Label' into 'Artist - Song'."""
    s = raw

    def _drop(m: re.Match) -> str:
        inner = m.group(0).lower()
        return "" if any(w in inner for w in _NOISE) else m.group(0)

    s = re.sub(r"[\(\[][^()\[\]]*[\)\]]", _drop, s)
    s = re.sub(r"\s*\|.*$", "", s)              # trailing "| Channel"
    s = re.sub(r"\s*\bft\.?\b.*$", "", s, flags=re.I)   # "ft. Someone"
    s = re.sub(r"\s*\bfeat\.?\b.*$", "", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" -–—_·")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _overlap(a: str, b: str) -> float:
    """Jaccard similarity over word tokens; 1.0 means identical wording."""
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _split_artist_title(s: str) -> tuple[str, str]:
    for sep in (" - ", " – ", " — ", " _ "):
        if sep in s:
            left, right = s.split(sep, 1)
            return left.strip(), right.strip()
    return "", s.strip()


def _itunes_enrich(meta: TrackMeta) -> None:
    """
    For YouTube/SoundCloud sources the 'metadata' is just a video title plus a
    channel name, and the cover is a video thumbnail. Look the song up on the
    keyless iTunes Search API and adopt the real name/artist/album/600x600 art.

    Matching is deliberately strict. Accepting a result because the *artist*
    matched (the previous behaviour) mislabels files: searching
    "Drake - Something New" returns Drake's other songs, and the first one
    would win - correct audio, wrong title and wrong cover. The song title
    must match; duration or artist then confirms it.
    """
    import httpx

    cleaned = _strip_noise(meta.name)
    guess_artist, guess_title = _split_artist_title(cleaned)
    term = cleaned or meta.name
    if not term:
        return

    try:
        r = httpx.get(
            "https://itunes.apple.com/search",
            params={"term": term, "media": "music", "entity": "song", "limit": 8},
            timeout=8,
        )
        r.raise_for_status()
        results = r.json().get("results") or []
    except Exception as e:
        log.warning("iTunes lookup failed: %s", e)
        return

    our_secs = meta.duration_ms / 1000 if meta.duration_ms else 0
    title_hay = guess_title or cleaned
    artist_hay = guess_artist or (meta.artists[0] if meta.artists else "")

    best: tuple[float, dict] | None = None
    for it in results:
        track_name = (it.get("trackName") or "").strip()
        artist_name = (it.get("artistName") or "").strip()
        if not track_name:
            continue

        n_track, n_hay = _norm(track_name), _norm(title_hay)
        title_ok = (
            n_track in n_hay
            or n_hay in n_track
            or _overlap(track_name, title_hay) >= 0.55
        )
        if not title_ok:
            continue  # never rename on an artist match alone

        their_secs = (it.get("trackTimeMillis") or 0) / 1000
        if our_secs and their_secs:
            delta = abs(our_secs - their_secs)
            if delta > 25:
                continue  # different cut: live, remix, extended, or wrong song
            duration_score = 1.0 - min(delta, 25) / 25
        else:
            duration_score = 0.0

        artist_score = max(
            _overlap(artist_name, artist_hay),
            1.0 if _norm(artist_name) and _norm(artist_name) in _norm(meta.name) else 0.0,
        )

        # Require corroboration beyond the title alone.
        if duration_score == 0.0 and artist_score < 0.3:
            continue

        score = _overlap(track_name, title_hay) + duration_score + artist_score
        if best is None or score > best[0]:
            best = (score, it)

    if best is None:
        log.info("iTunes: no confident match for %r - keeping source metadata", meta.name)
        return

    it = best[1]
    meta.name = (it.get("trackName") or meta.name).strip()
    if it.get("artistName"):
        meta.artists = [it["artistName"].strip()]
    meta.album = it.get("collectionName") or meta.album
    art = (it.get("artworkUrl100") or "").replace("100x100", "600x600")
    if art:
        meta.cover_url = art
    meta.itunes_url = it.get("trackViewUrl") or ""
    log.info("iTunes enriched (score %.2f): %s", best[0], meta.display)


def platform_links(meta: TrackMeta) -> dict[str, str]:
    """Direct links we actually know for this track; the keyboard falls back
    to per-platform search URLs for the rest."""
    links: dict[str, str] = {}
    url = meta.spotify_url or ""
    if "open.spotify.com" in url:
        links["spotify"] = url
    elif "youtube.com" in url or "youtu.be" in url:
        links["youtube"] = url
    elif "soundcloud.com" in url:
        links["soundcloud"] = url
    if getattr(meta, "itunes_url", ""):
        links["apple"] = meta.itunes_url
    return links


# ---------------- downloading ----------------

@run_in_thread
def download_track(meta: TrackMeta) -> Path:
    """
    Download best-quality audio (320kbps MP3) and embed album art + ID3
    tags. yt_/sc_ tracks download their exact source URL; Spotify tracks
    are located via a YouTube search.
    """
    from modules.youtube import ytdlp_run

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

    extra = {
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
    ytdlp_run(extra, lambda ydl: ydl.download([target]))

    if not out_path.exists():
        raise RuntimeError(f"دانلود فایلی تولید نکرد: {meta.display}")

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
