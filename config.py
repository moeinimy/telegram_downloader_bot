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

    # Logging
    log_level: str

    @property
    def has_instagram_session(self) -> bool:
        return bool(self.ig_sessionid and self.instagram_username)


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
    instagram_username=_get("INSTAGRAM_USERNAME"),
    ig_sessionid=_get("IG_SESSIONID"),
    ig_csrftoken=_get("IG_CSRFTOKEN"),
    ig_ds_user_id=_get("IG_DS_USER_ID"),
    bot_api_base_url=_get("BOT_API_BASE_URL"),
    download_dir=Path(_get("DOWNLOAD_DIR", "./downloads")).resolve(),
    max_upload_mb=int(_get("MAX_UPLOAD_MB", "50") or 50),
    yt_cookies_file=_cookies_path(),
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
