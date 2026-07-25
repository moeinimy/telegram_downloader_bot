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
        await _send_container_menu(msg, "al", route.resource_id)

    elif kind == SpotifyKind.PLAYLIST:
        await _send_container_menu(msg, "pl", route.resource_id)

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
        # Nothing in the music catalogues - go straight to the slow deep
        # search rather than making the user press a button for it.
        tracks = await sp.deep_search(query, limit=10)
        if not tracks:
            await msg.reply_text("نتیجه‌ای پیدا نکردم.")
            return
        await _send_tracklist(
            msg, title=f"نتایج «{query}»", tracks=tracks, bulk_callback=None
        )
        return

    await _send_tracklist(
        msg,
        title=f"نتایج «{query}»",
        tracks=tracks,
        bulk_callback=None,
        deep_query=query,
    )


# ---------- helpers ----------

# Callback data is capped at 64 bytes, so the deep-search query is kept here
# and referenced by a short hash.
_query_cache: dict[str, str] = {}


def _remember_query(query: str) -> str:
    import hashlib

    key = hashlib.md5(query.encode("utf-8")).hexdigest()[:12]
    _query_cache[key] = query
    return key


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
    msg, *, title: str, tracks, bulk_callback: str | None, deep_query: str | None = None
) -> None:
    rows = [
        [InlineKeyboardButton(_button_label(t), callback_data=f"sp:trk:{t.id}")]
        for t in tracks
    ]
    if bulk_callback:
        rows.append([InlineKeyboardButton("⬇️ دانلود همه", callback_data=bulk_callback)])
    if deep_query:
        key = _remember_query(deep_query)
        rows.append(
            [InlineKeyboardButton("🔎 نتایج بیشتر (یوتیوب و ساندکلاد)",
                                  callback_data=f"sp:more:{key}")]
        )
    await msg.reply_text(title, reply_markup=InlineKeyboardMarkup(rows))


# ---------- album / playlist browsing ----------

_PAGE = 10
_container_cache: dict[str, object] = {}


async def _load_container(kind: str, rid: str):
    """Fetch an album/playlist once and keep it; paging must not re-scrape."""
    key = f"{kind}:{rid}"
    container = _container_cache.get(key)
    if container is None:
        container = await (
            sp.get_album_tracks(rid) if kind == "al" else sp.get_playlist_tracks(rid)
        )
        _container_cache[key] = container
        if len(_container_cache) > 50:
            _container_cache.pop(next(iter(_container_cache)), None)
    return container


def _container_title(kind: str, c) -> str:
    if kind == "al":
        return f"💿 {c.name} — {', '.join(c.artists)}"
    return f"📜 {c.name} (by {c.owner})"


def _range_buttons(kind: str, rid: str, total: int) -> list[list[InlineKeyboardButton]]:
    """Bulk-download shortcuts. Only offer sizes the container actually has."""
    rows: list[list[InlineKeyboardButton]] = []
    presets = [n for n in (10, 30, 50, 100) if n < total]

    def chunk(buttons):
        return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]

    rows += chunk([
        InlineKeyboardButton(f"⬇️ {n} تای اول", callback_data=f"sp:pldl:{kind}:{rid}:0:{n}")
        for n in presets
    ])
    rows += chunk([
        InlineKeyboardButton(
            f"⬇️ {n} تای آخر",
            callback_data=f"sp:pldl:{kind}:{rid}:{max(0, total - n)}:{n}",
        )
        for n in presets
    ])
    rows.append([
        InlineKeyboardButton(
            f"⬇️ همه ({total})", callback_data=f"sp:pldl:{kind}:{rid}:0:{total}"
        )
    ])
    return rows


