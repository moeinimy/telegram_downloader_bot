"""
Meta Graph calls for the official Instagram Direct path.

Three jobs: prove the access token still works, keep it alive, and send the
occasional DM back (pairing feedback only - media always goes to Telegram, so
Instagram's 24-hour reply window can never block a delivery).

Token handling is the part that bites. The long-lived token lasts 60 days and
must be refreshed before it expires; a token left UNUSED for 60 days dies
permanently and no refresh can bring it back. So the refresh job is not
optional, and a failure has to be loud.

Refreshed tokens are written to downloads/ig_token.json rather than back into
.env: .env is systemd's EnvironmentFile and rewriting it from inside the
process would be a fine way to lock the bot out of its own configuration. The
value in .env is the seed; the file, once it exists, wins.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from config import settings
from utils.helpers import run_in_thread

from utils.secrets import scrub

log = logging.getLogger(__name__)

API = "https://graph.instagram.com"
VERSION = "v23.0"

_TOKEN_PATH: Path = settings.download_dir / "ig_token.json"
_lock = threading.Lock()
_token_cache: dict | None = None

# Refresh well before the cliff: a single failed attempt must not be the
# difference between a live token and a dead one.
_REFRESH_WHEN_DAYS_LEFT = 10
_REFRESH_CHECK_SECONDS = 6 * 3600

_alert: Callable[[str], Awaitable[None]] | None = None
_refresh_task: asyncio.Task | None = None
last_error: str = ""


def set_alert(fn: Callable[[str], Awaitable[None]]) -> None:
    """Where to shout when the token is in trouble. Wired to the admin chat."""
    global _alert
    _alert = fn


# ---------------- token storage ----------------

def _read_token_file() -> dict:
    global _token_cache
    if _token_cache is None:
        try:
            _token_cache = json.loads(_TOKEN_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _token_cache = {}
        except Exception as e:
            log.warning("ig token file unreadable (%s) - falling back to .env", e)
            _token_cache = {}
    return _token_cache


def _write_token(token: str, expires_in: int) -> None:
    global _token_cache
    payload = {
        "access_token": token,
        "expires_at": time.time() + expires_in,
        "refreshed_at": time.time(),
    }
    with _lock:
        try:
            _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _TOKEN_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(_TOKEN_PATH)
            # chmod after the replace: the temp file is created with the
            # process umask and this is a 60-day credential.
            _TOKEN_PATH.chmod(0o600)
        except Exception as e:
            log.error("could not persist the refreshed Instagram token: %s", e)
        _token_cache = payload


def token() -> str:
    """The token to use right now. The refreshed one beats the seed in .env."""
    with _lock:
        stored = _read_token_file().get("access_token")
    return stored or settings.ig_access_token


def expires_at() -> float | None:
    with _lock:
        return _read_token_file().get("expires_at")


def days_left() -> float | None:
    exp = expires_at()
    return None if not exp else (exp - time.time()) / 86400


# ---------------- graph calls ----------------

@run_in_thread
def _get(path: str, params: dict) -> dict:
    from utils import http

    r = http.get(f"{API}/{path}", params={**params, "access_token": token()}, timeout=15)
    try:
        payload = r.json()
    except Exception:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        raise RuntimeError(f"{err.get('type', 'error')}: {err.get('message', '')[:160]}")
    return payload


@run_in_thread
def _post(path: str, body: dict) -> dict:
    from utils import http

    r = http.client().post(
        f"{API}/{VERSION}/{path}",
        params={"access_token": token()},
        json=body,
        timeout=20,
    )
    try:
        payload = r.json()
    except Exception:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        raise RuntimeError(f"{err.get('type', 'error')}: {err.get('message', '')[:160]}")
    return payload


async def me() -> dict:
    """Identity of the account the token belongs to. Cheapest liveness check."""
    return await _get(f"{VERSION}/me", {"fields": "user_id,username"})


async def health() -> tuple[bool, str]:
    """Is the official path actually able to deliver messages right now?

    Two questions, not one. A valid token with no webhook subscription looks
    perfectly healthy from the token's side and delivers nothing, which is
    exactly the silent failure the standby poller exists to cover.
    """
    if not settings.has_ig_webhook:
        return False, "not configured"

    try:
        who = await me()
    except Exception as e:
        return False, f"token rejected - {scrub(e)}"

    username = who.get("username") or who.get("user_id") or "?"

    try:
        subs = await _get(f"{VERSION}/me/subscribed_apps", {})
        fields = []
        for row in subs.get("data") or []:
            fields += list(row.get("subscribed_fields") or [])
        if not fields:
            return False, f"@{username}: no webhook subscription"
        if "messages" not in fields:
            return False, f"@{username}: subscribed to {', '.join(fields)} but not messages"
    except Exception as e:
        # The token works, so messages can still arrive; we just could not
        # confirm the subscription. Do not fail over on an unreadable check -
        # that would wake the unofficial poller for no reason.
        return True, f"@{username} (subscription unverified: {e})"

    left = days_left()
    suffix = f", token {left:.0f}d left" if left is not None else ""
    return True, f"@{username}{suffix}"


async def send_text(igsid: str, text: str) -> bool:
    """Reply in the Instagram DM. Best effort by design.

    Only ever used for pairing feedback. Instagram allows a reply just inside
    24 hours of the user's last message, and a share from a stranger might sit
    outside that window - so this failing must never stop the media reaching
    Telegram.
    """
    try:
        await _post("me/messages", {"recipient": {"id": igsid}, "message": {"text": text}})
        return True
    except Exception as e:
        log.info("ig direct: could not reply in DM to %s: %s", igsid, e)
        return False


# ---------------- token refresh ----------------

@run_in_thread
def _refresh_call() -> dict:
    from utils import http

    r = http.get(
        f"{API}/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token()},
        timeout=20,
    )
    payload = r.json()
    if payload.get("error"):
        err = payload["error"]
        raise RuntimeError(f"{err.get('type', 'error')}: {err.get('message', '')[:200]}")
    if not payload.get("access_token"):
        raise RuntimeError(f"no token in response: {str(payload)[:160]}")
    return payload


async def refresh(force: bool = False) -> tuple[bool, str]:
    """Renew the long-lived token. Returns (changed, detail)."""
    global last_error

    left = days_left()
    if not force and left is not None and left > _REFRESH_WHEN_DAYS_LEFT:
        return False, f"{left:.0f} days left - no refresh needed"

    try:
        payload = await _refresh_call()
    except Exception as e:
        last_error = str(e)
        # Meta refuses to refresh a token younger than 24 hours. That is the
        # expected answer right after the first Business Login, not a fault.
        if "24 hours" in last_error or "hours old" in last_error:
            return False, "token is younger than 24h - will retry"
        return False, f"refresh FAILED: {last_error}"

    _write_token(payload["access_token"], int(payload.get("expires_in", 60 * 86400)))
    last_error = ""
    return True, f"refreshed, valid for {int(payload.get('expires_in', 0)) // 86400} days"


async def refresh_loop() -> None:
    """Check a few times a day. Sixty days of silence ends with a dead token,
    so a persistent failure is escalated to the admin chat rather than logged."""
    while True:
        try:
            changed, detail = await refresh()
            if changed:
                log.info("ig token: %s", detail)
            elif "FAILED" in detail:
                log.error("ig token: %s", detail)
                left = days_left()
                if _alert and (left is None or left < _REFRESH_WHEN_DAYS_LEFT):
                    await _alert(
                        "⚠️ تمدید توکن اینستاگرام شکست خورد.\n\n"
                        f"{detail}\n\n"
                        "اگه توکن منقضی شه، دیگه قابل تمدید نیست و باید دوباره "
                        "از اول Business Login بزنی."
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ig token: refresh loop blew up")
        await asyncio.sleep(_REFRESH_CHECK_SECONDS)


def start_refresh_loop() -> None:
    global _refresh_task
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.create_task(refresh_loop())


def stop_refresh_loop() -> None:
    global _refresh_task
    if _refresh_task:
        _refresh_task.cancel()
        _refresh_task = None
