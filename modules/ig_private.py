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
from modules.ig_items import media_from_item as _media_from_item
from modules.ig_items import to_direct_message
from modules.ig_items import walk_json as _walk_json
from utils.helpers import run_in_thread

log = logging.getLogger(__name__)

_SESSION_PATH: Path = settings.download_dir / "ig_private_session.json"

# The device fingerprint, kept SEPARATELY from the session and never deleted
# by a session reset.
#
# instagrapi invents a random phone on every fresh Client(). To Instagram that
# is a new device signing into the account, and it responds by refusing the
# direct endpoints - reading the inbox 403s with error_code 1404006 while the
# login itself succeeds, which is exactly what happened here: the inbox worked
# for hours, a session reset generated a new phone, and it never worked again.
#
# Reusing one fingerprint means the account keeps signing in from the same
# device it always has.
_DEVICE_PATH: Path = settings.download_dir / "ig_device.json"

# Which parts of instagrapi's settings describe the device rather than the
# session. Everything else is per-login and must not be carried over.
_DEVICE_KEYS = (
    "uuids", "device_settings", "user_agent", "device_id", "phone_id",
    "advertising_id", "android_device_id", "request_id", "client_session_id",
    "country", "country_code", "locale", "timezone_offset",
)

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


def _make_responsive(client) -> None:
    """Remove instagrapi's two built-in sleeps.

    Both exist to pace a scraper hammering the API in a burst. This is a
    poller: it makes one request every N seconds by construction, so the
    sleeps hid nothing and were simply added to every measurement.

    Together they were the entire latency budget. Measured on the live bot:

        sweep 2461ms   for two requests of ~410ms each

        request_timeout = 1   -> time.sleep(1) before EVERY private request
                                 (instagrapi/mixins/private.py, not optional
                                  and not documented as a delay)
        delay_range           -> up to another second on top

    request_timeout is not a network timeout despite the name; it is a
    hardcoded pause. Left slightly above zero so the two calls in one sweep
    are not fired back to back.
    """
    client.request_timeout = 0.1
    client.delay_range = [0, 0]


def _save(client) -> None:
    try:
        client.dump_settings(str(_SESSION_PATH))
        _SESSION_PATH.chmod(0o600)
    except Exception as e:
        log.warning("ig poll: could not save the session (%s)", e)
    _remember_device(client)


def _remember_device(client) -> None:
    """Persist the fingerprint so the next sign-in is the same phone."""
    import json

    try:
        settings_blob = client.get_settings() or {}
        device = {k: settings_blob[k] for k in _DEVICE_KEYS if k in settings_blob}
        if not device:
            return
        tmp = _DEVICE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(device), encoding="utf-8")
        tmp.replace(_DEVICE_PATH)
        _DEVICE_PATH.chmod(0o600)
    except Exception as e:
        log.warning("ig poll: could not save the device fingerprint (%s)", e)


def _apply_device(client) -> bool:
    """Put the remembered phone back on a fresh client. True if there was one."""
    import json

    if not _DEVICE_PATH.exists():
        return False
    try:
        client.set_settings(json.loads(_DEVICE_PATH.read_text(encoding="utf-8")))
        log.info("ig poll: reusing the stored device fingerprint")
        return True
    except Exception as e:
        log.warning("ig poll: stored device unusable (%s) - a new one will be made", e)
        return False


def _new_client():
    from instagrapi import Client

    client = Client()
    _make_responsive(client)
    _apply_device(client)

    # This server's address is refused by Instagram outright - even
    # users/<id>/info/ during login comes back as a 403 html page, and the
    # same host answers Shazam's endpoint with an identical 403. Two
    # unrelated services refusing one IP is the IP, not the code, and no
    # retry or credential changes where a request comes from.
    if settings.ig_dm_proxy:
        try:
            client.set_proxy(settings.ig_dm_proxy)
            log.info("ig poll: routing through the configured proxy")
        except Exception as e:
            log.error("ig poll: IG_DM_PROXY rejected (%s) - continuing direct", e)

    return client


def _warm_up(client) -> None:
    """A couple of ordinary reads before touching direct.

    A client that signs in and immediately asks for the inbox does not look
    like the app, which opens a feed first. Cheap insurance on the request
    that is actually being refused.
    """
    for call in (client.get_timeline_feed,):
        try:
            call()
        except Exception as e:
            log.info("ig poll: warm-up call failed (%s) - continuing", e)