def _page_keyboard(kind: str, rid: str, container, offset: int) -> InlineKeyboardMarkup:
    total = len(container.tracks)
    page = container.tracks[offset : offset + _PAGE]

    rows = [
        [InlineKeyboardButton(
            f"{offset + i + 1}. {_button_label(t)}", callback_data=f"sp:trk:{t.id}"
        )]
        for i, t in enumerate(page)
    ]

    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton(
            "◀️ قبلی", callback_data=f"sp:plpg:{kind}:{rid}:{max(0, offset - _PAGE)}"
        ))
    last_start = max(0, ((total - 1) // _PAGE) * _PAGE)
    nav.append(InlineKeyboardButton(
        f"{offset // _PAGE + 1}/{last_start // _PAGE + 1}", callback_data="sp:noop"
    ))
    if offset + _PAGE < total:
        nav.append(InlineKeyboardButton(
            "بعدی ▶️", callback_data=f"sp:plpg:{kind}:{rid}:{offset + _PAGE}"
        ))
    if len(nav) > 1:
        rows.append(nav)

    rows += _range_buttons(kind, rid, total)
    return InlineKeyboardMarkup(rows)


async def _send_container_menu(msg, kind: str, rid: str) -> None:
    container = await _load_container(kind, rid)
    total = len(container.tracks)
    if not total:
        await msg.reply_text("این لیست ترکی نداره.")
        return
    await msg.reply_text(
        f"{_container_title(kind, container)}\n🎵 {total} ترک",
        reply_markup=_page_keyboard(kind, rid, container, 0),
    )


async def _download_range(msg, container, offset: int, count: int) -> None:
    tracks = container.tracks[offset : offset + count]
    if not tracks:
        await msg.reply_text("چیزی تو این محدوده نیست.")
        return

    status = await msg.reply_text(f"⬇️ شروع دانلود {len(tracks)} ترک…")
    done = failed = 0
    for i, t in enumerate(tracks, 1):
        if await _send_and_download_track(msg, t):
            done += 1
        else:
            failed += 1
        # Editing on every track would burn the rate limit on long playlists.
        if i % 3 == 0 or i == len(tracks):
            try:
                await status.edit_text(
                    f"⬇️ {i}/{len(tracks)} — ✅ {done}" + (f" · ❌ {failed}" if failed else "")
                )
            except Exception:
                pass

    summary = f"✅ {done} ترک فرستاده شد"
    if failed:
        summary += f" · {failed} تا ناموفق"
    try:
        await status.edit_text(summary)
    except Exception:
        pass


async def _send_and_download_track(msg, meta) -> bool:
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
            return True
        except Exception as e:
            log.info("cached file_id rejected (%s) - re-downloading", e)
            file_cache.drop(cache_key)

    status = await msg.reply_text(f"⬇️ دانلود: *{meta.display}*", parse_mode="Markdown")
    try:
        path = await sp.download_track(meta)
    except Exception as e:
        await status.edit_text(f"❌ {e}")
        return False

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
        return True
    except Exception as e:
        log.exception("spotify upload failed")
        await status.edit_text(f"❌ آپلود ناموفق: {e}")
        return False


# ---------- callbacks ----------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "sp:noop":
        return

    if data.startswith("sp:plpg:"):
        _, _, kind, rid, offset = data.split(":", 4)
        container = await _load_container(kind, rid)
        try:
            await query.edit_message_reply_markup(
                reply_markup=_page_keyboard(kind, rid, container, int(offset))
            )
        except Exception as e:
            log.info("page edit failed: %s", e)
        return

    if data.startswith("sp:pldl:"):
        _, _, kind, rid, offset, count = data.split(":", 5)
        container = await _load_container(kind, rid)
        await _download_range(query.message, container, int(offset), int(count))
        return

    if data.startswith("sp:more:"):
        key = data.split(":", 2)[2]
        search_query = _query_cache.get(key)
        if not search_query:
            await query.message.reply_text("⌛ سشن منقضی شده. دوباره سرچ کن.")
            return
        status = await query.message.reply_text(
            "🔎 دنبال ریمیکس‌ها و نسخه‌های دیگه می‌گردم… (چند ثانیه طول می‌کشه)"
        )
        try:
            tracks = await sp.deep_search(search_query, limit=12)
        except Exception as e:
            await status.edit_text(f"❌ {e}")
            return
        if not tracks:
            await status.edit_text("چیز بیشتری پیدا نکردم.")
            return
        await status.delete()
        await _send_tracklist(
            query.message,
            title=f"🔎 نتایج بیشتر «{search_query}»",
            tracks=tracks,
            bulk_callback=None,
        )
        return

    if data.startswith("sp:trk:"):
        track_id = data.split(":", 2)[2]
        meta = await sp.get_track_meta(track_id)
        await _send_and_download_track(query.message, meta)
        return

    # Legacy "download everything" buttons from messages sent before the
    # paged menu existed.
    if data.startswith("sp:all:"):
        _, _, kind, rid = data.split(":", 3)
        container = await _load_container("al" if kind == "album" else "pl", rid)
        await _download_range(query.message, container, 0, len(container.tracks))
