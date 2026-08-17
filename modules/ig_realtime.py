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


# The mobile session written by `botctl iglogin`, shared with ig_private.
SESSION_FILE = settings.download_dir / "ig_private_session.json"


def usable() -> bool:
    # A stored mobile session counts as a credential in its own right. It is
    # the only one this api actually accepts, so requiring a browser cookie or
    # a password beside it would refuse to start realtime in exactly the setup
    # built for it.
    return bool(available() and settings.ig_dm_username
                and (SESSION_FILE.exists() or settings.ig_dm_sessionid
                     or settings.ig_dm_password))


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
    cookie_error = ""

    # A stored MOBILE session first, whenever one exists.
    #
    # This is the whole difference between realtime working and not. A browser
    # sessionid belongs to the web api; handing it to the mobile api is what
    # produces "We're sorry, but something went wrong" here, over and over,
    # while the web poller uses the same cookie without trouble. A session
    # created by a real mobile sign-in is native to this api, and lasts months
    # rather than hours, so it is tried ahead of the cookie rather than after.
    #
    # Written by `botctl iglogin` and shared with modules/ig_private.py, which
    # has kept it in this exact format all along.
    if SESSION_FILE.exists():
        try:
            client.load_settings(str(SESSION_FILE))
            await client.get_timeline_feed()  # proves it is really live
            signed_in = True
            log.info("ig mqtt: reused the stored mobile session")
        except Exception as e:
            log.warning("ig mqtt: stored mobile session rejected (%s)", e)

    if not signed_in and settings.ig_dm_sessionid:
        try:
            await client.login_by_sessionid(settings.ig_dm_sessionid)
            signed_in = True
            log.info("ig mqtt: signed in with the sessionid cookie")
        except Exception as e:
            cookie_error = str(e)
            log.warning("ig mqtt: sessionid login failed (%s)", e)

    if not signed_in:
        if not settings.ig_dm_password:
            # WHY the cookie was refused decides whether retrying can ever
            # work, so it has to travel with the exception. Leaving it behind
            # in a log line left the reconnect loop unable to tell a dead
            # credential from a dropped socket, and it retried for both.
            raise RuntimeError(
                "sessionid login failed and no IG_DM_PASSWORD is set: " + cookie_error
            )
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


# Instagram saying the credential itself is gone, as opposed to the socket
# being gone. `user_has_logged_out` with `logout_reason` is what a session that
# was invalidated server-side answers - which is exactly what the old teardown
# did to this account by calling accounts/logout/ on every reconnect.
#
# NOT in this list, deliberately: checkpoint_required and challenge_required. A
# checkpoint lifts by itself within hours and the poller is built to wait it
# out; treating it as terminal switches the feature off through a recovery that
# would have happened on its own.
#
# A redirect loop belongs here too, and it is not a network fault: an expired
# cookie makes Instagram answer the sign-in with a bounce to the login page,
# which bounces again, until the client gives up counting. A proxy that is
# genuinely broken fails to connect - it does not redirect.
_DEAD_MARKERS = ("user_has_logged_out", "logout_reason",
                 "login_required", "loginrequired",
                 "exceeded maximum allowed redirects", "too many redirects")

_IDLE_MARKERS = ("timed out", "timeout", "temporarily unavailable",
                 "would block", "read operation")

# Set when the cookie is refused outright. A human has to paste a new one, so
# the loop stops rather than signing in forever against a dead session.
dead_reason: str = ""
dead_since: float = 0.0

# Set when realtime stops trying because Instagram keeps refusing it. Not the
# same as a dead cookie: the cookie is fine, the web source is using it. This
# is only realtime's own path being closed.
given_up: str = ""

_alert = None


def set_alert(fn) -> None:
    """Where to shout when the cookie dies. Wired to the admin chat."""
    global _alert
    _alert = fn


async def _warn_admin(message: str) -> None:
    if _alert is None:
        return
    try:
        await _alert(message)
    except Exception as e:
        log.info("ig mqtt: could not reach an admin (%s)", e)


def _is_dead_credential(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _DEAD_MARKERS)


# Instagram's generic error page. It is not a named refusal, so it is not in
# _DEAD_MARKERS and the ladder keeps trying - which is right, because it does
# sometimes pass. What it usually means here is the address: this is what a
# datacenter IP gets served, and the exit is now the VPS itself.
_REFUSED_MARKERS = ("something went wrong", "please try again")

# Retrying past this has never once worked in this feature's history, and a
# loop nobody is told about is how eight hours went by last time.
_ALERT_AFTER = 5


def _is_refusal(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _REFUSED_MARKERS)


