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

import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from config import settings
from handlers.spotify_handler import _send_and_download_track
from modules import recognize as rec
from modules import spotify as sp

log = logging.getLogger(__name__)


async def recognize_from_url(msg, url: str) -> None:
    """Sample up to two windows of the video; songs often start mid-video."""
    status = await msg.reply_text("🎧 در حال گوش دادن به ویدیو و پیدا کردن آهنگ…")

    song = None
    for offset in (0, 60):
        try:
            snippet = await rec.fetch_audio_snippet(url, offset=offset)
        except Exception as e:
            if offset == 0:
                await status.edit_text(f"❌ نتونستم صدای ویدیو رو بگیرم: {e}")
                return
            break  # second window unavailable (video too short) - keep first result

        try:
            song = await rec.recognize_file(snippet)
        finally:
            snippet.unlink(missing_ok=True)

        if song:
            break
        if offset == 0:
            await status.edit_text("🎧 پنجره اول جواب نداد، وسط ویدیو رو گوش می‌دم…")

    await _handle_recognition_result(msg, status, song)


async def recognize_from_file(msg, path: Path, cleanup: bool = False) -> None:
    status = await msg.reply_text("🎧 در حال پیدا کردن آهنگ…")
    await _recognize_and_send(msg, status, path, cleanup=cleanup)


async def _recognize_and_send(msg, status, audio_path: Path, *, cleanup: bool) -> None:
    try:
        song = await rec.recognize_file(audio_path)
    except Exception as e:
        await status.edit_text(f"❌ خطا در تشخیص آهنگ: {e}")
        return
    finally:
        if cleanup:
            try:
                audio_path.unlink(missing_ok=True)
            except Exception:
                pass

    await _handle_recognition_result(msg, status, song)


async def _handle_recognition_result(msg, status, song) -> None:
    if not song:
        await status.edit_text("😕 نتونستم آهنگی تو این ویدیو تشخیص بدم.")
        return

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

    status = await msg.reply_text("📥 در حال دریافت فایل…")
    out_dir = settings.download_dir / "recognize"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"upload_{media.file_unique_id}"

    try:
        tg_file = await context.bot.get_file(media.file_id)
        saved = await tg_file.download_to_drive(custom_path=str(dest))
    except Exception as e:
        await status.edit_text(f"❌ دریافت فایل ناموفق: {e}")
        return

    await status.delete()
    await recognize_from_file(msg, Path(saved), cleanup=True)
