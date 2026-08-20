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
from utils.i18n import t
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

    video = next((f for f in files if f.suffix.lower() in _VIDEO_EXTS), None)
    if kind in (InstagramKind.POST, InstagramKind.REEL, InstagramKind.IGTV):
        await post_menu(
            msg.get_bot(), msg.chat_id, route.resource_id, route.url, video,
            offer_music=bool(video),
        )
        if video:
            context.chat_data.setdefault("ig_rec", {})[route.resource_id] = str(video)


async def post_menu(bot, chat_id: int, shortcode: str, permalink: str,
                    video: Path | None, offer_music: bool = True) -> None:
    """The button strip under a delivered post.

    Shared by the pasted-link flow and the DM bridge, so the two differ only
    in what triggered them. Music identification stays a separate row because
    it is the one action that reads the file rather than the post.
    """
    from handlers import ig_post_menu

    if video:
        ig_post_menu.remember_video(shortcode, video)

    rows = list(ig_post_menu.keyboard(chat_id, shortcode, permalink).inline_keyboard)
    if offer_music:
        rows.append([InlineKeyboardButton(
            t(chat_id, "🎧 پیدا کردن آهنگ این ویدیو"), callback_data=f"ig:rec:{shortcode}"
        )])

    try:
        await bot.send_message(
            chat_id,
            t(chat_id, "چیکار دیگه‌ای برات بکنم؟"),
            reply_markup=InlineKeyboardMarkup(rows),
        )
    except Exception as e:
        log.info("could not attach the post menu in %s: %s", chat_id, e)


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

        from handlers import ig_post_menu

        shortcode = username  # third segment is the shortcode here
        # chat_data covers the pasted-link flow; the module cache covers the
        # DM bridge, which has no chat_data to write into.
        path_str = (
            context.chat_data.get("ig_rec", {}).get(shortcode)
            or ig_post_menu._video_cache.get(shortcode)
        )
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

# Telegram's sendPhoto only reliably accepts JPEG and PNG. WEBP is a sticker
# format to it, and a still Instagram served as WEBP came back as
# "Image_process_failed" - a Telegram error for a file that had downloaded
# perfectly, which reads like a download failure and is not one.
_TELEGRAM_PHOTO_EXTS = {".jpg", ".jpeg", ".png"}


def _prepare_photo(path: Path) -> tuple[Path, str]:
    """(file to send, how to send it) for one downloaded still.

    Re-encodes anything Telegram will not take. If Pillow cannot open it at
    all then it is not an image, and sending it as a document at least gets
    the user their file instead of an error.
    """
    if path.suffix.lower() in _TELEGRAM_PHOTO_EXTS:
        return path, "photo"
    try:
        from PIL import Image

        out = path.with_name(f"{path.stem}_tg.jpg")
        with Image.open(path) as img:
            img.convert("RGB").save(out, "JPEG", quality=92)
        return out, "photo"
    except Exception as e:
        log.warning("instagram: %s is not a sendable image (%s) - sending as file", path.name, e)
        return path, "document"


def video_kwargs(path) -> dict:
    """What Telegram needs to be TOLD about a video, rather than guess.

    Given no dimensions and no duration, Telegram picks a default box, shows
    the length as 00:00, and will not stream the file - it has to be fully
    downloaded before it plays. The YouTube path was taught this; the
    Instagram path was not, which is why a reel arrived unstreamable with no
    timeline while a YouTube video of the same size arrived fine.

    Shared rather than copied, because that divergence is the bug.
    """
    from modules.youtube import probe_dimensions

    width, height, seconds = probe_dimensions(path)
    return {
        "supports_streaming": True,
        "duration": seconds or None,
        "width": width or None,
        "height": height or None,
    }


def _classify(files: list[Path]) -> list[tuple[Path, str]]:
    out = []
    for f in files:
        if f.suffix.lower() in _VIDEO_EXTS:
            out.append((f, "video"))
        else:
            out.append(_prepare_photo(f))
    return out


async def deliver(bot, chat_id: int, files: list[Path], caption: str | None = None) -> None:
    """Upload downloaded Instagram media to a chat.

    Addressed by bot + chat_id rather than by a Message, because the DM bridge
    has no incoming Telegram message to reply to - the trigger was an
    Instagram DM. The link-paste flow goes through _send_media, which is a thin
    wrapper over this, so both paths share one upload implementation.
    """
    from contextlib import ExitStack

    items = _classify(files)

    if len(items) == 1:
        path, kind = items[0]
        with path.open("rb") as handle:
            if kind == "video":
                await bot.send_video(chat_id=chat_id, video=handle,
                                     caption=caption,
                                     **video_kwargs(path))
            elif kind == "photo":
                await bot.send_photo(chat_id=chat_id, photo=handle, caption=caption)
            else:
                await bot.send_document(chat_id=chat_id, document=handle, caption=caption)
        return

    # batch into groups of 10 (Telegram limit)
    for i in range(0, len(items), 10):
        chunk = items[i : i + 10]
        # A media group takes photos and videos but not documents, so anything
        # unsendable as either goes on its own afterwards rather than making
        # the whole group fail.
        with ExitStack() as stack:
            media = []
            for index, (path, kind) in enumerate(chunk):
                if kind == "document":
                    continue
                handle = stack.enter_context(path.open("rb"))
                # Telegram shows only the first item's caption for a group.
                text = caption if (i == 0 and index == 0) else None
                if kind == "video":
                    # An album item needs telling too, or the same reel
                    # that streams on its own arrives in a group with no
                    # timeline and no preview.
                    media.append(InputMediaVideo(media=handle, caption=text,
                                                 **video_kwargs(path)))
                else:
                    media.append(InputMediaPhoto(media=handle, caption=text))
            if media:
                await bot.send_media_group(chat_id=chat_id, media=media)

        for path, kind in chunk:
            if kind == "document":
                with path.open("rb") as handle:
                    await bot.send_document(chat_id=chat_id, document=handle)


async def _send_media(msg, files: list[Path]) -> None:
    """Send single file directly; send multiple as media group(s) of <=10."""
    await deliver(msg.get_bot(), msg.chat_id, files)
