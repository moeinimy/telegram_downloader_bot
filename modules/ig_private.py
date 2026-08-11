"""
The standby inbox reader: instagrapi logged in as the account.

This exists because the official path has a cliff in front of it. Under
Standard Access Meta only delivers webhooks for senders who hold a role on the
app, so until App Review grants Advanced Access the bot works for nobody else -
and afterwards, a dead token or a dropped subscription puts it back in the same
place. This covers that gap.

It is not a peer of the official path and must not be run as one:

* It violates Instagram's Terms of Use. The account can be disabled with no
  appeal, and datacenter IPs are flagged aggressively.
* A challenge or 2FA prompt breaks the session silently and needs a human.
* It breaks whenever Instagram changes its private endpoints - which is why
  every field is read through getattr rather than by attribute access.

So modules/ig_direct.py only wakes it when the official path fails a health
check, and stands it down again the moment that recovers.

instagrapi is deliberately absent from requirements.txt: it pins pydantic and
fights shazamio's resolver. Install it on its own (`botctl igdirect`). Without
it this source reports unavailable and the bot starts normally.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path

from config import settings
from modules.ig_direct import DirectMessage, Dispatch, Source
from utils.helpers import run_in_thread

log = logging.getLogger(__name__)

_SESSION_PATH: Path = settings.download_dir / "ig_private_session.json"

_client = None
_client_lock = threading.Lock()
_task: asyncio.Task | None = None
# High-water mark, so a steady-state poll only looks at what is new. Upstream
# de-duplication is the real safety net; this just keeps the work small.
_seen_after = 0.0


def available() -> bool:
    try:
        # A real import, not find_spec: a half-installed instagrapi resolves
        # as present and then explodes on first use.
        import instagrapi

        return instagrapi is not None
    except Exception:
        return False


def _login():
    """Reuse the stored session; only fall back to a password login.

    Every password login is a fresh device fingerprint to Instagram and a good
    way to trigger a challenge, so the session file is the normal path and the
    password is the recovery path.
    """
    global _client

    with _client_lock:
        if _client is not None:
            return _client

        from instagrapi import Client

        client = Client()
        # instagrapi's inter-request jitter, applied to EVERY call. Its
        # default of 2-5s was the dominant cost: a sweep makes up to two
        # requests, so up to ten seconds sat on top of the poll interval
        # before a shared reel was even looked for. Kept non-zero because
        # perfectly regular timing is itself a signal, but the poll interval
        # is now the thing that controls the request rate.
        client.delay_range = [0, 1]

        if _SESSION_PATH.exists():
            try:
                client.load_settings(str(_SESSION_PATH))
                client.login(settings.ig_dm_username, settings.ig_dm_password)
                client.get_timeline_feed()  # proves the session is really live
                _client = client
                log.info("ig poll: reused the stored session")
                return _client
            except Exception as e:
                log.warning("ig poll: stored session rejected (%s) - logging in fresh", e)
                client = Client()
                client.delay_range = [2, 5]

        client.login(settings.ig_dm_username, settings.ig_dm_password)
        try:
            client.dump_settings(str(_SESSION_PATH))
            _SESSION_PATH.chmod(0o600)
        except Exception as e:
            log.warning("ig poll: could not save the session (%s)", e)

        _client = client
        log.info("ig poll: logged in as %s", settings.ig_dm_username)
        return _client


def client():
    """The logged-in client, for anything that wants authenticated access.

    modules/instagram.py uses this as its first download route. An account
    that Instagram considers logged in sees posts the anonymous routes cannot:
    age-gated and "sensitive" media, private accounts it follows, stories at
    all, and - the case that started this - shares whose only url resolves to
    a login wall when fetched without a session.
    """
    return _login()


def usable() -> bool:
    """Configured, installed, and not obviously broken. Cheap - no network."""
    return bool(available() and settings.has_ig_private)


def _str(value) -> str:
    """instagrapi hands back pydantic Url objects and enums as often as plain
    strings, and str() is the only thing that behaves for all of them."""
    return "" if value is None else str(value)


_SHORTCODE_IN_URL = re.compile(r"/(?:reels?|p|tv)/([A-Za-z0-9_-]{5,})", re.I)


def _populated_fields(message) -> list[str]:
    """Names of the fields this message actually filled in. Pydantic v1 and
    v2 disagree on where the field list lives, so both are tried."""
    schema = getattr(type(message), "model_fields", None) or getattr(message, "__fields__", None)
    if not schema:
        return []
    return sorted(name for name in schema if getattr(message, name, None) is not None)


def _shared_media(message) -> tuple[str, str, str]:
    """(permalink, media_id, media_url) for whatever this message shared.

    Instagram has renamed this payload repeatedly - reel_share became clip,
    then xma_share - so every known shape is tried instead of trusting one.
    """
    for attr in ("clip", "media_share", "story_share", "media", "visual_media"):
        obj = getattr(message, attr, None)
        if obj is None:
            continue
        # story_share wraps the media one level deeper.
        obj = getattr(obj, "media", None) or obj

        code = _str(getattr(obj, "code", ""))
        if code:
            return f"https://www.instagram.com/p/{code}/", _str(getattr(obj, "pk", "")), ""

        pk = _str(getattr(obj, "pk", ""))
        if pk:
            return "", pk, ""

    # Cross-app shares (xma). These carry a page link where a media url is
    # expected: fetching xma.video_url returned 600KB of Instagram login-wall
    # HTML with the post named nowhere in it. So every url on the object is
    # examined for a permalink first, and the raw url is only the last resort.
    for attr in ("xma_share", "xma_media_share", "xma_reel_share", "xma_reel_mention"):
        xma = getattr(message, attr, None)
        if xma is None:
            continue
        # instagrapi sometimes models these as a list of one.
        if isinstance(xma, (list, tuple)):
            xma = xma[0] if xma else None
        if xma is None:
            continue

        urls = [
            _str(getattr(xma, field, ""))
            for field in ("target_url", "url", "preview_url", "video_url")
        ]
        for candidate in urls:
            if "instagram.com/" in candidate and _SHORTCODE_IN_URL.search(candidate):
                return candidate, "", ""

        raw = next((u for u in urls if u), "")
        return "", "", raw

    return "", "", ""


@run_in_thread
def _collect(threads_wanted: int, since: float, with_pending: bool = True) -> list[DirectMessage]:
    """One synchronous sweep of the inbox. Returns what is newer than `since`."""
    client = _login()
    out: list[DirectMessage] = []

    threads = list(client.direct_threads(amount=threads_wanted))
    if with_pending:
        try:
            # Message requests from people who have never messaged us before
            # land here rather than in the inbox, and a first-time sharer is
            # exactly the case that matters.
            threads += list(client.direct_pending_inbox(amount=threads_wanted))
        except Exception as e:
            log.info("ig poll: pending inbox unavailable (%s)", e)

    me = _str(getattr(client, "user_id", ""))

    for thread in threads:
        for message in getattr(thread, "messages", None) or []:
            sender = _str(getattr(message, "user_id", ""))
            if not sender or sender == me:
                continue  # our own replies

            stamp = getattr(message, "timestamp", None)
            ts = stamp.timestamp() if hasattr(stamp, "timestamp") else float(stamp or 0)
            if ts <= since:
                continue

            permalink, media_id, media_url = _shared_media(message)
            text = _str(getattr(message, "text", ""))
            if not (permalink or media_id or media_url or text):
                continue

            out.append(
                DirectMessage(
                    igsid=sender,
                    mid=_str(getattr(message, "id", "")),
                    text=text,
                    media_url=media_url,
                    permalink=permalink,
                    media_id=media_id,
                    timestamp=ts,
                    source="poll",
                    # Which fields the message actually carried. Instagram
                    # renames this payload often enough that "we did not find
                    # the media" is useless on its own - the next report needs
                    # to say where it was hiding instead.
                    raw={
                        "item_type": _str(getattr(message, "item_type", "")),
                        "fields": _populated_fields(message),
                    },
                )
            )

    return out


@run_in_thread
def _send(user_id: str, text: str) -> bool:
    client = _login()
    client.direct_send(text, user_ids=[int(user_id)])
    return True


async def send_text(user_id: str, text: str) -> bool:
    """Reply in the DM. Best effort - pairing feedback only."""
    try:
        return await _send(user_id, text)
    except Exception as e:
        log.info("ig poll: could not reply in DM to %s: %s", user_id, e)
        return False


async def _sweep(
    dispatch: Dispatch, threads_wanted: int, since: float, with_pending: bool = True
) -> int:
    global _seen_after

    messages = await _collect(threads_wanted, since, with_pending)
    for dm in sorted(messages, key=lambda m: m.timestamp):
        _seen_after = max(_seen_after, dm.timestamp)
        await dispatch(dm)
    return len(messages)


# How many consecutive sweeps have been throttled. Aggressive polling has to
# be able to give ground on its own, or the first bad afternoon costs the
# account rather than a few seconds of latency.
_throttled = 0
_THROTTLE_MARKERS = ("wait a few minutes", "429", "rate", "throttl", "please wait")


async def _loop(dispatch: Dispatch) -> None:
    """Poll fast while the user is active, slowly while they are not.

    Polling has no push channel, so the interval IS the latency - there is no
    way to be told about a DM, only to ask. Asking every second around the
    clock would be ~3600 requests an hour against a private API, which is the
    surest way to lose the account.

    Sharing is bursty, though: somebody sends the pairing token and then three
    reels within a minute. So any message drops the loop into fast mode for a
    window, and everything after the first arrives effectively instantly. Set
    IG_DM_POLL_SECONDS equal to IG_DM_FAST_SECONDS to stay fast permanently
    and accept the risk.
    """
    global _seen_after, _throttled

    # Only messages from here on. The catch-up sweep is what covers the
    # outage window; without this floor the first poll would replay the
    # entire inbox and re-upload everything already delivered.
    if not _seen_after:
        _seen_after = time.time()

    idle = max(1, settings.ig_dm_poll_seconds)
    fast = max(1, min(settings.ig_dm_fast_seconds, idle))
    window = max(0, settings.ig_dm_fast_window)

    last_activity = 0.0
    sweeps = 0

    while True:
        hot = (time.time() - last_activity) < window
        sweeps += 1
        # The pending inbox is a second round trip and only ever matters for a
        # sender who has never messaged before - a once-per-user event. Paying
        # for it on every fast sweep would double the request rate for nothing.
        with_pending = not hot or sweeps % 10 == 1

        try:
            found = await _sweep(dispatch, 5 if hot else 10, _seen_after, with_pending)
            _throttled = 0
            if found:
                last_activity = time.time()
                hot = True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # A challenge, a changed endpoint, or Instagram telling us to slow
            # down. Back off hard and keep backing off: continuing at the same
            # rate into a throttle is what turns it into a ban.
            text = str(e).lower()
            if any(marker in text for marker in _THROTTLE_MARKERS):
                _throttled += 1
                penalty = min(300, idle * (2 ** _throttled))
                log.warning(
                    "ig poll: throttled by Instagram (%s) - backing off to %ds", e, penalty
                )
                await asyncio.sleep(penalty)
            else:
                log.warning("ig poll: sweep failed (%s) - backing off", e)
                await asyncio.sleep(idle * 4)
            continue

        await asyncio.sleep(fast if hot else idle)


# ---------------- lifecycle ----------------

async def start(dispatch: Dispatch) -> None:
    global _task

    if not available():
        raise RuntimeError("instagrapi is not installed (botctl igdirect)")

    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(dispatch))


async def stop() -> None:
    global _task

    if _task:
        _task.cancel()
        _task = None


async def catch_up(dispatch: Dispatch) -> int:
    """Everything shared in the last day, for the moment this source wakes up.

    A webhook cannot be asked about the past, so whatever arrived while the
    official path was down exists only in the inbox. Duplicates are expected
    here and are dropped upstream by content key, not by message id - the two
    sources number the same message differently.
    """
    return await _sweep(dispatch, threads_wanted=20, since=time.time() - 86400)


async def health() -> tuple[bool, str]:
    if not available():
        return False, "instagrapi not installed"
    try:
        client = await asyncio.to_thread(_login)
        return True, f"logged in as {_str(getattr(client, 'username', '')) or settings.ig_dm_username}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def source() -> Source:
    if not available():
        raise RuntimeError("instagrapi is not installed")
    return Source(name="poll", start=start, stop=stop, health=health, catch_up=catch_up)
