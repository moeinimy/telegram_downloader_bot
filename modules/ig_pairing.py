"""
The link between a Telegram chat and an Instagram account.

Instagram never tells us who a sender is in Telegram terms - it gives an
IGSID, an id scoped to our own account. The only way to connect the two
identities is to have the user carry a secret across by hand: the bot shows a
short token, the user DMs that token to our Instagram account, and the sender
of that DM is by definition the person who was looking at the bot screen.

Identities are namespaced, and that is not decoration. Meta's IGSID is scoped
to our app; the unofficial client sees the account's raw numeric pk. They are
different numbers for the same person and there is no way to map between them,
so a chat can hold one identity per namespace:

    ig:<igsid>   seen by the official webhook
    pk:<userpk>  seen by the standby poller

A user who paired over the webhook and then arrives through the poller during
a failover is asked to pair once more; after that both ids point at the same
chat and either source recognises them.

Deliberately minimal: that id is the only thing about the user's Instagram
account that is ever written down. No username, no profile, no message
history. Unlinking removes every row for the chat.

Persisted to downloads/ig_pairing.json with the same temp-file + replace dance
as utils/file_cache.py, so a crash mid-write cannot corrupt the map. That
directory is also the only path the systemd unit grants write access to.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

# No 0/O, no 1/I/L. These tokens get read off a phone screen and retyped into
# a different app, which is exactly where those characters get confused.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_TOKEN_LEN = 6
_PREFIX = "IG-"

TOKEN_TTL = 15 * 60  # seconds

_LOCK = threading.Lock()
_PATH: Path = settings.download_dir / "ig_pairing.json"
_data: dict | None = None


def _blank() -> dict:
    return {"pending": {}, "links": {}}


def _load() -> dict:
    global _data
    if _data is None:
        try:
            loaded = json.loads(_PATH.read_text(encoding="utf-8"))
            _data = {"pending": loaded.get("pending") or {}, "links": loaded.get("links") or {}}
            log.info("ig pairing: %d link(s) loaded", len(_data["links"]))
        except FileNotFoundError:
            _data = _blank()
        except Exception as e:
            # Losing the pairings is recoverable - users can re-pair - while
            # refusing to start is not. Say it loudly and carry on.
            log.error("ig pairing store unreadable (%s) - starting empty", e)
            _data = _blank()
    return _data


def _flush(data: dict) -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_PATH)
    except Exception as e:
        log.warning("ig pairing store write failed: %s", e)


def _purge_expired(data: dict) -> bool:
    now = time.time()
    dead = [tok for tok, row in data["pending"].items() if row.get("expires", 0) <= now]
    for tok in dead:
        data["pending"].pop(tok, None)
    return bool(dead)


def normalize(text: str) -> str:
    """Accept what a human actually types: lowercase, spaces, a missing prefix.

    Users retype these by hand and phone keyboards autocapitalise, so being
    strict here would mostly produce 'invalid token' for correct tokens.
    """
    cleaned = "".join(ch for ch in text.strip().upper() if ch.isalnum())
    if cleaned.startswith("IG"):
        cleaned = cleaned[2:]
    return f"{_PREFIX}{cleaned}" if cleaned else ""


def looks_like_token(text: str) -> bool:
    """Cheap pre-check so an ordinary DM is not treated as a failed pairing."""
    candidate = normalize(text)
    body = candidate[len(_PREFIX):]
    return len(body) == _TOKEN_LEN and all(ch in _ALPHABET for ch in body)


# ---------------- public API ----------------

def issue(chat_id: int) -> str:
    """Mint a single-use token for this chat, replacing any earlier one.

    Replacing matters: if an old token stayed valid, a user who pressed the
    button twice would be looking at one token while a different one also
    worked, and only one of them can be the shown-on-screen truth.
    """
    with _LOCK:
        data = _load()
        _purge_expired(data)
        for tok, row in list(data["pending"].items()):
            if row.get("chat_id") == chat_id:
                data["pending"].pop(tok, None)

        while True:
            token = _PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(_TOKEN_LEN))
            if token not in data["pending"]:
                break

        data["pending"][token] = {"chat_id": chat_id, "expires": time.time() + TOKEN_TTL}
        _flush(data)
        return token


def _namespace(identity: str) -> str:
    return str(identity).split(":", 1)[0]


def redeem(token: str, identity: str) -> int | None:
    """Consume a token and link a namespaced identity to its chat. None if the
    token is unknown or expired. Single use: it is gone either way."""
    key = normalize(token)
    if not key or not identity:
        return None

    with _LOCK:
        data = _load()
        _purge_expired(data)
        row = data["pending"].pop(key, None)
        if not row:
            _flush(data)
            return None

        chat_id = int(row["chat_id"])
        # Within one namespace a chat has exactly one identity, so re-pairing
        # from a different Instagram account moves the link instead of
        # duplicating it. Identities in the OTHER namespace are left alone -
        # they describe the same person seen through the other source.
        namespace = _namespace(identity)
        for existing, link in list(data["links"].items()):
            if (
                link.get("chat_id") == chat_id
                and existing != identity
                and _namespace(existing) == namespace
            ):
                data["links"].pop(existing, None)

        data["links"][str(identity)] = {"chat_id": chat_id, "linked_at": time.time()}
        _flush(data)
        log.info("ig pairing: linked %s -> chat=%s", identity, chat_id)
        return chat_id


def chat_for(identity: str) -> int | None:
    """Which Telegram chat this Instagram sender belongs to."""
    if not identity:
        return None
    with _LOCK:
        row = _load()["links"].get(str(identity))
    return int(row["chat_id"]) if row else None


def linked_ids(chat_id: int) -> list[str]:
    """Every identity linked to this chat - at most one per namespace."""
    with _LOCK:
        links = dict(_load()["links"])
    return [ident for ident, row in links.items() if row.get("chat_id") == chat_id]


def is_linked(chat_id: int) -> bool:
    return bool(linked_ids(chat_id))


def linked_at(chat_id: int) -> float | None:
    with _LOCK:
        links = dict(_load()["links"])
    for row in links.values():
        if row.get("chat_id") == chat_id:
            return row.get("linked_at")
    return None


def unlink(chat_id: int) -> bool:
    """Drop the link and any half-finished pairing for this chat."""
    with _LOCK:
        data = _load()
        removed = False
        for identity, row in list(data["links"].items()):
            if row.get("chat_id") == chat_id:
                data["links"].pop(identity, None)
                removed = True
        for tok, row in list(data["pending"].items()):
            if row.get("chat_id") == chat_id:
                data["pending"].pop(tok, None)
                removed = True
        if removed:
            _flush(data)
        return removed


def unlink_identity(identity: str) -> bool:
    """Used when Instagram tells us the conversation is gone - the user
    blocked the account or deleted the chat - so the pairing expires cleanly
    instead of wedging on a sender we can never reach again."""
    with _LOCK:
        data = _load()
        if data["links"].pop(str(identity), None) is None:
            return False
        _flush(data)
        log.info("ig pairing: dropped %s", identity)
        return True


def count() -> int:
    with _LOCK:
        return len(_load()["links"])


def pending_count() -> int:
    with _LOCK:
        data = _load()
        if _purge_expired(data):
            _flush(data)
        return len(data["pending"])
