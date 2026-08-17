"""
Resource limits, so one large request cannot take the server down.

Three things were unbounded before:

* Concurrency. The batch downloader created its semaphore *inside* each batch,
  so the limit was per request, not per server: ten users each downloading a
  playlist meant forty simultaneous yt-dlp + ffmpeg jobs.
* Threads. Every blocking call went to asyncio's default executor, so slow
  downloads starved quick metadata lookups and the bot felt frozen for
  everyone.
* Disk. Downloaded audio was never deleted; a 4000-track playlist is tens of
  gigabytes.

All three are capped here and tunable from .env.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

log = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, "") or default))
    except ValueError:
        return default


# Heavy = a download that runs yt-dlp and ffmpeg. Keep this near the core
# count: these are CPU and bandwidth bound, and piling more on makes every
# job slower rather than getting more done.
MAX_CONCURRENT_DOWNLOADS = _int_env("MAX_CONCURRENT_DOWNLOADS", 4)
MAX_DOWNLOADS_PER_USER = _int_env("MAX_DOWNLOADS_PER_USER", 2)
MAX_ACTIVE_BATCHES_PER_USER = _int_env("MAX_ACTIVE_BATCHES_PER_USER", 1)
MAX_DISK_MB = _int_env("MAX_DISK_MB", 8000)


# --------------------------------------------------------------------------
# Thread pools
# --------------------------------------------------------------------------

# Separate pools so a queue of downloads cannot delay a search: a metadata
# lookup finishing in 200ms must not wait behind four 30-second downloads.
_light_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="light")
_heavy_pool = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT_DOWNLOADS + 2, thread_name_prefix="heavy"
)


def light_pool() -> ThreadPoolExecutor:
    return _light_pool


def heavy_pool() -> ThreadPoolExecutor:
    return _heavy_pool


# --------------------------------------------------------------------------
# Concurrency slots
# --------------------------------------------------------------------------

_global_sem: asyncio.Semaphore | None = None
_user_sems: dict[int, asyncio.Semaphore] = {}
_active_batches: dict[int, int] = {}
_lock = threading.Lock()


def _global() -> asyncio.Semaphore:
    # Created on first use so it binds to the running loop.
    global _global_sem
    if _global_sem is None:
        _global_sem = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
    return _global_sem


def _for_user(user_id: int) -> asyncio.Semaphore:
    sem = _user_sems.get(user_id)
    if sem is None:
        sem = asyncio.Semaphore(MAX_DOWNLOADS_PER_USER)
        _user_sems[user_id] = sem
        if len(_user_sems) > 500:
            # Drop an idle entry; a fresh semaphore is harmless.
            for uid, s in list(_user_sems.items()):
                if uid != user_id and s._value == MAX_DOWNLOADS_PER_USER:
                    _user_sems.pop(uid, None)
                    break
    return sem


@asynccontextmanager
async def download_slot(user_id: int):
    """Hold one global slot and one per-user slot for the duration of a
    download. The per-user cap stops a single person filling every slot."""
    async with _for_user(user_id):
        async with _global():
            yield


def start_batch(user_id: int) -> bool:
    """Claim a batch slot. False when the user already has one running."""
    with _lock:
        if _active_batches.get(user_id, 0) >= MAX_ACTIVE_BATCHES_PER_USER:
            return False
        _active_batches[user_id] = _active_batches.get(user_id, 0) + 1
        return True


def end_batch(user_id: int) -> None:
    with _lock:
        left = _active_batches.get(user_id, 1) - 1
        if left > 0:
            _active_batches[user_id] = left
        else:
            _active_batches.pop(user_id, None)


def stats_snapshot() -> dict:
    return {
        "batches": dict(_active_batches),
        "free_global_slots": _global_sem._value if _global_sem else MAX_CONCURRENT_DOWNLOADS,
        "light_queue": _light_pool._work_queue.qsize(),
        "heavy_queue": _heavy_pool._work_queue.qsize(),
    }


# --------------------------------------------------------------------------
# Disk
# --------------------------------------------------------------------------

_last_sweep = 0.0

# Files under downloads/ that are state, not cache. The sweeper deletes the
# oldest thing it can find, and these are the oldest things there - so left
# unprotected it would quietly drop every Instagram pairing, the refreshed
# 60-day access token and the logged-in session, and the bot would look like
# it had forgotten its users.
PROTECTED_NAMES = frozenset({
    "file_ids.json",
    "ig_pairing.json",
    "ig_seen.json",
    "ig_token.json",
    "ig_private_session.json",
    # Losing this one changes the device Instagram sees, which is what made
    # the direct endpoints start refusing us.
    "ig_device.json",
    # And this one holds the ROTATED session cookie. The value in .env is only
    # the seed; deleting the jar sends the bot back to a cookie Instagram has
    # already replaced.
    "ig_web_cookies.json",
    # The request-rate history: small, and the only record of what this
    # account has actually been doing over the last day.
    "ig_web_rate.json",
})

# Subdirectories that hold something expensive to re-acquire rather than
# something we downloaded for a user. whisper/ is 1.5GB of model weights;
# deleting it to make room for a reel means the next subtitle request
# re-downloads the lot.
PROTECTED_DIRS = frozenset({
    "whisper",
    # yt-dlp's cachedir: the EJS challenge solver fetched from GitHub, plus
    # the signature caches. It is cache by name and state by behaviour - a
    # sweeper looking for the oldest untouched files finds exactly this, and
    # deleting it makes every extraction re-fetch and re-solve from scratch.
    # That is what turned a working YouTube download into a two-minute wait
    # for the quality menu.
    ".ytdlp-cache",
})


def _is_protected(path: Path, root: Path) -> bool:
    if path.name in PROTECTED_NAMES or path.name.startswith("stats.db"):
        return True
    try:
        return path.relative_to(root).parts[0] in PROTECTED_DIRS
    except (ValueError, IndexError):
        return False


def disk_report(root: Path) -> dict:
    """What is actually taking the space, and how much is left on the volume.

    Worth reporting rather than inferring: a full disk stops ffmpeg writing
    the clip that music recognition fingerprints, and the extractor returns
    "no window matched" for that - so out of space reaches the user as
    "I couldn't identify any music", which is not a clue.
    """
    import shutil

    reclaimable = protected = 0
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if _is_protected(path, root):
                protected += size
            else:
                reclaimable += size
    except OSError as e:
        log.warning("disk report failed to scan %s: %s", root, e)

    try:
        usage = shutil.disk_usage(root)
        free, total = usage.free, usage.total
    except OSError:
        free = total = 0

    return {
        "reclaimable_mb": reclaimable // (1024 * 1024),
        "protected_mb": protected // (1024 * 1024),
        "free_mb": free // (1024 * 1024),
        "total_mb": total // (1024 * 1024),
        "cap_mb": MAX_DISK_MB,
    }


def sweep_downloads(root: Path, keep_mb: int | None = None, min_interval: float = 60.0) -> int:
    """
    Delete the oldest media until the directory is back under the cap.

    Uploaded files are already reusable through the Telegram file_id cache, so
    deleting them costs nothing but a re-download in the rare case the id is
    rejected. Returns the number of bytes freed.

    Protected paths are excluded from both the deletions and the total: they
    cannot be reclaimed, so counting them toward the cap would make the
    sweeper delete real downloads to make room for a model file.
    """
    global _last_sweep
    now = time.monotonic()
    if now - _last_sweep < min_interval:
        return 0
    _last_sweep = now

    cap = (keep_mb if keep_mb is not None else MAX_DISK_MB) * 1024 * 1024
    try:
        files = [
            (p.stat().st_mtime, p.stat().st_size, p)
            for p in root.rglob("*")
            if p.is_file() and not _is_protected(p, root)
        ]
    except OSError as e:
        log.warning("disk sweep failed to scan %s: %s", root, e)
        return 0

    total = sum(size for _, size, _ in files)
    if total <= cap:
        return 0

    files.sort(key=lambda item: item[0])  # oldest first
    freed = 0
    for _, size, path in files:
        if total - freed <= cap:
            break
        try:
            path.unlink()
            freed += size
        except OSError:
            continue

    if freed:
        log.info(
            "disk sweep: freed %.0fMB (was %.0fMB, cap %.0fMB)",
            freed / 1e6, total / 1e6, cap / 1e6,
        )
    return freed


# --------------------------------------------------------------------------
# Bounded dict for module-level caches
# --------------------------------------------------------------------------

class BoundedDict(OrderedDict):
    """Dict that forgets its least recently used entry past `maxsize`.

    The search and metadata caches grew forever otherwise: every track anyone
    ever searched stayed in memory for the life of the process.
    """

    def __init__(self, maxsize: int = 1000):
        super().__init__()
        self.maxsize = maxsize

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self.maxsize:
            self.popitem(last=False)

    def get(self, key, default=None):
        if key in self:
            self.move_to_end(key)
        return super().get(key, default)
