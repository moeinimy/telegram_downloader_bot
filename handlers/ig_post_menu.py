"""
The buttons that ride under a delivered Instagram post.

    👁 مشاهده در اینستاگرام   🔄 بروزرسانی اطلاعات
    🎬 همه کیفیت‌ها            🎧 فقط صدا
    🔗 لینک مستقیم             📝 کپشن

Attached by both entry points - a pasted link and a DM share - so the two
flows differ only in what triggered them, never in what the user ends up
holding.

Everything here reads from one modules.instagram.fetch_info call. The video
file downloaded during delivery is remembered so "audio only" is a remux
rather than a second download, and the info object is cached so five button
presses are not five round trips to Instagram.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from modules import instagram as ig
from utils import limits
from utils.helpers import fmt_duration
from utils.i18n import t
from utils.limits import BoundedDict

log = logging.getLogger(__name__)

# shortcode -> PostInfo, and shortcode -> path of the video already sent.
_info_cache: BoundedDict = BoundedDict(400)
_video_cache: BoundedDict = BoundedDict(400)

PREFIX = "igp"


def remember_video(shortcode: str, path: Path) -> None:
    _video_cache[shortcode] = str(path)


def public_link(shortcode: str, permalink: str = "", info: ig.PostInfo | None = None) -> str:
    """The post's address, never invented.

    A url was being built as /reel/<shortcode> whenever no permalink was
    passed. For a photo post that is the wrong path, and for a story it was
    fabricated twice over - the "shortcode" was itself derived from a story
    pk that has no shortcode - so a shared story arrived captioned with a
    reel link to nothing.
    """
    clean = (permalink or "").split("?")[0]
    if clean:
        return clean
    if not shortcode:
        return ""
    kind = "p" if (info and not info.is_video) else "reel"
    return f"https://www.instagram.com/{kind}/{shortcode}/"


def keyboard(chat_id: int, shortcode: str, permalink: str = "") -> InlineKeyboardMarkup:
    link = public_link(shortcode, permalink, _info_cache.get(shortcode))

    rows = []
    top = []
    if link:
        top.append(InlineKeyboardButton(t(chat_id, "👁 مشاهده در اینستاگرام"), url=link))
    top.append(InlineKeyboardButton(t(chat_id, "🔄 بروزرسانی اطلاعات"),
                                    callback_data=f"{PREFIX}:info:{shortcode}"))
    rows.append(top)
    return InlineKeyboardMarkup(rows + _action_rows(chat_id, shortcode))


def _action_rows(chat_id: int, shortcode: str) -> list[list[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton(t(chat_id, "🎬 همه کیفیت‌ها"),
                                 callback_data=f"{PREFIX}:q:{shortcode}"),
            InlineKeyboardButton(t(chat_id, "🎧 فقط صدا"),
                                 callback_data=f"{PREFIX}:aud:{shortcode}"),
        ],
        [
            InlineKeyboardButton(t(chat_id, "🔗 لینک مستقیم"),
                                 callback_data=f"{PREFIX}:link:{shortcode}"),
            InlineKeyboardButton(t(chat_id, "📝 کپشن"),
                                 callback_data=f"{PREFIX}:cap:{shortcode}"),
        ],
        [
            InlineKeyboardButton(t(chat_id, "💬 زیرنویس ویدیو (آزمایشی)"),
                                 callback_data=f"{PREFIX}:sub:{shortcode}"),
        ],
    ]


async def _info(shortcode: str, refresh: bool = False) -> ig.PostInfo:
    cached = _info_cache.get(shortcode)
    if cached and not refresh:
        return cached
    info = await ig.fetch_info(shortcode)
    _info_cache[shortcode] = info
    return info


def _num(value: int) -> str:
    """Counters read better rounded; 17019 is noise, 17K is the point."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1_000:
        return f"{value / 1_000:.1f}K".replace(".0K", "K")
    return str(value)


