"""
Music-recognition flow: identify the song in a video / reel / uploaded file
and send the full track back.

Entry points:
  - recognize_from_url(msg, url)   : download a snippet, recognize, send.
  - recognize_from_file(msg, path) : recognize an already-local file, send.
  - on_media(update, ctx)          : handler for videos/audio sent directly
                                     to the bot.

After recognition we reuse the Spotify pipeline (search -> download_track ->
send with cover art) so the user gets a tagged 320kbps MP3.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import settings
from handlers.spotify_handler import _send_and_download_track


def _outage_text(chat_id: int) -> str:
    """The outage message, with the reason attached for whoever can fix it.

    Falling through to the other engines already happens - the log says
    "falling through to the others" - but with neither ACOUSTID_API_KEY nor
    AUDD_API_TOKEN set there is nothing to fall through TO, so the fallback
    runs, finds no engines, and the user sees the same outage message as
    before. From the outside that is indistinguishable from the fallback not
    existing, which is exactly how it was reported.
    """
    from utils.i18n import t as _t

    text = _t(chat_id, "⏳ سرویس تشخیص آهنگ الان جواب نمی‌ده. چند دقیقه دیگه دوباره امتحان کن.")

    if chat_id in settings.admin_ids and not (settings.acoustid_key or settings.audd_token):
        text += (
            "\n\n———\n"
            "👤 فقط تو (ادمین) اینو می‌بینی:\n\n"
            "شزم به IP این سرور ۴۰۳ می‌ده، و *هیچ موتور جایگزینی ست نشده* — "
            "پس چیزی برای سوییچ کردن وجود نداره.\n\n"
            "• کلید رایگان AcoustID (بدون سقف عملی):\n"
            "  acoustid.org/new-application → botctl engines\n"
            "• یا پروکسی برای خود شزم: SHAZAM_PROXY"
        )
    return text
from modules import recognize as rec
from modules import spotify as sp
from utils.i18n import t
from utils.limits import BoundedDict

from utils.secrets import scrub

log = logging.getLogger(__name__)


async def recognize_from_url(msg, url: str) -> None:
    """Sample up to two chunks of the video; songs often start mid-video."""
    status = await msg.reply_text(t(msg.chat_id, "🎧 در حال گوش دادن به ویدیو و پیدا کردن آهنگ…"))

    candidates: list = []
    for offset in (0, 60):
        try:
            snippet = await rec.fetch_audio_snippet(url, seconds=90, offset=offset)
        except Exception as e:
            if offset == 0:
                await status.edit_text(t(msg.chat_id, "❌ نتونستم صدای ویدیو رو بگیرم: {err}").format(err=scrub(e)))
                return
            break  # second chunk unavailable (video too short)

        try:
            candidates = await rec.recognize_candidates(snippet)
        except rec.RecognitionUnavailable:
            snippet.unlink(missing_ok=True)
            await status.edit_text(
                _outage_text(msg.chat_id)
            )
            return
        finally:
            snippet.unlink(missing_ok=True)

        if candidates:
            break
        if offset == 0:
            await status.edit_text(t(msg.chat_id, "🎧 اول ویدیو جواب نداد، وسطش رو گوش می‌دم…"))

    await _handle_candidates(msg, status, candidates)


async def recognize_from_file(msg, path: Path, cleanup: bool = False) -> None:
    status = await msg.reply_text(t(msg.chat_id, "🎧 در حال پیدا کردن آهنگ…"))
    await _recognize_and_send(msg, status, path, cleanup=cleanup)


async def _recognize_and_send(msg, status, audio_path: Path, *, cleanup: bool) -> None:
    import time as _time

    started = _time.monotonic()
    try:
        candidates = await rec.recognize_candidates(audio_path)
    except rec.RecognitionUnavailable:
        await status.edit_text(
            _outage_text(msg.chat_id)
        )
        return
    except Exception as e:
        await status.edit_text(t(msg.chat_id, "❌ خطا در تشخیص آهنگ: {err}").format(err=scrub(e)))
        return
    finally:
        if cleanup:
            try:
                audio_path.unlink(missing_ok=True)
            except Exception:
                pass

    identified = _time.monotonic()
    await _handle_candidates(msg, status, candidates)

    # Two numbers, because they have completely different fixes: recognition
    # is Shazam round trips (a proxy multiplies them), delivery is yt-dlp
    # plus the Telegram upload. "It is slow" has been about the second one
    # more often than the first.
    log.info(
        "recognize: identify %.1fs, fetch+send %.1fs (total %.1fs) - phases %s",
        identified - started, _time.monotonic() - identified,
        _time.monotonic() - started, rec.last_timing,
    )


# Ambiguous results, keyed by a short hash so they fit in callback data.
_pending = BoundedDict(200)


async def _handle_candidates(msg, status, candidates: list) -> None:
    if not candidates:
        await status.edit_text(
            t(msg.chat_id,
              "😕 آهنگی تشخیص ندادم.\n\n"
              "معمولا یعنی صدای موزیک زیر حرف/افکت گم شده یا تیکه خیلی کوتاهه. "
              "یه بخش بلندتر که موزیکش واضح‌تره بفرست، یا اسم آهنگ رو تایپ کن.")
        )
        return

    top_song, top_votes = candidates[0]

    # Only a match two independent windows agreed on is treated as certain.
    # A single hit used to be announced as fact, which is how a 14-second clip
    # came back confidently labelled as an unrelated track.
    if top_votes >= 2:
        await _download_recognized(msg, status, top_song)
        return

    import hashlib

    key = hashlib.md5(f"{id(candidates)}{top_song.query}".encode()).hexdigest()[:12]
    _pending[key] = [s for s, _ in candidates]

    rows = [
        [InlineKeyboardButton(f"🎵 {s.artist} — {s.title}"[:60],
                              callback_data=f"rec:pick:{key}:{i}")]
        for i, (s, _) in enumerate(candidates[:5])
    ]
    header = (
        "🎧 مطمئن نیستم — این احتمالات رو پیدا کردم:"
        if len(candidates) > 1
        else "🎧 این رو پیدا کردم ولی مطمئن نیستم:"
    )
    await status.edit_text(
        f"{header}\n\n"
        "اگه درسته بزن روش. اگه نه، اسم آهنگ رو تایپ کن یا یه تیکه‌ی "
        "بلندتر/واضح‌تر از ویدیو بفرست.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _download_recognized(msg, status, song) -> None:
    await status.edit_text(
        f"✅ پیدا شد: *{song.artist} — {song.title}*\n⬇️ در حال دانلود…",
        parse_mode="Markdown",
    )

    tracks = await sp.search_tracks(song.query, limit=1)
    if not tracks:
        await status.edit_text(
            f"🎵 آهنگ: {song.artist} — {song.title}\n😕 ولی برای دانلود پیداش نکردم."
        )
        return

    await status.delete()
    await _send_and_download_track(msg, tracks[0])


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User chose which of the ambiguous matches was right."""
    query = update.callback_query
    await query.answer()
    _, _, key, idx = query.data.split(":", 3)

    options = _pending.get(key)
    if not options:
        await query.message.reply_text(t(query.message.chat_id, "⌛ سشن منقضی شده. دوباره ویدیو رو بفرست."))
        return
    try:
        song = options[int(idx)]
    except (ValueError, IndexError):
        return

    status = await query.message.reply_text("⬇️ …")  # placeholder, no text to translate
    await _download_recognized(query.message, status, song)


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Identify the music in any video/audio the user sends or forwards.

    Files sent "as file" arrive as documents, so those count too - but only
    when the MIME type is audio/video, never for arbitrary attachments."""
    msg = update.effective_message
    media = msg.video or msg.audio or msg.voice or msg.video_note
    if media is None and msg.document:
        mime = (msg.document.mime_type or "").lower()
        if mime.startswith(("audio/", "video/")):
            media = msg.document
    if not media:
        return

    # The public Bot API refuses to hand a bot any file over 20MB - a normal
    # phone video clears that easily, and the failure surfaces as an unhelpful
    # "Not Found". Say what actually happened instead.
    size = getattr(media, "file_size", 0) or 0
    if not settings.bot_api_base_url and size > 20 * 1024 * 1024:
        await msg.reply_text(
            f"⚠️ این فایل {size / 1024 / 1024:.0f} مگابایته و تلگرام اجازه نمی‌ده "
            "بات فایل‌های بزرگ‌تر از ۲۰ مگ رو بگیره.\n\n"
            "دو راه داری:\n"
            "• یه تیکه کوتاه‌تر از ویدیو بفرست\n"
            "• یا Local Bot API رو فعال کن (تو سرور: botctl → گزینه ۹) تا این "
            "محدودیت کلا برداشته بشه."
        )
        return

    status = await msg.reply_text(t(msg.chat_id, "📥 در حال دریافت فایل…"))
    out_dir = settings.download_dir / "recognize"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"upload_{media.file_unique_id}"

    try:
        saved = await _fetch_to_disk(context, media.file_id, dest)
    except Exception as e:
        await status.edit_text(t(msg.chat_id, "❌ دریافت فایل ناموفق: {err}").format(err=scrub(e)))
        return

    await status.delete()
    await recognize_from_file(msg, Path(saved), cleanup=True)


async def _fetch_to_disk(context, file_id: str, dest: Path) -> Path:
    """
    Get a Telegram file onto local disk.

    With a local Bot API server in --local mode, getFile returns a path on the
    server's own filesystem instead of a download URL. That only works if the
    bot can actually see that path: when the server runs in Docker on a named
    volume, the path exists solely inside the container and every download
    fails. Copy it directly when it is visible, and say exactly what is wrong
    when it is not.
    """
    import shutil

    log.info(
        "get_file: id=%s local_api=%s local_mode=%s",
        file_id[:24],
        settings.bot_api_base_url or "-",
        getattr(context.bot, "local_mode", "?"),
    )
    try:
        tg_file = await context.bot.get_file(file_id)
    except Exception as e:
        log.warning("get_file failed: %s: %s", type(e).__name__, e)
        if settings.bot_api_base_url and "not found" in str(e).lower():
            # python-telegram-bot only keeps the server's absolute path when it
            # can stat it. If the bot user cannot read the Bot API data dir the
            # path looks remote, PTB retries it over HTTP, and the local server
            # replies 404 - which arrives here as a bare "Not Found".
            raise RuntimeError(
                "سرور Local Bot API فایل رو گرفته ولی بات اجازه‌ی خوندنش رو نداره.\n\n"
                "روی سرور این رو بزن:\n"
                "botctl fixperms\n\n"
                "و برای اینکه دیگه تکرار نشه یه بار «botctl → گزینه ۹» رو دوباره اجرا کن."
            ) from e
        raise

    file_path = getattr(tg_file, "file_path", "") or ""
    log.info("get_file ok: file_path=%r", file_path)
    src = _server_path(file_path)

    if src is not None:
        if src.exists():
            await asyncio.to_thread(shutil.copyfile, src, dest)
            return dest
        raise RuntimeError(
            "سرور Local Bot API فایل رو اینجا گذاشته:\n"
            f"{src}\n"
            "ولی بات نمی‌تونه بخونتش (دسترسی فایل).\n\n"
            "روی سرور این رو بزن:\n"
            "botctl fixperms"
        )

    try:
        return Path(await tg_file.download_to_drive(custom_path=str(dest)))
    except Exception as e:
        # Include what we were actually asked to fetch: a bare "Not Found"
        # says nothing about which path or URL failed.
        raise RuntimeError(f"{e} (file_path={file_path!r})") from e


def _server_path(file_path: str) -> Path | None:
    """
    Recover the Bot API server's on-disk path for a file.

    In --local mode getFile answers with an absolute path, but
    python-telegram-bot only leaves it alone when it can stat it: if the file
    is unreadable it rewrites the value into a download URL. The local server
    does not serve files, so that URL 404s and arrives as "Not Found". Pull
    the path back out of the URL so the real cause (permissions) can be
    reported instead of Telegram's generic error.
    """
    if not settings.bot_api_base_url or not file_path:
        return None

    if file_path.startswith("/") or Path(file_path).is_absolute():
        return Path(file_path)

    if "://" in file_path:
        for marker in ("/var/lib/telegram-bot-api/", "/var/lib/telegram-bot-api"):
            idx = file_path.find(marker)
            if idx != -1:
                return Path(file_path[idx:])
    return None
