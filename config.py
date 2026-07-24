"""
Central configuration loaded from environment variables.
All modules import settings from here instead of touching os.environ directly.
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
    return value or ""


@dataclass(frozen=True)
class Settings:
    # Telegram
    telegram_token: str

    # Spotify
    spotify_client_id: str
    spotify_client_secret: str

    # Instagram. Preferred auth: browser session cookies (sessionid etc.),
    # which avoid the login-from-datacenter-IP checkpoint that
    # username/password logins trigger.
    instagram_username: str
    instagram_password: str
    ig_sessionid: str
    ig_csrftoken: str
    ig_ds_user_id: str

    # Optional: local Bot API server (https://github.com/tdlib/telegram-bot-api)
    # e.g. http://127.0.0.1:8081 - raises the upload limit from 50MB to 2GB.
    bot_api_base_url: str

    # Filesystem
    download_dir: Path
    max_upload_mb: int

    # YouTube cookies (optional but recommended on datacenter IPs).
    # Path to a Netscape-format cookies.txt exported from a logged-in browser.
    yt_cookies_file: str

    # Logging
    log_level: str


settings = Settings(
    telegram_token=_get("TELEGRAM_BOT_TOKEN", required=True),
    spotify_client_id=_get("SPOTIFY_CLIENT_ID", required=True),
    spotify_client_secret=_get("SPOTIFY_CLIENT_SECRET", required=True),
    instagram_username=_get("INSTAGRAM_USERNAME"),
    instagram_password=_get("INSTAGRAM_PASSWORD"),
    ig_sessionid=_get("IG_SESSIONID"),
    ig_csrftoken=_get("IG_CSRFTOKEN"),
    ig_ds_user_id=_get("IG_DS_USER_ID"),
    bot_api_base_url=_get("BOT_API_BASE_URL", ""),
    download_dir=Path(_get("DOWNLOAD_DIR", "./downloads")).resolve(),
    max_upload_mb=int(_get("MAX_UPLOAD_MB", "50")),
    yt_cookies_file=_get("YT_COOKIES_FILE", ""),
    log_level=_get("LOG_LEVEL", "INFO"),
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
