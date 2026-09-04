"""`3:28-4:53` — keep that piece, drop the rest.

How it decides WHICH file.

Replying to the media is the unambiguous way, and it is the one that also
works on something the bot itself sent: a track it delivered is an ordinary
audio message, so `reply_to_message.audio` finds it with no bookkeeping at
all. That matters more than it sounds - "download this song, now cut the
chorus out of it" is the common request, and it costs nothing to support.

Without a reply, the last media in this chat is used. That is the shape
people actually type - send the file, then send the range - and demanding a
reply for it would be a rule invented for the bot's convenience.

Nothing here is a command. A message that is nothing but a time range is not
a search anybody meant, so it is read as a cut and the router hands it over
before it starts looking for songs called "3:28-4:53".
"""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import settings
from modules import cut as cutter
from utils.helpers import run_in_thread, safe_filename
from utils.i18n import t, localise
from utils.secrets import scrub

log = logging.getLogger(__name__)

# The last media seen in each chat, so "send the file, then send the range"
# works without a reply.
#
# A module dict rather than chat_data because the sends that matter most -
# _upload_track, the YouTube callbacks - have no `context` to write into, and
# threading one through them to support a convenience would be the tail
# wagging the dog. Same per-chat scoping, nothing else needed.
#
# Only the file_id and a few labels. Never a path: the file on disk is
# deleted right after sending, and a remembered path is a promise that stops
# being true within seconds.
_last: dict[int, dict] = {}
_LAST_LIMIT = 500


def remember(message) -> None:
    """Note the media in `message` as its chat's most recent, if any."""
    media, kind = _media_of(message)
    chat_id = getattr(getattr(message, "chat", None), "id", None)
    if media is None or chat_id is None:
        return
    if len(_last) >= _LAST_LIMIT:
        # Cheap and good enough: this is a convenience for the next few
        # seconds, not a store anything depends on.
        _last.clear()
    _last[chat_id] = {
        "file_id": media.file_id,
        "kind": kind,
        "name": getattr(media, "file_name", "") or "",
        "title": getattr(media, "title", "") or "",
        "performer": getattr(media, "performer", "") or "",
        "duration": getattr(media, "duration", 0) or 0,
    }


def _media_of(message):
    """(media, kind) for whichever kind of media a message carries."""
    if message is None:
        return None, ""
    for kind in ("audio", "video", "voice", "video_note", "animation", "document"):
        media = getattr(message, kind, None)
        if media:
            # A document is only ours when it is actually media - a pdf
            # replied to with a time range is not a cut request.
            if kind == "document":
                mime = (getattr(media, "mime_type", "") or "").lower()
                if not mime.startswith(("audio/", "video/")):
                    continue
                kind = "video" if mime.startswith("video/") else "audio"
            return media, kind
    return None, ""


def cut_button(chat_id: int | None = None) -> InlineKeyboardMarkup:
    """The button that goes under a file the bot just sent.

    It carries no id. The file is the message the button is attached to, so
    the callback reads it off `query.message` - which means the button never
    goes stale, never outgrows Telegram's 64-byte callback_data, and works
    the same on a track, a clip and a TikTok.
    """
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        t(chat_id, "✂️ برش بزن"), callback_data="cut:ask")]])


async def on_callback(update, context) -> None:
    """Pressing ✂️ points the next time range at THIS file.

    Telegram gives a bot no way to ask a question and wait for the answer, so
    the button cannot collect a range by itself. What it can do is make the
    file unambiguous: remember it as this chat's current one, then say what
    to type. The range that arrives next is handled by the ordinary text
    path, and lands on the right file even if three other things were sent
    in between.
    """
    query = update.callback_query
    await query.answer()
    media, _ = _media_of(query.message)
    if media is None:
        await query.message.reply_text(
            t(query.message.chat_id, "این پیام فایلی نداره."))
        return
    remember(query.message)
    await prompt_for_range(query.message, query.message)


async def prompt_for_range(reply_to, source) -> None:
    """Say what to type, with an example sized to the actual file.

    Shared with the menu under a file the user sent, so the two entry points
    cannot drift into saying different things.
    """
    media, _ = _media_of(source)
    total = getattr(media, "duration", 0) or 0 if media else 0
    example = ("0:15-0:45" if not total or total > 60
               else f"0:05-{cutter.format_stamp(max(6, total - 2))}")
    chat_id = reply_to.chat_id
    await reply_to.reply_text(
        t(chat_id, "✂️ بازه رو بنویس، مثلا `{example}`{total}")
        .format(example=example,
                total=(t(chat_id, "\n(کل فایل {len})")
                       .format(len=cutter.format_stamp(total)) if total else "")),
        parse_mode="Markdown")


