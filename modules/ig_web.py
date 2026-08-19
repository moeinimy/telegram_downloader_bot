"""
The inbox, read through Instagram's WEB api.

Why this exists. The unofficial poller talks to i.instagram.com - the mobile
app's api - and that api expects a session the mobile app created, signed with
a device it has seen before. The credential we actually have is a `sessionid`
cookie taken from a browser. Handing a browser cookie to the mobile api is the
mismatch, and it answers exactly as it did here:

    403 Forbidden  https://i.instagram.com/api/v1/users/<id>/info/
    instagrapi.exceptions.LoginRequired: login_required

No device fingerprint, proxy or fresh cookie fixes that, because none of them
change which api the cookie belongs to.

So this reader uses the api the cookie was issued for:

    https://www.instagram.com/api/v1/direct_v2/inbox/

the same endpoint instagram.com/direct/inbox calls in a browser tab, with the
same headers a browser sends. No login step at all - the cookie IS the
session - so there is no device to be unknown and nothing to sign.

The payload is identical to the mobile one, so modules/ig_items.py parses both
and everything downstream is unchanged.

Needs three cookies, all from the same browser panel:
    sessionid   IG_DM_SESSIONID
    csrftoken   IG_DM_CSRFTOKEN   (sent back as the X-CSRFToken header)
    ds_user_id  IG_DM_DS_USER_ID  (so our own messages can be skipped)
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from config import settings
from modules.ig_direct import DirectMessage, Dispatch, Source
from modules.ig_items import to_direct_message, to_epoch
from utils import proxies
from utils.helpers import run_in_thread

log = logging.getLogger(__name__)

INBOX = "https://www.instagram.com/api/v1/direct_v2/inbox/"

# Message requests, where a first-time sender's DM lands - which is where the
# pairing token arrives from anyone the account does not already have a thread
# with. Worth some effort to get right.
#
# The mobile endpoint answers 404 on the web api, so several spellings are
# tried once and the one that works is remembered. If none do, the check is
# switched off rather than repeated every sweep: it was logging a whole 404
# html page into the journal twice a minute.
PENDING_CANDIDATES = (
    ("https://www.instagram.com/api/v1/direct_v2/pending_inbox/",
     {"visual_message_return_type": "unseen", "persistentBadging": "true",
      "is_prefetching": "false"}),
    (INBOX, {"visual_message_return_type": "unseen", "persistentBadging": "true",
             "folder": "1", "thread_message_limit": 5, "limit": 10}),
    (INBOX, {"visual_message_return_type": "unseen", "persistentBadging": "true",
             "pending": "true", "thread_message_limit": 5, "limit": 10}),
)

# None = not tried yet, False = none of them work, or (url, params).
_pending_route: object = None

# The public web-client id instagram.com sends with its own XHRs. Without it
# these endpoints answer 403 even with a perfectly good cookie.
_APP_ID = "936619743392459"
# The fallback only. Instagram ties a session to the client that created it,
# so the User-Agent that goes with a cookie has to be the one from the browser
# that produced it - otherwise every request is that session appearing on a
# different machine, and sessions do not survive that for long. Sessions here
# have been dying within hours, which is what that looks like.
#
# This default is also a Chrome that stopped being current two years ago, so
# on its own it is a thing to notice rather than a thing to blend in with.
_UA_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _user_agent() -> str:
    return settings.ig_dm_user_agent or _UA_FALLBACK

_task: asyncio.Task | None = None
_seen_after = 0.0
_last_error = ""

# ONE long-lived client, with its cookie jar kept on disk.
#
# Instagram rotates `sessionid` and hands the new value back in Set-Cookie.
# The first version built a fresh httpx.Client per request from the static
# config, so every rotation was discarded and the next call went out with a
# dead cookie - which is why the reader worked, then returned a 608KB login
# page a few requests later.
#
# The same jar also carries csrftoken, mid and ig_did, which the web app
# keeps for the life of a tab.
_COOKIE_PATH = settings.download_dir / "ig_web_cookies.json"
_session = None
_session_lock = threading.Lock()

# instagram.com issues this on a response and expects it echoed on the next
# request. Sending a hardcoded "0" forever both marks the client as not-a-
# browser and, left stale, gets the session invalidated.
_www_claim = "0"


def usable() -> bool:
    """Only the sessionid is strictly required; the rest improve reliability."""
    return bool(settings.ig_dm_sessionid)


def _cookies() -> dict:
    jar = {"sessionid": settings.ig_dm_sessionid}
    if settings.ig_dm_csrftoken:
        jar["csrftoken"] = settings.ig_dm_csrftoken
    if settings.ig_dm_ds_user_id:
        jar["ds_user_id"] = settings.ig_dm_ds_user_id
    return jar


def _headers() -> dict:
    # csrftoken comes from the live jar when Instagram has rotated it; the
    # configured value is only the seed.
    csrf = settings.ig_dm_csrftoken
    if _session is not None:
        csrf = _session.cookies.get("csrftoken") or csrf

    return {
        "User-Agent": _user_agent(),
        "X-IG-App-ID": _APP_ID,
        "X-ASBD-ID": "129477",
        # Echoed from the last response rather than hardcoded.
        "X-IG-WWW-Claim": _www_claim,
        "X-CSRFToken": csrf or "missing",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/direct/inbox/",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    }


# --------------------------------------------------------------------------
# Request accounting
#
# Every number given for "how much traffic does this generate" so far has been
# a model with an assumed hour of daily use. What actually matters is the rate
# this account is really producing, so it is counted rather than predicted.
#
# One counter per hour, kept for a day. Cheap to hold, cheap to persist, and
# it survives the restarts that a day-long picture would otherwise lose.
# --------------------------------------------------------------------------

_RATE_PATH = settings.download_dir / "ig_web_rate.json"
_rate: dict[str, int] = {}
_rate_saved = 0.0

# Not published limits - nobody has those. Anchored on this bot's own history:
# a flat 15s poll was ~5,700/day and the account was actioned within days.
_RATE_BANDS = ((2000, "محتاطانه"), (3500, "متعادل"), (5000, "پرریسک"))


def _count_request() -> None:
    global _rate_saved

    hour = str(int(time.time()) // 3600)
    _rate[hour] = _rate.get(hour, 0) + 1

    cutoff = int(time.time()) // 3600 - 24
    for key in [k for k in _rate if int(k) < cutoff]:
        _rate.pop(key, None)

    # At most once a minute: this runs on every request and the point is to
    # measure the load, not add to it.
    if time.time() - _rate_saved > 60:
        _rate_saved = time.time()
        try:
            _RATE_PATH.write_text(json.dumps(_rate), encoding="utf-8")
        except Exception:
            pass


def _load_rate() -> None:
    try:
        stored = json.loads(_RATE_PATH.read_text(encoding="utf-8"))
        cutoff = int(time.time()) // 3600 - 24
        _rate.update({k: int(v) for k, v in stored.items() if int(k) >= cutoff})
    except Exception:
        pass


def rate() -> dict:
    """What this account is actually doing, and what that means."""
    if not _rate:
        _load_rate()

    now_hour = int(time.time()) // 3600
    last_hour = _rate.get(str(now_hour), 0) + _rate.get(str(now_hour - 1), 0)
    day = sum(_rate.values())
    hours = max(1, len(_rate))

    # A partial day extrapolates; a full one speaks for itself.
    projected = day if hours >= 24 else round(day / hours * 24)

    verdict = "بن‌آور"
    for ceiling, label in _RATE_BANDS:
        if projected < ceiling:
            verdict = label
            break

    return {
        "last_hour": last_hour,
        "day": day,
        "hours_measured": hours,
        "projected": projected,
        "verdict": verdict,
    }


def _short(text: str, limit: int = 120) -> str:
    """One line, bounded.

    Instagram answers errors with a full html page. Interpolated raw it goes
    into the journal as dozens of lines, twice a minute, burying everything
    else - which is what a 404 on the pending inbox was doing.
    """
    return " ".join((text or "").split())[:limit]


def _load_cookies() -> dict:
    """The jar as last saved, unless .env has been given a newer seed.

    Stored values normally win - they are the ones Instagram rotated to, and
    .env only holds what was first pasted in. But that rule had a hole: after
    a session died, pasting a FRESH sessionid into .env changed nothing,
    because the dead stored cookie kept overriding it. Every retry used the
    cookie that had already stopped working, and the bot reported "expired -
    get a fresh one" to somebody who just had.

    So the seed the jar was built from is recorded with it. A different seed
    means a human typed a new one, and that always wins.
    """
    jar = dict(_cookies())
    try:
        stored = json.loads(_COOKIE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return jar
    except Exception as e:
        log.warning("ig web: cookie store unreadable (%s) - using .env", e)
        return jar

    if stored.get("_seed") and stored["_seed"] != settings.ig_dm_sessionid:
        log.info("ig web: IG_DM_SESSIONID changed - discarding the stored jar")
        return jar

    jar.update({k: v for k, v in stored.items() if v and not k.startswith("_")})
    return jar


def _save_cookies(client) -> None:
    try:
        jar = {k: v for k, v in client.cookies.items() if v}
        if not jar.get("sessionid"):
            return  # never overwrite a good jar with a logged-out one
        # Which .env value this jar grew from, so a newly pasted one can be
        # told apart from a rotation and take precedence.
        jar["_seed"] = settings.ig_dm_sessionid
        tmp = _COOKIE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(jar), encoding="utf-8")
        tmp.replace(_COOKIE_PATH)
        _COOKIE_PATH.chmod(0o600)
    except Exception as e:
        log.warning("ig web: could not persist cookies (%s)", e)


def _proxy_url() -> str | None:
    """The proxy in the spelling httpx accepts. See utils/proxies.py."""
    return proxies.normalize(settings.ig_dm_proxy)


def _client():
    """The one shared session. Built once, kept for the life of the process."""
    global _session

    with _session_lock:
        if _session is not None:
            return _session

        import httpx

        proxy = _proxy_url()
        # httpx names it `proxy` from 0.26 and `proxies` before that.
        try:
            _session = httpx.Client(timeout=25, follow_redirects=True, proxy=proxy)
        except TypeError:
            _session = httpx.Client(timeout=25, follow_redirects=True, proxies=proxy)

        for name, value in _load_cookies().items():
            _session.cookies.set(name, value, domain=".instagram.com")
        return _session


def reset_client() -> None:
    """Drop the session so new cookies or a new proxy take effect."""
    global _session

    with _session_lock:
        if _session is not None:
            try:
                _session.close()
            except Exception:
                pass
        _session = None


def _get(url: str, params: dict) -> dict:
    global _www_claim

    client = _client()
    _count_request()
    response = client.get(url, params=params, headers=_headers())

    # Instagram hands back a rotated claim and, periodically, a rotated
    # sessionid. Both have to be kept or the session dies a few calls later.
    claim = response.headers.get("x-ig-set-www-claim")
    if claim:
        _www_claim = claim
    _save_cookies(client)

    if response.status_code == 404:
        raise LookupError(f"HTTP 404: {_short(response.text)}")
    if response.status_code == 401:
        raise RuntimeError("sessionid رد شد (401) - کوکی منقضی شده، یه تازه بگیر")
    if response.status_code == 403:
        raise RuntimeError(
            "403 from the web api - the cookie is not accepted from this address"
        )
    if _is_checkpoint(response.text):
        raise CheckpointRequired(_short(response.text))
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {_short(response.text)}")

    try:
        return response.json()
    except Exception:
        # A login wall is html, and it comes back with a 200.
        raise RuntimeError(f"not json ({len(response.content)} bytes) - probably a login page")


# How often the message-requests folder is worth a request of its own.
#
# Every sweep was reading TWO endpoints - the inbox and the pending folder -
# so the measured request rate was exactly double what the poll interval
# suggests, and every calculation about safe intervals was out by a factor of
# two. On a busy day that is ~4,500 requests where ~2,250 was intended.
#
# The pending folder only ever holds something for an account we have never
# had a thread with, which in practice means one thing: somebody redeeming a
# pairing token. Anyone already paired arrives in the ordinary inbox. So it is
# read on the sweep when a token is actually outstanding, and rarely
# otherwise - a token nobody issued cannot arrive.
_PENDING_IDLE_SECONDS = 300.0
_pending_last = 0.0


def _pending_due() -> bool:
    global _pending_last

    try:
        from modules import ig_pairing

        waiting = ig_pairing.pending_count() > 0
    except Exception:
        waiting = True  # never trade a pairing for a saving

    if waiting:
        _pending_last = time.monotonic()
        return True

    if time.monotonic() - _pending_last >= _PENDING_IDLE_SECONDS:
        _pending_last = time.monotonic()
        return True
    return False


def _pending_threads() -> list:
    """Message requests, or [] when this account's web api has no route to them."""
    global _pending_route

    if _pending_route is False:
        return []

    candidates = PENDING_CANDIDATES if _pending_route is None else [_pending_route]
    for url, params in candidates:
        try:
            data = _get(url, params)
        except LookupError:
            continue  # 404: wrong spelling, try the next
        except Exception as e:
            # A real failure - auth, network - is not a reason to give up on
            # the route, so leave the choice alone and report it.
            log.info("ig web: pending inbox failed (%s)", _short(str(e)))
            return []

        if _pending_route is None:
            _pending_route = (url, params)
            log.info("ig web: message requests via %s", url.rsplit("/", 2)[-2])
        return ((data.get("inbox") or {}).get("threads")) or []

    _pending_route = False
    log.warning(
        "ig web: no working message-requests endpoint - first-time senders will "
        "only be seen once they have a thread. Following the account first is "
        "what avoids that."
    )
    return []


