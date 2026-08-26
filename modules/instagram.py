"""
Instagram module.

Cookie-free by default. Four independent anonymous routes are tried in
order - Instagram's own GraphQL endpoint, the mobile media-info API, the
public embed page, then yt-dlp - because Instagram has been closing these
one at a time and which of them still answers depends on the requesting IP.
A single method means the feature dies the moment that one is throttled;
diagnose() reports which ones work from the host actually running the bot.

Session cookies remain optional. When IG_SESSIONID + INSTAGRAM_USERNAME are
set, instaloader is tried first (it also covers stories and multi-photo
carousels) and the anonymous routes act as the fallback. Stories are the one
thing that genuinely cannot work without an account.

Public coroutines:
  - fetch_post(shortcode)        -> list[Path]
  - fetch_profile_pic(username)  -> Path
  - fetch_story(username)        -> list[Path]  (requires session)
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
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
    "استوری بدون اکانت اینستاگرام قابل دانلود نیست.\n\n"
    "دو راه داره:\n"
    "• «botctl igdirect» و گزینه ۲ — یه اکانت کامل لاگین می‌کنه و بهترین گزینه‌ست\n"
    "• یا کوکی یه اکانت یه‌بارمصرف تو .env "
    "(IG_SESSIONID / IG_CSRFTOKEN / IG_DS_USER_ID / INSTAGRAM_USERNAME)"
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

    # The common case. Note this is per POST, not a blanket block: most posts
    # download fine anonymously, and only ones Instagram marks restricted
    # (age-gated, "sensitive", or region-limited) demand a login. Saying
    # "anonymous access is closed" here was simply wrong.
    if any(k in low for k in (
        _HIDDEN, "empty media response", "no video formats", "unsupported url",
        "requested content is not available", "login required",
        "you need to log in", "rate-limit reached", "no media",
    )):
        if have_session:
            return RuntimeError(
                "اینستاگرام این پست رو نداد. معمولا یعنی کوکی‌های اکانت منقضی شدن "
                "یا پست خصوصی/حذف شده‌ست.\n\n"
                "کوکی‌های تازه بگیر و با «botctl → گزینه ۱۰» ست کن."
            )
        return RuntimeError(
            "😕 این پست رو نتونستم بگیرم.\n\n"
            "خود اینستاگرام بعضی پست‌ها رو محدود می‌کنه (محدودیت سنی، "
            "علامت «حساس»، یا اکانت خصوصی) و اونا رو فقط به کاربر لاگین‌کرده نشون می‌ده.\n\n"
            "یه پست دیگه امتحان کن — معمولا مشکلی پیش نمیاد."
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
            return _anonymous_fetch(shortcode, target)
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
        log.warning("instaloader failed for %s (%s) — trying anonymous routes.", shortcode, e)
        try:
            return _anonymous_fetch(shortcode, target)
        except Exception:
            # yt-dlp mostly handles video; for photo/carousel posts the
            # original instaloader error is the meaningful one.
            raise _friendly_error(e) from e


@run_in_thread(heavy=True)
def fetch_direct_url(url: str, name: str) -> list[Path]:
    """Fetch whatever url a DM attachment handed us.

    The last resort for a share that carries no permalink and no media id.
    Two things can come back and only one of them is media:

    * A signed CDN url pointing at the file. Saved directly - these expire
      within minutes, which is why this runs the moment the message arrives.
    * An instagram.com PAGE. Cross-app shares routinely carry a page link
      where a video url is expected, and downloading it produced 609KB of
      HTML that reached the user as a broken file. The page names the post,
      though, so the shortcode is recovered from it and the normal route
      ladder - yt-dlp included - takes over.
    """
    from utils import http

    target = settings.download_dir / "instagram" / f"dm_{safe_filename(name)}"

    r = http.get(url, headers={"User-Agent": _WEB_UA, "Referer": "https://www.instagram.com/"})
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    ext = _sniff_ext(r.content, url, ctype)

    if ext != ".bin":
        target.mkdir(parents=True, exist_ok=True)
        dest = target / f"00{ext}"
        dest.write_bytes(r.content)
        return [dest]

    shortcode = _shortcode_from_html(r.text) if "html" in ctype.lower() else ""
    if shortcode:
        log.info("instagram: DM url was a page - recovered shortcode %s", shortcode)
        try:
            return _anonymous_fetch(shortcode, target)
        except Exception as e:
            raise _friendly_error(e) from e

    raise _friendly_error(RuntimeError(
        f"the shared link returned {ctype or 'no content-type'} "
        f"({len(r.content)} bytes) and named no post"
    ))


_HTML_SHORTCODE_RES = (
    # A permalink in the markup is unambiguous, so it goes first. The bare
    # "shortcode" field is a weaker signal - plenty of unrelated json on an
    # Instagram page has a "code" key - and is only consulted after it.
    re.compile(r"instagram\.com/(?:reels?|p|tv)/([A-Za-z0-9_-]{5,})", re.I),
    re.compile(r'"shortcode"\s*:\s*"([A-Za-z0-9_-]{5,})"'),
)


def _shortcode_from_html(html: str) -> str:
    for pattern in _HTML_SHORTCODE_RES:
        found = pattern.search(html)
        if found:
            return found.group(1)
    return ""


@run_in_thread
def fetch_profile_pic(username: str) -> Path:
    target = settings.download_dir / "instagram" / f"profile_{username}"
    target.mkdir(parents=True, exist_ok=True)
    out = target / f"{safe_filename(username)}_pp.jpg"

    # The web session first. It is the one credential an operator can
    # actually obtain - a sessionid copied out of a browser - and it is the
    # only route left: the anonymous mirror this used to fall back on now
    # requires a paid plan, and Instagram answers a logged-out profile
    # lookup with 429.
    from modules import ig_stories

    if ig_stories.usable():
        try:
            return _download_urls([ig_stories.profile_pic_url(username)], target)[0]
        except Exception as e:
            log.warning("web-session profile pic failed for %s (%s) - "
                        "trying the other routes.", username, e)

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


def _story_urls_web(username: str) -> list[str]:
    """Active stories over the browser session.

    Tried before instagrapi because of which credential each one accepts. A
    sessionid copied out of a browser is issued to the WEB api; the mobile
    api refuses it permanently. Stories used to be wired only to the mobile
    side, so the cookie the operator had could never reach them - both
    "options" the failure message offered led to the same wall.
    """
    from modules import ig_stories

    if not ig_stories.usable():
        return []
    try:
        urls, user = ig_stories.story_urls(username)
        if not urls and user.get("is_private"):
            raise RuntimeError(
                f"«{username}» پیجش خصوصیه و اکانت بات فالوش نمی‌کنه، "
                "برای همین استوری‌هاش دیده نمی‌شن."
            )
        return urls
    except RuntimeError:
        raise
    except Exception as e:
        log.info("instagram: web story fetch failed for %s: %s", username, e)
        return []


def _story_urls_private(username: str) -> list[str]:
    """Active stories via the logged-in account, or [] if that is not set up."""
    from modules import ig_private

    if not ig_private.usable():
        return []
    try:
        client = ig_private.client()
        user_id = client.user_id_from_username(username)
        items = client.user_stories(user_id)
    except Exception as e:
        log.info("instagram: private story fetch failed for %s: %s", username, e)
        return []

    urls: list[str] = []
    for item in items:
        url = getattr(item, "video_url", None) or getattr(item, "thumbnail_url", None)
        if url:
            urls.append(str(url))
    return urls


@run_in_thread(heavy=True)
def fetch_story(username: str) -> list[Path]:
    """All active story items. Needs an account - stories are the one thing
    that genuinely cannot be reached logged out.

    The Instagram Direct account is tried first when it exists: it is a real
    logged-in session rather than scraped browser cookies, so it does not go
    stale the way IG_SESSIONID does.
    """
    target = settings.download_dir / "instagram" / f"stories_{safe_filename(username)}"

    urls = _story_urls_web(username)
    if urls:
        log.info("instagram: %d story item(s) for %s via the web session",
                 len(urls), username)
        return _download_urls(urls, target)

    urls = _story_urls_private(username)
    if urls:
        log.info("instagram: %d story item(s) for %s via the logged-in account", len(urls), username)
        return _download_urls(urls, target)

    if not settings.has_instagram_session:
        raise RuntimeError(_NO_SESSION_MSG)

    _throttle()
    import instaloader

    L = _loader_instance()
    if not L.context.is_logged_in:
        raise RuntimeError(_NO_SESSION_MSG)
    try:
        profile = instaloader.Profile.from_username(L.context, username)
        target.mkdir(parents=True, exist_ok=True)
        for story in L.get_stories(userids=[profile.userid]):
            for item in story.get_items():
                L.download_storyitem(item, target=str(target))
        return _collect_media(target)
    except FileNotFoundError:
        raise RuntimeError("این کاربر الان استوری فعالی نداره.")
    except Exception as e:
        raise _friendly_error(e) from e


@run_in_thread(heavy=True)
def list_highlights(username: str) -> list[dict]:
    """The highlight covers on a profile, for the menu to offer.

    Separate from fetching one because a profile can hold dozens and nobody
    asked for all of them - the menu is the point.
    """
    from modules import ig_stories

    if not ig_stories.usable():
        raise RuntimeError(_NO_SESSION_MSG)
    trays, user = ig_stories.highlights(username)
    if not trays:
        if user.get("is_private"):
            raise RuntimeError(
                f"«{username}» پیجش خصوصیه و اکانت بات فالوش نمی‌کنه."
            )
        raise RuntimeError(f"«{username}» هایلایتی نداره.")
    return trays


@run_in_thread(heavy=True)
def fetch_highlight(username: str, highlight_id: str) -> list[Path]:
    """Every item inside one highlight."""
    from modules import ig_stories

    if not ig_stories.usable():
        raise RuntimeError(_NO_SESSION_MSG)
    urls = ig_stories.highlight_urls(highlight_id, username)
    if not urls:
        raise RuntimeError("این هایلایت خالیه یا دیگه در دسترس نیست.")
    target = (settings.download_dir / "instagram" /
              f"hl_{safe_filename(username)}_{safe_filename(highlight_id)}")
    return _download_urls(urls, target)


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
    # This file IS the session. Every other secret this bot writes is 0600;
    # this one was left at whatever the umask gave it, which on a normal
    # server is world-readable - so anyone with a shell on the box could
    # lift the account out of it.
    path.chmod(0o600)
    return str(path)


# --------------------------------------------------------------------------
# Cookie-free strategies.
#
# Instagram has been closing anonymous access endpoint by endpoint, and which
# ones still answer depends heavily on the requesting IP: a residential or
# clean server address often still gets a reply where a flagged one gets 403.
# Relying on a single method therefore means the whole feature dies the moment
# that one is throttled. Four independent paths are tried in order, cheapest
# first, and diagnose() reports which of them actually work from this host.
# --------------------------------------------------------------------------

_APP_ID = "936619743392459"  # public web-client id, sent by instagram.com itself
_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_SHORTCODE_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _shortcode_to_media_id(shortcode: str) -> int:
    """Instagram shortcodes are the media id in base64url."""
    n = 0
    for ch in shortcode.split("?")[0]:
        n = n * 64 + _SHORTCODE_ALPHABET.index(ch)
    return n


def media_id_to_shortcode(media_id: str) -> str:
    """The inverse of _shortcode_to_media_id.

    Needed by the DM bridge: Meta's ig_reel attachment identifies the reel by
    numeric media id and gives no permalink, but every route in this module is
    addressed by shortcode. Ids arrive either bare or as "<pk>_<owner_id>";
    only the pk is encoded in the shortcode.
    """
    raw = str(media_id or "").split("_")[0].strip()
    if not raw.isdigit():
        return ""
    n = int(raw)
    if n <= 0:
        return ""
    out: list[str] = []
    while n:
        n, rem = divmod(n, 64)
        out.append(_SHORTCODE_ALPHABET[rem])
    return "".join(reversed(out))


def _media_urls_from_node(node: dict) -> list[str]:
    """Pull every media URL out of a GraphQL media node, carousel included."""
    out: list[str] = []

    def one(n: dict) -> None:
        if n.get("is_video") and n.get("video_url"):
            out.append(n["video_url"])
        elif n.get("display_url"):
            out.append(n["display_url"])

    children = ((node.get("edge_sidecar_to_children") or {}).get("edges")) or []
    if children:
        for edge in children:
            one(edge.get("node") or {})
    else:
        one(node)
    return [u for u in out if u]


# Filled in by the probes so the diagnostic can say WHY a route returned
# nothing - "0 media" alone cannot distinguish "Instagram refused us" from
# "our parsing is wrong", and those need different fixes.
_last_reason: dict[str, str] = {}

# Instagram answered "this exists but you may not see it": a null media node
# with status ok and no errors. Distinct from being blocked or throttled.
_HIDDEN = "not-visible-logged-out"


# Verified working: this pair is what instagram.com's own web player sends.
# The others are kept as backups because Instagram rotates them.
_DOC_IDS = ("10015901848480474", "8845758582119845", "9510064595728286")

_boot_at = 0.0
_BOOT_TTL = 1800.0


def _ensure_anon_cookies() -> str:
    """
    Fetch instagram.com once so the client holds csrftoken/mid, exactly as a
    browser does before any XHR.

    This is the difference between the GraphQL endpoint answering 403 HTML and
    answering JSON: without these cookies every anonymous call is rejected out
    of hand, which is why the endpoint looked permanently closed.
    """
    global _boot_at

    from utils import http

    client = http.client()
    csrf = client.cookies.get("csrftoken")
    if csrf and (time.time() - _boot_at) < _BOOT_TTL:
        return csrf
    try:
        client.get("https://www.instagram.com/", headers={"User-Agent": _WEB_UA})
        _boot_at = time.time()
    except Exception as e:
        log.info("instagram cookie bootstrap failed: %s", e)
    return client.cookies.get("csrftoken") or ""


# --------------------------------------------------------------------------
# Post metadata
#
# Everything the post menu offers - caption, stats, quality list, direct links
# - comes out of one media-info call. i.instagram.com/api/v1/media/<id>/info/
# and the authenticated client return the SAME item shape, so one parser
# serves both and the logged-in path is just a better-authorised caller.
# --------------------------------------------------------------------------

@dataclass
class PostInfo:
    shortcode: str
    username: str = ""
    caption: str = ""
    likes: int = 0
    comments: int = 0
    views: int = 0
    taken_at: float = 0.0
    duration: float = 0.0
    is_video: bool = False
    # (label, url), best first. Empty for a photo-only post.
    qualities: list[tuple[str, str]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    @property
    def permalink(self) -> str:
        kind = "reel" if self.is_video else "p"
        return f"https://www.instagram.com/{kind}/{self.shortcode}/"


def _parse_media_item(item: dict, shortcode: str) -> PostInfo:
    caption = ((item.get("caption") or {}) or {}).get("text") or ""
    user = item.get("user") or {}

    def versions(node: dict) -> list[tuple[str, str]]:
        out = []
        for v in node.get("video_versions") or []:
            url = v.get("url")
            if url:
                height = v.get("height") or 0
                out.append((f"{height}p" if height else "?", url))
        # Instagram lists these best-first already, but it has not always.
        out.sort(key=lambda pair: int(pair[0][:-1] or 0) if pair[0].endswith("p") else 0,
                 reverse=True)
        return out

    def stills(node: dict) -> list[str]:
        candidates = ((node.get("image_versions2") or {}).get("candidates")) or []
        return [candidates[0]["url"]] if candidates and candidates[0].get("url") else []

    carousel = item.get("carousel_media") or []
    nodes = carousel or [item]

    urls: list[str] = []
    qualities: list[tuple[str, str]] = []
    for node in nodes:
        node_versions = versions(node)
        if node_versions:
            urls.append(node_versions[0][1])
            if not carousel:
                qualities = node_versions
        else:
            urls += stills(node)

    return PostInfo(
        shortcode=shortcode,
        username=user.get("username") or "",
        caption=caption,
        likes=int(item.get("like_count") or 0),
        comments=int(item.get("comment_count") or 0),
        views=int(item.get("play_count") or item.get("view_count") or 0),
        taken_at=float(item.get("taken_at") or 0),
        duration=float(item.get("video_duration") or 0),
        is_video=bool(qualities or any(n.get("video_versions") for n in nodes)),
        qualities=qualities,
        urls=[u for u in urls if u],
    )


@run_in_thread
def fetch_info(shortcode: str) -> PostInfo:
    """Caption, counters and every available quality for one post.

    Tries the logged-in client first for the same reason the download ladder
    does: logged out, Instagram withholds this for anything it considers
    restricted, and returns a login page rather than an error.
    """
    from modules import ig_private, ig_web

    if ig_web.usable():
        try:
            item = ig_web.media_info_sync(_shortcode_to_media_id(shortcode))
            if item:
                return _parse_media_item(item, shortcode)
        except Exception as e:
            log.info("instagram: web media info failed for %s: %s", shortcode, e)

    if ig_private.usable():
        try:
            client = ig_private.client()
            pk = client.media_pk_from_code(shortcode)
            data = client.private_request(f"media/{pk}/info/")
            items = data.get("items") or []
            if items:
                return _parse_media_item(items[0], shortcode)
        except Exception as e:
            log.info("instagram: authenticated media info failed for %s: %s", shortcode, e)

    from utils import http

    r = http.get(
        f"https://i.instagram.com/api/v1/media/{_shortcode_to_media_id(shortcode)}/info/",
        headers={"User-Agent": "Instagram 219.0.0.12.117 Android", "X-IG-App-ID": _APP_ID},
    )
    if r.status_code != 200:
        raise _friendly_error(RuntimeError(f"HTTP{r.status_code}"))
    items = (r.json().get("items") or [])
    if not items:
        raise _friendly_error(RuntimeError("no media"))
    return _parse_media_item(items[0], shortcode)


@run_in_thread(heavy=True)
def fetch_by_pk(media_pk: str) -> list[Path]:
    """Download by numeric media id, using the logged-in account.

    The exact address, and the only one that works for a story. A story has no
    shortcode - it is not reachable at /p/<code> at all - so deriving one from
    its pk produced a plausible-looking string that could never resolve, and a
    shared story simply failed.

    Posts go through here too when an account is configured: the pk came
    straight off the DM, while a shortcode is something we computed from it.
    """
    from modules import ig_private, ig_web

    pk = str(media_pk).split("_")[0]
    target = settings.download_dir / "instagram" / f"pk_{pk}"

    # The web session first, for the same reason it leads the route ladder:
    # it is the credential that works. A story is served here too, so this
    # usually answers on its own.
    if ig_web.usable():
        try:
            item = ig_web.media_info_sync(pk)
            if item:
                urls = _parse_media_item(item, "").urls
                if urls:
                    return _download_urls(urls, target)
        except Exception as e:
            log.info("instagram: web media_info(%s) failed (%s)", pk, e)

    if not ig_private.usable():
        raise _friendly_error(RuntimeError("no Instagram session could fetch this"))

    client = ig_private.client()

    urls: list[str] = []
    try:
        urls = _urls_from_instagrapi(client.media_info(pk))
    except Exception as e:
        log.info("instagram: media_info(%s) failed (%s) - trying story_info", pk, e)

    if not urls:
        # Stories live behind their own endpoint and expire in 24h.
        try:
            story = client.story_info(pk)
            url = getattr(story, "video_url", None) or getattr(story, "thumbnail_url", None)
            if url:
                urls = [str(url)]
        except Exception as e:
            raise _friendly_error(e) from e

    if not urls:
        raise _friendly_error(RuntimeError("no media"))
    return _download_urls(urls, target)


@run_in_thread(heavy=True)
def fetch_quality(shortcode: str, url: str, label: str) -> Path:
    """One specific video rendition, straight from its CDN url."""
    target = settings.download_dir / "instagram" / shortcode / "q"
    return _download_urls([url], target / safe_filename(label))[0]


@run_in_thread(heavy=True)
def extract_audio(video: Path) -> Path:
    """Strip the audio track out of an already-downloaded video.

    Copies the stream rather than re-encoding whenever the container allows
    it: Instagram audio is AAC, m4a is an AAC container, so the common case
    is a remux that takes milliseconds instead of a full transcode.
    """
    import subprocess

    fmt = settings.audio_format
    out = video.with_name(f"{video.stem}_audio.{'m4a' if fmt == 'm4a' else fmt}")
    if out.exists():
        return out

    codec = ["-c:a", "copy"] if fmt == "m4a" else (
        ["-c:a", "libmp3lame", "-q:a", "2"] if fmt == "mp3" else ["-c:a", "flac"]
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vn", *codec, str(out)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not out.exists():
        raise RuntimeError(f"ffmpeg: {(result.stderr or '')[:120]}")
    return out


def _urls_from_instagrapi(media) -> list[str]:
    """Every downloadable url on an instagrapi Media, carousel included.

    Read through getattr because instagrapi's model shifts between releases
    and a renamed field must degrade to "this route found nothing" rather than
    to an AttributeError that looks like the account is broken.
    """
    out: list[str] = []

    def one(item) -> None:
        url = getattr(item, "video_url", None) or getattr(item, "thumbnail_url", None)
        if url:
            out.append(str(url))

    resources = getattr(media, "resources", None) or []
    if resources:
        for resource in resources:
            one(resource)
    else:
        one(media)
    return [u for u in out if u]


def _try_web_api(shortcode: str) -> list[str]:
    """The logged-in WEB route, using the browser cookies.

    First, because it is the session that actually works: the same cookies
    that read the DM inbox read a post, while the mobile api refuses this
    account entirely. Reading the inbox and then downloading over a refused
    api would have left the feature half working.

    Costs nothing when unconfigured - returns before touching the network.
    """
    from modules import ig_web

    if not ig_web.usable():
        _last_reason["web"] = "no browser cookies"
        return []
    try:
        item = ig_web.media_info_sync(_shortcode_to_media_id(shortcode))
    except Exception as e:
        _last_reason["web"] = str(e)[:80]
        return []
    if not item:
        _last_reason["web"] = "no media in response"
        return []
    return _parse_media_item(item, shortcode).urls


def _try_web_api(shortcode: str) -> list[str]:
    """The logged-in WEB route, using the browser cookies.

    First, because it is the session that actually works: the mobile api
    refuses a browser-issued cookie, so the instagrapi route below fails for
    the same account that reads its inbox fine over the web api. Reading and
    downloading should not disagree about which session to use.
    """
    from modules import ig_web

    if not ig_web.usable():
        _last_reason["web"] = "no cookies configured"
        return []

    try:
        item = ig_web.media_info_sync(_shortcode_to_media_id(shortcode))
    except Exception as e:
        _last_reason["web"] = str(e)[:80]
        return []

    if not item:
        _last_reason["web"] = "no media in response"
        return []
    return _parse_media_item(item, shortcode).urls


def _try_private_api(shortcode: str) -> list[str]:
    """The logged-in route.

    First in the ladder whenever an account is configured, because it is the
    only one that is not guessing: the anonymous endpoints get whatever
    Instagram feels like showing a stranger, which for restricted, age-gated
    or cross-app-shared posts is a login wall - 600KB of HTML with the post
    nowhere in it.

    Costs nothing when unconfigured: it returns before touching the network.
    """
    from modules import ig_private

    if not ig_private.usable():
        _last_reason["private"] = "no account configured"
        return []

    try:
        client = ig_private.client()
    except Exception as e:
        _last_reason["private"] = f"login failed: {str(e)[:70]}"
        return []

    try:
        media = client.media_info(client.media_pk_from_code(shortcode))
    except Exception as e:
        _last_reason["private"] = str(e)[:80]
        return []

    urls = _urls_from_instagrapi(media)
    if not urls:
        _last_reason["private"] = f"media_type={getattr(media, 'media_type', '?')} but no urls"
    return urls


def _try_graphql(shortcode: str) -> list[str]:
    """
    instagram.com/graphql/query, called the way the site's own player does.

    Three details all have to be right or it returns nothing: the anonymous
    cookies from _ensure_anon_cookies, the full variables shape below (a bare
    {"shortcode": ...} yields "execution error"), and a current doc_id.
    """
    import json

    from utils import http

    csrf = _ensure_anon_cookies()
    headers = {
        "User-Agent": _WEB_UA,
        "X-IG-App-ID": _APP_ID,
        "X-ASBD-ID": "129477",
        "X-IG-WWW-Claim": "0",
        "X-CSRFToken": csrf,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": f"https://www.instagram.com/reel/{shortcode}/",
        "Accept": "*/*",
    }
    variables = {
        "shortcode": shortcode,
        "fetch_tagged_user_count": None,
        "hoisted_comment_id": None,
        "hoisted_reply_id": None,
    }

    reasons = []
    hidden = False
    for doc_id in _DOC_IDS:
        r = http.client().post(
            "https://www.instagram.com/graphql/query/",
            data={"doc_id": doc_id, "variables": json.dumps(variables)},
            headers=headers,
        )
        if r.status_code != 200:
            reasons.append(f"{doc_id[:6]}:HTTP{r.status_code}")
            continue
        try:
            payload = r.json()
        except Exception:
            reasons.append(f"{doc_id[:6]}:not-json")
            continue

        data = payload.get("data") or {}
        node = data.get("xdt_shortcode_media") or data.get("shortcode_media")
        if node:
            urls = _media_urls_from_node(node)
            if urls:
                return urls
            reasons.append(f"{doc_id[:6]}:no-urls")
            continue

        # A null node with status ok and no errors is Instagram's way of
        # saying the post exists but is not visible to a logged-out viewer.
        if "xdt_shortcode_media" in data and not payload.get("errors"):
            hidden = True
            reasons.append(f"{doc_id[:6]}:not-visible-logged-out")
            break
        reasons.append(f"{doc_id[:6]}:empty-data")

    # Kept as a stable ASCII marker so callers can branch on it.
    _last_reason["graphql"] = _HIDDEN if hidden else ", ".join(reasons)
    return []


def _try_api_v1(shortcode: str) -> list[str]:
    """i.instagram.com media info, addressed by the numeric media id."""
    from utils import http

    media_id = _shortcode_to_media_id(shortcode)
    r = http.get(
        f"https://i.instagram.com/api/v1/media/{media_id}/info/",
        headers={
            "User-Agent": "Instagram 219.0.0.12.117 Android",
            "X-IG-App-ID": _APP_ID,
        },
    )
    if r.status_code != 200:
        _last_reason["api_v1"] = f"HTTP{r.status_code} {r.text[:60]}"
        return []
    items = (r.json().get("items") or [])
    if not items:
        _last_reason["api_v1"] = "no items"
        return []
    item = items[0]

    def from_item(it: dict) -> str | None:
        vids = it.get("video_versions") or []
        if vids:
            return vids[0].get("url")
        cands = ((it.get("image_versions2") or {}).get("candidates")) or []
        return cands[0].get("url") if cands else None

    carousel = item.get("carousel_media") or []
    urls = [from_item(c) for c in carousel] if carousel else [from_item(item)]
    return [u for u in urls if u]


def _try_embed(shortcode: str) -> list[str]:
    """The public embed page still carries the media URLs on some hosts."""
    import json
    import re

    from utils import http

    r = http.get(
        f"https://www.instagram.com/p/{shortcode}/embed/captioned/",
        headers={"User-Agent": _WEB_UA},
    )
    if r.status_code != 200:
        return []
    body = r.text

    m = re.search(r'"gql_data"\s*:\s*(\{.*?\})\s*,\s*"', body, re.S)
    if m:
        try:
            node = (json.loads(m.group(1)) or {}).get("shortcode_media")
            if node:
                urls = _media_urls_from_node(node)
                if urls:
                    return urls
        except Exception:
            pass

    # Fall back to the raw fields; they appear escaped inside a JS string.
    urls = [
        u.encode().decode("unicode_escape")
        for u in re.findall(r'"video_url":"([^"]+)"', body)
    ] or [
        u.encode().decode("unicode_escape")
        for u in re.findall(r'"display_url":"([^"]+)"', body)
    ]
    return urls


_CTYPE_EXT = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
}


def _sniff_ext(data: bytes, url: str, content_type: str = "") -> str:
    """The real container: magic bytes, then the server's own label, then the
    url as a last resort.

    Guessing from the url alone was wrong twice over. Instagram serves plenty
    of stills as WEBP, and mapping ".webp" in the url to a ".jpg" filename
    made sendPhoto answer IMAGE_PROCESS_FAILED for a file that had downloaded
    perfectly. Worse, a signed CDN url that has expired or been refused comes
    back 200 with an HTML page, and nothing in the url says so.
    """
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    # ISO base media: mp4, m4v and mov all share this box layout. fMP4
    # segments lead with styp rather than ftyp.
    if data[4:8] in (b"ftyp", b"styp"):
        return ".mov" if data[8:12] == b"qt  " else ".mp4"

    label = (content_type or "").split(";")[0].strip().lower()
    if label in _CTYPE_EXT:
        return _CTYPE_EXT[label]

    # A server saying "text/html" outranks a url that ends in .mp4, and that
    # ordering is the whole point: an expired signed CDN url still ends in
    # .mp4 while serving a login page. Falling through to the url here is how
    # a 595KB HTML page became a video file.
    if label and not label.startswith(("image/", "video/", "audio/", "application/octet-stream")):
        return ".bin"

    clean = url.split("?")[0].lower()
    for ext in (".mp4", ".mov", ".jpg", ".jpeg", ".png", ".webp"):
        if clean.endswith(ext):
            return ext
    return ".bin"


def _download_urls(urls: list[str], target: Path) -> list[Path]:
    """Save direct CDN URLs. Much cheaper than a yt-dlp run once we have them."""
    from utils import http

    target.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, url in enumerate(urls):
        r = http.get(url, headers={"User-Agent": _WEB_UA, "Referer": "https://www.instagram.com/"})
        r.raise_for_status()

        ctype = r.headers.get("content-type", "")
        ext = _sniff_ext(r.content, url, ctype)
        if ext == ".bin":
            # 200 OK is not the same as "this is media". A refused or expired
            # signed url comes back as an HTML page with a 200, and saving it
            # produced a 595KB "00.bin" that reached the user as a file.
            #
            # Raising here is the point: _anonymous_fetch catches it and moves
            # on to the next route, so yt-dlp still gets its turn instead of
            # the whole download being declared a success.
            raise RuntimeError(
                f"CDN returned {ctype or 'no content-type'} "
                f"({len(r.content)} bytes starting {r.content[:12]!r}) instead of media"
            )

        dest = target / f"{i:02d}{ext}"
        dest.write_bytes(r.content)
        saved.append(dest)
    if not saved:
        raise FileNotFoundError("no media downloaded")
    return saved


# Which route last produced media. Whichever works on a given server is
# stable for long stretches, so trying the others first only adds latency:
# three futile HTTP calls sat in front of every single download when yt-dlp
# was the one that worked.
_preferred_route: str | None = None

_HTTP_ROUTES = {
    # Logged in first: these are the only routes not asking Instagram what it
    # will show a stranger. Both skip in microseconds when unconfigured.
    #
    # web before private because the browser cookies are the credential that
    # works - the mobile api refuses this account outright.
    "web": _try_web_api,
    "private": _try_private_api,
    "graphql": _try_graphql,
    "api_v1": _try_api_v1,
    "embed": _try_embed,
}


def _route_order() -> list[str]:
    """Known-good route first, then the rest, then yt-dlp."""
    names = list(_HTTP_ROUTES) + ["ytdlp"]
    if _preferred_route in names:
        names.remove(_preferred_route)
        names.insert(0, _preferred_route)
    return names


def _anonymous_fetch(shortcode: str, target: Path) -> list[Path]:
    """Try the cookie-free routes; the first that yields media wins."""
    global _preferred_route

    errors: list[str] = []
    for name in _route_order():
        try:
            if name == "ytdlp":
                files = _ytdlp_fetch(shortcode, target)
                _preferred_route = "ytdlp"
                log.info("instagram: yt-dlp served %d files for %s", len(files), shortcode)
                return files

            urls = _HTTP_ROUTES[name](shortcode)
            if urls:
                # Only after the bytes are on disk. Marking a route preferred
                # for handing back urls it cannot actually serve pinned every
                # later download to the one route that was failing.
                files = _download_urls(urls, target)
                _preferred_route = name
                log.info("instagram: %s served %d media for %s", name, len(urls), shortcode)
                return files
            errors.append(f"{name}: {_last_reason.get(name, 'no media')}")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:90]}")
            log.info("instagram %s failed for %s: %s", name, shortcode, e)

    # When Instagram explicitly said the post is not visible logged-out, that
    # is the answer - do not bury it among the other routes' noise.
    if _last_reason.get("graphql") == _HIDDEN:
        raise RuntimeError(_HIDDEN)
    raise RuntimeError("; ".join(errors) or "no route returned media")


def _probe_routes(shortcode: str) -> dict[str, int]:
    """How many media each cookie-free route yields. -1 means it errored."""
    results: dict[str, int] = {}
    for name, fn in (
        ("web", _try_web_api),
        ("private", _try_private_api),
        ("graphql", _try_graphql),
        ("api_v1", _try_api_v1),
        ("embed", _try_embed),
    ):
        try:
            results[name] = len(fn(shortcode))
        except Exception as e:
            log.info("probe %s failed for %s: %s", name, shortcode, e)
            results[name] = -1
    try:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            results["yt-dlp"] = len(_ytdlp_fetch(shortcode, Path(tmp)))
    except Exception:
        results["yt-dlp"] = -1
    return results


@run_in_thread
def diagnose(shortcode: str | None = None) -> str:
    """
    Report which cookie-free routes work from THIS host, for a specific post
    and for a control post.

    The control matters: without it a single failing post looks identical to
    "anonymous access is blocked for this server", and those need completely
    different answers. Instagram serves most posts to logged-out clients and
    withholds the ones it marks restricted, so the useful question is not
    "does it work" but "does it work for everything, or just not this one".
    """
    # A plain, long-public reel used purely as a control.
    CONTROL = "DZfwtaiob79"
    target = (shortcode or CONTROL).strip()

    def render(sc: str, res: dict[str, int]) -> list[str]:
        out = [f"📄 {sc}"]
        for name, n in res.items():
            if n > 0:
                out.append(f"   ✅ {name}: {n} media")
            else:
                # Why it failed, not just that it did.
                why = _last_reason.get(name, "error" if n < 0 else "no media")
                out.append(f"   ❌ {name}: {why[:70]}")
        return out

    lines: list[str] = []
    target_res = _probe_routes(target)
    lines += render(target, target_res)
    target_ok = any(n > 0 for n in target_res.values())

    control_ok = target_ok
    if target != CONTROL:
        lines.append("")
        control_res = _probe_routes(CONTROL)
        lines += render(CONTROL + "  (کنترل)", control_res)
        control_ok = any(n > 0 for n in control_res.values())

    lines.append("")
    if target_ok:
        lines.append("✅ این پست بدون کوکی قابل دانلوده.")
    elif control_ok:
        lines.append(
            "⚠️ فقط همین پست مشکل داره — سرور مشکلی نداره.\n"
            "این پست محدودیت سنی/حساس یا خصوصیه و اینستاگرام بدون لاگین نمی‌دتش."
        )
    else:
        lines.append(
            "❌ هیچ پستی بدون کوکی نمیاد — اینستاگرام IP این سرور رو محدود کرده.\n"
            "چند ساعت صبر کن، یا کوکی ست کن (botctl → گزینه ۱۰)."
        )

    lines.append("")
    lines.append(
        "کوکی ست شده" if settings.has_instagram_session else "کوکی ست نشده (حالت بدون اکانت)"
    )
    return "\n".join(lines)


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
    """The last resort, and it no longer works for most accounts.

    Kept because it costs one request and occasionally still answers, but it
    is not a route to rely on: unavatar.io moved its Instagram provider
    behind a paid plan and now returns

        403  {"code":"EPRO","message":"This provider requires a pro plan"}

    Checked at the same time: picuki and imginn both 403 this server, and
    Instagram's own web_profile_info answers 429 without a session. There is
    no dependable logged-out way to fetch a profile picture any more, which
    is why the web session is tried first rather than last.
    """
    import httpx

    url = f"https://unavatar.io/instagram/{username}?fallback=false"
    with httpx.Client(timeout=20, follow_redirects=True) as client:
        r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 403 and "EPRO" in r.text:
            raise RuntimeError(
                "عکس پروفایل بدون لاگین دیگه در دسترس نیست — سرویس رایگانی "
                "که استفاده می‌کردیم پولی شد.\n"
                "روی سرور:  botctl igdirect"
            )
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
