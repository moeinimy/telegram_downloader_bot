"""
Detects what platform a URL belongs to and what kind of resource it is.
Keeps platform-detection logic out of the handlers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    SPOTIFY = "spotify"
    SOUNDCLOUD = "soundcloud"
    UNKNOWN = "unknown"


class InstagramKind(str, Enum):
    POST = "post"          # /p/<shortcode>/
    REEL = "reel"          # /reel/<shortcode>/ or /reels/
    STORY = "story"        # /stories/<user>/<id>/
    PROFILE = "profile"    # /<username>/
    IGTV = "igtv"
    UNKNOWN = "unknown"


class SpotifyKind(str, Enum):
    TRACK = "track"
    ALBUM = "album"
    PLAYLIST = "playlist"
    ARTIST = "artist"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RouteResult:
    platform: Platform
    kind: str          # InstagramKind | SpotifyKind | "video" for youtube
    url: str
    resource_id: str   # shortcode / spotify id / youtube video id


_YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", re.IGNORECASE
)
_SOUNDCLOUD_RE = re.compile(
    r"^(https?://)?(www\.|m\.|on\.)?soundcloud\.com/", re.IGNORECASE
)
_INSTAGRAM_RE = re.compile(r"^(https?://)?(www\.)?instagram\.com/", re.IGNORECASE)
_SPOTIFY_RE = re.compile(
    r"^(https?://)?(open\.)?spotify\.com/(?:intl-[a-z]+/)?(track|album|playlist|artist)/([A-Za-z0-9]+)",
    re.IGNORECASE,
)


def route(text: str) -> RouteResult | None:
    """Return RouteResult if text contains a known URL, else None."""
    text = text.strip()

    # Spotify
    m = _SPOTIFY_RE.search(text)
    if m:
        return RouteResult(
            platform=Platform.SPOTIFY,
            kind=m.group(3).lower(),
            url=m.group(0),
            resource_id=m.group(4),
        )

    # SoundCloud
    if _SOUNDCLOUD_RE.search(text):
        kind = "playlist" if "/sets/" in text else "track"
        return RouteResult(
            platform=Platform.SOUNDCLOUD,
            kind=kind,
            url=text,
            resource_id=text,
        )

    # YouTube
    if _YOUTUBE_RE.search(text):
        return RouteResult(
            platform=Platform.YOUTUBE,
            kind="video",
            url=text,
            resource_id=_extract_youtube_id(text),
        )

    # Instagram
    if _INSTAGRAM_RE.search(text):
        kind, rid = _classify_instagram(text)
        return RouteResult(
            platform=Platform.INSTAGRAM,
            kind=kind.value,
            url=text,
            resource_id=rid,
        )

    return None


def _extract_youtube_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else ""


def _classify_instagram(url: str) -> tuple[InstagramKind, str]:
    if "/stories/" in url:
        # resource is the USERNAME - stories are fetched per-user, the
        # numeric story id is useless on its own.
        m = re.search(r"/stories/([^/?#]+)", url)
        return InstagramKind.STORY, m.group(1) if m else ""
    if "/reel/" in url or "/reels/" in url:
        m = re.search(r"/reels?/([^/?#]+)", url)
        return InstagramKind.REEL, m.group(1) if m else ""
    if "/p/" in url:
        m = re.search(r"/p/([^/?#]+)", url)
        return InstagramKind.POST, m.group(1) if m else ""
    if "/tv/" in url:
        m = re.search(r"/tv/([^/?#]+)", url)
        return InstagramKind.IGTV, m.group(1) if m else ""
    # last fallback: profile
    m = re.search(r"instagram\.com/([^/?#]+)/?", url)
    return InstagramKind.PROFILE, m.group(1) if m else ""