@run_in_thread
def _collect(limit: int, since: float, with_pending: bool = True) -> list[DirectMessage]:
    data = _get(INBOX, {
        "visual_message_return_type": "unseen",
        "thread_message_limit": 5,
        "persistentBadging": "true",
        "limit": limit,
    })
    threads = list(((data.get("inbox") or {}).get("threads")) or [])

    if with_pending and _pending_due():
        threads += _pending_threads()

    me = settings.ig_dm_ds_user_id or str((data.get("viewer") or {}).get("pk") or "")

    out: list[DirectMessage] = []
    seen_any = skipped = 0
    for thread in threads:
        for item in thread.get("items") or []:
            seen_any += 1
            ts = to_epoch(item.get("timestamp"))
            if ts <= since:
                skipped += 1
                continue
            message = to_direct_message(item, "web", me)
            if message:
                out.append(message)

    # Reading an inbox full of messages and delivering none of them is what a
    # mis-parsed timestamp looks like, and it is otherwise indistinguishable
    # from a quiet inbox. Say it once when it happens.
    if seen_any and not out and skipped == seen_any:
        log.debug("ig web: %d item(s) read, all older than the high-water mark", skipped)
    return out


def media_info_sync(media_pk: str) -> dict:
    """Full media info for a pk, over the web api.

    The same cookies that read the inbox can read a post, so downloads take
    the working session rather than falling back to the mobile api that is
    refusing this account. Returns the raw item, in the shape
    modules/instagram.py already parses.
    """
    pk = str(media_pk).split("_")[0]
    data = _get(f"https://www.instagram.com/api/v1/media/{pk}/info/", {})
    items = data.get("items") or []
    return items[0] if items else {}


