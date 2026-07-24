"""
Instagram module — instaloader with browser-session auth, plus a yt-dlp
fallback for reels/video posts when instaloader is rate-limited.

Auth strategy (in order):
  1. IG_SESSIONID (+ optional IG_CSRFTOKEN / IG_DS_USER_ID) from .env —
     cookies copied from a logged-in browser. This avoids the login
     checkpoint Instagram triggers for username/password logins from
     datacenter IPs.
  2. INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD login (legacy, often blocked).
  3. Anonymous (rate-limited hard on datacenter IPs).

Public coroutines:
  - fetch_post(shortcode)        -> list[Path]
  - fetch_profile_pic(username)  -> Path
  - fetch_story(username)        -> list[Path]  (requires session)
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from pathlib import Path

import instaloader

from config import settings
from utils.helpers import run_in_thread, safe_filename

log = logging.getLogger(__name__)


# Global pacing: Instagram flags accounts that fire requests back-to-back.
# Enforce a minimum gap between bot-initiated Instagram operations.
_ig_gate = threading.Lock()
_last_op = 0.0
_MIN_INTERVAL = 20.0  # seconds


def _throttle() -> None:
    global _last_op
    with _ig_gate:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_op)
        if wait > 0:
            time.sleep(wait)
        _last_op = time.monotonic()


def _friendly_error(e: Exception) -> RuntimeError:
    s = str(e)
    if "feedback_required" in s:
        return RuntimeError(
            "اینستاگرام اکانت رو موقتا محدود کرده. تو مرورگر وارد اکانت شو، "
            "اگه پیام تایید اومد تاییدش کن و چند ساعت بعد دوباره امتحان کن."
        )
    if "429" in s or "Too Many Requests" in s:
        return RuntimeError("اینستاگرام ریت‌لیمیت کرده. حدود یک ساعت صبر کن و دوباره امتحان کن.")
    if "login_required" in s.lower() or "401" in s:
        return RuntimeError(
            "سشن اینستاگرام منقضی شده. کوکی‌ها رو از مرورگر دوباره بگیر و تو .env آپدیت کن."
        )
    return RuntimeError(f"Instagram: {s}")


class _FastFailRateController(instaloader.RateController):
    """instaloader's default behaviour on HTTP 429 is to sleep for up to
    30 minutes inside the worker thread, freezing that task. Fail fast
    instead so the user gets an error and the bot stays usable."""

    def sleep(self, secs: float) -> None:
        if secs > 90:
            raise instaloader.exceptions.AbortDownloadException(
                f"Instagram rate-limited us (asked to wait {int(secs)}s). "
                "Try again in ~30 minutes."
            )
        time.sleep(secs)


def _session_cookies() -> dict[str, str]:
    cookies = {}
    if settings.ig_sessionid:
        cookies["sessionid"] = settings.ig_sessionid
    if settings.ig_csrftoken:
        cookies["csrftoken"] = settings.ig_csrftoken
    if settings.ig_ds_user_id:
        cookies["ds_user_id"] = settings.ig_ds_user_id
    return cookies


def _new_loader() -> instaloader.Instaloader:
    loader = instaloader.Instaloader(
        download_pictures=True,
        download_videos=True,
        download_video_thumbnails=False,
        download_comments=False,
        save_metadata=False,
        post_metadata_txt_pattern="",
        dirname_pattern=str(settings.download_dir / "instagram" / "{shortcode}"),
        quiet=True,
        max_connection_attempts=1,
        rate_controller=lambda ctx: _FastFailRateController(ctx),
    )

    cookies = _session_cookies()
    if cookies and settings.instagram_username:
        try:
            loader.load_session(settings.instagram_username, cookies)
            log.info("Instagram: loaded browser session for %s", settings.instagram_username)
            return loader
        except Exception as e:
            log.warning("Instagram session-cookie load failed: %s", e)

    if settings.instagram_username and settings.instagram_password:
        try:
            loader.login(settings.instagram_username, settings.instagram_password)
            log.info("Instagram: password login OK for %s", settings.instagram_username)
        except Exception as e:
            log.warning("Instagram password login failed: %s", e)

    return loader


_loader: instaloader.Instaloader | None = None


def _loader_instance() -> instaloader.Instaloader:
    global _loader
    if _loader is None:
        _loader = _new_loader()
    return _loader


# ---------------- public API ----------------

@run_in_thread
def fetch_post(shortcode: str) -> list[Path]:
    """Single post / reel / carousel — returns all media files in order.
    Falls back to yt-dlp for video content if instaloader is blocked."""
    _throttle()
    target = settings.download_dir / "instagram" / shortcode
    try:
        L = _loader_instance()
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        target.mkdir(parents=True, exist_ok=True)
        L.download_post(post, target=str(target))
        return _collect_media(target)
    except Exception as e:
        log.warning("instaloader failed for %s (%s) — trying yt-dlp fallback.", shortcode, e)
        try:
            return _ytdlp_fallback(shortcode, target)
        except Exception:
            # yt-dlp only handles video; for photo/carousel posts surface
            # the original instaloader error, which is the meaningful one.
            raise _friendly_error(e) from e


@run_in_thread
def fetch_profile_pic(username: str) -> Path:
    _throttle()
    try:
        L = _loader_instance()
        profile = instaloader.Profile.from_username(L.context, username)
        target = settings.download_dir / "instagram" / f"profile_{username}"
        target.mkdir(parents=True, exist_ok=True)
        url = profile.profile_pic_url
        out = target / f"{safe_filename(username)}_pp.jpg"
        L.context.get_and_write_raw(url, str(out))
        return out
    except Exception as e:
        raise _friendly_error(e) from e


@run_in_thread
def fetch_story(username: str) -> list[Path]:
    """Requires a logged-in session. Downloads ALL active story items."""
    _throttle()
    L = _loader_instance()
    if not L.context.is_logged_in:
        raise RuntimeError(
            "سشن اینستاگرام لازمه — IG_SESSIONID رو تو .env ست کن."
        )
    try:
        profile = instaloader.Profile.from_username(L.context, username)
        target = settings.download_dir / "instagram" / f"stories_{username}"
        target.mkdir(parents=True, exist_ok=True)
        for story in L.get_stories(userids=[profile.userid]):
            for item in story.get_items():
                L.download_storyitem(item, target=str(target))
        return _collect_media(target)
    except FileNotFoundError:
        raise RuntimeError("این کاربر الان استوری فعالی نداره.")
    except Exception as e:
        raise _friendly_error(e) from e


# ---------------- yt-dlp fallback (reels / video posts) ----------------

def _instagram_cookiefile() -> str | None:
    """Write a Netscape cookies.txt from the IG session cookies in .env so
    yt-dlp can authenticate the same way instaloader does. Returns the path
    or None if no session cookies are configured."""
    cookies = _session_cookies()
    if not cookies:
        return None
    path = settings.download_dir / "instagram" / "ig_cookies.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    # far-future expiry; Instagram session cookies are long-lived
    expiry = 2000000000
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in cookies.items():
        lines.append(f".instagram.com\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _ytdlp_fallback(shortcode: str, target: Path) -> list[Path]:
    """yt-dlp can often grab reels even when the GraphQL API is
    rate-limiting instaloader. Only works for video content."""
    from yt_dlp import YoutubeDL

    target.mkdir(parents=True, exist_ok=True)
    url = f"https://www.instagram.com/reel/{shortcode}/"
    opts = {
        "quiet": True,
        "socket_timeout": 30,
        "outtmpl": str(target / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
    }
    cookiefile = _instagram_cookiefile()
    if cookiefile:
        opts["cookiefile"] = cookiefile

    with YoutubeDL(opts) as ydl:
        ydl.download([url])
    return _collect_media(target)


# ---------------- internals ----------------

_MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".mp4", ".webp", ".mov"}


def _collect_media(folder: Path) -> list[Path]:
    files = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in _MEDIA_EXTS
    )
    if not files:
        raise FileNotFoundError(f"No media downloaded into {folder}")
    return files


def cleanup(folder: Path) -> None:
    """Remove a download folder after the bot has sent everything to Telegram."""
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
