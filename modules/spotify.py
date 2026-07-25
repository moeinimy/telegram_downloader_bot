"""
Music module (metadata + multi-source search/download). No API keys required.

Metadata:
  - Spotify embed pages -> tracks/albums/playlists/artist top-10, no auth.
  - iTunes Search API   -> free, keyless; replaces raw YouTube titles and
                           thumbnails with the real song name, artist and
                           600x600 album art before downloading.

Search (free text):
  - iTunes  -> ids prefixed "it_"   } keyless JSON APIs, ~200ms, real song
  - Deezer  -> ids prefixed "dz_"   } titles/artists/album art
  - yt-dlp ytsearch/scsearch is the fallback only ("yt_" / "sc_"): it costs a
    full extraction pass per query and reports channel names as artists.
  Results from both APIs are interleaved, then ranked by query match, each
  API's own ordering, and Deezer's popularity so the original beats covers.

Download:
  - yt-dlp bestaudio -> 320kbps MP3 through the YouTube module's client-fallback
    ladder. Album art + ID3 tags embedded via mutagen.
"""

from __future__ import annotations

import glob
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
    # Search-ranking signals (unused outside _music_api_search).
    src_rank: int = 0       # position in the source's own result list
    popularity: float = 0.0  # 0..1, from Deezer's rank; 0 when unknown

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
    cover_url: str = ""
    tracks: list[TrackMeta] = field(default_factory=list)
    truncated: bool = False  # set when Spotify's embed capped the track list


# Cache for non-Spotify results ("yt_*" / "sc_*") so inline-button callbacks
# can resolve them later without re-searching.
_yt_cache: dict[str, TrackMeta] = {}


# ---------------- meta loaders (Spotify embed, keyless) ----------------

@run_in_thread
def get_track_meta(track_id: str) -> TrackMeta:
    if track_id.startswith(("yt_", "sc_", "it_", "dz_")):
        cached = _yt_cache.get(track_id)
        if cached:
            return cached
        if track_id.startswith(("it_", "dz_")):
            return _lookup_metadata_track(track_id)
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
    """
    Free-text search.

    Music metadata APIs (iTunes + Deezer) are queried first: they are plain
    keyless JSON endpoints that answer in ~200ms with the real song title,
    artist, album and cover art. yt-dlp's ytsearch/scsearch needs a full
    extraction pass per query (5-15s) and returns raw video titles with
    channel names as the "artist", so it is only the fallback now.
    """
    tracks = _music_api_search(query, limit)
    if tracks:
        return tracks
    log.info("music APIs returned nothing for %r - falling back to yt-dlp", query)
    return _fallback_search_tracks(query, limit)


# ---------------- fast keyless music metadata search ----------------

def _itunes_search(query: str, limit: int) -> list[TrackMeta]:
    from utils import http

    r = http.get(
        "https://itunes.apple.com/search",
        params={"term": query, "media": "music", "entity": "song", "limit": limit},
        timeout=6,
    )
    r.raise_for_status()
    out = []
    for idx, it in enumerate(r.json().get("results") or []):
        tid, name = it.get("trackId"), (it.get("trackName") or "").strip()
        if not tid or not name:
            continue
        out.append(
            TrackMeta(
                src_rank=idx,
                id=f"it_{tid}",
                name=name,
                artists=[(it.get("artistName") or "").strip() or "Unknown"],
                album=it.get("collectionName") or "",
                duration_ms=int(it.get("trackTimeMillis") or 0),
                cover_url=(it.get("artworkUrl100") or "").replace("100x100", "600x600"),
                spotify_url=it.get("trackViewUrl") or "",
                itunes_url=it.get("trackViewUrl") or "",
            )
        )
    return out


