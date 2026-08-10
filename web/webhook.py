"""
The HTTPS endpoint Meta delivers Instagram DMs to.

Runs inside the bot's own event loop on a loopback port; Caddy or nginx
terminates TLS in front of it. One process, one systemd unit, one thing to
deploy - and the handler can touch the bot's Application directly instead of
shipping events between services.

This is the one part of the bot that is exposed to the open internet, so it
assumes every request is hostile until the signature says otherwise:

* The body is read as raw bytes and the HMAC is checked BEFORE anything is
  parsed. json.loads on unauthenticated input is the vulnerability, not the
  request itself.
* The reply is 200 the moment the signature checks out. Meta retries anything
  that does not answer quickly, and a retry while the first copy is still
  downloading means the same reel uploaded twice.
* Duplicate `mid`s are the normal case, not an exception - handled upstream in
  modules/ig_direct.py, which sees both sources and can catch a duplicate the
  webhook alone cannot.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

from config import settings
from modules.ig_direct import DirectMessage, Dispatch, Source

log = logging.getLogger(__name__)

_runner = None
_dispatch: Dispatch | None = None

# Attachment kinds that carry something worth downloading. Anything else
# (a plain photo the user typed, a sticker, a template) is not a share.
_MEDIA_TYPES = {"ig_reel", "share", "video", "image", "story_mention"}


# ---------------- signature ----------------

def verify_signature(raw: bytes, header: str | None) -> bool:
    """Meta signs the raw body with the app secret as `sha256=<hex>`."""
    if not header or not settings.ig_app_secret:
        return False
    prefix, _, received = header.partition("=")
    if prefix != "sha256" or not received:
        return False
    expected = hmac.new(
        settings.ig_app_secret.encode("utf-8"), raw, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received.strip())


# ---------------- payload parsing ----------------

def parse_entries(payload: dict) -> list[DirectMessage]:
    """Flatten Meta's envelope into DirectMessages.

    One message can carry several attachments, and each is an independent
    thing to download, so each becomes its own DirectMessage with its own
    de-duplication id.
    """
    out: list[DirectMessage] = []

    for entry in payload.get("entry") or []:
        for event in entry.get("messaging") or []:
            message = event.get("message") or {}
            if not message:
                continue  # read receipts, postbacks, reactions
            # Echoes are our own outbound pairing replies coming back around.
            if message.get("is_echo") or message.get("is_deleted"):
                continue

            igsid = str((event.get("sender") or {}).get("id") or "")
            if not igsid:
                continue

            mid = str(message.get("mid") or "")
            # Meta's timestamps are milliseconds.
            ts = float(event.get("timestamp") or 0) / 1000.0

            attachments = message.get("attachments") or []
            if isinstance(attachments, dict):  # some payloads wrap it in {"data": [...]}
                attachments = attachments.get("data") or []

            media = [a for a in attachments if (a or {}).get("type") in _MEDIA_TYPES]

            for index, attachment in enumerate(media):
                payload_field = attachment.get("payload") or {}
                url = str(payload_field.get("url") or "")
                permalink = url if "instagram.com/" in url else ""
                out.append(
                    DirectMessage(
                        igsid=igsid,
                        mid=f"{mid}:{index}" if mid else "",
                        text=str(message.get("text") or ""),
                        media_url="" if permalink else url,
                        permalink=permalink,
                        media_id=str(
                            payload_field.get("reel_video_id")
                            or payload_field.get("id")
                            or ""
                        ),
                        timestamp=ts,
                        source="webhook",
                        raw=event,
                    )
                )

            # A message with no attachment is either the pairing token or
            # someone talking to us; either way the handler decides.
            if not media and message.get("text"):
                out.append(
                    DirectMessage(
                        igsid=igsid,
                        mid=mid,
                        text=str(message["text"]),
                        timestamp=ts,
                        source="webhook",
                        raw=event,
                    )
                )

    return out


# ---------------- aiohttp handlers ----------------

async def _handle_verify(request):
    """Meta's subscription handshake: echo the challenge, but only to someone
    who already knows the verify token."""
    from aiohttp import web as aioweb

    params = request.rel_url.query
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")

    if mode == "subscribe" and token and hmac.compare_digest(token, settings.ig_verify_token):
        log.info("ig webhook: verification handshake accepted")
        return aioweb.Response(text=challenge, content_type="text/plain")

    log.warning("ig webhook: verification REJECTED (mode=%r)", mode)
    return aioweb.Response(status=403, text="forbidden")


async def _handle_event(request):
    from aiohttp import web as aioweb

    raw = await request.read()
    if not verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        log.warning("ig webhook: bad signature from %s - discarded", request.remote)
        return aioweb.Response(status=403, text="bad signature")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as e:
        log.warning("ig webhook: unparseable body (%s)", e)
        return aioweb.Response(status=400, text="bad json")

    if payload.get("object") != "instagram":
        return aioweb.Response(text="ignored")

    messages = parse_entries(payload)
    if messages and _dispatch is not None:
        # Answer first, work second. Anything else invites a retry storm.
        for dm in messages:
            asyncio.create_task(_deliver(dm))

    return aioweb.Response(text="ok")


async def _deliver(dm: DirectMessage) -> None:
    try:
        if _dispatch:
            await _dispatch(dm)
    except Exception:
        log.exception("ig webhook: dispatch failed for %s", dm.mid)


async def _handle_health(request):
    """A local liveness probe, so `curl 127.0.0.1:8088/healthz` on the box
    answers the "is the listener even up" question without involving Meta."""
    from aiohttp import web as aioweb

    return aioweb.Response(text="ok")


# ---------------- lifecycle ----------------

async def start(dispatch: Dispatch) -> None:
    global _runner, _dispatch

    from aiohttp import web as aioweb

    _dispatch = dispatch

    app = aioweb.Application()
    app.router.add_get(settings.ig_webhook_path, _handle_verify)
    app.router.add_post(settings.ig_webhook_path, _handle_event)
    app.router.add_get("/healthz", _handle_health)

    _runner = aioweb.AppRunner(app, access_log=None)
    await _runner.setup()
    site = aioweb.TCPSite(_runner, settings.ig_webhook_host, settings.ig_webhook_port)
    await site.start()


async def stop() -> None:
    global _runner

    if _runner is not None:
        await _runner.cleanup()
        _runner = None


async def health() -> tuple[bool, str]:
    from modules import ig_graph

    return await ig_graph.health()


def source() -> Source:
    return Source(name="webhook", start=start, stop=stop, health=health)
