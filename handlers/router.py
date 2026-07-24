"""
Top-level message handler.
Looks at incoming text:
  - URL?  -> route to platform-specific handler (instagram/youtube/spotify).
  - Plain text? -> treat as Spotify search query.

Each platform handler lives in its own module and is imported lazily so a
broken optional dep can't take down the bot.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from utils.url_router import Platform, route

from . import instagram_handler, soundcloud_handler, spotify_handler, youtube_handler

log = logging.getLogger(__name__)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    result = route(text)

    try:
        if result is None:
            # not a URL → Spotify free-text search
            await spotify_handler.handle_search(update, context, query=text)
            return

        if result.platform == Platform.YOUTUBE:
            await youtube_handler.handle_url(update, context, result)
        elif result.platform == Platform.INSTAGRAM:
            await instagram_handler.handle_url(update, context, result)
        elif result.platform == Platform.SPOTIFY:
            await spotify_handler.handle_url(update, context, result)
        elif result.platform == Platform.SOUNDCLOUD:
            await soundcloud_handler.handle_url(update, context, result)
        else:
            await msg.reply_text("🤔 لینک رو نشناختم. یوتوب / اینستا / اسپاتیفای ساپورت میشه.")
    except Exception as e:
        log.exception("handle_text failed")
        await msg.reply_text(f"❌ خطا: {e}")