def _login():
    """Get a working client, trying the least risky route first.

    Three routes, in this order:

      1. the stored session file
      2. IG_DM_SESSIONID - a sessionid cookie taken from a browser where the
         account is already signed in
      3. a username/password login from this server

    Route 3 is last because on a datacenter address it usually loses. It sends
    a device fingerprint Instagram has never seen from an IP it does not
    trust, and the answer is:

        BadPassword: We can send you an email to help you get back into your
        account. This can also happen when Instagram rejects the proxy/IP,
        device fingerprint, or login context, even if the password is correct.

    The password was correct. The login context was the problem. Route 2
    avoids it entirely: the sign-in already happened somewhere Instagram
    trusts, and the server only carries the result.
    """
    global _client

    with _client_lock:
        if _client is not None:
            return _client

        if _SESSION_PATH.exists():
            client = _new_client()
            try:
                client.load_settings(str(_SESSION_PATH))
                _make_responsive(client)
                client.get_timeline_feed()  # proves the session is really live
                _client = client
                log.info("ig poll: reused the stored session")
                return _client
            except Exception as e:
                log.warning("ig poll: stored session rejected (%s)", e)

        if settings.ig_dm_sessionid:
            client = _new_client()
            client.login_by_sessionid(settings.ig_dm_sessionid)
            _warm_up(client)
            _save(client)
            _client = client
            log.info("ig poll: signed in with the sessionid cookie")
            return _client

        if not settings.ig_dm_password:
            raise RuntimeError(
                "no IG_DM_SESSIONID and no IG_DM_PASSWORD - run 'botctl igdirect'"
            )

        client = _new_client()
        client.login(settings.ig_dm_username, settings.ig_dm_password)
        _warm_up(client)
        _save(client)
        _client = client
        log.info("ig poll: logged in with a password as %s", settings.ig_dm_username)
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


# Item parsing lives in modules/ig_items.py: Instagram serves the same
# inbox payload from the mobile and the web api, so both readers hand their
# items to one parser and only the transport differs.


@run_in_thread
def _collect(threads_wanted: int, since: float, with_pending: bool = True) -> list[DirectMessage]:
    """One synchronous sweep of the inbox. Returns what is newer than `since`."""
    client = _login()
    out: list[DirectMessage] = []

    # Fetch through instagrapi's own method, read the result raw.
    #
    # Calling private_request directly looked equivalent and was not: whatever
    # else differs, direct_threads() was answering 200 for hours and the
    # hand-built request answered 403. Its raw payload is still what gets
    # parsed - client.last_json holds the response the models were built from,
    # so the story fields the model layer drops are all still visible.
    #
    # The model construction is wasted work, but it is a few hundred
    # milliseconds against a poll interval of seconds, and it is the
    # difference between working and not.
    try:
        client.direct_threads(amount=threads_wanted, thread_message_limit=5)
    except TypeError:
        client.direct_threads(amount=threads_wanted)
    threads = list(((client.last_json or {}).get("inbox") or {}).get("threads") or [])

    if with_pending:
        try:
            # Message requests from people who have never messaged us before
            # land here rather than in the inbox, and a first-time sharer is
            # exactly the case that matters.
            client.direct_pending_inbox(amount=threads_wanted)
            threads += ((client.last_json or {}).get("inbox") or {}).get("threads") or []
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

            message = to_direct_message(item, "poll", me)
            if message:
                out.append(message)

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

# Instagram refusing the session outright rather than asking us to slow down.
# 1404006 with a 403 is what a flagged account gets on every private call;
# the rest are the named forms of the same thing.
# Instagram refusing to let this client in at all. A human has to act, so the
# poller stops rather than retrying something that cannot start succeeding.
#
# NOT in this list, deliberately: error_code 1404006 and a bare 403. Those are
# the generic direct-messaging refusal, and treating them as an account ban
# was wrong - the account signed in fine on a phone the whole time. It was our
# request that was malformed, not the account that was banned.
_BLOCK_MARKERS = (
    "challenge_required", "checkpoint", "login_required", "consent_required",
    "feedback_required", "user_has_logged_out",
    # instagrapi's exception class names, matched because the prose does not
    # always contain the machine-readable form.
    "challengerequired", "loginrequired", "checkpointrequired",
    "badpassword", "twofactorrequired",
    "get back into your account", "rejects the proxy", "device fingerprint",
    "two-factor", "verification code",
)