def _exit_ip_line() -> str:
    """A cookie is bound to the address it was issued to, so whether that
    address moved belongs in the message that asks for a new one."""
    try:
        from utils import exit_ip

        line = exit_ip.summary()
        return f"{line}\n\n" if line else ""
    except Exception:
        return ""


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
            global dead_reason, dead_since

            connected_since = 0.0
            last_error = str(e)[:200]

            # A dead cookie is not a dropped connection, and the ladder below
            # is the wrong answer to it. Every rung re-runs the sign-in, so a
            # session Instagram has already answered with user_has_logged_out
            # would be posting a login every five minutes, forever, with no
            # reachable outcome - which is precisely the mechanical request
            # pattern that got this account checkpointed three times. Retrying
            # here cannot succeed and can only make the flag worse.
            if _is_dead_credential(last_error):
                dead_reason, dead_since = last_error, time.time()
                log.error("ig mqtt: the sessionid is dead (%s) - stopping. "
                          "A new cookie has to be pasted: botctl igdirect", last_error)
                await _close()
                await _warn_admin(
                    "🔑 کوکی اینستاگرام بات باطل شده و دایرکت خوابید.\n\n"
                    "اینستاگرام می‌گه این session خارج شده، پس هیچ تلاش دوباره‌ای "
                    "جواب نمی‌ده و بات دست از تلاش برداشت تا اکانت flag نشه.\n\n"
                    "برای راه‌اندازی دوباره:\n"
                    "۱. با مرورگر (نه اپ) با همون اکانت وارد اینستاگرام شو\n"
                    "۲. کوکی sessionid تازه رو بردار\n"
                    "۳. رو سرور: botctl igdirect → گزینه ۲\n\n"
                    f"{_exit_ip_line()}"
                    f"جزئیات: {last_error[:120]}"
                )
                return

            attempt += 1
            # A realtime drop is normal - phones lose connections too. Rebuild
            # calmly rather than hammering, and cap the wait so a night-time
            # outage does not leave it asleep all morning.
            wait = min(300, 10 * (2 ** min(attempt, 5)))
            log.warning("ig mqtt: connection lost (%s) - reconnecting in %ds", e, wait)
            await _close()

            # Backing off forever is still an outage; it is just a quiet one.
            # Say it once, at the point where the retries have stopped being
            # optimism, and keep trying after that.
            # Known, expected, and already answered: a browser cookie being
            # refused by the mobile api, with no mobile session on disk to try
            # instead. That is not news - it is the documented split this
            # feature has known about from the start - and paging about it on
            # every restart trains the reader to ignore the alerts that do
            # matter. It is logged, /srcstatus carries it, and the remedy
            # (botctl iglogin) does not become more available by being sent
            # twice an hour.
            expected = _is_refusal(last_error) and not SESSION_FILE.exists()

            if attempt == _ALERT_AFTER and expected:
                log.warning("ig mqtt: refused with only a browser cookie to "
                            "offer - expected, not alerting. The web source "
                            "has the inbox; botctl iglogin fixes this for good.")

            if attempt == _ALERT_AFTER and not expected:
                if _is_refusal(last_error):
                    # Said "your address is blocked" here once. Then the web
                    # source kept working from that same address with that
                    # same cookie, which disproves it: what is refused is this
                    # cookie on the MOBILE api, the split this feature has
                    # known about since the beginning. realtime is the only
                    # source that goes there.
                    detail = (
                        "این خطای عمومی اینستاگرامه و مخصوص realtimeست، چون "
                        "تنها منبعیه که از API موبایل رد می‌شه — و کوکی مرورگر "
                        "اونجا همیشگی نیست.\n\n"
                        "اگه دایرکت داره کار می‌کنه، یعنی منبع web با همین کوکی "
                        "سالمه و چیزی از دست نرفته؛ فقط پولینگ به‌جای realtime."
                    )
                else:
                    detail = "بات هر ۵ دقیقه دوباره تلاش می‌کنه."
                await _warn_admin(
                    f"📡 دایرکت اینستاگرام بعد از {attempt} تلاش وصل نشد.\n\n"
                    f"{detail}\n\n"
                    f"{_exit_ip_line()}"
                    f"جزئیات: {last_error[:140]}"
                )

            # A refusal is not a connection that might come back, and retrying
            # it is not free. Every attempt is a failed mobile-api sign-in on
            # the same account the web poller depends on - roughly 288 of them
            # a day, forever, on an account whose problem is being noticed.
            # The web source has been delivering intermittently while this ran,
            # which is at least consistent with it being throttled for them.
            #
            # So: stop. A browser cookie on the mobile api is not something
            # that starts working on the twentieth try, and a restart is the
            # right way to retry after a genuinely new credential.
            if attempt >= _ALERT_AFTER and _is_refusal(last_error):
                global given_up
                given_up = last_error
                log.error("ig mqtt: refused %d times - giving up rather than "
                          "signing in at the account all day. The web source "
                          "carries the inbox; restart to try realtime again.",
                          attempt)
                await _close()
                return

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
    if dead_reason:
        raise RuntimeError(f"the sessionid is dead: {dead_reason[:120]}")
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

    # Ahead of the grace period: a dead cookie is not "still connecting", and
    # reporting it as such would hide the one failure a human has to fix.
    if dead_reason:
        held = (time.time() - dead_since) / 60
        return False, f"sessionid is dead ({held:.0f}m) - paste a fresh one: {dead_reason[:90]}"

    # Distinct from a dead cookie on purpose: the cookie works, the web source
    # is using it right now. Only realtime's own path is closed, and saying
    # "dead" here would send someone to replace a credential that is fine.
    if given_up:
        if not SESSION_FILE.exists():
            return False, ("کوکی مرورگر رو api موبایل قبول نمی‌شه — web داره "
                           "اینباکس رو می‌بره. راه‌حل دائمی: botctl iglogin")
        return False, f"realtime refused - web is carrying the inbox: {given_up[:80]}"

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
