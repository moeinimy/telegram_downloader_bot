"""The address Instagram actually sees us from, and whether it stays put.

A sessionid is bound to the address it was issued to. This bot reaches
Instagram through Cloudflare WARP, which is anycast: nothing promises the exit
stays the same, and a session that hops between addresses is a session
Instagram has good reason to close.

That is a theory, and this feature has already cost several nights to theories
that were argued rather than measured. So this records the address instead of
assuming what it does. When the next cookie dies, "did the exit move first?"
has an answer with a timestamp on it, and the choice between buying a static
proxy and looking somewhere else stops being a guess.

Nothing here touches Instagram. It asks the same endpoint `botctl proxy`
already uses, through the same proxy, at the cadence of the health loop.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# api.ipify.org is what deploy/manage.sh already probes with, so a reading here
# and a reading from botctl are comparable.
_ENDPOINT = "https://api.ipify.org"

current: str = ""
since: float = 0.0
last_error: str = ""

# (when, from, to). Bounded: this is evidence for the last failure, not a log.
moves: list[tuple[float, str, str]] = []
_MAX_MOVES = 20


def _note(ip: str) -> bool:
    """Record a reading. True when the address actually changed."""
    global current, since

    if not ip:
        return False

    if not current:
        current, since = ip, time.time()
        log.info("ig exit ip: %s", ip)
        return False

    if ip == current:
        return False

    held = (time.time() - since) / 60
    moves.append((time.time(), current, ip))
    del moves[:-_MAX_MOVES]
    log.warning("ig exit ip: moved %s -> %s after %.0f min - a sessionid issued "
                "to the old address may stop being accepted", current, ip, held)
    current, since = ip, time.time()
    return True


async def refresh(proxy: str | None) -> str:
    """Read the exit address through the same proxy Instagram is reached on."""
    global last_error

    import asyncio

    import httpx

    from utils import proxies

    def read() -> str:
        kwargs = {"timeout": 10.0}
        normalised = proxies.normalize(proxy)
        if normalised:
            # httpx renamed this argument; ig_web carries the same fallback.
            try:
                client = httpx.Client(proxy=normalised, **kwargs)
            except TypeError:
                client = httpx.Client(proxies=normalised, **kwargs)
        else:
            client = httpx.Client(**kwargs)
        with client:
            return client.get(_ENDPOINT).text.strip()

    try:
        ip = await asyncio.to_thread(read)
        last_error = ""
        _note(ip)
        return ip
    except Exception as e:
        # Not reaching ipify says nothing about the session, so this stays a
        # note rather than a source of failover decisions.
        last_error = f"{type(e).__name__}: {e}"[:120]
        log.info("ig exit ip: could not be read (%s)", last_error)
        return ""


def held_minutes() -> float:
    return (time.time() - since) / 60 if since else 0.0


def summary() -> str:
    """One line for an admin alert: was the address stable, or was it not."""
    if not current:
        return ""
    if not moves:
        return f"IP خروجی از ابتدا ثابت بوده: {current} ({held_minutes():.0f} دقیقه)"

    recent = sum(1 for when, _, _ in moves if time.time() - when < 86400)
    return (f"IP خروجی در ۲۴ ساعت گذشته {recent} بار عوض شد "
            f"(الان {current}، {held_minutes():.0f} دقیقه ثابت)")


def status() -> dict:
    return {
        "current": current,
        "held_minutes": held_minutes(),
        "moves": len(moves),
        "moves_24h": sum(1 for when, _, _ in moves if time.time() - when < 86400),
        "error": last_error,
    }