@run_in_thread
def media_info(media_pk: str) -> dict:
    return media_info_sync(media_pk)


@run_in_thread
def _send(user_id: str, text: str) -> bool:
    """Reply in the DM. Best effort - pairing feedback only."""
    client = _client()
    response = client.post(
        "https://www.instagram.com/api/v1/direct_v2/threads/broadcast/text/",
        data={"recipient_users": f"[[{user_id}]]", "text": text,
              "action": "send_item"},
        headers=_headers(),
    )
    _save_cookies(client)
    return response.status_code == 200


async def send_text(user_id: str, text: str) -> bool:
    try:
        return await _send(user_id, text)
    except Exception as e:
        log.info("ig web: could not reply in DM to %s: %s", user_id, e)
        return False


# Instagram's vocabulary for "slow down" - short of an actual action. Every
# one of these is a warning that the account is being noticed.
_SOFT_BLOCK_MARKERS = (
    "please wait a few minutes", "wait a few minutes", "try again later",
    "429", "rate limit", "too many requests", "spam", "feedback_required",
    "action blocked", "we restrict certain activity",
)

# Long enough that Instagram sees the behaviour stop, not merely slow.
_SOFT_BLOCK_PAUSE = 3600

# A login wall is not a failed sweep. It means the session is no longer a
# session, and the backoff below cannot make it one - it just re-asks, at the
# 600s ceiling, 144 times a day, on behalf of an account whose whole problem is
# being noticed. Eight hours of that bought nothing but traffic.
#
# Distinct from a checkpoint, which is handled above and does lift by itself.
# Nothing lifts this one except a new cookie.
_LOGIN_WALL_MARKERS = (
    "probably a login page", "cookie is not accepted",
    "exceeded maximum allowed redirects", "too many redirects",
    # The 401 text, matched on the machine-readable half so the Persian
    # wording can change without switching this off.
    "(401)",
)

