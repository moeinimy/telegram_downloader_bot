"""
YouTube flow:
  1) probe -> show thumbnail + inline keyboard (qualities + audio).
  2) callback `yt:<videoid>:<choice>` -> download and upload.

We keep per-message state in chat_data: {video_id: {"url": ..., "info": ...}}.
"""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from config import settings
from modules import stats
from modules import youtube as yt
from utils.i18n import t
from utils import file_cache
from utils.helpers import file_too_big, fmt_duration, prepare_telegram_thumb
from utils.progress import ProgressReporter
from utils.url_router import RouteResult

log = logging.getLogger(__name__)


async def handle_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE, route: RouteResult
) -> None:
    msg = update.effective_message
    status = await msg.reply_text(t(msg.chat_id, "🔎 در حال گرفتن اطلاعات ویدیو…"))

    try:
        info = await yt.probe_video(route.url)
    except Exception as e:
        await status.edit_text(t(msg.chat_id, "❌ نتونستم اطلاعات ویدیو رو بگیرم: {err}").format(err=e))
        return

    # stash for later
    context.chat_data.setdefault("yt", {})[info.id] = {"url": route.url, "info": info}

    kb_rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for q in yt.quality_options_for(info):
        row.append(InlineKeyboardButton(f"🎬 {q}", callback_data=f"yt:{info.id}:{q}"))
        if len(row) == 3:
            kb_rows.append(row)
            row = []
    if row:
        kb_rows.append(row)
    kb_rows.append([InlineKeyboardButton(t(msg.chat_id, "🎵 Audio (MP3)"), callback_data=f"yt:{info.id}:audio")])
    kb_rows.append([InlineKeyboardButton(t(msg.chat_id, "🎧 پیدا کردن آهنگ ویدیو (Shazam)"), callback_data=f"yt:{info.id}:shazam")])

    caption = (
        f"*{info.title}*\n"
        f"👤 {info.uploader}\n"
        f"⏱ {fmt_duration(info.duration)}"
    )
    await status.delete()
    await msg.reply_photo(
        photo=info.thumbnail,
        caption=caption,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles callbacks of the form yt:<videoid>:<quality|audio>."""
    query = update.callback_query
    await query.answer()
    _, video_id, choice = query.data.split(":", 2)

    state = context.chat_data.get("yt", {}).get(video_id)
    if not state:
        await query.message.reply_text(t(query.message.chat_id, "⌛ سشن منقضی شده. دوباره لینک رو بفرست."))
        return

    info = state["info"]
    url = state["url"]

    if choice == "shazam":
        from handlers.recognize_handler import recognize_from_url
        await recognize_from_url(query.message, url)
        return

    # Fast path: we already uploaded this exact video/quality once.
    cache_key = f"{'audio' if choice == 'audio' else 'video'}:yt_{video_id}" + (
        "" if choice == "audio" else f":{choice}"
    )
    cached = file_cache.get(cache_key)
    if cached:
        try:
            if choice == "audio":
                from handlers.lyrics_handler import lyrics_button

                await query.message.reply_audio(
                    audio=cached,
                    title=info.title,
                    performer=info.uploader,
                    duration=info.duration,
                    reply_markup=lyrics_button(
                        info.uploader, info.title, chat_id=query.message.chat_id),
                )
            else:
                await query.message.reply_video(
                    video=cached, caption=info.title, supports_streaming=True
                )
            return
        except Exception as e:
            log.info("cached file_id rejected (%s) - re-downloading", e)
            file_cache.drop(cache_key)

    status = await query.message.reply_text(t(query.message.chat_id, "⬇️ شروع دانلود…"))
    reporter = ProgressReporter(message=status, loop=asyncio.get_running_loop())

    try:
        if choice == "audio":
            path = await yt.download_audio(url, info, progress_hook=reporter.hook)
        else:
            path = await yt.download_video(url, info, quality=choice, progress_hook=reporter.hook)
    except Exception as e:
        log.exception("yt download failed")
        await status.edit_text(t(query.message.chat_id, "❌ دانلود ناموفق: {err}").format(err=e))
        return

    if file_too_big(path, settings.max_upload_mb):
        size_mb = path.stat().st_size / (1024 * 1024)
        await status.edit_text(
            t(query.message.chat_id,
              "⚠️ فایل {size}MB شد که از حد مجاز تلگرام ({limit}MB) بزرگ‌تره. "
              "یه کیفیت پایین‌تر انتخاب کن.").format(
                  size=f"{size_mb:.0f}", limit=settings.max_upload_mb)
        )
        path.unlink(missing_ok=True)
        return

    # iOS/Desktop clients only show thumbnails passed via the API.
    thumb_path = None
    if info.thumbnail:
        thumb_path = await prepare_telegram_thumb(
            info.thumbnail, settings.download_dir / "thumbs" / f"{info.id}.jpg"
        )

    # An upload has no progress hook, and media_write_timeout is 600s - so a
    # slow one looks identical to a hung one for ten minutes, and the only
    # thing on screen is a message that never changes. Say how big it is, and
    # time it, so "it does not upload" can be told from "it takes four
    # minutes".
    size_mb = path.stat().st_size / (1024 * 1024)
    await status.edit_text(
        t(query.message.chat_id, "📤 در حال آپلود… ({size}MB)").format(
            size=f"{size_mb:.0f}")
    )
    log.info("upload starting: %s, %.1fMB", path.name, size_mb)
    upload_started = time.monotonic()
    try:
        with path.open("rb") as fh:
            if choice == "audio":
                from handlers.lyrics_handler import lyrics_button

                sent = await query.message.reply_audio(
                    audio=fh,
                    title=info.title,
                    performer=info.uploader,
                    duration=info.duration,
                    thumbnail=thumb_path.open("rb") if thumb_path else None,
                    reply_markup=lyrics_button(
                        info.uploader, info.title, chat_id=query.message.chat_id),
                )
                if sent and sent.audio:
                    file_cache.put(cache_key, sent.audio.file_id)
                stats.record_download(query.message.chat_id, "yt-audio", info.title)
            else:
                sent = await query.message.reply_video(
                    video=fh,
                    caption=info.title,
                    supports_streaming=True,
                    thumbnail=thumb_path.open("rb") if thumb_path else None,
                )
                if sent and sent.video:
                    file_cache.put(cache_key, sent.video.file_id)
                stats.record_download(query.message.chat_id, "yt-video", info.title)
        log.info("upload finished: %.1fMB in %.1fs", size_mb,
                 time.monotonic() - upload_started)
        await status.delete()
    except Exception as e:
        log.exception("upload failed after %.1fs (%.1fMB)",
                      time.monotonic() - upload_started, size_mb)
        await status.edit_text(t(query.message.chat_id, "❌ آپلود ناموفق: {err}").format(err=e))
    finally:
        path.unlink(missing_ok=True)
