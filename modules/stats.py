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
        _ready = True


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
                       actions    = users.actions + 1""",
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
