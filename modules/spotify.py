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
from utils.limits import BoundedDict

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
    # Search-ranking signals (unused outside search).
    src_rank: int = 0       # position in the source's own result list
    popularity: float = 0.0  # 0..1, from Deezer's rank; 0 when unknown
    # The text this result should be *matched* against, which is not the text
    # it is displayed as. A YouTube title lists the featured artists ("... ft.
    # Arown, Sami Low & Raha") and tidying it for display throws them away -
    # but they are exactly what somebody searching for a featured artist
    # typed, so ranking has to keep reading the original.
    match_text: str = ""
    credits_done: bool = False  # full artist list already resolved
    isrc: str = ""  # the recording's global id, when any source will tell us

    @property
    def display(self) -> str:
        return f"{', '.join(self.artists)} — {self.name}"

    @property
    def match_hay(self) -> str:
        return self.match_text or f"{', '.join(self.artists)} {self.name} {self.album}"

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
# Bounded: this used to keep every track from every search for the life of
# the process. Buttons older than the window re-resolve by id instead.
_yt_cache = BoundedDict(3000)


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
    from modules import spotify_api
    from modules.spotify_scraper import fetch_album

    if spotify_api.available():
        try:
            return spotify_api.fetch_album(album_id)
        except Exception as e:
            log.warning("Spotify API album failed (%s) - using embed page", e)
    return fetch_album(album_id)


@run_in_thread
def get_playlist_tracks(playlist_id: str) -> PlaylistMeta:
    """The embed page caps out around 100 tracks, so use the Web API when
    credentials are configured - that is the only way to reach a playlist
    with thousands of entries."""
    from modules import spotify_api
    from modules.spotify_scraper import fetch_playlist

    if spotify_api.available():
        try:
            return spotify_api.fetch_playlist(playlist_id)
        except Exception as e:
            log.warning("Spotify API playlist failed (%s) - using embed page", e)
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
    channel names as the "artist", so it is not paid for unless it is needed.

    "Needed" used to mean the catalogues returned literally nothing, which
    almost never happens - asked for a remix, iTunes answers with the original
    and the wide search never ran. So the catalogue's *best match* is judged
    against what was typed instead, and a weak one (or an outright request for
    a remix, bootleg, live cut...) opens the search up to YouTube and
    SoundCloud, where that material actually lives.
    """
    import time

    tracks = _music_api_search(query, limit)
    if not _needs_wide_search(query, tracks):
        return _focus(query, tracks)

    # The wide search costs a yt-dlp pass, so remember its outcome: paging back
    # to a result list, or retyping the same thing, must not buy it twice.
    wide_key = f"wide|{query.strip().lower()}|{limit}"
    hit = _search_cache.get(wide_key)
    if hit and time.monotonic() - hit[0] < _SEARCH_TTL:
        return hit[1]

    log.info("catalogue match for %r is weak - widening to yt-dlp", query)
    wide = _fallback_search_tracks(query, limit)
    if not wide:
        return tracks
    # These arrive as raw video titles under a channel name. Tidying them here
    # makes the result list readable, lets them de-duplicate against the
    # catalogue entries, and is what the lyrics lookup later reads.
    for t in wide:
        _clean_source_title(t)
    merged = _merge_results(query, tracks, wide, limit)
    if merged:
        _search_cache[wide_key] = (time.monotonic(), merged)
    return merged


# Material no commercial catalogue carries. Asking for any of these means the
# answer is on YouTube or SoundCloud, whatever iTunes chooses to reply with.
_WIDE_MARKERS = (
    "remix", "bootleg", "mashup", "mash up", "cover", "live", "acoustic",
    "slowed", "reverb", "sped up", "speed up", "nightcore", "8d", "instrumental",
    "unreleased", "leak", "demo", "snippet", "edit", "version", "vip", "flip",
    "ریمیکس", "لایو", "اجرای زنده", "نسخه", "بی کلام", "دمو", "کاور",
)

# Below this, the best catalogue hit is not really an answer to the question.
_WEAK_MATCH = 0.55


def _needs_wide_search(query: str, tracks: list[TrackMeta]) -> bool:
    if not tracks:
        return True
    q = _norm(query)
    if any(_norm(m) in q for m in _WIDE_MARKERS):
        return True
    best = max(
        _overlap(query, f"{t.artists[0] if t.artists else ''} {t.name}") for t in tracks
    )
    return best < _WEAK_MATCH


def _merge_results(
    query: str, catalogue: list[TrackMeta], wide: list[TrackMeta], limit: int
) -> list[TrackMeta]:
    """Rank both pools against the query together, catalogue nudged ahead on ties."""
    n_query = _norm(query)

    def rank(t: TrackMeta) -> float:
        hay = t.match_hay
        # Coverage leads: a result that leaves part of the query unexplained is
        # answering a different question, however tidy its title.
        score = 2.5 * _coverage(query, hay) + 0.6 * _overlap(query, hay)
        if n_query and n_query in _norm(hay):
            score += 0.6
        score += 0.3 / (1 + t.src_rank)
        score += 0.4 * t.popularity
        if _marks(hay, query, _NON_MUSIC):
            score -= 1.5
        # Somebody who did not type "remix" wants the original; the remix is
        # still listed, just below it.
        if _marks(hay, query, _VARIANT_WORDS):
            score -= 1.0
        # A catalogue entry brings a real artist, album and 600x600 art, so it
        # wins an otherwise equal race against a raw video title.
        if t.id.startswith(("it_", "dz_")):
            score += 0.15
        return score

    seen: set[str] = set()
    out: list[TrackMeta] = []
    for t in sorted(catalogue + wide, key=rank, reverse=True):
        key = _dedupe_key(t)
        if key in seen:
            continue
        seen.add(key)
        _yt_cache[t.id] = t
        out.append(t)
        if len(out) >= limit:
            break
    return _focus(query, out)


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
_search_cache = BoundedDict(200)
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
        both = t.match_hay
        score = 2.0 * _coverage(query, both) + 0.5 * _overlap(query, both)
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
        kind="search",
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
    title = e.get("title") or "Unknown"
    channel = e.get("channel") or e.get("uploader") or prefix
    meta = TrackMeta(
        id=f"{prefix}_{vid}",
        name=title,
        artists=[channel],
        album="",
        duration_ms=int((e.get("duration") or 0) * 1000),
        cover_url=(thumbs[-1].get("url", "") if thumbs else e.get("thumbnail") or ""),
        spotify_url=url,
        match_text=f"{title} {channel}",
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
        hay = t.match_hay
        score = 2.5 * _coverage(query, hay) + 0.6 * _overlap(query, hay)
        if _norm(query) in _norm(hay):
            score += 0.5
        score += 0.3 / (1 + t.src_rank)
        if _marks(hay, query, _NON_MUSIC):
            score -= 1.5
        if _marks(hay, query, _VARIANT_WORDS):
            score -= 1.0
        return score

    tracks.sort(key=_relevance, reverse=True)
    tracks = _focus(query, tracks[:limit])
    for t in tracks:
        _clean_source_title(t)
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
    this waited out two full yt-dlp round trips before showing anything.

    Both sources are asked for a full list rather than half each: a flat search
    costs one extraction pass whatever it returns, and splitting the budget
    used to drop SoundCloud results on the floor before anything ranked them.
    Callers rank the pool and take what they need."""
    from concurrent.futures import ThreadPoolExecutor

    per_source = max(limit, 8)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_yt = pool.submit(_search_one, f"ytsearch{per_source}:{query}", "yt")
        f_sc = pool.submit(_search_one, f"scsearch{per_source}:{query}", "sc")
        return f_yt.result() + f_sc.result()


