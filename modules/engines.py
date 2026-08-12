"""
Music-recognition engines, tried in priority order.

Shazam is the workhorse: free, no key, no quota. The others exist because it
is not infallible, and each has a different failure mode:

  shazam    unofficial Shazam. Free and unlimited in practice. Strong on
            real-world clips (phone audio, background music), weak when the
            music is buried under speech.
  acoustid  AcoustID + MusicBrainz. Free, open, effectively unlimited, needs
            only a free key. It fingerprints the exact recording, so it is
            excellent for clean audio files and useless for a noisy capture -
            the complement of Shazam's weakness.
  audd      Commercial. Best accuracy on hard clips; limited free quota.
  acrcloud  Commercial. Similar; free tier is a daily allowance.

Order comes from RECOGNITION_ENGINES. When a metered engine is exhausted or
erroring it is skipped for a while, so a spent quota degrades to Shazam
instead of breaking recognition.

Note on Google: there is no public API for Sound Search / "hum to search".
It exists only inside Google's own apps, so it cannot be wired in here.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)


class EngineResult:
    __slots__ = ("artist", "title", "engine", "score")

    def __init__(self, artist: str, title: str, engine: str, score: float = 1.0):
        self.artist = artist.strip()
        self.title = title.strip()
        self.engine = engine
        self.score = score

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.engine}: {self.artist} - {self.title} ({self.score:.2f})>"


# An engine that just failed or ran out of quota is skipped until this passes,
# so one exhausted key cannot slow every later request.
_cooldown: dict[str, float] = {}
_COOLDOWN = 900.0


def _on_cooldown(name: str) -> bool:
    return _cooldown.get(name, 0) > time.monotonic()


# Why each engine was last put on cooldown. A bad key and an exhausted quota
# both stop the engine, but only one of them comes back on its own - and
# reporting "temporarily disabled" for a key that will never work sends the
# admin off to wait instead of to fix it.
_cooldown_reason: dict[str, str] = {}

_AUTH_MARKERS = ("400", "401", "403", "authorization", "invalid api key",
                 "invalid client", "unauthorized", "authentication")


def _is_auth_failure(why: str) -> bool:
    lowered = why.lower()
    return any(marker in lowered for marker in _AUTH_MARKERS)


def _cool(name: str, why: str) -> None:
    _cooldown[name] = time.monotonic() + _COOLDOWN
    _cooldown_reason[name] = why
    log.warning("engine %s disabled for %.0f min: %s", name, _COOLDOWN / 60, why)


# --------------------------------------------------------------------------
# AcoustID (free, open, unlimited)
# --------------------------------------------------------------------------

def acoustid_available() -> bool:
    return bool(settings.acoustid_key) and shutil.which("fpcalc") is not None


def _fingerprint(path: Path) -> tuple[int, str] | None:
    """Chromaprint fingerprint via fpcalc, which ships in libchromaprint-tools."""
    try:
        proc = subprocess.run(
            ["fpcalc", "-json", "-length", "120", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            log.info("fpcalc failed: %s", proc.stderr[:150])
            return None
        import json

        data = json.loads(proc.stdout)
        return int(data["duration"]), data["fingerprint"]
    except Exception as e:
        log.info("fpcalc error: %s", e)
        return None


def recognize_acoustid(path: Path) -> EngineResult | None:
    from utils import http

    fp = _fingerprint(path)
    if not fp:
        return None
    duration, fingerprint = fp

    r = http.get(
        "https://api.acoustid.org/v2/lookup",
        params={
            "client": settings.acoustid_key,
            "duration": duration,
            "fingerprint": fingerprint,
            "meta": "recordings",
        },
    )
    if r.status_code != 200:
        _cool("acoustid", f"HTTP {r.status_code}")
        return None

    data = r.json()
    if data.get("status") != "ok":
        _cool("acoustid", str(data.get("error", ""))[:80])
        return None

    for result in data.get("results") or []:
        for rec in result.get("recordings") or []:
            title = rec.get("title") or ""
            artists = rec.get("artists") or []
            artist = (artists[0].get("name") if artists else "") or ""
            if title:
                return EngineResult(artist, title, "acoustid", result.get("score", 1.0))
    return None


# --------------------------------------------------------------------------
# AudD (commercial, small free quota)
# --------------------------------------------------------------------------

def audd_available() -> bool:
    return bool(settings.audd_token)


def recognize_audd(path: Path) -> EngineResult | None:
    from utils import http

    with path.open("rb") as fh:
        r = http.client().post(
            "https://api.audd.io/",
            data={"api_token": settings.audd_token, "return": ""},
            files={"file": fh},
        )
    if r.status_code != 200:
        _cool("audd", f"HTTP {r.status_code}")
        return None

    payload = r.json()
    if payload.get("status") != "success":
        # A spent quota answers with an error rather than a status code.
        _cool("audd", str(payload.get("error", {}))[:80])
        return None

    res = payload.get("result") or None
    if not res:
        return None
    return EngineResult(res.get("artist", ""), res.get("title", ""), "audd")


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

_ENGINES = {
    "acoustid": (acoustid_available, recognize_acoustid),
    "audd": (audd_available, recognize_audd),
}


def active_engines() -> list[str]:
    """Configured, currently usable extra engines, in the requested order."""
    out = []
    for name in settings.recognition_engines:
        entry = _ENGINES.get(name)
        if not entry:
            continue
        available, _ = entry
        if available() and not _on_cooldown(name):
            out.append(name)
    return out


def recognize_with(name: str, path: Path) -> EngineResult | None:
    entry = _ENGINES.get(name)
    if not entry:
        return None
    _, fn = entry
    try:
        return fn(path)
    except Exception as e:
        _cool(name, f"{type(e).__name__}: {e}")
        return None


def status() -> list[str]:
    """Human-readable engine availability, for the admin diagnostic."""
    lines = []
    for name, (available, _) in _ENGINES.items():
        if not available():
            why = "کلید ست نشده"
            if name == "acoustid" and settings.acoustid_key and not shutil.which("fpcalc"):
                why = "fpcalc نصب نیست (apt install libchromaprint-tools)"
            lines.append(f"⚪ {name}: {why}")
        elif _on_cooldown(name):
            why = _cooldown_reason.get(name, "")
            left = int((_cooldown[name] - time.monotonic()) / 60)
            if _is_auth_failure(why):
                # This one does not heal. Say so, or the admin waits out a
                # cooldown for a key that is simply wrong.
                lines.append(f"❌ {name}: کلید قبول نشد — {why[:60]}")
                lines.append(f"   ↳ کلید رو درست کن: botctl engines")
            else:
                lines.append(f"🟡 {name}: موقتا غیرفعال ({left} دقیقه دیگه) — {why[:50]}")
        else:
            lines.append(f"✅ {name}: فعال")
    return lines
