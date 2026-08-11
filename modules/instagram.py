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
    """Save a CDN url handed to us by a DM attachment.

    The last resort for a share that carries no permalink and no media id.
    These urls are signed and expire within minutes, which is why this is
    called the moment the message arrives rather than queued.
    """
    target = settings.download_dir / "instagram" / f"dm_{safe_filename(name)}"
    try:
        return _download_urls([url], target)
    except Exception as e:
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


def _sniff_ext(data: bytes, url: str) -> str:
    """The real container, read from the bytes.

    Guessing from the URL was wrong in a way that only showed up at the
    Telegram end: Instagram serves plenty of stills as WEBP, the old guess
    mapped ".webp" in the url to a ".jpg" filename, and sendPhoto answered
    IMAGE_PROCESS_FAILED for a file that had downloaded perfectly. Anything
    unrecognised fell to ".bin" and was then sent as a photo too.
    """
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    # ISO base media: mp4, m4v and mov all share this box layout.
    if data[4:8] == b"ftyp":
        return ".mov" if data[8:12] == b"qt  " else ".mp4"

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
        dest = target / f"{i:02d}{_sniff_ext(r.content, url)}"
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
                _preferred_route = name
                log.info("instagram: %s served %d media for %s", name, len(urls), shortcode)
                return _download_urls(urls, target)
            errors.append(f"{name}: {_last_reason.get(name, 'no media')}")
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}")
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
