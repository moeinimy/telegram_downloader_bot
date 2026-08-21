"""
Mirror every delivered file into the archive channel.

Why by file_id and not by re-uploading. Once Telegram has carried a file it
will send the same one again from its id, to any chat, without the bytes ever
leaving this server a second time. So archiving a 700MB video costs one api
call and no bandwidth - which is the only reason doing it for everything is
reasonable.

The archive is a private channel the bot is an admin of (CACHE_CHANNEL_ID).
Two things come out of having one:

  * inline mode can offer a track as real audio instead of a card, because a
    file_id that exists is the whole requirement there.
  * there is a record of what the bot has actually served, which is otherwise
    only a count in the stats table.

Nothing here may break a delivery. The user's file has already been sent by
the time this runs; a failure to ALSO put it somewhere else is not their
problem, so every path swallows and logs.
"""

from __future__ import annotations

import logging

import asyncio

from config import settings

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(settings.cache_channel_id)


async def mirror(bot, kind: str, file_id: str, caption: str = "") -> None:
    """Put one already-sent file into the archive. Never raises.

    `kind` picks the send method so the channel gets a playable audio or
    video rather than a document - the same file either way, but a document
    has no player and no duration.
    """
    if not enabled() or not file_id:
        return

    sender = {
        "audio": bot.send_audio,
        "video": bot.send_video,
        "photo": bot.send_photo,
    }.get(kind, bot.send_document)
    field = {"audio": "audio", "video": "video", "photo": "photo"}.get(
        kind, "document")

    try:
        await sender(chat_id=settings.cache_channel_id,
                     caption=caption[:1000] or None,
                     **{field: file_id})
    except Exception as e:
        # Worth a line, not worth a retry: the delivery that matters already
        # happened, and a channel that is misconfigured would otherwise log
        # once per download forever.
        log.info("archive: could not mirror %s (%s)", kind, e)


async def mirror_cache(bot, progress=None, should_stop=None) -> tuple[int, int]:
    """Push everything already in the file_id cache into the archive.

    For the channel created after the bot had already been running: those
    files exist on Telegram and are reachable by id, they just were never put
    anywhere visible.

    Returns (sent, failed).
    """
    from utils import file_cache

    entries = file_cache.snapshot()
    sent = failed = 0
    # Telegram refuses past roughly 20 messages a minute to one channel.
    # This is a backfill, so it can afford to crawl.
    for i, (key, file_id) in enumerate(entries.items(), 1):
        # Checked before each file rather than between batches: a backfill of
        # 282 files at three seconds each runs for fourteen minutes, and
        # "stop" has to mean now, not at the end of the current chunk.
        if should_stop is not None and should_stop():
            log.info("archive: stopped after %d of %d", i - 1, len(entries))
            break
        kind = key.split(":", 1)[0]
        try:
            await mirror(bot, kind, file_id, caption=key)
            sent += 1
        except Exception:
            failed += 1
        if progress and i % 10 == 0:
            await progress(i, len(entries), sent, failed)

        await asyncio.sleep(3.0)
    return sent, failed