# Twice in a row before believing it. A single login page can come from one bad
# hop through the proxy, and standing a working session down over that costs
# more than one extra sweep does.
_LOGIN_WALL_LIMIT = 2

# Set when the session is refused outright; cleared by a new cookie.
session_dead: str = ""
session_dead_at: float = 0.0


def _is_login_wall(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _LOGIN_WALL_MARKERS)


def _exit_ip_line() -> str:
    """Whether the address moved, put in front of the person who has to decide
    between pasting another cookie and changing the proxy."""
    try:
        from utils import exit_ip

        line = exit_ip.summary()
        return f"{line}\n\n" if line else ""
    except Exception:
        return ""

# A checkpoint is heavier than a rate limit and lighter than a ban.
#
# I first made this stop the loop permanently, on the reasoning that only a
# human clears a checkpoint. That was wrong, and the bot disproved it: one
# appeared, the loop backed off, and some hours later Instagram lifted it on
# its own and five queued messages were delivered. Stopping dead would have
# left the bot switched off through a recovery that happened by itself.
#
# So: back off hard and keep checking, slowly. Long enough that Instagram sees
# the automated traffic stop, short enough to notice when the wall comes down.
_CHECKPOINT_MARKERS = (
    # What the api returns.
    "checkpoint_required", "challenge_required", "checkpoint_url", "/challenge/",
    # And what instagrapi raises, whose prose contains none of the above.
    "challengerequired", "checkpointrequired", "manual verification",
)

