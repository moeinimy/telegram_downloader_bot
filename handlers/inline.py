"""
Inline mode: `@bot <song>` typed inside any chat.

Why this exists beyond convenience. Every other way into this bot needs
somebody to already know it is there. Inline results are used in front of an
audience - the bot's name sits under the message in whichever chat it was
sent to - so the people who see it are exactly the people who were about to
want it. It is the one feature here that recruits.

Two kinds of result come back, and the difference is whether the file already
exists on Telegram's side:

  cached   the track has been sent by this bot before, so its file_id is in
           utils/file_cache. Telegram delivers the real audio instantly, with
           no round trip through us at all.

  fresh    everything else. An inline result cannot make us download a track
           on the spot - Telegram wants an answer in seconds, and a download
           takes longer than that - so the result posts a compact card with a
           deep link that fetches it in the bot.

The search itself is the ordinary catalogue one, which answers in about
200ms; nothing here waits on yt-dlp.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultCachedAudio,
    InputTextMessageContent,
    Update,
)
from telegram.ext import ContextTypes

from modules import spotify as sp
from utils import file_cache

log = logging.getLogger(__name__)

# Telegram shows fifty at most, and an inline query is re-sent on every
# keystroke - so this is a per-keystroke cost, not a per-search one.
_LIMIT = 12

# How long Telegram may reuse an answer for the same query. A minute keeps
# fast typing cheap without pinning a stale result list for long.
_CACHE_SECONDS = 60


def _bot_username(context) -> str:
    user = getattr(context.bot, "username", "") or ""
    return user.lstrip("@")


async def _search(query: str):
    """The catalogue search.

    sp.search_tracks is ALREADY @run_in_thread - it is awaitable, and it
    already runs off the event loop. Wrapping it in a second run_in_thread
    made the inner call return a coroutine that nothing ever awaited, so the
    handler got a coroutine where it expected a list, `for track in tracks`
    raised TypeError, and the query was never answered at all. From the chat
    that looks exactly like a bot with inline mode switched off.
    """
    return await sp.search_tracks(query, limit=_LIMIT)


async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline = update.inline_query
    query = (inline.query or "").strip()

    if not query:
        # An empty query is what Telegram sends the moment the username is
        # typed, before any song. Answering with instructions rather than
        # nothing is the difference between looking broken and looking ready.
        await inline.answer(
            [], cache_time=_CACHE_SECONDS, is_personal=False,
            switch_pm_text="🎧 اسم آهنگ رو بنویس",
            switch_pm_parameter="inline",
        )
        return

    try:
        tracks = await _search(query)
    except Exception as e:
        log.info("inline search failed for %r: %s", query, e)
        tracks = []

    if not tracks:
        await inline.answer([], cache_time=_CACHE_SECONDS,
                            switch_pm_text="نتیجه‌ای پیدا نشد — تو بات امتحان کن",
                            switch_pm_parameter="inline")
        return

    username = _bot_username(context)
    results: list = []

    for track in tracks:
        artists = ", ".join(track.artists) if track.artists else "?"
        cached = file_cache.get(f"audio:{track.id}")

        if cached:
            # The real file, sent by Telegram itself. No download, no wait.
            results.append(
                InlineQueryResultCachedAudio(
                    id=str(uuid4()),
                    audio_file_id=cached,
                    caption=f"🎧 {track.name} — {artists}",
                )
            )
            continue

        # Not on Telegram yet. A deep link is the only honest option: the
        # download cannot happen inside the seconds an inline answer has.
        link = f"https://t.me/{username}?start=trk_{track.id}" if username else ""
        button = (
            InlineKeyboardMarkup([[InlineKeyboardButton("⬇️ دریافت آهنگ", url=link)]])
            if link else None
        )
        results.append(
            InlineQueryResultArticle(
                id=str(uuid4()),
                title=track.name,
                description=artists,
                thumbnail_url=track.cover_url or None,
                input_message_content=InputTextMessageContent(
                    f"🎧 *{track.name}*\n👤 {artists}",
                    parse_mode="Markdown",
                ),
                reply_markup=button,
            )
        )

    await inline.answer(results, cache_time=_CACHE_SECONDS, is_personal=False)
