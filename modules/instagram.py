"""
Instagram module.

Two paths, picked automatically:

  1. No session cookies configured (the simple, account-free default):
     everything goes straight through yt-dlp, which handles reels, video
     posts and most photo posts anonymously. Stories are not reachable
     without an account and return a clear message.

  2. IG_SESSIONID + INSTAGRAM_USERNAME set: instaloader is tried first
     (it also covers stories and multi-photo carousels), with the yt-dlp
     path as a fallback.

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

from config import settings
from utils.helpers import run_in_thread, safe_filename

log = logging.getLogger(__name__)


# Global pacing: Instagram flags accounts that fire requests back-to-back.
# Only relevant on the logged-in path; anonymous yt-dlp calls are not paced.
_ig_gate = threading.Lock()
_last_op = 0.0
_MIN_INTERVAL = 20.0  # seconds

_NO_SESSION_MSG = (
    "استوری بدون اکانت اینستاگرام قابل دانلود نیست. "
    "اگه لازمش داری، کوکی‌های یه اکانت یه‌بارمصرف رو تو .env ست کن "
    "(IG_SESSIONID / IG_CSRFTOKEN / IG_DS_USER_ID / INSTAGRAM_USERNAME)."
)


def _throttle() -> None:
    global _last_op
    with _ig_gate:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_op)
        if wait > 0:
            time.sleep(wait)
        _last_op = time.monotonic()


def _friendly_error(e: Exception) -> RuntimeError:
    """
    Turn an extractor error into something the user can act on.

    Instagram's own messages are long, English and full of yt-dlp flags that
    mean nothing to someone in a chat window, so anything left unmapped used
    to be dumped verbatim. Whether session cookies are currently configured
    changes the advice completely, so it is part of every answer.
    """
    s = str(e)
    low = s.lower()
    have_session = settings.has_instagram_session

    if "feedback_required" in low:
        return RuntimeError(
            "اینستاگرام اکانت رو موقتا محدود کرده. تو مرورگر وارد اکانت شو، "
            "اگه پیام تایید اومد تاییدش کن و چند ساعت بعد دوباره امتحان کن."
        )
    if "429" in s or "too many requests" in low:
        return RuntimeError("اینستاگرام ریت‌لیمیت کرده. حدود یک ساعت صبر کن و دوباره امتحان کن.")

    # The session is present but Instagram rejected it.
    if any(k in low for k in ("login_required", "checkpoint_required", "401", "not logged in")):
        return RuntimeError(
            "سشن اینستاگرام دیگه معتبر نیست. کوکی‌ها رو از مرورگر دوباره بگیر "
            "و تو سرور با «botctl → گزینه ۱۰» آپدیتشون کن."
        )

    # Instagram increasingly refuses anonymous access. This is the single most
    # common failure, and the raw text used to reach the user unchanged.
    if any(k in low for k in (
        "empty media response", "no video formats", "unsupported url",
        "requested content is not available", "login required",
        "you need to log in", "rate-limit reached",
    )):
        if have_session:
            return RuntimeError(
                "اینستاگرام این پست رو نداد. معمولا یعنی کوکی‌های اکانت منقضی شدن "
                "یا پست خصوصی/حذف شده‌ست.\n\n"
                "کوکی‌های تازه بگیر و با «botctl → گزینه ۱۰» ست کن."
            )
        return RuntimeError(
            "اینستاگرام این پست رو بدون لاگین نمی‌ده.\n\n"
            "اینستاگرام دسترسی بدون اکانت رو بسته؛ برای دانلود باید کوکی‌های "
            "یه اکانت یه‌بارمصرف تو سرور ست بشه:\n"
            "botctl → گزینه ۱۰"
        )

    if "private" in low or "not available" in low:
        return RuntimeError("این پست خصوصیه یا حذف شده.")

    return RuntimeError(f"Instagram: {s}")


@run_in_thread
def check_session() -> tuple[bool, str]:
    """
    Report whether Instagram access is currently working.

    Returns (ok, human readable detail). Used by the admin /igcheck command so
    a dead session can be spotted directly instead of being inferred from a
    failed download.
    """
    if not settings.has_instagram_session:
        return False, (
            "کوکی ست نشده — فقط حالت بدون اکانت، که اینستاگرام بیشترش رو بسته.\n"
            "botctl → گزینه ۱۰"
        )
    try:
        import instaloader

        L = _loader_instance()
        if not L.context.is_logged_in:
            return False, "کوکی هست ولی اینستاگرام لاگین رو قبول نکرده (منقضی شده)."
        # Cheapest authenticated call available.
        profile = instaloader.Profile.from_username(L.context, settings.instagram_username)
        return True, f"سشن سالمه — وارد شده به عنوان @{profile.username}"
    except Exception as e:
        return False, f"سشن مشکل داره: {_friendly_error(e)}"


def _session_cookies() -> dict[str, str]:
    cookies = {}
    if settings.ig_sessionid:
        cookies["sessionid"] = settings.ig_sessionid
    if settings.ig_csrftoken:
        cookies["csrftoken"] = settings.ig_csrftoken
    if settings.ig_ds_user_id:
        cookies["ds_user_id"] = settings.ig_ds_user_id
    return cookies


# ---------------- instaloader (only when a session is configured) ----------------

_loader = None


def _new_loader():
    import instaloader

    class _FastFailRateController(instaloader.RateController):
        """instaloader's default behaviour on HTTP 429 is to sleep for up to
        30 minutes inside the worker thread, freezing that task. Fail fast
        instead so the user gets an error and the bot stays usable."""

        def sleep(self, secs: float) -> None:
            if secs > 90:
                raise instaloader.exceptions.AbortDownloadException(
                    f"Instagram rate-limited us (asked to wait {int(secs)}s)."
                )
            time.sleep(secs)

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

    try:
        loader.load_session(settings.instagram_username, _session_cookies())
        log.info("Instagram: loaded browser session for %s", settings.instagram_username)
    except Exception as e:
        log.warning("Instagram session-cookie load failed: %s", e)

    return loader


def _loader_instance():
    global _loader
    if _loader is None:
        _loader = _new_loader()
    return _loader


# ---------------- public API ----------------

@run_in_thread(heavy=True)
def fetch_post(shortcode: str) -> list[Path]:
    """Single post / reel / carousel — returns all media files in order."""
    target = settings.download_dir / "instagram" / shortcode

    # Account-free default: yt-dlp only. Never touch instaloader, which would
    # burn anonymous GraphQL requests and get the IP rate-limited for nothing.
    if not settings.has_instagram_session:
        try:
            return _ytdlp_fetch(shortcode, target)
        except Exception as e:
            raise _friendly_error(e) from e

    _throttle()
    try:
        import instaloader

        L = _loader_instance()
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        target.mkdir(parents=True, exist_ok=True)
        L.download_post(post, target=str(target))
        return _collect_media(target)
    except Exception as e:
        log.warning("instaloader failed for %s (%s) — trying yt-dlp fallback.", shortcode, e)
        try:
            return _ytdlp_fetch(shortcode, target)
        except Exception:
            # yt-dlp mostly handles video; for photo/carousel posts the
            # original instaloader error is the meaningful one.
            raise _friendly_error(e) from e


@run_in_thread
def fetch_profile_pic(username: str) -> Path:
    target = settings.download_dir / "instagram" / f"profile_{username}"
    target.mkdir(parents=True, exist_ok=True)
    out = target / f"{safe_filename(username)}_pp.jpg"

    if settings.has_instagram_session:
        _throttle()
        try:
            import instaloader

            L = _loader_instance()
            profile = instaloader.Profile.from_username(L.context, username)
            L.context.get_and_write_raw(profile.profile_pic_url, str(out))
            return out
        except Exception as e:
            log.warning("instaloader profile pic failed (%s) — trying anonymous.", e)

    try:
        return _anonymous_profile_pic(username, out)
    except Exception as e:
        raise _friendly_error(e) from e


@run_in_thread(heavy=True)
def fetch_story(username: str) -> list[Path]:
    """Requires a logged-in session. Downloads ALL active story items."""
    if not settings.has_instagram_session:
        raise RuntimeError(_NO_SESSION_MSG)

    _throttle()
    import instaloader

    L = _loader_instance()
    if not L.context.is_logged_in:
        raise RuntimeError(_NO_SESSION_MSG)
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


# ---------------- anonymous (yt-dlp) path ----------------

def _instagram_cookiefile() -> str | None:
    """Write a Netscape cookies.txt from the IG session cookies in .env so
    yt-dlp can authenticate the same way instaloader does. Returns the path
    or None if no session cookies are configured."""
    cookies = _session_cookies()
    if not cookies:
        return None
    path = settings.download_dir / "instagram" / "ig_cookies.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    expiry = 2000000000  # far future; IG session cookies are long-lived
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in cookies.items():
        lines.append(f".instagram.com\tTRUE\t/\tTRUE\t{expiry}\t{name}\t{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _ytdlp_fetch(shortcode: str, target: Path) -> list[Path]:
    """Download a post/reel with yt-dlp. Works anonymously for reels, video
    posts and single photos; multi-photo carousels are hit-or-miss."""
    from yt_dlp import YoutubeDL

    target.mkdir(parents=True, exist_ok=True)
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 3,
        "outtmpl": str(target / "%(id)s_%(autonumber)s.%(ext)s"),
        "merge_output_format": "mp4",
    }
    cookiefile = _instagram_cookiefile()
    if cookiefile:
        opts["cookiefile"] = cookiefile

    # /p/ and /reel/ are interchangeable server-side, but the extractor picks
    # a different code path for each, so try both before giving up.
    last: Exception | None = None
    for kind in ("p", "reel"):
        try:
            with YoutubeDL(opts) as ydl:
                ydl.download([f"https://www.instagram.com/{kind}/{shortcode}/"])
            return _collect_media(target)
        except Exception as e:
            last = e
            log.warning("yt-dlp /%s/ failed for %s: %s", kind, shortcode, e)

    raise last or RuntimeError("yt-dlp could not fetch the post")


def _anonymous_profile_pic(username: str, out: Path) -> Path:
    """Profile pictures are available from unauthenticated mirrors."""
    import httpx

    url = f"https://unavatar.io/instagram/{username}?fallback=false"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        if not r.headers.get("content-type", "").startswith("image"):
            raise RuntimeError("عکس پروفایل رو پیدا نکردم.")
        out.write_bytes(r.content)
    return out


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