# Set while one is in force; cleared by the first sweep that succeeds.
checkpointed = ""
checkpoint_since = 0.0

# 30 minutes, then an hour, then two - capped. Retrying a checkpoint every few
# seconds is what turns it into a disable; retrying it never is what turns a
# temporary one into a dead feature.
_CHECKPOINT_WAITS = (1800, 3600, 7200)


class CheckpointRequired(RuntimeError):
    """Instagram wants manual verification. Distinct from every other failure
    because it is the only one a human has to clear, and the only one where
    retrying makes things worse rather than merely wasting a request."""


def _is_checkpoint(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _CHECKPOINT_MARKERS)

_alert = None


def set_alert(fn) -> None:
    """Where to report a warning. Wired to the admin chat at startup."""
    global _alert
    _alert = fn


def _is_soft_block(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _SOFT_BLOCK_MARKERS)


async def _warn_admin(message: str) -> None:
    if _alert is None:
        return
    try:
        await _alert(message)
    except Exception as e:
        log.info("ig web: could not reach an admin (%s)", e)


def _in_quiet_hours(now: time.struct_time | None = None) -> bool:
    """True inside the configured overnight window, e.g. IG_DM_QUIET_HOURS=2-8.

    Nobody shares reels at 4am, so those requests buy nothing - and a session
    holding exactly the same rhythm around the clock is one of the plainest
    signals that nobody is holding the phone.
    """
    spec = (settings.ig_dm_quiet_hours or "").strip()
    if "-" not in spec:
        return False
    try:
        start, end = (int(part) % 24 for part in spec.split("-", 1))
    except ValueError:
        return False

    hour = (now or time.localtime()).tm_hour
    # A window that wraps past midnight (22-6) is the normal case.
    return start <= hour < end if start < end else (hour >= start or hour < end)