def _probe_source_track_sync(url: str, prefix: str) -> TrackMeta:
    """Resolve full metadata for a single YouTube/SoundCloud URL."""
    from modules.youtube import ytdlp_run

    info = ytdlp_run(
        {"skip_download": True},
        lambda ydl: ydl.extract_info(url, download=False),
        kind="probe",
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
    # "Song (ft. X & Y)" loses its tail to the rule above and keeps the opening
    # bracket, which then shows up in the track list as "Friend Zone (".
    s = re.sub(r"\s*[\(\[]\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip(" -–—_·([")


# Arabic and Persian write the same sound several ways, and the two scripts
# disagree on a handful of letters. Folding them means "کرکس" typed one way
# still matches "كركس" spelt the other.
_FOLD = str.maketrans({
    "ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه", "أ": "ا", "إ": "ا", "آ": "ا",
    "ؤ": "و", "ئ": "ی",
    "‌": " ", "‎": " ", "‏": " ",  # ZWNJ + bidi marks
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
})


def _norm(s: str) -> str:
    """
    Fold a title down to comparable tokens.

    This kept only [a-z0-9], which quietly erased Persian, Arabic and every
    other non-Latin script: two completely different Persian songs both
    normalised to the empty string, so they compared as *equal*. That made the
    de-duplication in search throw away all but the first Persian result, and
    every title comparison against a Persian name score zero.
    """
    s = (s or "").lower().translate(_FOLD)
    s = re.sub(r"[ً-ْٰ]+", "", s)  # harakat: written optionally
    # An apostrophe closes a word, it does not break it. Falling through to the
    # separator rule below split "God's" into "god"+"s", so a search for
    # "drake gods plan" did not contain the token "gods" that Drake's own track
    # is named after: it scored 0.667 coverage and _focus discarded it as a
    # partial answer - while the covers, which spell it "Gods" and name Drake
    # in their album ("Orchestral String Versions of Drake"), scored 1.0 and
    # were all that came back. Nobody types the apostrophe, and every catalogue
    # prints it.
    s = re.sub("['’‘ʼ`´]+", "", s)
    return re.sub(r"[^a-z0-9ء-ۿ]+", " ", s).strip()


def _overlap(a: str, b: str) -> float:
    """Jaccard similarity over word tokens; 1.0 means identical wording."""
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _coverage(query: str, text: str) -> float:
    """
    Share of what was *typed* that the candidate accounts for.

    Jaccard is the wrong tool for ranking a search: it punishes a candidate for
    carrying words the query did not, which is precisely what a YouTube title
    does when it credits the featured artists. Searching "friend zone raha"
    scored six unrelated songs called "Friend Zone" above the one actually
    featuring Raha, because their short titles wasted fewer tokens. What
    matters is the opposite question - is any part of what I asked for
    unaccounted for?
    """
    q = set(_norm(query).split())
    if not q:
        return 0.0
    normalised = _norm(text)
    tokens = set(normalised.split())
    # People type an artist's name the way they say it, not the way it is
    # spelt: "samilow" is "Sami Low". Long tokens are also looked for in the
    # space-stripped text, with a length floor so short words cannot land
    # inside an unrelated one by accident.
    glued = normalised.replace(" ", "")
    hits = sum(1 for w in q if w in tokens or (len(w) >= 5 and w in glued))
    return hits / len(q)


def _focus(query: str, ranked: list[TrackMeta], keep_min: int = 3) -> list[TrackMeta]:
    """
    Drop partial answers once a complete one exists.

    Searching "tiem tuning" returned Tiem's track and then six unrelated songs
    with "Tuning" in the name - none of them by Tiem, all of them noise. If
    something accounts for the whole query, results that ignore half of it are
    not near-misses worth showing. A few are kept regardless, so an unlucky
    exact match can never be the only thing on offer.
    """
    if not ranked:
        return ranked
    covers = [(t, _coverage(query, t.match_hay)) for t in ranked]
    if max(c for _, c in covers) < 0.999:
        return ranked
    full = [t for t, c in covers if c >= 0.999]
    rest = [t for t, c in covers if c < 0.999]
    return full + rest[: max(0, keep_min - len(full))]


# ---------------- identity: is this the same recording? ----------------
#
# One decision, one place. Every part of the bot that asks "is this catalogue
# entry / this YouTube upload the track in front of me?" comes through here,
# because getting it wrong in *any* of them produces the same symptom: a file
# whose cover and title describe a song other than the one playing.
#
# The rule that caused that: corroboration used to be "title AND (artist OR
# duration)". Duration alone was accepted - so "Arman Miladi - Friend Zone"
# (3:35) matched "Adekunle Gold - Friend Zone" (3:35) and the bot replaced the
# artist, the album and the cover with a stranger's song. Two unrelated tracks
# sharing a title and a runtime is not a coincidence worth betting on; 3:35 is
# the most ordinary length a song has. The artist must agree too.


def _agree(ours: str, theirs: str, floor: float) -> float | None:
    """How far two names agree, or None when they plainly do not."""
    a, b = _norm(ours), _norm(theirs)
    if not a or not b:
        return None
    # Whole-token containment, so "Ama" does not match inside "Amazing" and a
    # short label name cannot swallow a long credit list.
    if f" {a} " in f" {b} " or f" {b} " in f" {a} ":
        return 1.0
    score = _overlap(ours, theirs)
    return score if score >= floor else None


def _claims_no_other_artist(their_title: str, our_artist: str) -> bool:
    """
    True when the candidate credits nobody who contradicts us.

    An upload titled "Artist - Song" is asserting who made it, and if that is
    somebody else the track is somebody else's - this is the guard that keeps
    "Adekunle Gold - Friend Zone" away from Arman Miladi's song of the same
    name and length. An upload titled plainly "Song" asserts nothing, and a
    lot of obscure music is uploaded exactly that way.
    """
    claimed, rest = _split_artist_title(_strip_noise(their_title))
    if not claimed or not rest:
        return True
    return _agree(our_artist, claimed, 0.3) is not None


def _same_recording(
    *,
    our_artist: str,
    our_title: str,
    our_secs: float,
    their_artist: str,
    their_title: str,
    their_secs: float,
    artist_floor: float = 0.4,
    unnamed_artist_ok: bool = False,
) -> float | None:
    """Confidence that both sides describe one recording; None means they don't.

    `our_artist` may be a whole haystack - a video title plus its channel, say -
    since the artist is often only named inside the title.
    """
    if _is_other_recording(their_title, f"{our_artist} {our_title}"):
        return None

    title = _agree(our_title, their_title, 0.55)
    if title is None:
        return None

    artist = _agree(our_artist, their_artist, artist_floor)
    if artist is None:
        if not (unnamed_artist_ok and _claims_no_other_artist(their_title, our_artist)):
            return None
        # Nobody is contradicted and nobody is confirmed, so the title and the
        # runtime carry the identification alone and both are held to a tighter
        # bound than usual. The title must be *equal*, not merely contained:
        # the usual containment rule would read an upload called "Friend" as
        # "Friend Zone". The runtime then contributes nothing to the score.
        _, their_song = _split_artist_title(_strip_noise(their_title))
        if _norm(their_song) != _norm(our_title):
            return None
        if not (
            our_secs
            and their_secs
            and abs(our_secs - their_secs) <= _DURATION_UNNAMED
        ):
            return None
        artist = 0.0

    if our_secs and their_secs:
        delta = abs(our_secs - their_secs)
        if delta > _DURATION_REJECT:
            # One gap is explainable: an official music video of the same song
            # runs longer than the audio release - intro, dialogue, outro.
            # "UGLY GEMINI - SHY BOY" is 2:28 on Spotify and 3:05 as a video,
            # and refusing it left the track undownloadable. So a *longer*
            # candidate is allowed when nothing else is in any doubt; a shorter
            # one is not, because a wrong song is as likely to be either.
            if not (
                0 < their_secs - our_secs <= _DURATION_STRETCH
                and title >= 0.99
                and artist >= 0.99
            ):
                return None  # a different cut: live, extended, or the wrong song
            duration = 0.0  # explainable, but it corroborates nothing
        else:
            duration = 1.0 - delta / _DURATION_REJECT
    else:
        duration = 0.0

    return 2.0 * title + 1.5 * artist + 1.5 * duration


def _split_artist_title(s: str) -> tuple[str, str]:
    for sep in (" - ", " – ", " — ", " _ "):
        if sep in s:
            left, right = s.split(sep, 1)
            return left.strip(), right.strip()
    return "", s.strip()


def _clean_source_title(meta: TrackMeta) -> None:
    """
    Tidy a raw YouTube/SoundCloud title when no catalogue match was found.

    Without this the video title survives untouched - "HesamTiem - Karkas
    (Official Music Video)" under the channel name "HesamTiem", which displays
    as the artist twice and, worse, is what gets sent to the lyrics lookup, so
    it never matches anything. Persian and other non-catalogue music hits this
    every time, which is exactly where the raw title is all we have.
    """
    cleaned = _strip_noise(meta.name)
    if not cleaned:
        return

    artist, title = _split_artist_title(cleaned)
    channel = meta.artists[0] if meta.artists else ""

    if artist and title:
        # "Artist - Title" in the video title is more trustworthy than the
        # channel name, and collapses the duplicate when they are the same.
        # Uploaders sometimes repeat themselves ("X - X - Song"), so keep
        # peeling while the remainder still opens with the artist.
        for _ in range(3):
            nxt_artist, nxt_title = _split_artist_title(title)
            if nxt_artist and _norm(nxt_artist) == _norm(artist) and nxt_title:
                title = nxt_title
                continue
            break
        meta.artists = [artist]
        meta.name = title
    else:
        meta.name = cleaned
        # A title that merely repeats the channel name tells the lyrics search
        # nothing; drop the redundancy.
        if channel and _norm(cleaned).startswith(_norm(channel)):
            rest = cleaned[len(channel):].strip(" -–—:|")
            if rest:
                meta.name = rest

    log.info("cleaned source title -> %s", meta.display)


def _itunes_enrich(meta: TrackMeta) -> None:
    """
    For YouTube/SoundCloud sources the 'metadata' is just a video title plus a
    channel name, and the cover is a video thumbnail. Look the song up on the
    keyless iTunes Search API and adopt the real name/artist/album/600x600 art.

    Nothing is adopted unless the artist agrees as well as the title and the
    runtime - see _same_recording. Enrichment is an upgrade or it is nothing;
    it is never allowed to contradict what the source already told us.
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
    # Everything that could name the artist: the split guess, the channel, and
    # the untouched title - a credit like "(feat. Sami Low, Raha)" lives there
    # and nowhere else.
    artist_hay = " ".join(
        filter(None, [guess_artist, " ".join(meta.artists), meta.match_text or meta.name])
    )

    best: tuple[float, dict] | None = None
    for it in results:
        track_name = (it.get("trackName") or "").strip()
        artist_name = (it.get("artistName") or "").strip()
        if not track_name:
            continue

        score = _same_recording(
            our_artist=artist_hay,
            our_title=title_hay,
            our_secs=our_secs,
            their_artist=artist_name,
            their_title=track_name,
            their_secs=(it.get("trackTimeMillis") or 0) / 1000,
        )
        if score is None:
            continue
        if best is None or score > best[0]:
            best = (score, it)

    if best is None:
        log.info("iTunes: no confident match for %r - cleaning the raw title", meta.name)
        _clean_source_title(meta)
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


# ---------------- full artist credits ----------------

# The credit runs to the end of the title, and never spans a bracket - so
# "No Feat (Live)" is not read as featuring somebody called Live. "with" is
# deliberately not a marker: it would turn "Dancing With Myself" into "Dancing"
# by an artist named Myself.
_FEAT_RE = re.compile(
    r"\s*[\(\[]?\s*\b(?:feat|ft|featuring)\b\.?\s*([^()\[\]]+?)\s*[\)\]]?\s*$", re.I
)


def _feat_names(title: str) -> list[str]:
    """Pull the guests out of 'Song (feat. A, B & C)'."""
    m = _FEAT_RE.search(title)
    if not m:
        return []
    return [
        p.strip(" .")
        for p in re.split(r",|&|\band\b|\bx\b", m.group(1), flags=re.I)
        if p.strip(" .")
    ]


def _yt_blocked() -> bool:
    """Whether YouTube has just refused this server outright, rather than
    simply having nothing that matches."""
    try:
        from modules.youtube import bot_checked_recently

        return bot_checked_recently()
    except Exception:
        return False


def _merge_names(*groups: list[str]) -> list[str]:
    """Order-preserving union, case- and spacing-insensitive.

    One source hands back "Wantons, Koorosh, Arta" as a single name; another
    hands back the same three separately. Comparing whole strings finds no
    overlap, so both went in and display joined them with commas a second
    time:

        Wantons, Koorosh, Arta, Wantons, Koorosh, Arta — Hanoozam

    That string is also what search_text sends to YouTube and SoundCloud, so
    the track was not found and the download was refused for a track that
    exists.

    A comma-joined name is therefore compared by its parts as well as whole.
    Names that genuinely contain a comma survive: one is dropped only when
    every part of it is already present on its own.
    """
    out: list[str] = []
    seen: set[str] = set()
    within: set[str] = set()

    for group in groups:
        for name in group:
            key = _norm(name)
            if not key or key in seen:
                continue

            pieces = [p.strip() for p in name.split(",") if p.strip()]
            if len(pieces) > 1:
                if all(_norm(p) in seen for p in pieces):
                    continue
                within.update(_norm(p) for p in pieces)
            elif key in within:
                continue

            seen.add(key)
            out.append(name.strip())
    return out


def _adopt_canonical_duration(meta: TrackMeta, secs) -> None:
    """
    Trust the catalogue's runtime for the recording over a listing's.

    Every acceptance in this module is gated on duration - 25 seconds normally,
    8 when nothing confirms the artist. A runtime that is a few seconds out
    therefore does not degrade matching, it inverts it: the right upload falls
    outside the window and a wrong one falls inside. Search listings round and
    occasionally just disagree, so the per-track record wins.
    """
    try:
        ms = int(float(secs) * 1000)
    except (TypeError, ValueError):
        return
    if ms <= 0 or abs(ms - meta.duration_ms) <= 2000:
        return
    log.info("duration corrected for %s: %ds -> %ds",
             meta.display, meta.duration_ms // 1000, ms // 1000)
    meta.duration_ms = ms


def _isrc_lookup(isrc: str) -> dict:
    """The Deezer record for an exact recording. {} when it has no such id."""
    from utils import http

    try:
        r = http.get(f"https://api.deezer.com/track/isrc:{isrc}", timeout=8)
        d = r.json() if r.status_code == 200 else {}
        return d if d.get("id") else {}
    except Exception as e:
        log.info("isrc lookup failed for %s: %s", isrc, e)
        return {}


def _ensure_credits(meta: TrackMeta) -> None:
    """
    Name everyone on the track, not just whoever the search endpoint led with.

    Deezer's /search returns one `artist`; the track endpoint carries the full
    `contributors` list. "Tuning" is credited to Tiem, HesamTiem and Salii and
    reached the bot as plain "Tiem". iTunes keeps its guests in the *title*
    instead, as "(feat. ...)" - the same information in a worse place, since it
    then pollutes every title match. Both are normalised here: guests belong in
    the artist list, and the title keeps the song's actual name.

    One request, and only for a track somebody actually picked.
    """
    if meta.credits_done:
        return
    meta.credits_done = True

    from utils import http

    try:
        if meta.id.startswith("dz_"):
            d = http.get(f"https://api.deezer.com/track/{meta.id[3:]}").json()
            names = [
                (c.get("name") or "").strip() for c in (d.get("contributors") or [])
            ]
            if any(names):
                meta.artists = _merge_names([n for n in names if n], meta.artists)
            # Deezer keeps "(Remix)", "(Live)" and the like in a separate field.
            version = (d.get("version") or "").strip()
            if version and _norm(version) not in _norm(meta.name):
                meta.name = f"{meta.name} {version}".strip()
            # Free, in a request already being made.
            meta.isrc = meta.isrc or (d.get("isrc") or "").strip()
            _adopt_canonical_duration(meta, d.get("duration"))

        # A Spotify link carries no ISRC on its embed page and Odesli will not
        # give one either, so the Web API is the only route - and it needs the
        # app owner to hold Premium. When it is reachable the payoff is real:
        # the ISRC pins the exact recording, and Deezer's record for it settles
        # the runtime and the credits without a single fuzzy comparison.
        if not meta.isrc and not meta.id.startswith(("yt_", "sc_", "it_", "dz_")):
            from modules import spotify_api

            meta.isrc = spotify_api.track_isrc(meta.id)

        if meta.isrc:
            exact = _isrc_lookup(meta.isrc)
            if exact:
                names = [
                    (c.get("name") or "").strip()
                    for c in (exact.get("contributors") or [])
                ]
                if any(names):
                    meta.artists = _merge_names(
                        [n for n in names if n], meta.artists
                    )
                _adopt_canonical_duration(meta, exact.get("duration"))
                if not meta.cover_url:
                    album = exact.get("album") or {}
                    meta.cover_url = (
                        album.get("cover_xl") or album.get("cover_big") or ""
                    )
                log.info("isrc %s resolved -> %s", meta.isrc, meta.display)

        guests = _feat_names(meta.name)
        if guests:
            meta.artists = _merge_names(meta.artists, guests)
            stripped = _FEAT_RE.sub("", meta.name).strip(" -–—([")
            if stripped:
                meta.name = stripped
        if guests or meta.id.startswith("dz_"):
            log.info("credits resolved -> %s", meta.display)
    except Exception as e:
        log.info("credit lookup failed for %s: %s", meta.display, e)


@run_in_thread
def fill_details(meta: TrackMeta) -> None:
    """Async wrapper so handlers can complete a track without blocking the loop."""
    _ensure_credits(meta)
    ensure_cover(meta)


def ensure_cover(meta: TrackMeta) -> None:
    """
    Fill in artwork the source could not supply.

    Spotify's embed track lists carry no per-track image, so playlist entries
    arrive with an empty cover. One keyless catalogue lookup by artist+title
    gets the real album art (and album name) - far cheaper than fetching each
    track's own embed page.

    The match has to satisfy the same identity rule as everything else. This
    checked only the title and the runtime, which is how a song acquired a
    stranger's album art: plenty of unrelated tracks share both.
    """
    if meta.cover_url:
        return
    artist = meta.artists[0] if meta.artists else ""
    try:
        hits = _music_api_search(f"{artist} {meta.name}".strip(), 5)
    except Exception as e:
        log.warning("cover lookup failed for %s: %s", meta.display, e)
        return

    our_secs = meta.duration_ms / 1000 if meta.duration_ms else 0
    for h in hits:
        if not h.cover_url:
            continue
        if _same_recording(
            our_artist=f"{artist} {meta.match_text or meta.name}",
            our_title=meta.name,
            our_secs=our_secs,
            their_artist=h.artists[0] if h.artists else "",
            their_title=h.name,
            their_secs=h.duration_ms / 1000 if h.duration_ms else 0,
        ) is None:
            continue
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


@run_in_thread
def other_versions(meta: TrackMeta, limit: int = 8) -> list[TrackMeta]:
    """
    Remixes, live cuts, acoustic takes, slowed edits and covers of this song.

    Deliberately the mirror image of the download matcher: that one refuses
    every recording which is not the catalogue take, and everything it refuses
    is exactly what belongs here. None of it is in the commercial catalogues,
    so this is a wide search narrowed back down to the same composition.
    """
    artist = meta.artists[0] if meta.artists else ""
    query = f"{artist} {meta.name}".strip()
    if not query:
        return []

    ours = meta.duration_ms / 1000 if meta.duration_ms else 0
    out: list[TrackMeta] = []
    seen = {_dedupe_key(meta)}

    for cand in _fallback_search_tracks(query, limit * 2):
        hay = cand.match_hay
        if _marks(hay, "", _NON_MUSIC):
            continue
        # Same song...
        if _agree(meta.name, cand.name, 0.5) is None:
            continue
        # Any credited artist will do. A remix upload names whoever the
        # remixer felt like naming, and it is rarely the lead - the "Friend
        # Zone" remix credits Arown and Sami Low but not Arman Miladi.
        if meta.artists and not any(
            _agree(a, hay, 0.3) is not None for a in meta.artists
        ):
            continue
        # ...but not the same recording, which is the whole point.
        theirs = cand.duration_ms / 1000 if cand.duration_ms else 0
        if not (
            _marks(hay, "", _VARIANT_WORDS)
            or (ours and theirs and abs(ours - theirs) > 8)
        ):
            continue
        _clean_source_title(cand)
        key = _dedupe_key(cand)
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
        if len(out) >= limit:
            break
    return out


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


# ---------------- locating the audio behind a catalogue track ----------------

# Words marking a *different recording* of a song. Grabbing one of these when
# the studio track was asked for is the worst failure this bot has: the cover
# and the tags are right, the audio is another performance entirely, and
# nothing looks wrong until you press play. So they are rejected, not merely
# ranked down - a track we refuse to guess at is recoverable, a wrong file
# that looks correct is not.
#
# Hour-long mixes, compilations and full albums are deliberately absent: the
# runtime check already excludes them, and listing them here would only add
# ways to misfire on an honest title.
_VARIANT_WORDS = (
    "live", "concert", "acoustic", "cover", "karaoke", "instrumental",
    "remix", "mashup", "bootleg", "nightcore", "8d", "sped up", "speed up",
    "slowed", "reverb", "snippet", "preview",
    # Not one track at all. These matter most now that a longer candidate can
    # be accepted as a music video: a compilation is exactly what that
    # allowance would otherwise let through.
    "full album", "mix", "megamix", "compilation", "nonstop", "greatest hits",
    "اجرای زنده", "کنسرت", "لایو", "ریمیکس", "کاور", "بی کلام", "دمو",
)

# Not a different cut - not music at all. Persian rap in particular is buried
# under reaction and breakdown uploads that carry every artist's name in the
# title, so they answer a search better than the song does.
_NON_MUSIC = (
    "reaction", "reacts", "reacting", "tutorial", "review", "interview", "parody",
    "podcast", "trailer", "teaser", "unboxing", "vlog", "explained",
    "breakdown", "behind the scenes", "making of",
    "ری اکشن", "ریاکشن", "واکنش", "آموزش", "نقد", "مصاحبه", "پادکست",
    "پشت صحنه", "بررسی",
)

# How far a candidate's runtime may sit from the catalogue's before it is a
# different recording. Music videos add an intro, so this is not tight.
_DURATION_REJECT = 25.0
# How much longer a music video may run than the audio release it carries.
_DURATION_STRETCH = 60.0
# When nothing confirms the artist, the runtime is the whole identification.
_DURATION_UNNAMED = 8.0
# Candidates pulled per search. A flat search costs one extraction pass no
# matter how many entries it returns, so a wider net is effectively free.
_SEARCH_POOL = 6

# Where the audio for a catalogue track may be found. yt-dlp ships search
# support for exactly these two of the music platforms it can extract:
# Bandcamp, Audiomack, Audius and Radio Javan have no search prefix, and their
# own APIs are either keyed (Audiomack answers 401) or carry catalogues too
# thin to have answered any track tested here.
_AUDIO_SOURCES = ("ytsearch", "scsearch")

# SoundCloud is uploader-driven, so a top placement there says less about
# being the canonical release than YouTube's does. Small, and only a tie
# breaker - a better duration or artist match still wins outright.
_SOURCE_BIAS = {"ytsearch": 0.0, "scsearch": -0.15}


def _marks(candidate: str, wanted: str, words: tuple[str, ...]) -> bool:
    """True when the candidate advertises something the request never asked for."""
    # "Cover Art" is a picture, not a cover version - the one phrase in which
    # these words describe the artwork rather than the performance.
    cand = re.sub(r"\bcover\s*art\w*\b", " ", _norm(candidate))
    want = _norm(wanted)
    # Whole tokens only: "livestream" is not "live", "8d" is not part of "8 days".
    return any(
        f" {_norm(w)} " in f" {cand} " and f" {_norm(w)} " not in f" {want} "
        for w in words
    )


def _is_other_recording(candidate: str, wanted: str) -> bool:
    return _marks(candidate, wanted, _VARIANT_WORDS + _NON_MUSIC)


def _entry_url(e: dict, source: str) -> str:
    """The page URL for a flat search hit.

    YouTube ids rebuild into a watch URL; SoundCloud's are numeric and cannot
    be turned back into anything, so its permalink has to be read off the entry.
    """
    url = e.get("url") or e.get("webpage_url") or ""
    if url.startswith("http"):
        return url
    if source == "ytsearch" and e.get("id"):
        return f"https://www.youtube.com/watch?v={e['id']}"
    return ""


def _match_entry(
    meta: TrackMeta, e: dict, rank: int, source: str, *, lenient: bool = False
) -> tuple[float, str] | None:
    """Score one search hit against the track we know we want; None = reject."""
    url, title = _entry_url(e, source), (e.get("title") or "").strip()
    if not url or not title:
        return None

    channel = (e.get("channel") or e.get("uploader") or "").strip()
    want_artist = meta.artists[0] if meta.artists else ""

    # Any credited artist identifies the recording, not just the lead. Deezer
    # lists "Pesare Bad" under Nassim while every upload of it is titled after
    # Sijal - both are on the track, and checking only artists[0] threw the
    # song away. This is why the credit list is resolved before locating.
    score = None
    for candidate_artist in meta.artists or [""]:
        # The artist is as often inside the video title as it is the channel
        # name, so both are offered as the haystack it must appear in.
        got = _same_recording(
            our_artist=candidate_artist,
            our_title=meta.name,
            our_secs=meta.duration_ms / 1000 if meta.duration_ms else 0.0,
            their_artist=f"{title} {channel}",
            their_title=title,
            their_secs=float(e.get("duration") or 0),
            artist_floor=0.3,
            unnamed_artist_ok=lenient,
        )
        if got is not None and (score is None or got > score):
            score = got
    if score is None:
        return None

    n_artist = _norm(want_artist)
    score += (
        # "<Artist> - Topic" is YouTube's auto-generated channel: the label's
        # own master, which is exactly the recording the catalogue lists.
        (0.8 if channel.lower().endswith("- topic") else 0.0)
        # Failing that, the artist's own channel. Re-upload channels routinely
        # pitch-shift or speed up a track to dodge copyright matching, so the
        # official upload is worth preferring even at an equal title score.
        + (0.5 if n_artist and f" {n_artist} " in f" {_norm(channel)} " else 0.0)
        + 0.3 / (1 + rank)
        # Ranks are per-source, so without this the second source's hits would
        # be compared against the first's on a scale that no longer means the
        # same thing. Sources differ in how much a top placement is worth.
        + _SOURCE_BIAS.get(source, 0.0)
    )
    return score, url


# Odesli/song.link resolves one release across every major service. Its free
# API stopped returning YouTube and Apple links, so it cannot point at a file
# to download - but it answers the question that actually matters when nothing
# is found: is this track absent from the open web, or did our search miss it?
_ODESLI = "https://api.song.link/v1-alpha.1/links"
_PLATFORM_FA = {
    "spotify": "اسپاتیفای", "tidal": "تایدال", "pandora": "پاندورا",
    "deezer": "دیزر", "appleMusic": "اپل موزیک", "itunes": "آیتونز",
    "amazonMusic": "آمازون", "amazonStore": "آمازون", "napster": "نپستر",
    "anghami": "انغامی", "boomplay": "بوم‌پلی", "audius": "آدیوس",
    "yandex": "یاندکس", "soundcloud": "ساندکلاد", "youtube": "یوتیوب",
    "youtubeMusic": "یوتیوب موزیک",
}


def odesli_state() -> str:
    """Empty when usable, otherwise why not.

    Read by /engines so a service that RETIRED its free tier is not shown
    as a fault in this bot. A red cross next to it sent somebody looking
    for a bug that was never here.
    """
    if settings.odesli_api_key:
        return ""
    return ("این سرویس API عمومیش رو بسته — "
            "بدون کلید کار نمی‌کنه. ODESLI_API_KEY")


def _where_else(meta: TrackMeta) -> list[str]:
    """Which services carry this exact release. Empty when we cannot tell.

    song.link answers anonymous callers with

        401 {"code": "PUBLIC_API_ACCESS_DEPRECATED"}

    for every url and every spelling of it. The free tier is gone, not broken,
    so the call is not made at all without a key: a request that is refused
    before it is read is latency spent on a certainty.

    Set ODESLI_API_KEY to bring it back. Nothing depends on it - this only ever
    answered "is the track absent from the open web, or did our search miss
    it?", which is a nicety on a failure path.
    """
    if not settings.odesli_api_key:
        return []

    url = meta.spotify_url or meta.itunes_url
    if not url.startswith("http"):
        return []
    try:
        from utils import http

        r = http.get(_ODESLI, params={"url": url, "userCountry": "US",
                                      "key": settings.odesli_api_key}, timeout=12)
        if r.status_code != 200:
            return []
        return sorted((r.json().get("linksByPlatform") or {}).keys())
    except Exception as e:
        log.info("odesli lookup failed for %s: %s", meta.display, e)
        return []


# Every one of these means "this address was turned away", not "this track
# does not exist". They are kept apart from a genuine deletion because the
# remedy is completely different, and because telling somebody a song is gone
# when the server is simply being refused is the most misleading thing the bot
# can say.
_REFUSED_MARKERS = (
    "sign in to confirm", "confirm you're not a bot", "not a bot",
    "use --cookies", "http error 403", "please sign in",
    "failed to extract any player response",
)
# A deno that will not run cannot solve YouTube's JS challenge, so every
# format disappears and yt-dlp reports the absence rather than the cause.
_RUNTIME_MARKERS = ("requested format is not available", "unable to extract")


def _download_failed(
    meta: TrackMeta, targets: list[str], reasons: list[str], last: Exception | None
) -> RuntimeError:
    """Say which of the three different failures this actually was.

    A Spotify link resolved to "Drake - Finesse", five candidate uploads were
    located and scored, and every single one of them then failed to download.
    The user was told the audio could not be FOUND - which was false, it had
    been found and identified - and that the video may have been deleted,
    which cannot be true of five different videos at the same moment.

    Five candidates failing together is a statement about this server, not
    about the track, and it is the same mistake this bot has made before:
    reporting a refusal aimed at us as a fact about the thing we asked for.
    """
    if not targets:
        return RuntimeError(
            f"آهنگ «{meta.display}» رو تو یوتیوب و ساندکلاد پیدا نکردم."
        )

    blob = " ".join(reasons)
    if any(m in blob for m in _REFUSED_MARKERS):
        log.error("all %d candidates for %r were refused - youtube is turning "
                  "this server away, not missing the track", len(targets), meta.display)
        return RuntimeError(
            f"«{meta.display}» پیدا شد ولی یوتیوب دانلودش رو به این سرور نداد "
            f"(هر {len(targets)} گزینه رد شد). آهنگ حذف نشده — مشکل آدرس سروره. "
            f"راه‌حل: botctl ytcookies یا botctl proxy"
        )

    if any(m in blob for m in _RUNTIME_MARKERS):
        log.error("all %d candidates for %r reported no usable format - deno "
                  "or yt-dlp is the suspect, not the track",
                  len(targets), meta.display)
        return RuntimeError(
            f"«{meta.display}» پیدا شد ولی هیچ‌کدوم از {len(targets)} گزینه فرمت قابل "
            f"دانلود نداشت. معمولا یعنی deno یا yt-dlp لنگه. "
            f"راه‌حل: botctl ytdlp"
        )

    return RuntimeError(
        f"دانلود «{meta.display}» از هر {len(targets)} گزینه شکست خورد"
        + (f" — {str(last)[:120]}" if last else "")
    )


def _locate_audio(meta: TrackMeta) -> list[str]:
    """
    Find the uploads that actually *are* this track, best first.

    This used to be `ytsearch1:<artist> <title> audio` handed straight to
    yt-dlp, i.e. whatever came back first was downloaded unverified. For
    anything thin on YouTube - a remix, a Persian release, an album cut - the
    first hit is regularly a different song, a full-album upload or an
    hour-long compilation, and the result is a file with the right cover and
    the right tags wrapped around the wrong audio.

    Candidates are checked against the runtime and the title the catalogue
    gave us, and nothing credible means an error rather than a wrong song.

    Every source is searched and every candidate scored together, rather than
    settling for the first source that offers something passable. YouTube alone
    left real gaps: "UGLY GEMINI - SHY BOY" exists there only as a 3:05 music
    video, while SoundCloud carries the 2:28 audio release the catalogue
    actually describes - the better answer, and previously never even looked at.
    """
    from concurrent.futures import ThreadPoolExecutor

    queries = [meta.search_query]
    if meta.album and _norm(meta.album) != _norm(meta.name):
        queries.append(f"{meta.search_query} official audio")
    else:
        queries.append(f"{meta.name} {meta.artists[0] if meta.artists else ''}".strip())

    def fetch(source: str, query: str) -> list[dict]:
        try:
            return _flat_entries(f"{source}{_SEARCH_POOL}:{query}")
        except Exception as e:
            log.warning("%s search failed for %r: %s", source, query, e)
            return []

    pools: dict[str, list[dict]] = {}
    for q in queries:
        with ThreadPoolExecutor(max_workers=len(_AUDIO_SOURCES)) as pool:
            pools = {
                source: fut.result()
                for source, fut in {
                    src: pool.submit(fetch, src, q) for src in _AUDIO_SOURCES
                }.items()
            }

        # Strict first. Only if nothing at all is confirmable is the bar
        # lowered, so a track that *can* be identified never gets a guess.
        scored: list[tuple[float, str, str]] = []
        for lenient in (False, True):
            for source, entries in pools.items():
                scored += [
                    (s, url, source)
                    for s, url in (
                        m
                        for m in (
                            _match_entry(meta, e, i, source, lenient=lenient)
                            for i, e in enumerate(entries)
                        )
                        if m is not None
                    )
                ]
            if scored:
                if lenient:
                    log.info("no confirmable match for %s - accepted on title "
                             "and runtime alone", meta.display)
                break

        if scored:
            scored.sort(key=lambda m: m[0], reverse=True)
            log.info(
                "audio candidates for %s: %s",
                meta.display,
                ", ".join(f"{src}:{s:.2f}" for s, _, src in scored[:4]),
            )
            return [url for _, url, _ in scored]
        log.info("no credible audio match for %r on any source", q)

    # Two different failures wear the same face, and telling them apart is the
    # difference between "wait and retry" and "this is not out there".
    looked_at = sum(len(v) for v in (pools or {}).values())

    # A third one hides behind both: YouTube refusing to answer this server at
    # all. Then ytsearch returns nothing, SoundCloud alone has no version, and
    # the bot reports the track as absent from the internet - which is a claim
    # about the track made from evidence about the server.
    if not (pools or {}).get("ytsearch") and _yt_blocked():
        raise RuntimeError(
            f"«{meta.display}» رو نتونستم بگردم چون یوتیوب جواب سرور رو نداد "
            "(«Sign in to confirm you're not a bot»).\n"
            "این یعنی ترک نیست نداره — یعنی سرچ انجام نشد.\n"
            "روی سرور:  botctl ytcookies"
        )

    if looked_at:
        detail = (
            "چیزی که پیدا شد آهنگ‌های دیگه‌ای بودن — نه نسخه‌ای از این ترک، "
            "برای همین نفرستادمش."
        )
    else:
        detail = "هیچ منبعی حتی چیزی شبیهش نداشت."

    # Say where the track actually lives. "Not found" reads like a bug in the
    # bot; "this exists on Spotify, Tidal and Pandora and nowhere else" is the
    # truth, and tells you retrying later is pointless. One request, and only
    # on the way to failing anyway.
    elsewhere = [p for p in _where_else(meta) if p not in ("spotify",)]
    hint = ""
    if elsewhere:
        # amazonMusic and amazonStore are two entries with one Persian name,
        # so the list read "آمازون، آمازون" - which looks like the bot cannot
        # count rather than like Amazon sells it twice.
        names = "، ".join(dict.fromkeys(_PLATFORM_FA.get(p, p) for p in elsewhere))
        reachable = [p for p in elsewhere if p in ("youtube", "youtubeMusic", "soundcloud")]
        hint = f"\nاین ترک روی {names} هست"
        hint += (
            " — یعنی باید پیدا می‌شد؛ سرچ خطا داده."
            if reachable
            else " و جای دیگه‌ای نیست. هیچ‌کدوم از این‌ها قابل دانلود نیستن."
        )

    raise RuntimeError(
        f"«{meta.display}» رو رو یوتیوب و ساندکلاد پیدا نکردم. {detail}{hint}\n"
        "اگه لینک مستقیمش رو از یوتیوب یا ساندکلاد داری، همون رو بفرست — "
        "لینک مستقیم بدون این بررسی‌ها دانلود می‌شه."
    )


# ---------------- downloading ----------------

@run_in_thread(heavy=True)
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
    _ensure_credits(meta)
    ensure_cover(meta)

    out_dir = settings.download_dir / "spotify"
    out_dir.mkdir(parents=True, exist_ok=True)

    # The track id goes in the name so two different tracks can never land on
    # one path. Keying only on "Artist — Title" meant a remix and the original
    # shared a file, and - far worse - one bad download poisoned that name
    # permanently: every later request for it was served the stale file off
    # disk without so much as a lookup.
    base = out_dir / f"{safe_filename(meta.display)} [{_id_tag(meta.id)}]"
    existing = _find_output(base)
    if existing is not None:
        return existing

    if meta.id.startswith(("yt_", "sc_")) and meta.spotify_url.startswith("http"):
        targets = [meta.spotify_url]
    elif meta.id.startswith("yt_"):
        targets = [f"https://www.youtube.com/watch?v={meta.id[3:]}"]
    else:
        # Spotify / iTunes / Deezer entries carry metadata only - the audio
        # itself still has to be located, and verified.
        targets = _locate_audio(meta)

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

    # Matching a track and being able to fetch it are different questions.
    # SoundCloud DRM-protects some label uploads, YouTube geo-blocks and
    # deletes others - and the best match is exactly as likely to be the one
    # that fails. So the ranked list is walked until something downloads,
    # instead of one refusal sinking a track we positively identified.
    last: Exception | None = None
    reasons: list[str] = []
    out_path = None
    best_thin: tuple | None = None
    for i, target in enumerate(targets):
        try:
            ytdlp_run(extra, lambda ydl: ydl.download([target]), kind="audio")
        except Exception as e:
            last = e
            reasons.append(str(e).lower())
            log.info("candidate %d/%d unusable (%s) - trying the next",
                     i + 1, len(targets), str(e)[:120])
            continue
        out_path = _find_output(base)
        if out_path is not None:
            # A file is not the same thing as the RIGHT file. On this server
            # YouTube's client ladder sometimes lands on a client that offers
            # only the 48kbps rungs, and the download succeeds perfectly - it
            # just produces a 1.1MB file for a track that is 3MB everywhere
            # else. Nothing upstream notices, because nothing failed.
            #
            # Measured against the duration the catalogue already gave us, so
            # this costs no extra request. A thin one is kept only if every
            # other candidate is thinner.
            kbps = _bitrate_kbps(out_path, meta)
            if kbps and kbps < _THIN_KBPS and i + 1 < len(targets):
                log.info("candidate %d/%d came back at %.0fkbps - too thin, "
                         "trying the next", i + 1, len(targets), kbps)
                if best_thin is None or kbps > best_thin[1]:
                    thin = out_path.with_suffix(out_path.suffix + f".thin{i}")
                    out_path.replace(thin)
                    if best_thin is not None:
                        best_thin[0].unlink(missing_ok=True)
                    best_thin = (thin, kbps)
                else:
                    out_path.unlink(missing_ok=True)
                out_path = None
                continue
            break
        log.info("candidate %d/%d produced no file - trying the next",
                 i + 1, len(targets))

    # Every candidate was thin. The best of them still beats nothing, and
    # beats an error that says the track could not be found when it was.
    if out_path is None and best_thin is not None:
        out_path = _find_output(base) or base.with_suffix(best_thin[0].suffix
                                                          .replace(".thin", ""))
        best_thin[0].replace(out_path)
        log.warning("every candidate for %r was thin - kept the best at %.0fkbps",
                    meta.display, best_thin[1])
    elif best_thin is not None:
        best_thin[0].unlink(missing_ok=True)

    if out_path is None:
        raise _download_failed(meta, targets, reasons, last)

    _embed_cover_and_tags(out_path, meta)
    return out_path


# Below this a file is not a copy of the track, it is a sketch of one. The
# 48kbps rungs YouTube keeps for slow connections land here; every real music
# upload is 128 and up.
_THIN_KBPS = 96.0


def _bitrate_kbps(path, meta) -> float:
    """The delivered bitrate, from the file size and the known duration.

    The duration comes from the catalogue entry we already have, so this is
    arithmetic rather than another probe. Returns 0 when it cannot be judged,
    which is treated as "do not reject".
    """
    seconds = (meta.duration_ms or 0) / 1000.0
    if seconds < 30:                      # too short to judge, or unknown
        return 0.0
    try:
        return path.stat().st_size * 8 / seconds / 1000.0
    except Exception:
        return 0.0


_LOSSLESS_CODECS = ("flac", "alac", "wav", "pcm")


def _audio_formats(target: str) -> list[dict]:
    """Every audio-only format the source offers, best bitrate first."""
    from modules.youtube import ytdlp_run

    info = ytdlp_run(
        {"skip_download": True, "quiet": True},
        lambda ydl: ydl.extract_info(target, download=False),
        kind="probe",
    )
    formats = [
        f for f in ((info or {}).get("formats") or [])
        if f.get("acodec") and f["acodec"] != "none" and f.get("vcodec") in (None, "none")
    ]
    return sorted(formats, key=lambda f: f.get("abr") or 0, reverse=True)


def describe_best(target: str) -> tuple[str, float, bool]:
    """(codec, bitrate kbps, is_lossless) for the best audio the source has.

    Used to tell the user what they are actually getting instead of promising
    a quality nobody can deliver: YouTube and SoundCloud are lossy, so a FLAC
    made from them is a bigger file carrying the same audio.
    """
    try:
        formats = _audio_formats(target)
    except Exception as e:
        log.info("could not list formats for %s: %s", target, e)
        return "", 0.0, False
    if not formats:
        return "", 0.0, False

    best = formats[0]
    codec = (best.get("acodec") or "").lower()
    lossless = any(c in codec for c in _LOSSLESS_CODECS)
    return codec, float(best.get("abr") or 0), lossless


@run_in_thread(heavy=True)
def download_best(meta: TrackMeta) -> tuple[Path, str, bool]:
    """The best audio the source actually has, kept in its native codec.

    (path, codec, is_lossless). Nothing is re-encoded: the source is already
    lossy in almost every case, and a second lossy pass - or an inflating pass
    to FLAC - can only make it worse or bigger. FLAC comes out only when the
    source really was lossless.
    """
    from modules.youtube import ytdlp_run

    _ensure_credits(meta)
    ensure_cover(meta)

    out_dir = settings.download_dir / "spotify"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"{safe_filename(meta.display)} [{_id_tag(meta.id)}] best"

    existing = _find_output(base)
    if existing is not None:
        codec = existing.suffix.lstrip(".").lower()
        return existing, codec, codec in _LOSSLESS_CODECS

    if meta.id.startswith(("yt_", "sc_")) and meta.spotify_url.startswith("http"):
        targets = [meta.spotify_url]
    elif meta.id.startswith("yt_"):
        targets = [f"https://www.youtube.com/watch?v={meta.id[3:]}"]
    else:
        targets = _locate_audio(meta)

    last: Exception | None = None
    for target in targets:
        codec, _abr, lossless = describe_best(target)
        extra = {
            "format": "bestaudio/best",
            "format_sort": ["abr", "asr"],
            "outtmpl": str(base) + ".%(ext)s",
            # Remux into a container Telegram shows as playable audio, without
            # touching the samples. No FFmpegExtractAudio: that re-encodes.
            "postprocessors": [{"key": "FFmpegMetadata"}],
        }
        try:
            ytdlp_run(extra, lambda ydl: ydl.download([target]), kind="audio")
        except Exception as e:
            last = e
            continue
        out_path = _find_output(base)
        if out_path is not None:
            _embed_cover_and_tags(out_path, meta)
            return out_path, codec or out_path.suffix.lstrip("."), lossless

    raise RuntimeError(
        f"نسخه‌ی باکیفیت «{meta.display}» پیدا نشد"
        + (f" — {str(last)[:120]}" if last else "")
    )


_AUDIO_EXTS = {".m4a", ".mp3", ".opus", ".ogg", ".oga", ".webm", ".aac", ".mp4", ".flac", ".wav"}


def _id_tag(track_id: str) -> str:
    """Short filesystem-safe stamp of a track id, to keep paths unique."""
    import hashlib

    return hashlib.md5(track_id.encode("utf-8")).hexdigest()[:8]


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


# ---------------- the music video behind a track ----------------

# What an official video calls itself. A match on one of these is the
# difference between the video and a static-image upload of the same audio.
_MV_MARKERS = ("official music video", "official video", "music video",
               "official mv", "video oficial", "موزیک ویدیو", "موزیک‌ویدیو")

# Uploads that are the song but not the video. Every one of these is something
# a search for "<artist> <title> official video" returns in quantity, and
# handing one back as "the music video" is worse than saying there is none -
# the user already has the audio.
_NOT_A_VIDEO = ("lyric", "lyrics", "audio only", "official audio", "full album",
                "visualizer", "visualiser", "slowed", "reverb", "sped up",
                "8d audio", "nightcore", "karaoke", "instrumental", "cover by",
                "reaction", "teaser", "trailer", "behind the scenes", "making of")


def _mv_score(meta: TrackMeta, entry: dict) -> float:
    """How much this entry looks like the official video of this track.

    Runtime is deliberately not part of it. A music video is routinely a
    different length from the release - intros, outros, a cold open - so the
    runtime rule that protects the audio search would reject exactly the
    thing being looked for here.
    """
    title = entry.get("title") or ""
    lowered = title.lower()

    if any(bad in lowered for bad in _NOT_A_VIDEO):
        return 0.0

    artist = meta.artists[0] if meta.artists else ""
    # Both halves have to be present: a video titled after the song alone is
    # as likely to be someone else's song of the same name.
    if _coverage(meta.name, title) < 0.75 or _coverage(artist, f"{title} {entry.get('channel') or ''}") < 0.5:
        return 0.0

    score = 2.0 * _coverage(f"{artist} {meta.name}", title)
    if any(marker in lowered for marker in _MV_MARKERS):
        score += 1.5
    # An official channel usually carries the artist's name, or is a label
    # topic channel - "- Topic" is auto-generated audio, never the video.
    channel = (entry.get("channel") or entry.get("uploader") or "")
    if channel.endswith("- Topic"):
        return 0.0
    if _norm(artist) and _norm(artist) in _norm(channel):
        score += 0.75
    return score


@run_in_thread
def find_music_video(meta: TrackMeta) -> str | None:
    """The YouTube url of this track's music video, or None.

    None is a real answer here and is returned rather than a best guess:
    plenty of tracks have no video, and offering a lyric upload or a
    soundalike in its place is worse than saying so, because the user already
    has the audio this would duplicate.
    """
    artist = meta.artists[0] if meta.artists else ""
    query = f"{artist} {meta.name} official music video".strip()

    entries = _flat_entries(f"ytsearch8:{query}")
    scored = [(s, e) for e in entries if (s := _mv_score(meta, e)) > 0]
    if not scored:
        log.info("no music video found for %s", meta.display)
        return None

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best = scored[0]
    log.info("music video for %s: %r (score %.2f)",
             meta.display, (best.get("title") or "")[:60], best_score)
    return f"https://www.youtube.com/watch?v={best['id']}" if best.get("id") else None
