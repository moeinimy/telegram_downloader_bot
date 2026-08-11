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
        # instagrapi sleeps this long before EVERY private request. Measured
        # on the live bot, it was the whole cost of a poll:
        #
        #     ⏱ lag 6.6s   sweep 2232ms
        #
        # Two requests per sweep against a [0,1] range is up to two seconds of
        # deliberate sleeping to hide timing that the poll interval already
        # determines - Instagram sees a request every N seconds either way, so
        # the jitter bought nothing and cost everything. Zero here; the pacing
        # lives in _loop.
        client.delay_range = [0, 0]

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
                # Same as above. This branch was leaving the default 2-5s on
                # the client for the rest of the process, so one rejected
                # session quietly restored the slow path for good.
                client.delay_range = [0, 0]

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

# --------------------------------------------------------------------------
# Reading the inbox
#
# Deliberately NOT through instagrapi's model layer. Two reasons, and both
# were live bugs:
#
# Speed. client.direct_threads() builds a pydantic object per thread and per
# message. Measured on the running bot, the HTTP call took 412ms while the
# sweep took 1363ms - the missing ~950ms was model construction for roughly
# 120 objects, every second, almost all of them messages already handled.
#
# Correctness. The model layer only surfaces the fields its version knows
# about, under the names that version uses. Instagram renames this payload
# constantly - reel_share became clip, became xma_share - and a shared STORY
# arrived under a key the installed instagrapi did not model at all, so it
# came through empty and was dropped. The raw json carries every field under
# its real name, whatever that name happens to be this month.
# --------------------------------------------------------------------------

# Keys that hold a shared media object, most specific first.
_MEDIA_KEYS = (
    "clip", "media_share", "story_share", "reel_share", "media",
    "visual_media", "raven_media", "direct_media", "felix_share",
)

# Cross-app shares: a bundle of urls rather than a media object.
_XMA_KEYS = (
    "xma_media_share", "xma_share", "xma_reel_share", "xma_story_share",
    "generic_xma", "xma_link_share",
)

_URL_FIELDS = ("target_url", "url", "video_url", "preview_url", "playable_url")

# Anything under these describes the sender, not the share - and a user
# object carries a pk too, which would be downloaded as if it were media.
_SKIP_KEYS = {"user", "users", "sender", "inviter", "from_user", "reactions",
              "preview_medias", "profile_pic_url"}


def _best_url(node: dict) -> str:
    for version in node.get("video_versions") or []:
        if version.get("url"):
            return str(version["url"])
    candidates = ((node.get("image_versions2") or {}).get("candidates")) or []
    if candidates and candidates[0].get("url"):
        return str(candidates[0]["url"])
    return ""


def _from_media_node(node) -> tuple[str, str, str]:
    """(permalink, pk, url) from something that looks like a media object."""
    if isinstance(node, (list, tuple)):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return "", "", ""

    # story_share and reel_share wrap the real media one level down.
    inner = node.get("media")
    if isinstance(inner, dict):
        found = _from_media_node(inner)
        if any(found):
            return found

    code = str(node.get("code") or "")
    pk = str(node.get("pk") or node.get("id") or "")
    url = _best_url(node)

    if code:
        return f"https://www.instagram.com/p/{code}/", pk, ""
    if pk:
        return "", pk, url
    if url:
        return "", "", url
    return "", "", ""


def _walk_json(node, depth: int = 0) -> tuple[str, str, str]:
    """Last resort: find the media anywhere in the item, by shape not by name.

    Every key in _MEDIA_KEYS is one Instagram has already renamed at least
    once, and each rename broke a kind of share silently until somebody
    reported it. Shape does not get renamed.
    """
    if depth > 5:
        return "", "", ""

    if isinstance(node, (list, tuple)):
        for item in node:
            found = _walk_json(item, depth + 1)
            if any(found):
                return found
        return "", "", ""

    if not isinstance(node, dict):
        return "", "", ""

    looks_like_media = "code" in node or (
        ("pk" in node or "id" in node)
        and ("video_versions" in node or "image_versions2" in node)
    )
    if looks_like_media:
        found = _from_media_node(node)
        if any(found):
            return found

    for key, value in node.items():
        if key in _SKIP_KEYS:
            continue
        found = _walk_json(value, depth + 1)
        if any(found):
            return found
    return "", "", ""


def _media_from_item(item: dict) -> tuple[str, str, str]:
    """(permalink, media_pk, media_url) for whatever this DM item shared."""
    for key in _MEDIA_KEYS:
        found = _from_media_node(item.get(key))
        if any(found):
            return found

    for key in _XMA_KEYS:
        node = item.get(key)
        if isinstance(node, (list, tuple)):
            node = node[0] if node else None
        if not isinstance(node, dict):
            continue
        urls = [str(node.get(field) or "") for field in _URL_FIELDS]
        # A permalink is worth far more than a signed url here: fetching an
        # xma video_url returned 600KB of login-wall HTML.
        for candidate in urls:
            if "instagram.com/" in candidate and _SHORTCODE_IN_URL.search(candidate):
                return candidate, "", ""
        raw = next((u for u in urls if u), "")
        if raw:
            return "", "", raw

    return _walk_json(item)


