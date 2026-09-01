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
from utils.secrets import scrub
from utils import archive, file_cache
from utils.helpers import file_too_big, fmt_duration, prepare_telegram_thumb
from utils.progress import ProgressReporter
from utils.url_router import RouteResult

log = logging.getLogger(__name__)


async def handle_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE, route: RouteResult
) -> None:
    await _send_video_menu(update.effective_message, context, route.url)


async def _send_video_menu(msg, context, url: str) -> None:
    """Probe a video and offer it. Shared by a pasted link and by a pick from
    the channel list, so the two cannot drift apart."""
    status = await msg.reply_text(t(msg.chat_id, "🔎 در حال گرفتن اطلاعات ویدیو…"))

    try:
        info = await yt.probe_video(url)
    except Exception as e:
        await status.edit_text(t(msg.chat_id, "❌ نتونستم اطلاعات ویدیو رو بگیرم: {err}").format(err=scrub(e)))
        return

    # stash for later
    context.chat_data.setdefault("yt", {})[info.id] = {"url": url, "info": info}

    kb_rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for q in yt.quality_options_for(info):
        # The size belongs on the button. Without it "best" is a bet, and the
        # bet was lost quietly: a 1640MB file that only announced itself once
        # it was already downloaded and the upload had stalled.
        approx = info.size_by_quality.get(q, 0)
        label = f"🎬 {q}" if not approx else f"🎬 {q} · {approx / 1048576:.0f}MB"
        row.append(InlineKeyboardButton(label, callback_data=f"yt:{info.id}:{q}"))
        if len(row) == 3:
            kb_rows.append(row)
            row = []
    if row:
        kb_rows.append(row)
    kb_rows.append([InlineKeyboardButton(t(msg.chat_id, "🎵 Audio (MP3)"), callback_data=f"yt:{info.id}:audio")])
    kb_rows.append([InlineKeyboardButton(t(msg.chat_id, "🎧 پیدا کردن آهنگ ویدیو (Shazam)"), callback_data=f"yt:{info.id}:shazam")])
    if info.channel_url:
        # Listing a channel is a real request to YouTube, so it happens when
        # this is pressed rather than on every video that gets probed. The bot
        # check has only just stopped biting; a free extra request per link is
        # exactly what put it there.
        kb_rows.append([InlineKeyboardButton(
            t(msg.chat_id, "📺 معروف‌ترین ویدیوهای این کانال"),
            callback_data=f"yt:{info.id}:chan")])

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

    if choice == "chan":
        await _send_channel_menu(query.message, info)
        return

    if choice.startswith("pick"):
        # A video chosen from the channel list. It goes through the same probe
        # and the same quality menu as a pasted link, rather than a second
        # path that would drift from it.
        picked = choice.split("=", 1)[1] if "=" in choice else ""
        if picked:
            await _send_video_menu(query.message, context,
                                   f"https://www.youtube.com/watch?v={picked}")
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
            # Only drop an id Telegram has actually disowned. The previous
            # rule dropped on ANY exception, so one flood-wait or dropped
            # connection threw away a perfectly good id and bought a full
            # re-download - the opposite of what the cache is for.
            if file_cache.is_dead_reference(e):
                log.info("cached file_id is dead (%s) - re-downloading", e)
                file_cache.drop(cache_key)
            else:
                log.info("cached send failed (%s) - keeping the id, retrying "
                         "the long way", e)

    status = await query.message.reply_text(t(query.message.chat_id, "⬇️ شروع دانلود…"))
    reporter = ProgressReporter(message=status, loop=asyncio.get_running_loop())

    try:
        if choice == "audio":
            path = await yt.download_audio(url, info, progress_hook=reporter.hook)
        else:
            path = await yt.download_video(url, info, quality=choice, progress_hook=reporter.hook)
    except Exception as e:
        log.exception("yt download failed")
        await status.edit_text(t(query.message.chat_id, "❌ دانلود ناموفق: {err}").format(err=scrub(e)))
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
                    # So a time range typed next cuts THIS file, no reply.
                    from handlers import cut_handler

                    cut_handler.remember(sent)
                    file_cache.put(cache_key, sent.audio.file_id)
                    await archive.mirror(context.bot, "audio",
                                         sent.audio.file_id, caption=info.title)
                stats.record_download(query.message.chat_id, "yt-audio", info.title)
            else:
                # Told, not guessed. Without these Telegram picks a default
                # box and stretches the thumbnail into it, and shows the
                # length as 00:00 - both visible on a 710MB upload that was
                # otherwise perfectly fine.
                width, height, probed = yt.probe_dimensions(path)
                sent = await query.message.reply_video(
                    video=fh,
                    caption=info.title,
                    supports_streaming=True,
                    duration=probed or info.duration or None,
                    width=width or None,
                    height=height or None,
                    thumbnail=thumb_path.open("rb") if thumb_path else None,
                )
                if sent and sent.video:
                    # So a time range typed next cuts THIS file, no reply.
                    from handlers import cut_handler

                    cut_handler.remember(sent)
                    file_cache.put(cache_key, sent.video.file_id)
                    await archive.mirror(context.bot, "video",
                                         sent.video.file_id, caption=info.title)
                stats.record_download(query.message.chat_id, "yt-video", info.title)
        log.info("upload finished: %.1fMB in %.1fs", size_mb,
                 time.monotonic() - upload_started)
        await status.delete()
    except Exception as e:
        log.exception("upload failed after %.1fs (%.1fMB)",
                      time.monotonic() - upload_started, size_mb)
        await status.edit_text(t(query.message.chat_id, "❌ آپلود ناموفق: {err}").format(err=scrub(e)))
    finally:
        path.unlink(missing_ok=True)


def _fmt_views(n: int) -> str:
    """Views at a glance. A raw 1483920 is a number to decode, not to read."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n) if n else "—"


async def _send_channel_menu(msg, info) -> None:
    """The uploader's most-watched videos, as buttons that open the normal
    quality menu."""
    status = await msg.reply_text(
        t(msg.chat_id, "📺 دنبال معروف‌ترین ویدیوهای کانال می‌گردم…"))

    try:
        videos = await yt.popular_from_channel(info.channel_url)
    except Exception as e:
        log.info("channel listing failed for %s: %s", info.channel_url, e)
        await status.edit_text(
            t(msg.chat_id, "😕 لیست ویدیوهای این کانال رو نتونستم بگیرم."))
        return

    if not videos:
        await status.edit_text(t(msg.chat_id, "😕 ویدیوی دیگه‌ای از این کانال پیدا نشد."))
        return

    rows = []
    for v in videos:
        # Telegram truncates a long button label with no ellipsis, so the title
        # is cut here where the numbers can be kept at the end.
        title = v.title if len(v.title) <= 32 else v.title[:31] + "…"
        rows.append([InlineKeyboardButton(
            f"▶️ {title} · {_fmt_views(v.views)}",
            callback_data=f"yt:{info.id}:pick={v.id}",
        )])

    await status.delete()
    await msg.reply_text(
        t(msg.chat_id, "📺 معروف‌ترین ویدیوهای *{name}*").format(name=info.uploader),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
