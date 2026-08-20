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

from utils import limits
from utils.i18n import t
from utils.url_router import Platform, route

from . import instagram_handler, soundcloud_handler, spotify_handler, youtube_handler

log = logging.getLogger(__name__)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return

    # Before anything is parsed, searched or downloaded. The semaphores
    # elsewhere cap how much runs at once; nothing capped how much could be
    # ASKED for, so a loop pasting links queued every one of them and each
    # queued job holds a search and eventually a slot.
    #
    # Sized to be invisible to a person: twenty in a burst, twenty a minute
    # after that. Refusal is not a ban - the bucket refills while the message
    # is being read - and admins are exempt.
    user = update.effective_user
    if user:
        from config import settings

        ok, wait = limits.allow(user.id, is_admin=user.id in settings.admin_ids)
        if not ok:
            try:
                await msg.reply_text(
                    t(msg.chat_id, "\u23f3 \u06cc\u06a9\u0645 \u0622\u0631\u0648\u0645\u200c\u062a\u0631! \u062d\u062f\u0648\u062f {n} \u062b\u0627\u0646\u06cc\u0647 \u062f\u06cc\u06af\u0647 \u062f\u0648\u0628\u0627\u0631\u0647 \u0628\u0641\u0631\u0633\u062a.")
                    .format(n=int(wait) + 1)
                )
            except Exception:
                pass
            return

    text = msg.text.strip()

    try:
        # A pending "which range of the playlist?" prompt claims this message
        # before it is treated as a search query.
        if await spotify_handler.handle_range_reply(update, context, text):
            return
    except Exception as e:
        log.exception("range reply failed")
        # Module errors are already user-facing sentences, so run them through
        # t() as well - otherwise an English user gets a Persian error body.
        await msg.reply_text(
            t(msg.chat_id, "❌ خطا: {err}").format(err=t(msg.chat_id, str(e)))
        )
        return

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
            await msg.reply_text(t(msg.chat_id, "🤔 لینک رو نشناختم. یوتوب / اینستا / اسپاتیفای ساپورت میشه."))
    except Exception as e:
        log.exception("handle_text failed")
        # Module errors are already user-facing sentences, so run them through
        # t() as well - otherwise an English user gets a Persian error body.
        await msg.reply_text(
            t(msg.chat_id, "❌ خطا: {err}").format(err=t(msg.chat_id, str(e)))
        )
