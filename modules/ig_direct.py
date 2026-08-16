"""
Instagram inbox, as one interface with two interchangeable implementations.

Nothing outside this module knows whether a DM arrived over Meta's webhook or
was scraped out of the inbox by an unofficial client. Both produce a
DirectMessage; the handler treats them identically.

Why two:

The official path is the only one that survives long term, but with Standard
Access Meta only delivers webhooks for senders who hold a role on the app -
everyone else needs Advanced Access, i.e. App Review. Until that lands, and
any time the token or the subscription breaks afterwards, the official path
goes silent. The unofficial poller covers that gap.

It is a STANDBY, not a peer. It only runs while the official path is failing
a health check, because logging into the same account with an unofficial
client is the surest way to get it banned. Silence is not the trigger - a
quiet inbox is normal and proves nothing - a failing health check is.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

Dispatch = Callable[["DirectMessage"], Awaitable[None]]


@dataclass
class DirectMessage:
    """One incoming Instagram DM, normalised across both sources."""

    igsid: str                      # who sent it, scoped to our account
    mid: str = ""                   # source's own message id, for de-duplication
    text: str = ""                  # carries the pairing token
    media_url: str = ""             # short-lived CDN url from the attachment
    permalink: str = ""             # the instagram.com link, when derivable
    media_id: str = ""              # reel/media id, convertible to a shortcode
    timestamp: float = 0.0
    source: str = ""                # "webhook" | "poll"
    raw: dict = field(default_factory=dict)

    def identity(self) -> str:
        """The namespaced id the pairing store keys on.

        The two sources see different numbers for the same person - Meta's
        IGSID is scoped to our app, the unofficial client sees the raw account
        pk - and nothing can map between them, so the namespace is part of the
        identity rather than an implementation detail.
        """
        return f"{'ig' if self.source == 'webhook' else 'pk'}:{self.igsid}"

    def shortcode(self) -> str:
        """The shortcode this DM points at, or "" if it only has a CDN url.

        Two routes, because the two sources describe the same share
        differently: a permalink can be parsed directly, while Meta's ig_reel
        attachment carries only a numeric media id and the video's CDN url.
        """
        from modules.instagram import media_id_to_shortcode
        from utils.url_router import InstagramKind, Platform, route

        if self.permalink:
            result = route(self.permalink)
            if result and result.platform == Platform.INSTAGRAM and result.resource_id:
                # A story url resolves to a USERNAME, not a shortcode - a
                # story has no shortcode at all. Returning it would have the
                # caller fetch a post named after the poster.
                if result.kind not in (InstagramKind.STORY.value, InstagramKind.PROFILE.value):
                    return result.resource_id

        # Some payloads bury the permalink in the text instead of a field.
        found = _IG_LINK_RE.search(self.text or "")
        if found:
            result = route(found.group(0))
            if result and result.resource_id:
                return result.resource_id

        if self.media_id:
            return media_id_to_shortcode(self.media_id)
        return ""

    def dedup_keys(self) -> list[str]:
        """Two keys, because the sources number messages differently.

        `mid` catches Meta's own retries, which are routine rather than
        exceptional. The content key catches the case that matters during a
        failover: the same DM seen once by the webhook and once by the poller,
        under two completely unrelated ids.
        """
        keys = []
        if self.mid:
            keys.append(f"mid:{self.source}:{self.mid}")

        material = self.permalink or self.media_id or self.media_url or self.text
        if material and self.igsid:
            digest = hashlib.sha1(material.encode("utf-8", "replace")).hexdigest()[:12]
            # Bucketed by the minute: the same reel shared twice inside one
            # minute is a duplicate, an hour later it is a second request.
            keys.append(f"msg:{self.igsid}:{digest}:{int(self.timestamp // 60)}")
        return keys


_IG_LINK_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:reels?|p|tv|stories)/[^\s]+", re.IGNORECASE
)


# --------------------------------------------------------------------------
# Seen-message store
#
# Survives restarts on purpose: Meta retries a notification it did not get a
# 200 for, and a restart is exactly when that happens. An in-memory set would
# re-upload every one of them.
# --------------------------------------------------------------------------

_SEEN_PATH: Path = settings.download_dir / "ig_seen.json"
_SEEN_TTL = 48 * 3600
_SEEN_MAX = 5000
_seen_lock = threading.Lock()
_seen: dict[str, float] | None = None


def _load_seen() -> dict[str, float]:
    global _seen
    if _seen is None:
        try:
            _seen = json.loads(_SEEN_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _seen = {}
        except Exception as e:
            log.warning("ig seen store unreadable (%s) - starting empty", e)
            _seen = {}
    return _seen


def _flush_seen(data: dict[str, float]) -> None:
    try:
        _SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SEEN_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(_SEEN_PATH)
    except Exception as e:
        log.warning("ig seen store write failed: %s", e)


def already_seen(dm: DirectMessage) -> bool:
    """True if this message was handled before. Records it when it was not,
    so this is a claim, not a question - call it exactly once per message."""
    keys = dm.dedup_keys()
    if not keys:
        return False

    now = time.time()
    with _seen_lock:
        data = _load_seen()
        if any(key in data for key in keys):
            return True

        for key in keys:
            data[key] = now

        cutoff = now - _SEEN_TTL
        stale = [k for k, seen_at in data.items() if seen_at < cutoff]
        for key in stale:
            data.pop(key, None)
        if len(data) > _SEEN_MAX:
            for key, _ in sorted(data.items(), key=lambda kv: kv[1])[: len(data) - _SEEN_MAX]:
                data.pop(key, None)

        _flush_seen(data)
    return False


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

@dataclass
class Source:
    """One way of reading the inbox. Both implementations expose exactly this."""

    name: str
    start: Callable[[Dispatch], Awaitable[None]]
    stop: Callable[[], Awaitable[None]]
    health: Callable[[], Awaitable[tuple[bool, str]]]
    # Fetch anything missed while this source was asleep. Only the poller can
    # do it - a webhook cannot be asked about the past.
    catch_up: Callable[[Dispatch], Awaitable[int]] | None = None


@dataclass
class _SourceState:
    configured: bool = False
    running: bool = False
    healthy: bool | None = None
    detail: str = ""
    last_event: float = 0.0
    last_check: float = 0.0
    events: int = 0


_states: dict[str, _SourceState] = {
    "webhook": _SourceState(),
    "mqtt": _SourceState(),
    "web": _SourceState(),
    "poll": _SourceState(),
}
_sources: dict[str, Source] = {}
_on_message: Dispatch | None = None
_health_task: asyncio.Task | None = None
# Handlers run as detached tasks so a 200 goes back to Meta immediately.
# Keeping references stops the garbage collector cancelling them mid-download.
_inflight: set[asyncio.Task] = set()


async def _dispatch(dm: DirectMessage) -> None:
    """Entry point every source calls. De-duplicates, then hands off."""
    state = _states.get(dm.source)
    if state:
        state.last_event = time.time()

    if already_seen(dm):
        log.info("ig direct: duplicate %s from %s (%s) - ignored", dm.mid, dm.igsid, dm.source)
        return

    if state:
        state.events += 1

    if _on_message is None:
        log.warning("ig direct: message arrived before a handler was registered")
        return

    task = asyncio.create_task(_guarded(_on_message, dm))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


async def _guarded(handler: Dispatch, dm: DirectMessage) -> None:
    try:
        await handler(dm)
    except Exception:
        log.exception("ig direct: handler failed for %s from %s", dm.mid, dm.igsid)


def _build_sources() -> dict[str, Source]:
    """Import the implementations lazily: instagrapi is an optional install,
    and a missing one must disable that source rather than stop the bot."""
    built: dict[str, Source] = {}

    if "webhook" in settings.ig_direct_sources and settings.has_ig_webhook:
        try:
            from web import webhook

            built["webhook"] = webhook.source()
            _states["webhook"].configured = True
        except Exception as e:
            # aiohttp arrives as a shazamio dependency rather than a direct
            # one, so it can genuinely be absent on a trimmed install.
            _states["webhook"].detail = f"unavailable: {e}"
            log.error("ig direct: webhook source unavailable (%s)", e)

    if "mqtt" in settings.ig_direct_sources and settings.has_ig_web:
        try:
            from modules import ig_realtime

            built["mqtt"] = ig_realtime.source()
            _states["mqtt"].configured = True
        except Exception as e:
            _states["mqtt"].detail = f"unavailable: {e}"
            log.info("ig direct: realtime source unavailable (%s)", e)

    if "web" in settings.ig_direct_sources and settings.has_ig_web:
        try:
            from modules import ig_web

            built["web"] = ig_web.source()
            _states["web"].configured = True
        except Exception as e:
            _states["web"].detail = f"unavailable: {e}"
            log.warning("ig direct: web source unavailable (%s)", e)

    if "poll" in settings.ig_direct_sources and settings.has_ig_private:
        try:
            from modules import ig_private

            built["poll"] = ig_private.source()
            _states["poll"].configured = True
        except Exception as e:
            _states["poll"].detail = f"unavailable: {e}"
            log.warning("ig direct: poll source unavailable (%s)", e)

    return built


async def start(on_message: Dispatch) -> None:
    """Bring up the configured sources. Safe to call when none are."""
    global _on_message, _health_task

    _on_message = on_message
    _sources.update(_build_sources())

    if not _sources:
        log.info("ig direct: no source configured - feature off")
        return

    # The official path starts immediately; the standby waits for the health
    # loop to decide it is needed.
    if "webhook" in _sources:
        await _sources["webhook"].start(_dispatch)
        _states["webhook"].running = True
        log.info(
            "ig direct: webhook listening on %s:%s%s",
            settings.ig_webhook_host, settings.ig_webhook_port, settings.ig_webhook_path,
        )

    # The web reader starts immediately too when no webhook is configured. It
    # is not a last resort like the mobile poller: it uses the api its cookie
    # was actually issued for, so it has no device to be unknown and nothing
    # to sign.
    # Realtime before the pollers. It is the only source that does not ask
    # Instagram anything on a timer - one connection, held open, exactly what
    # the app does - so while it is up there is no request rate to notice.
    if "mqtt" in _sources and "webhook" not in _sources:
        try:
            await _sources["mqtt"].start(_dispatch)
            _states["mqtt"].running = True
            log.info("ig direct: realtime channel starting - no polling while it holds")
        except Exception as e:
            _states["mqtt"].detail = str(e)[:120]
            log.warning("ig direct: realtime source failed to start (%s)", e)

    if "web" in _sources and "webhook" not in _sources and not _states["mqtt"].running:
        try:
            await _sources["web"].start(_dispatch)
            _states["web"].running = True
            log.info("ig direct: reading the inbox through the web api")
        except Exception as e:
            _states["web"].detail = str(e)[:120]
            log.error("ig direct: web source failed to start: %s", e)

    _health_task = asyncio.create_task(_health_loop())


async def stop() -> None:
    global _health_task

    if _health_task:
        _health_task.cancel()
        _health_task = None

    for name, source in _sources.items():
        if _states[name].running:
            try:
                await source.stop()
            except Exception as e:
                log.warning("ig direct: stopping %s failed: %s", name, e)
            _states[name].running = False


# The first health check runs about a second after start, which is before
# realtime can have finished its handshake - it took seven. So it reads
# "connecting…", which is correct for six more seconds and then sits in
# /srcstatus as the source's status for the whole interval: the panel said
# "connecting…" about a channel that had been up for five minutes.
#
# Re-check once the connect window has closed, then settle into the configured
# cadence. This costs no Instagram traffic: while realtime holds, the only
# health() reached reads module state and asks Instagram nothing.
_SETTLE_SECONDS = 75.0


def _health_wait(first_pass: bool, interval: float) -> float:
    return min(_SETTLE_SECONDS, interval) if first_pass else interval


async def _health_loop() -> None:
    """Decide, on a real signal, whether the standby should be awake."""
    interval = max(60, settings.ig_health_minutes * 60)
    first_pass = True
    while True:
        try:
            await _check_and_failover()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ig direct: health check blew up")
        await asyncio.sleep(_health_wait(first_pass, interval))
        first_pass = False


async def _check_and_failover() -> None:
    webhook = _sources.get("webhook")
    poll = _sources.get("poll")

    healthy = False
    if webhook:
        state = _states["webhook"]
        try:
            healthy, detail = await webhook.health()
        except Exception as e:
            healthy, detail = False, f"{type(e).__name__}: {e}"
        state.healthy, state.detail, state.last_check = healthy, detail, time.time()
        if not healthy:
            log.warning("ig direct: official path unhealthy - %s", detail)

    # The realtime channel is the preferred source, so the pollers exist to
    # cover it being down - which means its health decides whether they run.
    mqtt = _sources.get("mqtt")
    mqtt_ok = False
    if mqtt:
        try:
            mqtt_ok, mqtt_detail = await mqtt.health()
        except Exception as e:
            mqtt_ok, mqtt_detail = False, f"{type(e).__name__}: {e}"
        _states["mqtt"].healthy = mqtt_ok
        _states["mqtt"].detail = mqtt_detail
        _states["mqtt"].last_check = time.time()

    web = _sources.get("web")

    # Poll only while realtime is not carrying the inbox, and stand the poller
    # down again the moment it recovers. Two sources reading the same inbox is
    # double the traffic for no extra coverage.
    if web and mqtt:
        if mqtt_ok and _states["web"].running:
            log.info("ig direct: realtime is up - standing the web poller down")
            try:
                await web.stop()
            except Exception as e:
                log.warning("ig direct: stopping the web poller failed: %s", e)
            _states["web"].running = False
            _states["web"].detail = "standby (realtime is up)"
        elif not mqtt_ok and not _states["web"].running and not healthy:
            log.warning("ig direct: realtime down (%s) - falling back to polling",
                        _states["mqtt"].detail)
            try:
                await web.start(_dispatch)
                _states["web"].running = True
            except Exception as e:
                log.error("ig direct: web poller failed to start: %s", e)

    if web and _states["web"].running:
        try:
            web_ok, web_detail = await web.health()
        except Exception as e:
            web_ok, web_detail = False, f"{type(e).__name__}: {e}"
        _states["web"].healthy = web_ok
        _states["web"].detail = web_detail
        _states["web"].last_check = time.time()

    if not poll:
        return

    # The mobile poller is the last resort: it is the only source that needs
    # a device Instagram has to recognise, and the one whose failures cost
    # the account. It runs only when nothing above it is working.
    web_working = bool(web and _states["web"].running and _states["web"].healthy is not False)
    want_poll = not (healthy or web_working)
    state = _states["poll"]

    if want_poll and not state.running:
        log.warning("ig direct: waking the standby poller")
        try:
            await poll.start(_dispatch)
            state.running = True
            state.healthy, state.detail = True, "active"
        except Exception as e:
            state.healthy, state.detail = False, f"{type(e).__name__}: {e}"
            log.error("ig direct: standby poller failed to start: %s", e)
            return

        # Anything shared while the official path was down is still sitting in
        # the inbox; the webhook can never replay it, so sweep for it here.
        #
        # Only when there IS an official path to have missed something. With
        # the poller as the primary there is no gap to recover, and sweeping
        # would instead walk a day of pre-existing inbox history and DM
        # pairing instructions to everyone who ever messaged the account.
        if webhook and poll.catch_up:
            try:
                found = await poll.catch_up(_dispatch)
                log.info("ig direct: catch-up sweep recovered %d message(s)", found)
            except Exception as e:
                log.warning("ig direct: catch-up sweep failed: %s", e)

    elif not want_poll and state.running:
        log.info("ig direct: official path healthy again - standing the poller down")
        try:
            await poll.stop()
        except Exception as e:
            log.warning("ig direct: stopping the poller failed: %s", e)
        state.running = False
        state.detail = "standby"


async def reply_dm(dm: DirectMessage, text: str) -> bool:
    """Answer inside the Instagram DM, over whichever source it arrived on.

    Best effort on purpose. Instagram only allows a reply within 24 hours of
    the user's last message, and the two sources have entirely different send
    paths - so this is used for pairing feedback only. The media itself always
    goes to Telegram, where none of that applies.
    """
    try:
        if dm.source == "webhook":
            from modules import ig_graph

            return await ig_graph.send_text(dm.igsid, text)

        from modules import ig_private

        return await ig_private.send_text(dm.igsid, text)
    except Exception as e:
        log.info("ig direct: DM reply to %s failed: %s", dm.igsid, e)
        return False


def status() -> dict:
    """Snapshot for /srcstatus."""
    from modules import ig_pairing

    return {
        "enabled": bool(_sources),
        "sources": {
            name: {
                "configured": st.configured,
                "running": st.running,
                "healthy": st.healthy,
                "detail": st.detail,
                "last_event": st.last_event,
                "last_check": st.last_check,
                "events": st.events,
            }
            for name, st in _states.items()
        },
        "public_url": settings.ig_public_url,
        "links": ig_pairing.count(),
        "pending": ig_pairing.pending_count(),
    }
