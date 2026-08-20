"""
Usage statistics, stored in a small SQLite file next to the downloads.

Every update touches `users`; every successfully delivered file adds a row to
`downloads`. SQLite is part of the stdlib and handles the bot's concurrency
fine in WAL mode, so this costs no extra dependency and survives restarts.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

_DB: Path = settings.download_dir / "stats.db"
_lock = threading.Lock()
_ready = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    global _ready
    with _lock:
        if _ready:
            return
        _DB.parent.mkdir(parents=True, exist_ok=True)
        with _connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    user_id    INTEGER PRIMARY KEY,
                    username   TEXT,
                    first_name TEXT,
                    first_seen INTEGER,
                    last_seen  INTEGER,
                    actions    INTEGER NOT NULL DEFAULT 0
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS downloads (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    kind    TEXT,
                    title   TEXT,
                    ts      INTEGER
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dl_ts ON downloads(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_dl_user ON downloads(user_id)")
            # Added after the first release; ALTER is the migration.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
            if "lang" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN lang TEXT")
            # When a broadcast last found this user unreachable. A count of
            # "4 blocked" answers how many but not who, and who is the part
            # worth acting on.
            if "blocked_at" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN blocked_at INTEGER")
        _ready = True


def get_lang(user_id: int) -> str | None:
    try:
        init()
        with _lock, _connect() as conn:
            row = conn.execute(
                "SELECT lang FROM users WHERE user_id=?", (user_id,)
            ).fetchone()
        return row[0] if row and row[0] else None
    except Exception as e:
        log.warning("stats get_lang failed: %s", e)
        return None


def set_lang(user_id: int, lang: str) -> None:
    try:
        init()
        now = int(time.time())
        with _lock, _connect() as conn:
            conn.execute(
                """INSERT INTO users (user_id, first_seen, last_seen, actions, lang)
                   VALUES (?, ?, ?, 0, ?)
                   ON CONFLICT(user_id) DO UPDATE SET lang = excluded.lang""",
                (user_id, now, now, lang),
            )
    except Exception as e:
        log.warning("stats set_lang failed: %s", e)


def touch_user(user_id: int, username: str | None, first_name: str | None) -> None:
    if not user_id:
        return
    now = int(time.time())
    try:
        init()
        with _lock, _connect() as conn:
            conn.execute(
                """INSERT INTO users (user_id, username, first_name, first_seen, last_seen, actions)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(user_id) DO UPDATE SET
                       username   = excluded.username,
                       first_name = excluded.first_name,
                       last_seen  = excluded.last_seen,
                       actions    = users.actions + 1,
                       blocked_at = NULL""",
                (user_id, username or "", first_name or "", now, now),
            )
    except Exception as e:
        log.warning("stats touch_user failed: %s", e)


def record_download(user_id: int, kind: str, title: str) -> None:
    try:
        init()
        with _lock, _connect() as conn:
            conn.execute(
                "INSERT INTO downloads (user_id, kind, title, ts) VALUES (?, ?, ?, ?)",
                (user_id or 0, kind, title[:200], int(time.time())),
            )
    except Exception as e:
        log.warning("stats record_download failed: %s", e)


def _scalar(conn, sql: str, args=()) -> int:
    row = conn.execute(sql, args).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def summary() -> dict:
    init()
    now = int(time.time())
    day, week = now - 86400, now - 604800
    with _lock, _connect() as conn:
        data = {
            "users": _scalar(conn, "SELECT COUNT(*) FROM users"),
            "users_day": _scalar(conn, "SELECT COUNT(*) FROM users WHERE last_seen>=?", (day,)),
            "users_week": _scalar(conn, "SELECT COUNT(*) FROM users WHERE last_seen>=?", (week,)),
            "new_week": _scalar(conn, "SELECT COUNT(*) FROM users WHERE first_seen>=?", (week,)),
            "downloads": _scalar(conn, "SELECT COUNT(*) FROM downloads"),
            "downloads_day": _scalar(conn, "SELECT COUNT(*) FROM downloads WHERE ts>=?", (day,)),
            "downloads_week": _scalar(conn, "SELECT COUNT(*) FROM downloads WHERE ts>=?", (week,)),
            "actions": _scalar(conn, "SELECT COALESCE(SUM(actions),0) FROM users"),
        }
        data["by_kind"] = conn.execute(
            "SELECT kind, COUNT(*) c FROM downloads GROUP BY kind ORDER BY c DESC"
        ).fetchall()
        data["top_tracks"] = conn.execute(
            "SELECT title, COUNT(*) c FROM downloads GROUP BY title ORDER BY c DESC LIMIT 5"
        ).fetchall()
    return data


def list_users(limit: int = 10, offset: int = 0) -> list[tuple]:
    """(user_id, username, first_name, last_seen, actions, downloads)"""
    init()
    with _lock, _connect() as conn:
        return conn.execute(
            """SELECT u.user_id, u.username, u.first_name, u.last_seen, u.actions,
                      (SELECT COUNT(*) FROM downloads d WHERE d.user_id = u.user_id)
               FROM users u
               ORDER BY u.last_seen DESC
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()


def user_count() -> int:
    init()
    with _lock, _connect() as conn:
        return _scalar(conn, "SELECT COUNT(*) FROM users")


def user_detail(user_id: int) -> tuple | None:
    init()
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT user_id, username, first_name, first_seen, last_seen, actions "
            "FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        recent = conn.execute(
            "SELECT kind, title, ts FROM downloads WHERE user_id=? ORDER BY ts DESC LIMIT 10",
            (user_id,),
        ).fetchall()
        total = _scalar(conn, "SELECT COUNT(*) FROM downloads WHERE user_id=?", (user_id,))
    return row, recent, total


def mark_blocked(user_id: int, blocked: bool = True) -> None:
    """Remember that a broadcast could not reach this user.

    Cleared by touch_user, because Telegram delivers nothing at all to
    somebody who has blocked the bot - so any activity from them is proof
    they have not, and they should be in the next broadcast.
    """
    try:
        init()
        with _lock, _connect() as conn:
            conn.execute("UPDATE users SET blocked_at=? WHERE user_id=?",
                         (int(time.time()) if blocked else None, user_id))
    except Exception as e:
        log.warning("stats mark_blocked failed: %s", e)


def blocked_users(limit: int = 50) -> list[tuple]:
    """(user_id, username, first_name, blocked_at), most recent first."""
    init()
    with _lock, _connect() as conn:
        return conn.execute(
            """SELECT user_id, username, first_name, blocked_at
               FROM users WHERE blocked_at IS NOT NULL
               ORDER BY blocked_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()


def reachable_count() -> int:
    """Users a broadcast can actually land on."""
    init()
    with _lock, _connect() as conn:
        return _scalar(conn, "SELECT COUNT(*) FROM users WHERE blocked_at IS NULL")


def reachable_users(limit: int = 100000) -> list[tuple]:
    """(user_id, username, first_name) for everyone a broadcast can land on.

    Blocked users are left out rather than tried and counted: Telegram will
    refuse each one, which costs a request and 50ms per run to learn again
    something already known.
    """
    init()
    with _lock, _connect() as conn:
        return conn.execute(
            """SELECT user_id, username, first_name FROM users
               WHERE blocked_at IS NULL
               ORDER BY last_seen DESC LIMIT ?""",
            (limit,),
        ).fetchall()
