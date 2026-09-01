"""
SoundCloud flow: track links download directly; set (playlist) links show
a track list with per-track buttons. Reuses the music pipeline from the
spotify handler (download -> iTunes enrich -> cover art -> lyrics button).
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from handlers.spotify_handler import _send_and_download_track, _send_tracklist
from modules import spotify as sp
from utils.i18n import t, localise
from utils.secrets import scrub
from utils.url_router import RouteResult

log = logging.getLogger(__name__)


async def handle_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE, route: RouteResult
) -> None:
    msg = update.effective_message

    if route.kind == "playlist":
        status = await msg.reply_text(t(msg.chat_id, "🔎 در حال خوندن پلی‌لیست ساندکلاد…"))
        try:
            tracks = await sp.probe_soundcloud_set(route.url)
        except Exception as e:
            await status.edit_text(t(msg.chat_id, "❌ خطا: {err}").format(err=scrub(localise(msg.chat_id, e))))
            return
        if not tracks:
            await status.edit_text(t(msg.chat_id, "ترکی تو این پلی‌لیست پیدا نکردم."))
            return
        await status.delete()
        await _send_tracklist(
            msg, title=t(msg.chat_id, "☁️ پلی‌لیست ساندکلاد"), tracks=tracks, bulk_callback=None
        )
        return

    status = await msg.reply_text(t(msg.chat_id, "🔎 در حال گرفتن اطلاعات ترک…"))
    try:
        meta = await sp.probe_source_track(route.url, prefix="sc")
    except Exception as e:
        await status.edit_text(t(msg.chat_id, "❌ خطا: {err}").format(err=scrub(localise(msg.chat_id, e))))
        return
    await status.delete()
    await _send_and_download_track(msg, meta)