# The band boundary in _RATE_BANDS above, reused rather than reinvented: it is
# already anchored on this bot's own history rather than on a guess.
_FAST_CEILING = 3500

# Only so the transition is logged once instead of every sweep.
_was_congested = False


def congested() -> bool:
    """True once today's projected request count reaches the risky band.

    Fast mode is what a busy day is actually made of. Every ceiling and quiet
    window in here governs an idle account; none of them touch a day where
    messages keep arriving, because each one re-arms the fast window and the
    loop simply never leaves 3s. Measured over a day of that: ~16,000
    requests, three times what got an account actioned.

    So the budget is the thing that gives, not the account. Fast mode runs
    freely until the day's projection reaches the band this bot has already
    calibrated as risky, and then stops being available - which drops the loop
    back to the ordinary idle ladder and pulls the projection down with it. It
    is self-correcting: the busier the day, the sooner delivery slows, and the
    account never rides into the band that got the last three checkpointed.

    Users are told, rather than left wondering why a share took longer -
    handlers/ig_direct_handler.py says so on the download message.
    """
    try:
        return rate()["projected"] >= _FAST_CEILING
    except Exception:
        return False


def _next_delay(idle: float, fast: float, window: float, quiet_for: float,
                busy: bool = False) -> float:
    """How long to wait before the next sweep.

    A flat 15s poll is ~5,700 requests a day from what Instagram believes is
    a browser tab, at exactly the same rate at 4am as at 8pm, on intervals
    regular to the millisecond. That is the shape of the traffic, and the
    shape is what gets an account actioned - not any single request.

    So the loop backs off while nothing is happening and comes straight back
    the moment something does. Sharing is bursty: the cost is only ever on
    the FIRST message after a long silence, and that message puts the loop
    into fast mode for everything that follows it.

        active (within the fast window)   fast, ~1-5s
        recently quiet (< 10 min)         idle
        quiet 10-60 min                   idle x 3
        quiet over an hour                idle x 8

    Plus +/-25% of jitter, because a perfectly regular interval is a
    signature on its own.
    """
    import random

    # `busy` is the day's own request budget having run out. The rung is
    # simply not offered; everything below it still applies, so an active
    # conversation degrades to the idle interval rather than stopping.
    if quiet_for < window and not busy:
        base = fast
    elif quiet_for < 600:
        base = idle
    elif quiet_for < 3600:
        base = idle * 3
    else:
        base = idle * 8

    # The ceiling is the promise made to the user: nobody waits longer than
    # this. It caps the ladder, so a low ceiling and a low request count pull
    # against each other - that trade is IG_DM_MAX_INTERVAL and belongs to
    # whoever runs the bot, not to this function.
    base = min(base, max(fast, settings.ig_dm_max_interval))

    # Except overnight, where the promise does not apply because there is
    # nobody to make it to.
    if _in_quiet_hours() and quiet_for >= window:
        base = max(base, settings.ig_dm_quiet_interval)

    return max(0.3, base * random.uniform(0.75, 1.25))


