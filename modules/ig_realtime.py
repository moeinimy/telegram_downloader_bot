"""
The inbox over Instagram's own realtime channel - MQTT, no polling at all.

Why this exists, after three accounts were checkpointed.

Every previous source asks Instagram, on a timer, whether anything new has
arrived. The real Instagram app never does that: it opens one MQTT connection
and waits to be told. So no matter what interval we choose, the traffic has a
shape the app never produces - and lowering it from 5,760 requests a day to
396 did not stop the checkpoints, which is the evidence that the rate was not
the thing being noticed.

This holds a single connection open and receives Direct events as they
happen. Zero requests per hour while idle, and delivery in about the time it
takes Instagram to push - which is also the fastest this feature can ever be,
so the speed-versus-safety trade that has run through every poll interval
simply stops existing.

    aiograpi's realtime support is EXPERIMENTAL and the payload is a private
    transport Instagram can change without notice.

Two things make that survivable. The payload is parsed by shape in
modules/ig_items.py rather than by documented field names, which is how the
poll sources already cope with Instagram renaming things. And this is one
Source among several: if the connection cannot be established or dies for
good, modules/ig_direct.py falls back to the web poller and the feature keeps
working, slower and riskier but working.

aiograpi is a separate install (`botctl igmqtt`) for the same reason
instagrapi is: it must not be able to break a working venv on update.
"""

from __future__ import annotations

import asyncio
import logging
import time

from config import settings
from modules.ig_direct import Dispatch, Source
from modules.ig_items import to_direct_message
from utils import proxies

log = logging.getLogger(__name__)

_client = None
_realtime = None
_task: asyncio.Task | None = None
_dispatch: Dispatch | None = None

connected_since = 0.0
last_event = 0.0
last_error = ""

# When start() was called. Connecting involves a sign-in and an MQTT
# handshake and took nine seconds on the first run, so "not connected"
# during that window means "still connecting", not "broken".
_started_at = 0.0
_CONNECT_GRACE = 60.0


def available() -> bool:
    try:
        import aiograpi  # noqa: F401

        return True
    except Exception:
        return False


def usable() -> bool:
    return bool(available() and settings.ig_dm_username
                and (settings.ig_dm_sessionid or settings.ig_dm_password))


async def _connect():
    """Sign in and open the realtime channel.

    The sessionid is tried first for the same reason it is everywhere else in
    this feature: a password login originating from this server is a login
    context Instagram does not trust, and it answers BadPassword to a correct
    password. aiograpi documents only the password flow, so the cookie path
    may not work - hence the fallback rather than an assumption.
    """
    global _client, _realtime

    from aiograpi import Client

    client = Client()

    # aiograpi is async and rejects socks5h the way httpx does. Going direct
    # on a refused proxy is also wrong here: the sign-in that follows would
    # leave from the server's own address, which is the address Instagram has
    # been refusing all along - so a proxy that was asked for and not applied
    # has to be an error, not a warning.
    proxy = proxies.normalize(settings.ig_dm_proxy)
    if proxy:
        client.set_proxy(proxy)

    signed_in = False
    if settings.ig_dm_sessionid:
        try:
            await client.login_by_sessionid(settings.ig_dm_sessionid)
            signed_in = True
            log.info("ig mqtt: signed in with the sessionid cookie")
        except Exception as e:
            log.warning("ig mqtt: sessionid login failed (%s)", e)

    if not signed_in:
        if not settings.ig_dm_password:
            raise RuntimeError("sessionid login failed and no IG_DM_PASSWORD is set")
        await client.login(settings.ig_dm_username, settings.ig_dm_password)
        log.info("ig mqtt: signed in with a password")

    realtime = await client.realtime_connect()
    await realtime.direct_subscribe()

    _client, _realtime = client, realtime
    return client, realtime


def _on_message(payload) -> None:
    """Instagram pushed something. Turn it into DirectMessages and dispatch.

    Called from the library's read loop, so it must not block and must not
    raise: an exception here would take the connection down with it.
    """
    global last_event

    try:
        last_event = time.time()
        for item in _items_in(payload):
            message = to_direct_message(item, "mqtt", settings.ig_dm_ds_user_id)
            if message and _dispatch:
                asyncio.create_task(_dispatch(message))
    except Exception:
        log.exception("ig mqtt: could not handle a realtime payload")


_IDLE_MARKERS = ("timed out", "timeout", "temporarily unavailable",
                 "would block", "read operation")


