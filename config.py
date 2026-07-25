"""
Central configuration loaded from environment variables.
All modules import settings from here instead of touching os.environ directly.

Only TELEGRAM_BOT_TOKEN is required. Everything else is optional:
  - Spotify needs no credentials (metadata comes from public embed pages).
  - YouTube needs no cookies (alternative player clients are tried instead).
  - Instagram cookies only unlock stories and photo carousels; reels and
    video posts work without an account.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return (value or "").strip()


@dataclass(frozen=True)
class Settings:
    # Telegram (the only required setting)
    telegram_token: str

    # Spotify (optional). Only needed for playlists larger than ~100 tracks:
    # the keyless embed page never returns more than that. Normal user
    # playlists read fine with client credentials; Spotify's own editorial
    # playlists are blocked for new free apps since late 2024.
    spotify_client_id: str
    spotify_client_secret: str

    # Instagram (optional). Browser session cookies from a throwaway account
    # unlock stories + photo carousels. Without them the bot still handles
    # reels and video posts anonymously.
    instagram_username: str
    ig_sessionid: str
    ig_csrftoken: str
    ig_ds_user_id: str

    # Optional: local Bot API server (https://github.com/tdlib/telegram-bot-api)
    # e.g. http://127.0.0.1:8081 - raises the upload limit from 50MB to 2GB.
    bot_api_base_url: str

    # Filesystem
    download_dir: Path
    max_upload_mb: int

    # YouTube cookies (optional). Path to a Netscape-format cookies.txt.
    # Leave empty: the client ladder in modules/youtube.py handles the
    # "Sign in to confirm you're not a bot" check without an account.
    yt_cookies_file: str

    # Audio container for downloaded music: m4a (default), mp3 or flac.
    # NOTE: every source the bot can reach (YouTube, SoundCloud) serves lossy
    # audio, so "flac" produces a much larger file without recovering any
    # detail - it is a lossless container around lossy content.
    audio_format: str

    # Telegram user ids allowed to open the admin panel. Empty = disabled.
    admin_ids: frozenset[int]

    # Channel users must join before the bot answers, e.g. "@mychannel".
    # The bot has to be an admin of it. Empty = no gate.
    required_channel: str

    # Logging
    log_level: str

    @property
    def has_instagram_session(self) -> bool:
        return bool(self.ig_sessionid and self.instagram_username)


def _admin_ids() -> frozenset[int]:
    """Parse ADMIN_IDS ("123,456"). Anything unparseable is dropped with a
    warning rather than crashing the bot on startup."""
    out = set()
    for chunk in _get("ADMIN_IDS").replace(" ", "").split(","):
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            logging.getLogger(__name__).warning("ADMIN_IDS: ignoring %r", chunk)
    return frozenset(out)


def _channel_name() -> str:
    """Accept @name, a t.me link or a bare name; store it as @name."""
    raw = _get("REQUIRED_CHANNEL").strip()
    if not raw:
        return ""
    raw = raw.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "")
    raw = raw.strip("/ ")
    return raw if raw.startswith(("@", "-100")) else f"@{raw}"


def _cookies_path() -> str:
    """Ignore a configured cookies file that doesn't actually exist, so a
    stale default path can't break every YouTube download."""
    path = _get("YT_COOKIES_FILE")
    if path and not Path(path).is_file():
        logging.getLogger(__name__).warning(
            "YT_COOKIES_FILE=%s does not exist - continuing without cookies.", path
        )
        return ""
    return path


settings = Settings(
    telegram_token=_get("TELEGRAM_BOT_TOKEN", required=True),
    spotify_client_id=_get("SPOTIFY_CLIENT_ID"),
    spotify_client_secret=_get("SPOTIFY_CLIENT_SECRET"),
    instagram_username=_get("INSTAGRAM_USERNAME"),
    ig_sessionid=_get("IG_SESSIONID"),
    ig_csrftoken=_get("IG_CSRFTOKEN"),
    ig_ds_user_id=_get("IG_DS_USER_ID"),
    bot_api_base_url=_get("BOT_API_BASE_URL"),
    download_dir=Path(_get("DOWNLOAD_DIR", "./downloads")).resolve(),
    max_upload_mb=int(_get("MAX_UPLOAD_MB", "50") or 50),
    yt_cookies_file=_cookies_path(),
    audio_format=(_get("AUDIO_FORMAT", "m4a").lower() or "m4a"),
    admin_ids=_admin_ids(),
    required_channel=_channel_name(),
    log_level=_get("LOG_LEVEL", "INFO"),
)

if settings.audio_format not in ("m4a", "mp3", "flac"):
    raise RuntimeError(
        f"AUDIO_FORMAT={settings.audio_format!r} is not one of: m4a, mp3, flac"
    )

settings.download_dir.mkdir(parents=True, exist_ok=True)


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    # quiet noisy libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)