async def _loop(dispatch: Dispatch) -> None:
    global _seen_after, _last_error, checkpointed, checkpoint_since
    global _was_congested

    if not _seen_after:
        _seen_after = time.time()

    idle = max(0.3, settings.ig_dm_poll_seconds)
    fast = max(0.3, min(settings.ig_dm_fast_seconds, idle))
    window = max(0, settings.ig_dm_fast_window)
    last_activity = 0.0
    sweeps = 0
    failures = 0
    login_walls = 0
    idle_logged = False
    checkpoint_tries = 0
    # When the inbox was last read successfully. /srcstatus shows a stale
    # health error otherwise, which reads as broken while it is working.
    last_ok = 0.0

    while True:
        # Nobody paired means nothing to deliver, so every request is pure
        # risk with no upside. The bot still answers /igdirect, and the first
        # pairing brings the loop back within one interval.
        from modules import ig_pairing

        if not ig_pairing.count() and not ig_pairing.pending_count():
            if not idle_logged:
                log.info("ig web: no pairings - polling paused until someone connects")
                idle_logged = True
            await asyncio.sleep(60)
            continue
        idle_logged = False

        hot = (time.time() - last_activity) < window
        sweeps += 1
        started = time.monotonic()

        try:
            messages = await _collect(3 if hot else 8, _seen_after,
                                      not hot or sweeps % 10 == 1)
            _last_error, failures, login_walls = "", 0, 0
            last_ok = time.time()

            # A successful read IS the checkpoint being lifted - which does
            # happen on its own, and is how the queued messages got through
            # the first time.
            if checkpointed:
                held = (time.time() - checkpoint_since) / 60
                log.info("ig web: checkpoint cleared after %.0f min - resuming", held)
                await _warn_admin(
                    f"✅ checkpoint اینستاگرام بعد از {held:.0f} دقیقه برداشته شد. "
                    "دایرکت دوباره کار می‌کنه."
                )
                checkpointed = ""
                checkpoint_tries = 0

            for dm in sorted(messages, key=lambda m: m.timestamp):
                _seen_after = max(_seen_after, dm.timestamp)
                last_activity = time.time()
                hot = True
                log.info("ig web: message %s seen %.1fs after it was sent",
                         dm.mid, max(0.0, time.time() - dm.timestamp))
                await dispatch(dm)
        except asyncio.CancelledError:
            raise
        except CheckpointRequired as e:
            first = not checkpointed
            checkpointed = _short(str(e))
            _last_error = checkpointed
            if first:
                checkpoint_since = time.time()
                checkpoint_tries = 0

            wait = _CHECKPOINT_WAITS[min(checkpoint_tries, len(_CHECKPOINT_WAITS) - 1)]
            checkpoint_tries += 1
            log.error("ig web: CHECKPOINT - pausing %d min (try %d)",
                      wait // 60, checkpoint_tries)

            # Once. Repeating it every half hour would train the admin to
            # ignore the one message that matters.
            if first:
                await _warn_admin(
                    "🛑 اینستاگرام روی اکانت بات checkpoint گذاشته.\n\n"
                    "پولینگ خیلی کند شد (نیم‌ساعت تا ۲ ساعت) تا فشار برداشته شه.\n"
                    "بعضی وقتا خودش برداشته می‌شه و بات ادامه می‌ده.\n\n"
                    "اگه چند ساعت طول کشید:\n"
                    "۱. تو اپ رسمی اینستاگرام با همون اکانت وارد شو\n"
                    "۲. تاییدیه‌ای که می‌خواد رو کامل کن\n"
                    "۳. بعد رو سرور: botctl igdirect و کوکی تازه بذار\n\n"
                    f"جزئیات: {checkpointed[:120]}"
                )
            await asyncio.sleep(wait)
            continue
        except Exception as e:
            _last_error = _short(str(e), 200)
            failures += 1

            # Instagram almost always pushes back before it acts: a spam
            # flag, a wait-a-few-minutes, a 429. Continuing at the same rate
            # into that is how a warning becomes an action, so it is treated
            # as a full stop rather than one more failure to retry through.
            if _is_soft_block(_last_error):
                log.error("ig web: Instagram is pushing back (%s) - pausing %ds",
                          _last_error, _SOFT_BLOCK_PAUSE)
                await _warn_admin(
                    "⚠️ اینستاگرام داره به بات تذکر می‌ده (نه بن).\n\n"
                    f"«{_last_error[:120]}»\n\n"
                    f"پولینگ {_SOFT_BLOCK_PAUSE // 60} دقیقه متوقف شد تا تشدید نشه.\n"
                    "اگه تکرار شد، IG_DM_POLL_SECONDS رو ببر بالاتر."
                )
                await asyncio.sleep(_SOFT_BLOCK_PAUSE)
                failures = 0
                continue

            if _is_login_wall(_last_error):
                login_walls += 1
                if login_walls >= _LOGIN_WALL_LIMIT:
                    global session_dead, session_dead_at

                    session_dead, session_dead_at = _last_error, time.time()
                    log.error("ig web: login wall %d sweeps running (%s) - stopping. "
                              "A new cookie has to be pasted", login_walls, _last_error)
                    await _warn_admin(
                        "🔑 کوکی اینستاگرام دیگه معتبر نیست و دایرکت خوابید.\n\n"
                        "به‌جای اینباکس، صفحه‌ی لاگین برمی‌گرده. پولینگ متوقف شد "
                        "چون تکرارش این رو درست نمی‌کنه و فقط ترافیک بی‌فایده‌ست.\n\n"
                        "برای راه‌اندازی دوباره:\n"
                        "۱. با مرورگر (نه اپ) با همون اکانت وارد شو\n"
                        "۲. کوکی sessionid تازه رو بردار\n"
                        "۳. رو سرور: botctl igdirect → گزینه ۲\n\n"
                        f"{_exit_ip_line()}"
                        f"جزئیات: {_last_error[:120]}"
                    )
                    return
            else:
                login_walls = 0

            penalty = min(600, idle * (2 ** failures))
            log.warning("ig web: sweep failed (%s) - backing off to %.0fs",
                        _last_error, penalty)
            await asyncio.sleep(penalty)
            continue

        # Paced from the start of the sweep, so the request time is inside the
        # interval rather than added to it.
        busy = congested()
        if busy and not _was_congested:
            log.warning("ig web: request budget reached (%s/day projected) - "
                        "fast mode is off until it falls back",
                        rate()["projected"])
        elif _was_congested and not busy:
            log.info("ig web: back under the request budget - fast mode is on again")
        _was_congested = busy

        delay = _next_delay(idle, fast, window,
                            time.time() - (last_activity or 0), busy=busy)
        await asyncio.sleep(max(0.0, delay - (time.monotonic() - started)))


async def start(dispatch: Dispatch) -> None:
    global _task, checkpointed, session_dead, session_dead_at

    if not usable():
        raise RuntimeError("IG_DM_SESSIONID is not set")

    # A new cookie is the signal that somebody dealt with the checkpoint;
    # _load_cookies already discards the stale jar on a changed seed.
    checkpointed = ""
    session_dead, session_dead_at = "", 0.0

    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(dispatch))


async def stop() -> None:
    global _task

    if _task:
        _task.cancel()
        _task = None


async def health() -> tuple[bool, str]:
    if not usable():
        return False, "no sessionid"
    if checkpointed:
        return False, "checkpoint - تایید دستی تو اپ لازمه"
    # Ahead of the probe below, which would otherwise spend a request
    # rediscovering the login wall that already stopped the loop.
    if session_dead:
        held = (time.time() - session_dead_at) / 60
        return False, f"کوکی باطله ({held:.0f}m) - یه تازه لازمه: {session_dead[:80]}"
    try:
        await _collect(1, time.time(), False)
        return True, "web api reachable"
    except Exception as e:
        return False, str(e)[:120]


def source() -> Source:
    return Source(name="web", start=start, stop=stop, health=health)
