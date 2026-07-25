"""
Lyrics button: every music file the bot sends carries a "lyrics" inline
button. Pressing it looks the song up on lrclib.net and replies with the
text (chunked to fit Telegram's 4096-char message limit).

Callback data is capped at 64 bytes, so artist/title pairs are stored in an
in-process cache keyed by a short hash. Cache is lost on restart, which only
means old buttons answer with "session expired".
"""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import quote_plus

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from modules.lyrics import fetch_lyrics

log = logging.getLogger(__name__)

_cache: dict[str, tuple[str, str]] = {}


def lyrics_button(
    artist: str,
    title: str,
    links: dict[str, str] | None = None,
    *,
    track_id: str | None = None,
) -> InlineKeyboardMarkup:
    """Keyboard attached to the audio file: lyrics, plus recommendations when
    we know which catalogue track this is.

    `links` is accepted and ignored so older call sites keep working; platform
    links now live under the cover image (see platform_keyboard)."""
    key = hashlib.md5(f"{artist}|{title}".encode("utf-8")).hexdigest()[:12]
    _cache[key] = (artist, title)

    row = [InlineKeyboardButton("📜 متن آهنگ", callback_data=f"lyr:{key}")]
    if track_id:
        row.append(
            InlineKeyboardButton("🎧 شبیه این", callback_data=f"sp:sim:{track_id}")
        )
    return InlineKeyboardMarkup([row])


def platform_keyboard(
    artist: str, title: str, links: dict[str, str] | None = None
) -> InlineKeyboardMarkup:
    """Links to the song on each major service, shown under the cover art.
    Real URLs are used where the source gave us one; the rest fall back to
    that platform's search page, which always resolves to something."""
    q = quote_plus(f"{artist} {title}".strip())
    links = links or {}
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🟢 Spotify",
                    url=links.get("spotify") or f"https://open.spotify.com/search/{q}",
                ),
                InlineKeyboardButton(
                    "🔴 YouTube",
                    url=links.get("youtube")
                    or f"https://www.youtube.com/results?search_query={q}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🍎 Apple Music",
                    url=links.get("apple") or f"https://music.apple.com/search?term={q}",
                ),
                InlineKeyboardButton(
                    "🟠 SoundCloud",
                    url=links.get("soundcloud") or f"https://soundcloud.com/search?q={q}",
                ),
            ],
        ]
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]

    pair = _cache.get(key)
    if not pair:
        await query.message.reply_text("⌛ سشن منقضی شده. آهنگ رو دوباره بگیر.")
        return

    artist, title = pair
    status = await query.message.reply_text("🔎 دنبال متن آهنگ می‌گردم…")

    try:
        text = await fetch_lyrics(artist, title)
    except Exception as e:
        log.warning("lyrics lookup failed: %s", e)
        text = None

    if not text:
        await status.edit_text(f"😕 متنی برای «{artist} — {title}» پیدا نکردم.")
        return

    await status.delete()
    full = f"🎵 {artist} — {title}\n\n{text}"
    for i in range(0, len(full), 4000):
        await query.message.reply_text(full[i : i + 4000])