def _deezer_search(query: str, limit: int) -> list[TrackMeta]:
    from utils import http

    r = http.get(
        "https://api.deezer.com/search",
        params={"q": query, "limit": limit},
        timeout=6,
    )
    r.raise_for_status()
    out = []
    for idx, d in enumerate(r.json().get("data") or []):
        did, title = d.get("id"), (d.get("title") or "").strip()
        if not did or not title:
            continue
        album = d.get("album") or {}
        out.append(
            TrackMeta(
                src_rank=idx,
                # Deezer's rank is a play-count proxy (0..~1M); it is the one
                # signal that separates the real hit from a soundalike cover.
                popularity=min((d.get("rank") or 0) / 800_000, 1.0),
                id=f"dz_{did}",
                name=title,
                artists=[((d.get("artist") or {}).get("name") or "").strip() or "Unknown"],
                album=album.get("title") or "",
                duration_ms=int((d.get("duration") or 0) * 1000),
                cover_url=album.get("cover_xl") or album.get("cover_big") or "",
                spotify_url=d.get("link") or "",
            )
        )
    return out


def _dedupe_key(t: TrackMeta) -> str:
    return f"{_norm(t.artists[0] if t.artists else '')}|{_norm(t.name)}"


# Recent queries -> results. Re-typing the same search (or tapping through a
# list twice) should not pay for the network again.
_search_cache: dict[str, tuple[float, list[TrackMeta]]] = {}
_SEARCH_TTL = 600.0


