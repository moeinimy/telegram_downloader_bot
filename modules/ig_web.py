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
import logging
import time

from config import settings
from modules.ig_direct import DirectMessage, Dispatch, Source
from modules.ig_items import to_direct_message
from utils.helpers import run_in_thread

log = logging.getLogger(__name__)

INBOX = "https://www.instagram.com/api/v1/direct_v2/inbox/"
PENDING = "https://www.instagram.com/api/v1/direct_v2/pending_inbox/"

# The public web-client id instagram.com sends with its own XHRs. Without it
# these endpoints answer 403 even with a perfectly good cookie.
_APP_ID = "936619743392459"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_task: asyncio.Task | None = None
_seen_after = 0.0
_last_error = ""


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
    return {
        "User-Agent": _UA,
        "X-IG-App-ID": _APP_ID,
        "X-ASBD-ID": "129477",
        "X-IG-WWW-Claim": "0",
        "X-CSRFToken": settings.ig_dm_csrftoken or "missing",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.instagram.com/direct/inbox/",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }


def _proxy_url() -> str | None:
    """The proxy, in the spelling httpx accepts.

    Same trap as aiohttp-socks: httpx does not know the "h" suffix and raises

        ValueError: Unknown scheme for proxy URL URL('socks5h://...')

    before any request is made. curl and requests use socks5h to mean "resolve
    DNS at the proxy"; httpx's socks transport does that anyway, so the suffix
    is dropped rather than translated.
    """
    proxy = settings.ig_dm_proxy
    if not proxy:
        return None
    for suffix, plain in (("socks5h://", "socks5://"), ("socks4a://", "socks4://")):
        if proxy.lower().startswith(suffix):
            return plain + proxy[len(suffix):]
    return proxy


def _client():
    import httpx

    proxy = _proxy_url()
    # httpx names it `proxy` from 0.26 and `proxies` before that.
    try:
        return httpx.Client(timeout=25, follow_redirects=True, proxy=proxy)
    except TypeError:
        return httpx.Client(timeout=25, follow_redirects=True, proxies=proxy)


def _get(url: str, params: dict) -> dict:
    with _client() as client:
        response = client.get(url, params=params, headers=_headers(), cookies=_cookies())

    if response.status_code == 401:
        raise RuntimeError("sessionid رد شد (401) - کوکی منقضی شده، یه تازه بگیر")
    if response.status_code == 403:
        raise RuntimeError(
            "403 from the web api - the cookie is not accepted from this address"
        )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:160]}")

    try:
        return response.json()
    except Exception:
        # A login wall is html, and it comes back with a 200.
        raise RuntimeError(f"not json ({len(response.content)} bytes) - probably a login page")


@run_in_thread
def _collect(limit: int, since: float, with_pending: bool = True) -> list[DirectMessage]:
    data = _get(INBOX, {
        "visual_message_return_type": "unseen",
        "thread_message_limit": 5,
        "persistentBadging": "true",
        "limit": limit,
    })
    threads = list(((data.get("inbox") or {}).get("threads")) or [])

    if with_pending:
        try:
            pending = _get(PENDING, {"visual_message_return_type": "unseen",
                                     "persistentBadging": "true"})
            threads += ((pending.get("inbox") or {}).get("threads")) or []
        except Exception as e:
            log.info("ig web: pending inbox unavailable (%s)", e)

    me = settings.ig_dm_ds_user_id or str((data.get("viewer") or {}).get("pk") or "")

    out: list[DirectMessage] = []
    for thread in threads:
        for item in thread.get("items") or []:
            ts = float(item.get("timestamp") or 0) / 1_000_000
            if ts <= since:
                continue
            message = to_direct_message(item, "web", me)
            if message:
                out.append(message)
    return out


@run_in_thread
def _send(user_id: str, text: str) -> bool:
    """Reply in the DM. Best effort - pairing feedback only."""
    with _client() as client:
        response = client.post(
            "https://www.instagram.com/api/v1/direct_v2/threads/broadcast/text/",
            data={"recipient_users": f'[[{user_id}]]', "text": text,
                  "action": "send_item"},
            headers=_headers(), cookies=_cookies(),
        )
    return response.status_code == 200


async def send_text(user_id: str, text: str) -> bool:
    try:
        return await _send(user_id, text)
    except Exception as e:
        log.info("ig web: could not reply in DM to %s: %s", user_id, e)
        return False


async def _loop(dispatch: Dispatch) -> None:
    global _seen_after, _last_error

    if not _seen_after:
        _seen_after = time.time()

    idle = max(0.3, settings.ig_dm_poll_seconds)
    fast = max(0.3, min(settings.ig_dm_fast_seconds, idle))
    window = max(0, settings.ig_dm_fast_window)
    last_activity = 0.0
    sweeps = 0
    failures = 0

    while True:
        hot = (time.time() - last_activity) < window
        sweeps += 1
        started = time.monotonic()

        try:
            messages = await _collect(3 if hot else 8, _seen_after,
                                      not hot or sweeps % 10 == 1)
            _last_error, failures = "", 0
            for dm in sorted(messages, key=lambda m: m.timestamp):
                _seen_after = max(_seen_after, dm.timestamp)
                last_activity = time.time()
                hot = True
                log.info("ig web: message %s seen %.1fs after it was sent",
                         dm.mid, max(0.0, time.time() - dm.timestamp))
                await dispatch(dm)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _last_error = str(e)[:200]
            failures += 1
            penalty = min(600, idle * (2 ** failures))
            log.warning("ig web: sweep failed (%s) - backing off to %.0fs", e, penalty)
            await asyncio.sleep(penalty)
            continue

        # Paced from the start of the sweep, so the request time is inside the
        # interval rather than added to it.
        await asyncio.sleep(max(0.0, (fast if hot else idle) - (time.monotonic() - started)))


async def start(dispatch: Dispatch) -> None:
    global _task

    if not usable():
        raise RuntimeError("IG_DM_SESSIONID is not set")
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
    try:
        await _collect(1, time.time(), False)
        return True, "web api reachable"
    except Exception as e:
        return False, str(e)[:120]


def source() -> Source:
    return Source(name="web", start=start, stop=stop, health=health)