@run_in_thread(heavy=True)
def _do_cut(src: Path, start: float, end: float, dest: Path) -> Path:
    return cutter.cut(src, start, end, dest)


async def try_cut(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle the message as a cut request. False means it was not one."""
    msg = update.effective_message
    span = cutter.parse_range(msg.text or "")
    if span is None:
        return False
    start, end = span

    source = _media_of(msg.reply_to_message)[0]
    kind = _media_of(msg.reply_to_message)[1]
    remembered = None
    if source is None:
        remembered = _last.get(msg.chat_id)
        if not remembered:
            await msg.reply_text(t(
                msg.chat_id,
                "✂️ اول یه آهنگ یا ویدیو بفرست، بعد بازه رو بنویس — "
                "یا روی همون فایل ریپلای کن."))
            return True
        kind = remembered["kind"]

    file_id = source.file_id if source is not None else remembered["file_id"]
    duration = ((getattr(source, "duration", 0) or 0) if source is not None
                else remembered.get("duration", 0))

    # A range past the end of the file is a typo, and saying so beats handing
    # back an empty clip or an ffmpeg error.
    if duration and start >= duration:
        await msg.reply_text(t(
            msg.chat_id,
            "⚠️ این فایل {total} بیشتر نیست، ولی از {start} خواستی.").format(
                total=cutter.format_stamp(duration),
                start=cutter.format_stamp(start)))
        return True

    status = await msg.reply_text(t(msg.chat_id, "✂️ در حال برش…"))
    work = settings.download_dir / "cut"
    work.mkdir(parents=True, exist_ok=True)
    src = work / f"src_{file_id[:24]}"
    dest = None
    try:
        from handlers.recognize_handler import _fetch_to_disk

        src = await _fetch_to_disk(context, file_id, src)

        total = cutter.media_seconds(Path(src))
        if total and start >= total:
            await status.edit_text(t(
                msg.chat_id,
                "⚠️ این فایل {total} بیشتر نیست، ولی از {start} خواستی.").format(
                    total=cutter.format_stamp(total),
                    start=cutter.format_stamp(start)))
            return True
        # Asking past the end is a typo; asking a bit past it is just "to the
        # end", and trimming the request is kinder than refusing it.
        if total:
            end = min(end, total)

        stem = safe_filename(
            (remembered or {}).get("title")
            or getattr(source, "title", "")
            or getattr(source, "file_name", "")
            or "cut")
        suffix = Path(str(src)).suffix or (".mp3" if kind == "audio" else ".mp4")
        dest = work / (f"{stem} "
                       f"[{cutter.format_stamp(start)}-{cutter.format_stamp(end)}]"
                       f"{suffix}")
        dest = await _do_cut(Path(src), start, end, dest)

        caption = (f"✂️ {cutter.format_stamp(start)} — "
                   f"{cutter.format_stamp(end)}")
        with dest.open("rb") as fh:
            if kind in ("audio", "voice"):
                sent = await msg.reply_audio(
                    audio=fh, caption=caption,
                    title=(remembered or {}).get("title")
                    or getattr(source, "title", None),
                    performer=(remembered or {}).get("performer")
                    or getattr(source, "performer", None),
                    duration=int(end - start),
                    reply_markup=cut_button(msg.chat_id))
            else:
                sent = await msg.reply_video(
                    video=fh, caption=caption, duration=int(end - start),
                    supports_streaming=True,
                    reply_markup=cut_button(msg.chat_id))
        await status.delete()

        # The clip is a file of its own: cutting a cut is a normal thing to
        # want, and without this the chat's "last media" is still the source.
        remember(sent)
        try:
            from utils import archive

            await archive.mirror(context.bot, "audio" if kind in ("audio", "voice")
                                 else "video",
                                 (getattr(sent, "audio", None)
                                  or getattr(sent, "video", None)).file_id,
                                 caption=f"cut {stem} {caption}")
        except Exception:
            pass
    except Exception as e:
        log.warning("cut failed: %s", e)
        try:
            await status.edit_text("❌ " + scrub(localise(msg.chat_id, e)))
        except Exception:
            pass
    finally:
        Path(src).unlink(missing_ok=True)
        if dest is not None:
            Path(dest).unlink(missing_ok=True)
    return True