def _is_idle_timeout(error: Exception) -> bool:
    """An empty read, not a broken connection.

    MQTT holds the socket open and delivers nothing while the inbox is quiet,
    so the read hitting its deadline is the expected state - it is what "no
    new messages" looks like on a push channel.
    """
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return True
    text = f"{type(error).__name__} {error}".lower()
    return any(marker in text for marker in _IDLE_MARKERS)


def _items_in(payload) -> list[dict]:
    """Every DM item inside a realtime payload, whatever it is wrapped in.

    The transport is private and its shape is not documented, so this looks
    for the shape rather than a path: a dict carrying an item_id and a
    timestamp is a message, wherever it turns up.
    """
    found: list[dict] = []

    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, (list, tuple)):
            for entry in node:
                walk(entry, depth + 1)
            return
        if not isinstance(node, dict):
            return
        if "item_id" in node and "timestamp" in node:
            found.append(node)
            return
        for value in node.values():
            walk(value, depth + 1)

    walk(payload)
    return found


async def _loop(dispatch: Dispatch) -> None:
    global _dispatch, connected_since, last_error, _client, _realtime

    _dispatch = dispatch
    attempt = 0

    while True:
        try:
            client, realtime = await _connect()
            client.realtime_on("message", _on_message)
            connected_since = time.time()
            last_error = ""
            attempt = 0
            log.info("ig mqtt: realtime connected - polling is no longer needed")

            # One connection, read until it drops. No requests are made while
            # this waits; that is the entire point.
            #
            # A read that times out is the NORMAL case, not a failure: an idle
            # inbox has nothing to deliver and the socket says so on a timer.
            # Treating that as a lost connection tore down and rebuilt a
            # perfectly healthy channel every few seconds - and, worse, ran
            # the teardown that was signing the session out.
            idle_reads = 0
            while True:
                try:
                    await client.realtime_read_once()
                    idle_reads = 0
                except Exception as read_error:
                    if not _is_idle_timeout(read_error):
                        raise
                    idle_reads += 1
                    # A keepalive now and then, so a socket that is genuinely
                    # dead is still noticed instead of looking merely quiet.
                    if idle_reads % 10 == 0:
                        ping = getattr(realtime, "ping", None)
                        if ping:
                            result = ping()
                            if asyncio.iscoroutine(result):
                                await result

        except asyncio.CancelledError:
            raise
        except Exception as e:
            connected_since = 0.0
            last_error = str(e)[:200]
            attempt += 1
            # A realtime drop is normal - phones lose connections too. Rebuild
            # calmly rather than hammering, and cap the wait so a night-time
            # outage does not leave it asleep all morning.
            wait = min(300, 10 * (2 ** min(attempt, 5)))
            log.warning("ig mqtt: connection lost (%s) - reconnecting in %ds", e, wait)
            await _close()
            await asyncio.sleep(wait)


async def _close() -> None:
    """Drop the transport. Never the session.

    The first version called client.logout() here, which does not close a
    socket - it tells Instagram to invalidate the session, server-side:

        POST https://i.instagram.com/api/v1/accounts/logout/

    That destroys the sessionid cookie this whole feature is built on, and it
    ran on every reconnect. A dropped connection is not a reason to sign out;
    a phone that loses signal does not log you out of Instagram.
    """
    global _client, _realtime

    if _realtime is not None:
        try:
            call = getattr(_realtime, "disconnect", None)
            if call:
                result = call()
                if asyncio.iscoroutine(result):
                    await result
        except Exception:
            pass

    _realtime = _client = None


# ---------------- Source interface ----------------

async def start(dispatch: Dispatch) -> None:
    global _task

    global _started_at

    if not usable():
        raise RuntimeError("aiograpi is not installed, or no credentials are set")
    _started_at = time.time()
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(dispatch))


async def stop() -> None:
    global _task

    if _task:
        _task.cancel()
        _task = None
    await _close()


async def health() -> tuple[bool, str]:
    if not available():
        return False, "aiograpi not installed"

    if not connected_since:
        # Connecting means signing in and completing an MQTT handshake, which
        # took nine seconds on the first run. The supervisor's first health
        # check lands a second after start, so without this grace period it
        # declared realtime dead and started the poller against a channel that
        # was about to come up.
        if _started_at and time.time() - _started_at < _CONNECT_GRACE:
            return True, "connecting…"
        return False, last_error or "not connected yet"

    up = (time.time() - connected_since) / 60
    if last_event:
        quiet = (time.time() - last_event) / 60
        return True, f"realtime up {up:.0f}m, last event {quiet:.0f}m ago"
    return True, f"realtime up {up:.0f}m, no events yet"


def source() -> Source:
    return Source(name="mqtt", start=start, stop=stop, health=health)
