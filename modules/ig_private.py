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
        client.delay_range = [2, 5]  # instagrapi's own inter-request jitter

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


def _str(value) -> str:
    """instagrapi hands back pydantic Url objects and enums as often as plain
    strings, and str() is the only thing that behaves for all of them."""
    return "" if value is None else str(value)


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

    xma = getattr(message, "xma_share", None)
    if xma is not None:
        target = _str(getattr(xma, "target_url", ""))
        permalink = target if "instagram.com/" in target else ""
        return permalink, "", "" if permalink else _str(getattr(xma, "video_url", ""))

    return "", "", ""


@run_in_thread
def _collect(threads_wanted: int, since: float) -> list[DirectMessage]:
    """One synchronous sweep of the inbox. Returns what is newer than `since`."""
    client = _login()
    out: list[DirectMessage] = []

    threads = list(client.direct_threads(amount=threads_wanted))
    try:
        # Message requests from people who have never messaged us before land
        # here rather than in the inbox, and a first-time sharer is exactly
        # the case that matters.
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
                    raw={"item_type": _str(getattr(message, "item_type", ""))},
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


async def _sweep(dispatch: Dispatch, threads_wanted: int, since: float) -> int:
    global _seen_after

    messages = await _collect(threads_wanted, since)
    for dm in sorted(messages, key=lambda m: m.timestamp):
        _seen_after = max(_seen_after, dm.timestamp)
        await dispatch(dm)
    return len(messages)


async def _loop(dispatch: Dispatch) -> None:
    global _seen_after

    # Only messages from here on. The catch-up sweep is what covers the
    # outage window; without this floor the first poll would replay the
    # entire inbox and re-upload everything already delivered.
    if not _seen_after:
        _seen_after = time.time()

    interval = max(15, settings.ig_dm_poll_seconds)
    while True:
        try:
            await _sweep(dispatch, threads_wanted=10, since=_seen_after)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # A challenge or a changed endpoint lands here. Back off rather
            # than hammering an account Instagram is already unhappy with.
            log.warning("ig poll: sweep failed (%s) - backing off", e)
            await asyncio.sleep(interval * 4)
            continue
        await asyncio.sleep(interval)


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