def _music_api_search(query: str, limit: int) -> list[TrackMeta]:
    """iTunes + Deezer in parallel, interleaved by rank and de-duplicated."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    from itertools import zip_longest

    key = f"{query.strip().lower()}|{limit}"
    hit = _search_cache.get(key)
    if hit and time.monotonic() - hit[0] < _SEARCH_TTL:
        return hit[1]

    def _safe(fn, label):
        try:
            return fn(query, limit)
        except Exception as e:
            log.warning("%s search failed: %s", label, e)
            return []

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_it = pool.submit(_safe, _itunes_search, "iTunes")
        f_dz = pool.submit(_safe, _deezer_search, "Deezer")
        itunes, deezer = f_it.result(), f_dz.result()

    # Interleave so both catalogues are represented: concatenating let iTunes
    # fill every slot and Deezer-only tracks never showed up.
    candidates = [
        t
        for pair in zip_longest(itunes, deezer)
        for t in pair
        if t is not None
    ]

    # Then rank across both sources by how well each matches what was typed,
    # so "still here drake" puts Drake's track above another artist's song
    # that happens to share the title.
    def _relevance(t: TrackMeta) -> float:
        artist = t.artists[0] if t.artists else ""
        both = f"{artist} {t.name}"
        score = _overlap(query, both)
        if _norm(query) in _norm(both):
            score += 0.5
        # Each API already ranked its own results; keep that as a signal.
        score += 0.5 / (1 + t.src_rank)
        # Popularity breaks the tie between the original and the covers that
        # share its exact title.
        score += 0.6 * t.popularity
        return score

    candidates.sort(key=_relevance, reverse=True)

    seen: set[str] = set()
    out: list[TrackMeta] = []
    for t in candidates:
        k = _dedupe_key(t)
        if k in seen:
            continue
        seen.add(k)
        _yt_cache[t.id] = t
        out.append(t)
        if len(out) >= limit:
            break

    if out:
        _search_cache[key] = (time.monotonic(), out)
        if len(_search_cache) > 200:
            oldest = min(_search_cache, key=lambda k: _search_cache[k][0])
            _search_cache.pop(oldest, None)
    return out


def _lookup_metadata_track(track_id: str) -> TrackMeta:
    """Re-fetch an it_/dz_ track by id so inline buttons keep working after a
    restart, instead of answering 'search expired'."""
    from utils import http

    raw = track_id[3:]
    if track_id.startswith("it_"):
        r = http.get("https://itunes.apple.com/lookup", params={"id": raw})
        r.raise_for_status()
        results = r.json().get("results") or []
        if results:
            it = results[0]
            meta = TrackMeta(
                id=track_id,
                name=(it.get("trackName") or "Unknown").strip(),
                artists=[(it.get("artistName") or "Unknown").strip()],
                album=it.get("collectionName") or "",
                duration_ms=int(it.get("trackTimeMillis") or 0),
                cover_url=(it.get("artworkUrl100") or "").replace("100x100", "600x600"),
                spotify_url=it.get("trackViewUrl") or "",
                itunes_url=it.get("trackViewUrl") or "",
            )
            _yt_cache[track_id] = meta
            return meta
    else:
        r = http.get(f"https://api.deezer.com/track/{raw}")
        r.raise_for_status()
        d = r.json()
        if d.get("id"):
            album = d.get("album") or {}
            meta = TrackMeta(
                id=track_id,
                name=(d.get("title") or "Unknown").strip(),
                artists=[((d.get("artist") or {}).get("name") or "Unknown").strip()],
                album=album.get("title") or "",
                duration_ms=int((d.get("duration") or 0) * 1000),
                cover_url=album.get("cover_xl") or album.get("cover_big") or "",
                spotify_url=d.get("link") or "",
            )
            _yt_cache[track_id] = meta
            return meta

    raise RuntimeError("این آهنگ رو دیگه پیدا نکردم؛ دوباره سرچ کن.")


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


@run_in_thread
def deep_search(query: str, limit: int = 12) -> list[TrackMeta]:
    """
    Wide search across YouTube and SoundCloud.

    This is where remixes, bootlegs, slowed/reverb edits and SoundCloud-only
    uploads live - things no commercial catalogue lists. It costs a full
    yt-dlp extraction pass per source, so it runs on demand rather than on
    every search. Results are ranked by match against the query.
    """
    tracks = _fallback_search_tracks(query, limit)

    def _relevance(t: TrackMeta) -> float:
        artist = t.artists[0] if t.artists else ""
        both = f"{artist} {t.name}"
        score = _overlap(query, both)
        if _norm(query) in _norm(both):
            score += 0.5
        score += 0.3 / (1 + t.src_rank)
        return score

    tracks.sort(key=_relevance, reverse=True)
    return tracks


def _search_one(search_url: str, prefix: str) -> list[TrackMeta]:
    try:
        out = []
        for idx, e in enumerate(_flat_entries(search_url)):
            t = _entry_to_track(e, prefix)
            if t:
                t.src_rank = idx
                out.append(t)
        return out
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
    from utils import http

    cleaned = _strip_noise(meta.name)
    guess_artist, guess_title = _split_artist_title(cleaned)
    term = cleaned or meta.name
    if not term:
        return

    try:
        r = http.get(
            "https://itunes.apple.com/search",
            params={"term": term, "media": "music", "entity": "song", "limit": 8},
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


@run_in_thread
def fill_cover(meta: TrackMeta) -> None:
    """Async wrapper so handlers can fetch artwork without blocking the loop."""
    ensure_cover(meta)


def ensure_cover(meta: TrackMeta) -> None:
    """
    Fill in artwork the source could not supply.

    Spotify's embed track lists carry no per-track image, so playlist entries
    arrive with an empty cover. One keyless catalogue lookup by artist+title
    gets the real album art (and album name) - far cheaper than fetching each
    track's own embed page.
    """
    if meta.cover_url:
        return
    artist = meta.artists[0] if meta.artists else ""
    try:
        hits = _music_api_search(f"{artist} {meta.name}".strip(), 5)
    except Exception as e:
        log.warning("cover lookup failed for %s: %s", meta.display, e)
        return

    our = meta.duration_ms / 1000 if meta.duration_ms else 0
    for h in hits:
        if _overlap(h.name, meta.name) < 0.5:
            continue
        their = h.duration_ms / 1000 if h.duration_ms else 0
        if our and their and abs(our - their) > 25:
            continue
        if h.cover_url:
            meta.cover_url = h.cover_url
            meta.album = meta.album or h.album
            if not meta.itunes_url and h.itunes_url:
                meta.itunes_url = h.itunes_url
            return


def _deezer_artist_id(meta: TrackMeta) -> int | None:
    """
    Resolve the Deezer artist behind a track.

    Searching `artist:"Drake"` is unreliable - Deezer answered that with a
    French chanson singer. Resolving through the song itself (or the track id
    we already hold) lands on the right artist; the by-name lookup is a last
    resort and picks the most-followed match, since several no-name accounts
    share big artists' names.
    """
    from utils import http

    artist = meta.artists[0] if meta.artists else ""
    try:
        if meta.id.startswith("dz_"):
            d = http.get(f"https://api.deezer.com/track/{meta.id[3:]}").json()
            aid = (d.get("artist") or {}).get("id")
            if aid:
                return int(aid)

        hit = http.get(
            "https://api.deezer.com/search",
            params={"q": f"{artist} {meta.name}".strip(), "limit": 1},
        ).json()
        data = hit.get("data") or []
        if data:
            aid = (data[0].get("artist") or {}).get("id")
            if aid:
                return int(aid)

        cand = http.get(
            "https://api.deezer.com/search/artist", params={"q": artist, "limit": 5}
        ).json()
        entries = [
            a for a in (cand.get("data") or []) if _norm(a.get("name", "")) == _norm(artist)
        ] or (cand.get("data") or [])
        if entries:
            return int(max(entries, key=lambda a: a.get("nb_fan") or 0)["id"])
    except Exception as e:
        log.warning("deezer artist lookup failed for %s: %s", meta.display, e)
    return None


@run_in_thread
def similar_tracks(meta: TrackMeta, limit: int = 8) -> list[TrackMeta]:
    """
    Recommendations built from Deezer's keyless graph: more of the same
    artist's top tracks, then one hit each from related artists. Deezer has no
    per-track "related" endpoint, so the artist is the pivot.
    """
    from utils import http

    artist = meta.artists[0] if meta.artists else ""
    if not artist:
        return []

    try:
        artist_id = _deezer_artist_id(meta)
        if not artist_id:
            return []

        out: list[TrackMeta] = []
        seen = {_norm(meta.name)}

        def add(entries, cap):
            added = 0
            for d in entries:
                if added >= cap or len(out) >= limit:
                    break
                title = (d.get("title") or "").strip()
                if not title or _norm(title) in seen:
                    continue
                seen.add(_norm(title))
                album = d.get("album") or {}
                out.append(
                    TrackMeta(
                        id=f"dz_{d['id']}",
                        name=title,
                        artists=[((d.get("artist") or {}).get("name") or "").strip()],
                        album=album.get("title") or "",
                        duration_ms=int((d.get("duration") or 0) * 1000),
                        cover_url=album.get("cover_xl") or album.get("cover_big") or "",
                        spotify_url=d.get("link") or "",
                    )
                )
                added += 1

        top = http.get(
            f"https://api.deezer.com/artist/{artist_id}/top", params={"limit": 10}
        ).json()
        add(top.get("data") or [], cap=max(limit // 2, 3))

        related = http.get(f"https://api.deezer.com/artist/{artist_id}/related").json()
        for rel in (related.get("data") or [])[:6]:
            if len(out) >= limit:
                break
            rid = rel.get("id")
            if not rid:
                continue
            rtop = http.get(
                f"https://api.deezer.com/artist/{rid}/top", params={"limit": 2}
            ).json()
            add(rtop.get("data") or [], cap=1)

        for t in out:
            _yt_cache[t.id] = t
        return out
    except Exception as e:
        log.warning("similar-track lookup failed for %s: %s", meta.display, e)
        return []


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
    elif "deezer.com" in url:
        links["deezer"] = url
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
    ensure_cover(meta)

    out_dir = settings.download_dir / "spotify"
    out_dir.mkdir(parents=True, exist_ok=True)

    base = out_dir / safe_filename(meta.display)
    existing = _find_output(base)
    if existing is not None:
        return existing

    if meta.id.startswith(("yt_", "sc_")) and meta.spotify_url.startswith("http"):
        target = meta.spotify_url
    elif meta.id.startswith("yt_"):
        target = f"https://www.youtube.com/watch?v={meta.id[3:]}"
    else:
        # Spotify / iTunes / Deezer entries carry metadata only - the audio
        # itself still has to be located on YouTube.
        target = f"ytsearch1:{meta.search_query} audio"

    extra = {
        # Highest-bitrate audio available. When it is already AAC, ExtractAudio
        # remuxes with `-acodec copy` (instant, lossless); otherwise it encodes
        # at 320k. Forcing mp3 for everything used to burn seconds of CPU per
        # track and added a second lossy pass on an already-compressed source.
        "format": "bestaudio/best",
        "format_sort": ["abr"],
        "outtmpl": str(base) + ".%(ext)s",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": settings.audio_format,
                "preferredquality": "320",
            },
            {"key": "FFmpegMetadata"},
        ],
    }

    try:
        ytdlp_run(extra, lambda ydl: ydl.download([target]))
    except Exception as e:
        # A bare "artist title audio" query finds nothing for obscure tracks;
        # the plain title often does.
        if not target.startswith("ytsearch"):
            raise
        log.info("search download failed (%s) - retrying without 'audio'", e)
        ytdlp_run(extra, lambda ydl: ydl.download([f"ytsearch1:{meta.search_query}"]))

    out_path = _find_output(base)
    if out_path is None:
        raise RuntimeError(
            f"فایل صوتی برای «{meta.display}» پیدا نشد (رو یوتیوب نبود)."
        )

    _embed_cover_and_tags(out_path, meta)
    return out_path


_AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".ogg", ".oga", ".webm", ".aac", ".mp4", ".flac", ".wav"}


def _find_output(base: Path) -> Path | None:
    """Locate whatever yt-dlp actually wrote.

    Guessing a fixed extension list was fragile: the postprocessor's output
    extension depends on the source codec, and a miss surfaced to the user as
    'the download produced no file' even though the audio was on disk.
    """
    candidates = [
        p
        for p in base.parent.glob(f"{glob.escape(base.name)}.*")
        if p.is_file() and p.suffix.lower() in _AUDIO_EXTS
    ]
    if not candidates:
        return None
    # Prefer a finished audio file over the raw container yt-dlp downloaded first.
    for ext in (".m4a", ".mp3", ".opus", ".flac"):
        for p in candidates:
            if p.suffix.lower() == ext:
                return p
    return max(candidates, key=lambda p: p.stat().st_size)


def _fetch_cover(url: str) -> tuple[bytes, str] | None:
    from utils import http

    return http.get_bytes(url)


def _embed_cover_and_tags(path: Path, meta: TrackMeta) -> None:
    """Embed album art + title/artist/album tags. MP3 uses ID3 frames, MP4/M4A
    uses iTunes-style atoms - writing ID3 into an .m4a silently does nothing."""
    try:
        cover = _fetch_cover(meta.cover_url)

        if path.suffix.lower() == ".flac":
            from mutagen.flac import FLAC, Picture

            audio = FLAC(str(path))
            audio["title"] = meta.name
            audio["artist"] = ", ".join(meta.artists)
            if meta.album:
                audio["album"] = meta.album
            if cover:
                data, mime = cover
                pic = Picture()
                pic.type, pic.mime, pic.data = 3, mime, data
                audio.clear_pictures()
                audio.add_picture(pic)
            audio.save()
            return

        if path.suffix.lower() in (".m4a", ".mp4", ".aac"):
            from mutagen.mp4 import MP4, MP4Cover

            audio = MP4(str(path))
            audio["\xa9nam"] = [meta.name]
            audio["\xa9ART"] = [", ".join(meta.artists)]
            if meta.album:
                audio["\xa9alb"] = [meta.album]
            if cover:
                data, mime = cover
                fmt = MP4Cover.FORMAT_PNG if "png" in mime else MP4Cover.FORMAT_JPEG
                audio["covr"] = [MP4Cover(data, imageformat=fmt)]
            audio.save()
            return

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
        if cover:
            data, mime = cover
            tags.delall("APIC")
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))

        tags.save(str(path))
    except Exception as e:
        # Art is cosmetic — never fail the download over it.
        log.warning("Cover/tag embedding failed for %s: %s", meta.display, e)
