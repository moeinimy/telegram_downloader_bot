"""
Inline mode: `@bot <song>` typed inside any chat.

Why this exists beyond convenience. Every other way into this bot needs
somebody to already know it is there. Inline results are used in front of an
audience - the bot's name sits under the message in whichever chat it was
sent to - so the people who see it are exactly the people who were about to
want it. It is the one feature here that recruits.

Real audio first, and a card only where Telegram leaves no choice.

Telegram will not let a bot download-on-pick. An inline answer has seconds,
the message is posted from the result itself the instant it is chosen, and
there is no step in between where a bot could fetch anything - a text result
cannot later be edited into an audio one either. So "pick it and it downloads
and sends" is not a thing that can be built here, however reasonable it
sounds.

What can be built is a cache big enough that the question rarely comes up.
Anything this bot has ever sent has a file_id, and those come back as real
audio with no download and no wait. Everything else gets a card whose button
opens the bot and delivers the track there - not as good, and better than an
empty result list, which is what showing only cached results produced on any
song nobody had fetched yet.

The cache fills itself from use: an inline query warms its own best uncached
match in the background, so the next person to search it gets the file. That
needs somewhere to send, since a file_id does not exist until a file has been
carried - CACHE_CHANNEL_ID, a private channel the bot is admin of. Nothing
there reaches a user. Unset, the warm is skipped and the cache grows only from
real downloads.
"""

from __future__ import annotations

import asyncio
import logging
import time
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

from config import settings
from modules import spotify as sp
from utils import file_cache
from utils.i18n import t

log = logging.getLogger(__name__)

# Telegram shows fifty at most, and an inline query is re-sent on every
# keystroke - so this is a per-keystroke cost, not a per-search one.
_LIMIT = 12

# How long Telegram may reuse an answer. Kept short because a warm can land
# seconds later, and a stale answer would hide the file it just produced.
_CACHE_SECONDS = 10

# One track per query, and only after the typing has settled: an inline query
# arrives on every keystroke, so "drake" is eight queries and warming each
# would be eight downloads for one search.
_WARM_DEBOUNCE = 3.0
_warm_seen: dict[str, float] = {}
_warming: set[str] = set()


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


async def _warm(bot, meta) -> None:
    """Download one track and send it to the cache channel for its file_id.

    Nothing reaches a user from here. The channel is a store: the send exists
    because Telegram will not name a file until it has carried one.
    """
    key = f"audio:{meta.id}"
    if key in _warming or file_cache.get(key):
        return
    _warming.add(key)
    path = None
    try:
        path = await sp.download_track(meta)
        with path.open("rb") as fh:
            sent = await bot.send_audio(
                chat_id=settings.cache_channel_id,
                audio=fh,
                title=meta.name,
                performer=", ".join(meta.artists) if meta.artists else None,
                duration=(meta.duration_ms or 0) // 1000 or None,
            )
        if sent and sent.audio:
            file_cache.put(key, sent.audio.file_id)
            log.info("inline: warmed %s into the cache", meta.display)
    except Exception as e:
        log.info("inline: could not warm %s (%s)", meta.display, e)
    finally:
        _warming.discard(key)
        if path is not None:
            path.unlink(missing_ok=True)


def _should_warm(query: str) -> bool:
    """True once a query has stopped changing for a moment.

    Without this every keystroke queues a download: typing one song name is a
    dozen inline queries, and the first eleven are prefixes nobody searched
    for.
    """
    now = time.monotonic()
    last = _warm_seen.get(query, 0.0)
    _warm_seen[query] = now
    if len(_warm_seen) > 500:
        for k, seen in list(_warm_seen.items()):
            if now - seen > 300:
                _warm_seen.pop(k, None)
    return last and (now - last) >= _WARM_DEBOUNCE


async def on_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline = update.inline_query
    query = (inline.query or "").strip()

    if not query:
        # An empty query is what Telegram sends the moment the username is
        # typed, before any song.
        await inline.answer(
            [], cache_time=_CACHE_SECONDS, is_personal=False,
            switch_pm_text=t(inline.from_user.id, "🎧 اسم آهنگ رو بنویس"),
            switch_pm_parameter="inline",
        )
        return

    try:
        tracks = await _search(query)
    except Exception as e:
        log.info("inline search failed for %r: %s", query, e)
        tracks = []

    username = (getattr(context.bot, "username", "") or "").lstrip("@")
    results = []
    uncached = []
    for track in tracks:
        cached = file_cache.get(f"audio:{track.id}")
        if cached:
            # No caption. The audio message already carries the title and the
            # performer; a caption under it repeats them in a second voice.
            results.append(
                InlineQueryResultCachedAudio(id=str(uuid4()), audio_file_id=cached)
            )
        else:
            uncached.append(track)
            if username:
                artists = ", ".join(track.artists) if track.artists else "?"
                results.append(
                    InlineQueryResultArticle(
                        id=str(uuid4()),
                        title=track.name,
                        description=t(inline.from_user.id, "{artists} · تو بات باز می‌شه")
                        .format(artists=artists),
                        thumbnail_url=track.cover_url or None,
                        input_message_content=InputTextMessageContent(
                            f"🎧 {track.name} — {artists}"),
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                            t(inline.from_user.id, "⬇️ دریافت آهنگ"),
                            url=f"https://t.me/{username}?start=trk_{track.id}")]]),
                    )
                )

    # Warm the best match nobody has downloaded yet, so the next person to
    # search this gets the file rather than this same empty answer.
    if uncached and settings.cache_channel_id and _should_warm(query):
        asyncio.create_task(_warm(context.bot, uncached[0]))

    if not results:
        await inline.answer(
            [], cache_time=_CACHE_SECONDS,
            switch_pm_text=t(inline.from_user.id,
                             "🎧 تو بات بگیرش — بعدش اینجا هم میاد"),
            switch_pm_parameter="inline",
        )
        return

    await inline.answer(results, cache_time=_CACHE_SECONDS, is_personal=False)