# A refused sign-in and a disabled account need completely different answers,
# and telling someone their account is banned when it is not sends them to fix
# the wrong thing.
_LOGIN_MARKERS = (
    "badpassword", "get back into your account", "rejects the proxy",
    "device fingerprint", "login context",
)


def _is_login_problem(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _LOGIN_MARKERS)

# Set when the account is refused. Retrying past this point cannot succeed and
# makes the flag worse, so the loop stops and says so instead.
blocked_reason: str = ""
blocked_at: float = 0.0

_alert = None


def set_alert(fn) -> None:
    """Where to shout when the account gets blocked. Wired to the admin chat."""
    global _alert
    _alert = fn


def _is_blocked(text: str) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _BLOCK_MARKERS)


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
            global blocked_reason, blocked_at

            # The class name matters as much as the message: instagrapi
            # raises ChallengeRequired with prose that never says
            # "challenge_required".
            text = f"{type(e).__name__}: {e}"

            # Refused, not throttled. No interval fixes this: every further
            # request is another automated call from an address Instagram has
            # already decided about. The first version treated a 403 as an
            # ordinary error and retried every 9 seconds indefinitely, which
            # is the opposite of what a flagged account needs.
            if _is_blocked(text):
                blocked_reason, blocked_at = text[:300], time.time()
                log.error(
                    "ig poll: Instagram has BLOCKED this account - stopping the poller.\n"
                    "  %s\n"
                    "  Recovery: open the account in the Instagram app, clear any "
                    "security prompt, then run 'botctl igreset'.",
                    text[:300],
                )
                if _alert:
                    if _is_login_problem(text):
                        message = (
                            "🔑 اینستاگرام لاگین بات رو رد کرد.\n\n"
                            "این یعنی اکانت بن نیست — لاگین *از این سرور* قبول نشد. "
                            "پسورد هم معمولا درسته؛ اینستاگرام به IP دیتاسنتر "
                            "اعتماد نمی‌کنه.\n\n"
                            "راه‌حل: به‌جای پسورد، کوکی sessionid بده.\n"
                            "رو سرور:  botctl igdirect  → گزینه ۲\n\n"
                            f"جزئیات: {text[:150]}"
                        )
                    else:
                        message = (
                            "🚫 اینستاگرام دسترسی اکانت رو محدود کرده.\n\n"
                            "پولینگ متوقف شد تا وضعیت بدتر نشه.\n\n"
                            "۱. با همون اکانت تو اپ اینستاگرام لاگین کن\n"
                            "۲. اگه پیام امنیتی داد تاییدش کن\n"
                            "۳. رو سرور: botctl igreset\n\n"
                            f"جزئیات: {text[:150]}"
                        )
                    try:
                        await _alert(message)
                    except Exception:
                        pass
                return  # the loop ends here, deliberately

            _throttled += 1
            penalty = min(600, idle * (2 ** _throttled))
            if any(marker in text.lower() for marker in _THROTTLE_MARKERS):
                log.warning("ig poll: throttled by Instagram (%s) - backing off to %ds", e, penalty)
            else:
                # Everything unrecognised backs off too, and keeps backing
                # off. A flat retry delay on a persistent failure is just a
                # slower infinite loop.
                log.warning("ig poll: sweep failed (%s) - backing off to %ds", e, penalty)
            await asyncio.sleep(penalty)
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
    if blocked_reason:
        raise RuntimeError(f"account is blocked by Instagram: {blocked_reason[:120]}")

    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(dispatch))


def clear_block() -> None:
    """Called after a human has cleared the flag in the Instagram app. Drops
    the cached client too, so the next call logs in fresh."""
    global blocked_reason, blocked_at, _client, _throttled

    blocked_reason, blocked_at, _throttled = "", 0.0, 0
    with _client_lock:
        _client = None


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
    if blocked_reason:
        return False, f"BLOCKED by Instagram - {blocked_reason[:120]}"
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
