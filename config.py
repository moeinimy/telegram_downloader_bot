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

    # Instagram Direct: media shared to OUR Instagram DMs comes back to the
    # sharer in Telegram. Two independent ways to read that inbox, tried in
    # the order given by IG_DIRECT_SOURCES:
    #   webhook - Meta's official Instagram Platform API. Needs a public HTTPS
    #             endpoint and, for anyone who is not an app tester, App Review.
    #   poll    - instagrapi logged in as the account. Works for everyone
    #             immediately but violates Instagram's terms, so it is only
    #             woken up when the official path stops answering.
    ig_direct_sources: tuple[str, ...]
    ig_app_id: str
    ig_app_secret: str
    ig_access_token: str
    ig_verify_token: str
    ig_webhook_host: str
    ig_webhook_port: int
    ig_webhook_path: str
    ig_public_url: str
    ig_health_minutes: int
    ig_dm_username: str
    ig_dm_password: str
    # Floats: the poll interval IS the DM latency, so sub-second settings are
    # meaningful here even though nothing else in this file needs them.
    ig_dm_poll_seconds: float
    ig_dm_fast_seconds: float
    ig_dm_fast_window: int

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

    # faster-whisper model for the video-subtitles button. Anything below
    # medium is English-only in practice; see modules/transcribe.py.
    whisper_model: str

    # Audio container for downloaded music: m4a (default), mp3 or flac.
    # NOTE: every source the bot can reach (YouTube, SoundCloud) serves lossy
    # audio, so "flac" produces a much larger file without recovering any
    # detail - it is a lossless container around lossy content.
    audio_format: str

    # Extra music-recognition engines. Shazam always runs and needs nothing;
    # these are optional and tried in the order given by RECOGNITION_ENGINES.
    acoustid_key: str
    audd_token: str
    recognition_engines: tuple[str, ...]

    # Lyrics sources, in priority order. All are free and keyless.
    lyrics_sources: tuple[str, ...]

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

    @property
    def has_ig_webhook(self) -> bool:
        """Every piece the official path needs. The verify token is included
        because without it Meta's subscription handshake can never complete,
        so a half-configured webhook would sit there answering 403 forever."""
        return bool(self.ig_app_secret and self.ig_access_token and self.ig_verify_token)

    @property
    def has_ig_private(self) -> bool:
        return bool(self.ig_dm_username and self.ig_dm_password)

    @property
    def ig_direct_enabled(self) -> bool:
        """True when at least one configured source is also switched on."""
        return bool(
            ("webhook" in self.ig_direct_sources and self.has_ig_webhook)
            or ("poll" in self.ig_direct_sources and self.has_ig_private)
        )


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


def _int(name: str, default: int) -> int:
    """An unparseable number must not stop the bot from starting: the whole
    point of these knobs is that they have working defaults."""
    raw = _get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logging.getLogger(__name__).warning("%s=%r is not a number - using %d", name, raw, default)
        return default


def _float(name: str, default: float) -> float:
    raw = _get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logging.getLogger(__name__).warning("%s=%r is not a number - using %s", name, raw, default)
        return default


def _webhook_path() -> str:
    """Meta stores the callback URL verbatim, so a path that lost its leading
    slash would register as a different endpoint than the one aiohttp serves."""
    path = _get("IG_WEBHOOK_PATH", "/ig/webhook") or "/ig/webhook"
    return path if path.startswith("/") else f"/{path}"


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
    ig_direct_sources=tuple(
        s for s in _get("IG_DIRECT_SOURCES", "webhook,poll").replace(" ", "").lower().split(",") if s
    ),
    ig_app_id=_get("IG_APP_ID"),
    ig_app_secret=_get("IG_APP_SECRET"),
    ig_access_token=_get("IG_ACCESS_TOKEN"),
    ig_verify_token=_get("IG_VERIFY_TOKEN"),
    ig_webhook_host=_get("IG_WEBHOOK_HOST", "127.0.0.1") or "127.0.0.1",
    ig_webhook_port=_int("IG_WEBHOOK_PORT", 8088),
    ig_webhook_path=_webhook_path(),
    ig_public_url=_get("IG_PUBLIC_URL").rstrip("/"),
    ig_health_minutes=_int("IG_HEALTH_MINUTES", 10),
    ig_dm_username=_get("IG_DM_USERNAME"),
    ig_dm_password=_get("IG_DM_PASSWORD"),
    ig_dm_poll_seconds=_float("IG_DM_POLL_SECONDS", 2),
    ig_dm_fast_seconds=_float("IG_DM_FAST_SECONDS", 1),
    ig_dm_fast_window=_int("IG_DM_FAST_WINDOW", 120),
    bot_api_base_url=_get("BOT_API_BASE_URL"),
    download_dir=Path(_get("DOWNLOAD_DIR", "./downloads")).resolve(),
    max_upload_mb=int(_get("MAX_UPLOAD_MB", "50") or 50),
    yt_cookies_file=_cookies_path(),
    whisper_model=(_get("WHISPER_MODEL", "large-v3-turbo").lower() or "large-v3-turbo"),
    audio_format=(_get("AUDIO_FORMAT", "m4a").lower() or "m4a"),
    acoustid_key=_get("ACOUSTID_API_KEY"),
    audd_token=_get("AUDD_API_TOKEN"),
    recognition_engines=tuple(
        e for e in _get("RECOGNITION_ENGINES", "acoustid,audd").replace(" ", "").split(",") if e
    ),
    lyrics_sources=tuple(
        s for s in _get("LYRICS_SOURCES", "lrclib,lyricsovh,genius").replace(" ", "").split(",") if s
    ),
    admin_ids=_admin_ids(),
    required_channel=_channel_name(),
    log_level=_get("LOG_LEVEL", "INFO"),
)

if settings.audio_format not in ("m4a", "mp3", "flac"):
    raise RuntimeError(
        f"AUDIO_FORMAT={settings.audio_format!r} is not one of: m4a, mp3, flac"
    )

_unknown_sources = set(settings.ig_direct_sources) - {"webhook", "poll"}
if _unknown_sources:
    raise RuntimeError(
        f"IG_DIRECT_SOURCES contains unknown source(s): {', '.join(sorted(_unknown_sources))}. "
        "Valid names are: webhook, poll"
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
    # instagrapi logs the full url of every private request at INFO. At a
    # one-second poll that is two lines per second written to journald
    # forever - it buries every other line and costs real disk I/O. Failures
    # still come through at WARNING.
    logging.getLogger("instagrapi").setLevel(logging.WARNING)
    logging.getLogger("private_request").setLevel(logging.WARNING)
    logging.getLogger("public_request").setLevel(logging.WARNING)