def _inbox(client, endpoint: str, params: dict) -> dict:
    """One raw inbox call. private_request returns the parsed json directly."""
    return client.private_request(endpoint, params=params) or client.last_json or {}


@run_in_thread
def _collect(threads_wanted: int, since: float, with_pending: bool = True) -> list[DirectMessage]:
    """One synchronous sweep of the inbox. Returns what is newer than `since`."""
    client = _login()
    out: list[DirectMessage] = []

    data = _inbox(client, "direct_v2/inbox/", {
        "visual_message_return_type": "unseen",
        "thread_message_limit": 5,
        "persistentBadging": "true",
        "limit": threads_wanted,
    })
    threads = list(((data.get("inbox") or {}).get("threads")) or [])

    if with_pending:
        try:
            # Message requests from people who have never messaged us before
            # land here rather than in the inbox, and a first-time sharer is
            # exactly the case that matters.
            pending = _inbox(client, "direct_v2/pending_inbox/", {
                "visual_message_return_type": "unseen",
                "persistentBadging": "true",
            })
            threads += ((pending.get("inbox") or {}).get("threads")) or []
        except Exception as e:
            log.info("ig poll: pending inbox unavailable (%s)", e)

    me = str(getattr(client, "user_id", "") or "")

    for thread in threads:
        for item in thread.get("items") or []:
            sender = str(item.get("user_id") or "")
            if not sender or sender == me:
                continue  # our own replies

            # Instagram timestamps DM items in MICROseconds.
            ts = float(item.get("timestamp") or 0) / 1_000_000
            if ts <= since:
                continue

            permalink, media_id, media_url = _media_from_item(item)
            text = str(item.get("text") or "")
            if not (permalink or media_id or media_url or text):
                continue

            out.append(
                DirectMessage(
                    igsid=sender,
                    mid=str(item.get("item_id") or ""),
                    text=text,
                    media_url=media_url,
                    permalink=permalink,
                    media_id=media_id,
                    timestamp=ts,
                    source="poll",
                    # The item's own keys. When extraction finds nothing this
                    # is the only thing that says where the media was hiding,
                    # and its absence is why the story bug needed a report.
                    raw={
                        "item_type": str(item.get("item_type") or ""),
                        "keys": sorted(k for k in item if item.get(k) is not None),
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


# What the last few messages actually cost, end to end. Guessing at where the
# delay lives has been wrong twice now, so it is measured instead: `lag` is
# the age of the message when the bot first saw it, which is the number the
# poll interval is supposed to control.
last_sweep_ms: float = 0.0
last_lag: float = 0.0
_lag_samples: list[float] = []


def timing() -> dict:
    return {
        "sweep_ms": round(last_sweep_ms),
        "last_lag": round(last_lag, 1),
        "avg_lag": round(sum(_lag_samples) / len(_lag_samples), 1) if _lag_samples else 0.0,
        "samples": len(_lag_samples),
    }


async def _sweep(
    dispatch: Dispatch, threads_wanted: int, since: float, with_pending: bool = True
) -> int:
    global _seen_after, last_sweep_ms, last_lag

    started = time.monotonic()
    messages = await _collect(threads_wanted, since, with_pending)
    last_sweep_ms = (time.monotonic() - started) * 1000

    for dm in sorted(messages, key=lambda m: m.timestamp):
        _seen_after = max(_seen_after, dm.timestamp)
        if dm.timestamp:
            last_lag = max(0.0, time.time() - dm.timestamp)
            _lag_samples.append(last_lag)
            del _lag_samples[:-20]
            log.info(
                "ig poll: message %s seen %.1fs after it was sent (sweep %.0fms, pending=%s)",
                dm.mid, last_lag, last_sweep_ms, with_pending,
            )
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

    # Floors, not defaults. Below ~0.3s the sweep itself is the limit and the
    # only thing another request buys is a higher chance of being throttled.
    idle = max(0.3, settings.ig_dm_poll_seconds)
    fast = max(0.3, min(settings.ig_dm_fast_seconds, idle))
    window = max(0, settings.ig_dm_fast_window)

    last_activity = 0.0
    sweeps = 0

    while True:
        cycle_started = time.monotonic()
        hot = (time.time() - last_activity) < window
        sweeps += 1
        # The pending inbox is a second round trip and only ever matters for a
        # sender who has never messaged before - a once-per-user event. Paying
        # for it on every fast sweep would double the request rate for nothing.
        with_pending = not hot or sweeps % 10 == 1

        try:
            found = await _sweep(dispatch, 3 if hot else 8, _seen_after, with_pending)
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

        # Paced from the START of the cycle, not the end. Sleeping the full
        # interval AFTER the sweep made the real period interval + sweep -
        # a 5s setting with a 2.2s sweep polled every 7.2s, which is what the
        # 6.6s measured lag actually was.
        target = fast if hot else idle
        await asyncio.sleep(max(0.0, target - (time.monotonic() - cycle_started)))


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
