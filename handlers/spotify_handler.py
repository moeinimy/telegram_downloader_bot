"""
Spotify flow.

URL cases:
  - track    → download_track
  - album    → list tracks, button "Download All" + per-track buttons
  - playlist → same as album (paginated for >25)
  - artist   → top-10 tracks as selectable buttons

Free text:
  - treat as search; show top 10 tracks; user picks → download.

Callback formats:
  sp:trk:<id>         download a single track
  sp:all:album:<id>   download every track of album
  sp:all:pl:<id>      download every track of playlist
"""

from __future__ import annotations

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from modules import spotify as sp
from utils.url_router import RouteResult, SpotifyKind

log = logging.getLogger(__name__)


# ---------- URL entrypoint ----------

async def handle_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE, route: RouteResult
) -> None:
    msg = update.effective_message
    kind = SpotifyKind(route.kind)

    if kind == SpotifyKind.TRACK:
        meta = await sp.get_track_meta(route.resource_id)
        await _send_and_download_track(msg, meta)

    elif kind == SpotifyKind.ALBUM:
        album = await sp.get_album_tracks(route.resource_id)
        await _send_tracklist(
            msg,
            title=f"💿 {album.name} — {', '.join(album.artists)}",
            tracks=album.tracks,
            bulk_callback=f"sp:all:album:{album.id}",
        )

    elif kind == SpotifyKind.PLAYLIST:
        pl = await sp.get_playlist_tracks(route.resource_id)
        await _send_tracklist(
            msg,
            title=f"📜 {pl.name} (by {pl.owner})",
            tracks=pl.tracks,
            bulk_callback=f"sp:all:pl:{pl.id}",
        )

    elif kind == SpotifyKind.ARTIST:
        tracks = await sp.get_artist_top_tracks(route.resource_id)
        await _send_tracklist(
            msg, title="🎤 Top tracks", tracks=tracks, bulk_callback=None
        )


# ---------- free-text search ----------

async def handle_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query: str
) -> None:
    msg = update.effective_message

    # No "searching..." placeholder: the metadata APIs answer in a few hundred
    # milliseconds, so sending and then deleting a status message cost more
    # round trips than the search itself.
    tracks = await sp.search_tracks(query, limit=10)
    if not tracks:
        await msg.reply_text("نتیجه‌ای پیدا نکردم.")
        return

    await _send_tracklist(msg, title=f"نتایج «{query}»", tracks=tracks, bulk_callback=None)


# ---------- helpers ----------

def _truncate(s: str, n: int = 44) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _source_icon(track_id: str) -> str:
    if track_id.startswith("yt_"):
        return "▶️"
    if track_id.startswith("sc_"):
        return "☁️"
    return "🎵"


def _button_label(t) -> str:
    """'Artist — Song · 3:24'. Duration disambiguates the remixes and live
    cuts that otherwise look like identical entries."""
    from utils.helpers import fmt_duration

    label = _truncate(f"{_source_icon(t.id)} {t.display}")
    if t.duration_ms:
        label += f" · {fmt_duration(t.duration_ms // 1000)}"
    return label


async def _send_tracklist(
    msg, *, title: str, tracks, bulk_callback: str | None
) -> None:
    rows = [
        [InlineKeyboardButton(_button_label(t), callback_data=f"sp:trk:{t.id}")]
        for t in tracks
    ]
    if bulk_callback:
        rows.append([InlineKeyboardButton("⬇️ دانلود همه", callback_data=bulk_callback)])
    await msg.reply_text(title, reply_markup=InlineKeyboardMarkup(rows))


async def _send_and_download_track(msg, meta) -> None:
    from config import settings
    from handlers.lyrics_handler import lyrics_button
    from utils import file_cache
    from utils.helpers import prepare_telegram_thumb, safe_filename

    cache_key = f"audio:{meta.id}"

    # Fast path: Telegram already has these bytes. Re-sending the file_id is
    # one API call - no download, no ffmpeg, no upload.
    cached = file_cache.get(cache_key)
    if cached:
        try:
            await msg.reply_audio(
                audio=cached,
                title=meta.name,
                performer=", ".join(meta.artists),
                duration=meta.duration_ms // 1000,
                reply_markup=lyrics_button(
                    ", ".join(meta.artists), meta.name, sp.platform_links(meta)
                ),
            )
            return
        except Exception as e:
            log.info("cached file_id rejected (%s) - re-downloading", e)
            file_cache.drop(cache_key)

    status = await msg.reply_text(f"⬇️ دانلود: *{meta.display}*", parse_mode="Markdown")
    try:
        path = await sp.download_track(meta)
    except Exception as e:
        await status.edit_text(f"❌ {e}")
        return

    # iOS/Desktop clients only show thumbnails passed via the API parameter,
    # not the ID3 art embedded inside the file.
    thumb_path = None
    if meta.cover_url:
        thumb_path = await prepare_telegram_thumb(
            meta.cover_url,
            settings.download_dir / "thumbs" / f"{safe_filename(meta.display)}.jpg",
        )

    await status.edit_text("📤 آپلود به تلگرام…")
    try:
        with path.open("rb") as fh:
            sent = await msg.reply_audio(
                audio=fh,
                title=meta.name,
                performer=", ".join(meta.artists),
                duration=meta.duration_ms // 1000,
                thumbnail=thumb_path.open("rb") if thumb_path else None,
                reply_markup=lyrics_button(
                    ", ".join(meta.artists), meta.name, sp.platform_links(meta)
                ),
            )
        if sent and sent.audio:
            file_cache.put(cache_key, sent.audio.file_id)
        await status.delete()
    except Exception as e:
        log.exception("spotify upload failed")
        await status.edit_text(f"❌ آپلود ناموفق: {e}")


# ---------- callbacks ----------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("sp:trk:"):
        track_id = data.split(":", 2)[2]
        meta = await sp.get_track_meta(track_id)
        await _send_and_download_track(query.message, meta)
        return

    if data.startswith("sp:all:"):
        _, _, kind, rid = data.split(":", 3)
        if kind == "album":
            container = await sp.get_album_tracks(rid)
        else:
            container = await sp.get_playlist_tracks(rid)
        await query.message.reply_text(
            f"⬇️ دانلود {len(container.tracks)} ترک — این چند دقیقه طول می‌کشه…"
        )
        for t in container.tracks:
            try:
                await _send_and_download_track(query.message, t)
            except Exception as e:
                log.warning("track failed %s: %s", t.display, e)
                await query.message.reply_text(f"⚠️ رد شد: {t.display} ({e})")