def _stats(chat_id: int, info: ig.PostInfo) -> str:
    lines = []
    if info.username:
        lines.append(f"👤 @{info.username}")
    if info.taken_at:
        lines.append("🗓 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(info.taken_at)))
    counters = []
    if info.likes:
        counters.append(f"❤️ {_num(info.likes)}")
    if info.comments:
        counters.append(f"💬 {_num(info.comments)}")
    if info.views:
        counters.append(f"▶️ {_num(info.views)}")
    if counters:
        lines.append("  ".join(counters))
    return "\n".join(lines) or t(chat_id, "اطلاعاتی در دسترس نیست.")


async def caption_for(chat_id: int, shortcode: str, permalink: str = "") -> str:
    """The text under a delivered post.

    Was the raw shared url, query string and all - "?id=...&is_sponsored=
    false&is_ineligible_for_clips_chaining=false" is Instagram's plumbing,
    not something a user wants to read. Fetching the info here also warms the
    cache the buttons read from, so "caption" and "direct link" answer without
    a round trip.
    """
    try:
        info = await _info(shortcode)
    except Exception as e:
        log.info("ig post menu: no info for %s (%s) - plain link caption", shortcode, e)
        return public_link(shortcode, permalink)

    clean = public_link(shortcode, permalink, info)

    parts = []
    head = []
    if info.username:
        head.append(f"👤 @{info.username}")
    if info.duration:
        head.append(f"⏱ {fmt_duration(info.duration)}")
    if info.taken_at:
        head.append("🗓 " + time.strftime("%Y-%m-%d", time.localtime(info.taken_at)))
    if head:
        parts.append("  ".join(head))

    counters = []
    if info.likes:
        counters.append(f"❤️ {_num(info.likes)}")
    if info.comments:
        counters.append(f"💬 {_num(info.comments)}")
    if info.views:
        counters.append(f"▶️ {_num(info.views)}")
    if counters:
        parts.append("  ".join(counters))

    head = info.caption.strip().replace("\n", " ")
    if head:
        # Telegram caps a media caption at 1024 characters, and the full text
        # is one button away anyway.
        parts.append(head[:180] + ("…" if len(head) > 180 else ""))

    if clean:
        parts.append(clean)
    return "\n".join(parts)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    action, shortcode = parts[1], parts[2]
    chat_id = query.message.chat_id

    handlers = {
        "info": _do_info,
        "cap": _do_caption,
        "link": _do_link,
        "q": _do_qualities,
        "pick": _do_pick,
        "aud": _do_audio,
        "sub": _do_subtitles,
    }
    handler = handlers.get(action)
    if handler is None:
        await query.answer()
        return

    await query.answer()
    try:
        await handler(query, chat_id, shortcode, parts)
    except Exception as e:
        log.warning("ig post menu %s failed for %s: %s", action, shortcode, e)
        await query.message.reply_text(t(chat_id, "❌ خطا: {err}").format(err=e))


# ---------------- actions ----------------

async def _do_info(query, chat_id: int, shortcode: str, _parts) -> None:
    info = await _info(shortcode, refresh=True)
    await query.message.reply_text(
        t(chat_id, "📊 *اطلاعات پست*\n\n{stats}").format(stats=_stats(chat_id, info)),
        parse_mode="Markdown",
    )


async def _do_caption(query, chat_id: int, shortcode: str, _parts) -> None:
    info = await _info(shortcode)
    if not info.caption.strip():
        await query.message.reply_text(t(chat_id, "این پست کپشن نداره."))
        return
    # Telegram rejects anything over 4096 characters outright, and Instagram
    # captions run long.
    text = info.caption
    for i in range(0, len(text), 3900):
        await query.message.reply_text(text[i : i + 3900])


async def _do_link(query, chat_id: int, shortcode: str, _parts) -> None:
    info = await _info(shortcode)
    if not info.urls:
        await query.message.reply_text(t(chat_id, "لینک مستقیمی پیدا نکردم."))
        return
    body = "\n\n".join(info.urls)
    await query.message.reply_text(
        t(chat_id, "🔗 *لینک مستقیم*\n\n{links}\n\n⏳ این لینک‌ها امضا شدن و تا چند ساعت بیشتر کار نمی‌کنن.")
        .format(links=body),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def _do_qualities(query, chat_id: int, shortcode: str, _parts) -> None:
    info = await _info(shortcode)
    if not info.qualities:
        await query.message.reply_text(t(chat_id, "این پست ویدیو نیست یا فقط یه کیفیت داره."))
        return

    rows = [
        [InlineKeyboardButton(
            f"🎬 {label}", callback_data=f"{PREFIX}:pick:{shortcode}:{index}"
        )]
        for index, (label, _url) in enumerate(info.qualities[:8])
    ]
    await query.message.reply_text(
        t(chat_id, "🎬 کیفیت رو انتخاب کن:"), reply_markup=InlineKeyboardMarkup(rows)
    )


async def _do_pick(query, chat_id: int, shortcode: str, parts) -> None:
    info = await _info(shortcode)
    index = int(parts[3])
    if index >= len(info.qualities):
        await query.message.reply_text(t(chat_id, "⌛ سشن منقضی شده. دوباره لینک رو بفرست."))
        return

    label, url = info.qualities[index]
    status = await query.message.reply_text(
        t(chat_id, "⬇️ در حال دانلود {label}…").format(label=label)
    )
    async with limits.download_slot(chat_id):
        path = await ig.fetch_quality(shortcode, url, label)
    with path.open("rb") as handle:
        from handlers.instagram_handler import video_kwargs

        await query.message.reply_video(
            video=handle, caption=f"{label} · {info.permalink}",
            **video_kwargs(path))
    await status.delete()


async def _local_video(chat_id: int, shortcode: str):
    """The delivered file, re-fetching the smallest rendition if the disk
    sweeper has already removed it. None when the post has no video."""
    from pathlib import Path as _Path

    source = _video_cache.get(shortcode)
    if source and _Path(source).exists():
        return _Path(source)

    info = await _info(shortcode)
    if not info.qualities:
        return None
    label, url = info.qualities[-1]  # smallest is plenty for audio work
    path = await ig.fetch_quality(shortcode, url, label)
    _video_cache[shortcode] = str(path)
    return path


async def _do_subtitles(query, chat_id: int, shortcode: str, _parts) -> None:
    from modules import transcribe

    if not transcribe.available():
        await query.message.reply_text(
            t(chat_id, "💬 زیرنویس هنوز روی این سرور نصب نشده.\n\nادمین: `botctl whisper`"),
            parse_mode="Markdown",
        )
        return

    # The local model on a shared core takes minutes, and a silent wait that
    # long reads as a hung bot. Say which one is running.
    waiting = (
        "💬 دارم به ویدیو گوش می‌دم…"
        if transcribe.backend() == "api"
        else "💬 دارم به ویدیو گوش می‌دم… (روی این سرور چند دقیقه طول می‌کشه)"
    )
    status = await query.message.reply_text(t(chat_id, waiting))
    async with limits.download_slot(chat_id):
        video = await _local_video(chat_id, shortcode)
        if video is None:
            await status.edit_text(t(chat_id, "این پست ویدیو نیست."))
            return
        # Transcribing the extracted audio rather than the video: whisper
        # would shell out to ffmpeg for the same conversion anyway, and this
        # way it reuses the file the "audio only" button already made.
        audio = await ig.extract_audio(video)
        text, language = await transcribe.transcribe(audio)

    if not text:
        await status.edit_text(
            t(chat_id, "💬 حرفی توش نشنیدم — احتمالا فقط موزیکه.")
        )
        return

    await status.delete()
    header = t(chat_id, "💬 *زیرنویس* (زبان: {lang})\n\n").format(language=language, lang=language)
    body = header + text
    for i in range(0, len(body), 3900):
        await query.message.reply_text(
            body[i : i + 3900], parse_mode="Markdown" if i == 0 else None
        )


async def _do_audio(query, chat_id: int, shortcode: str, _parts) -> None:
    status = await query.message.reply_text(t(chat_id, "🎧 در حال جدا کردن صدا…"))

    async with limits.download_slot(chat_id):
        video = await _local_video(chat_id, shortcode)
        if video is None:
            await status.edit_text(t(chat_id, "این پست ویدیو نیست."))
            return
        audio = await ig.extract_audio(video)

    info = _info_cache.get(shortcode)
    title = f"@{info.username} · {shortcode}" if info and info.username else shortcode
    with audio.open("rb") as handle:
        await query.message.reply_audio(audio=handle, title=title, performer="Instagram")
    await status.delete()
