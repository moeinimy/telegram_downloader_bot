"""
Instagram flow.

Routing of the kind happens here:
  - post / reel / IGTV  → fetch_post(shortcode), send media group / single video.
  - story               → fetch_story(username); send each item.
  - profile             → ask user via inline keyboard:
                          [📸 عکس پروفایل]  [📖 استوری‌ها]  [📜 پست‌های اخیر]
"""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.ext import ContextTypes

from modules import instagram as ig
from utils.url_router import InstagramKind, RouteResult

log = logging.getLogger(__name__)

_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov"}


async def handle_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE, route: RouteResult
) -> None:
    msg = update.effective_message
    kind = InstagramKind(route.kind)

    if kind == InstagramKind.PROFILE:
        await _profile_menu(msg, username=route.resource_id, context=context)
        return

    status = await msg.reply_text(t(msg.chat_id, "⬇️ در حال دانلود از اینستاگرام…"))

    try:
        if kind in (InstagramKind.POST, InstagramKind.REEL, InstagramKind.IGTV):
            files = await ig.fetch_post(route.resource_id)
        elif kind == InstagramKind.STORY:
            files = await ig.fetch_story(route.resource_id)
        else:
            await status.edit_text(t(msg.chat_id, "🤔 نوع لینک اینستا رو نشناختم."))
            return
    except Exception as e:
        await status.edit_text(f"❌ خطا: {e}")
        return

    await _send_media(msg, files)
    await status.delete()

    # If the post contains video, offer to identify its music.
    video = next((f for f in files if f.suffix.lower() in _VIDEO_EXTS), None)
    if video:
        context.chat_data.setdefault("ig_rec", {})[route.resource_id] = str(video)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t(msg.chat_id, "🎧 پیدا کردن آهنگ این ویدیو"), callback_data=f"ig:rec:{route.resource_id}")]]
        )
        await msg.reply_text(t(msg.chat_id, "آهنگ این ویدیو رو برات پیدا کنم؟"), reply_markup=kb)


async def _profile_menu(msg, username: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data.setdefault("ig", {})[username] = {"username": username}
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t(msg.chat_id, "📸 عکس پروفایل"), callback_data=f"ig:pp:{username}"),
                InlineKeyboardButton(t(msg.chat_id, "📖 استوری‌ها"), callback_data=f"ig:story:{username}"),
            ]
        ]
    )
    await msg.reply_text(f"چی از پروفایل *{username}* میخوای؟", parse_mode="Markdown", reply_markup=kb)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, action, username = query.data.split(":", 2)

    # Music recognition on a previously downloaded reel/video.
    if action == "rec":
        from handlers.recognize_handler import recognize_from_file

        shortcode = username  # third segment is the shortcode here
        path_str = context.chat_data.get("ig_rec", {}).get(shortcode)
        if not path_str or not Path(path_str).exists():
            await query.message.reply_text(t(query.message.chat_id, "⌛ فایل ویدیو دیگه موجود نیست. دوباره لینک رو بفرست."))
            return
        await recognize_from_file(query.message, Path(path_str))
        return

    status = await query.message.reply_text(t(query.message.chat_id, "⬇️ گرفتن از اینستا…"))
    try:
        if action == "pp":
            path = await ig.fetch_profile_pic(username)
            await query.message.reply_photo(photo=path.open("rb"))
        elif action == "story":
            files = await ig.fetch_story(username)
            await _send_media(query.message, files)
        else:
            await status.edit_text(t(query.message.chat_id, "اکشن نامعتبر."))
            return
    except Exception as e:
        await status.edit_text(f"❌ {e}")
        return
    await status.delete()


# ---------- media sending ----------

async def _send_media(msg, files: list[Path]) -> None:
    """Send single file directly; send multiple as media group(s) of <=10."""
    if len(files) == 1:
        f = files[0]
        if f.suffix.lower() in _VIDEO_EXTS:
            await msg.reply_video(video=f.open("rb"))
        else:
            await msg.reply_photo(photo=f.open("rb"))
        return

    # batch into groups of 10 (Telegram limit)
    for i in range(0, len(files), 10):
        chunk = files[i : i + 10]
        media = []
        for f in chunk:
            handle = f.open("rb")
            if f.suffix.lower() in _VIDEO_EXTS:
                media.append(InputMediaVideo(media=handle))
            else:
                media.append(InputMediaPhoto(media=handle))
        await msg.reply_media_group(media=media)
