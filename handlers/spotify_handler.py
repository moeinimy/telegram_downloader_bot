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
from modules import stats
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


# ---------- custom range ----------

async def handle_range_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> bool:
    """Consume '500-600' after the custom-range button. Returns True when the
    message was a range and has been handled."""
    import re

    pending = context.user_data.get("awaiting_range")
    if not pending:
        return False

    m = re.fullmatch(r"\s*(\d*)\s*[-–—تا]+\s*(\d*)\s*", text)
    if not m or not (m.group(1) or m.group(2)):
        await update.effective_message.reply_text(
            "فرمت درست نیست. مثل `500-600` بنویس یا برای لغو /start بزن.",
            parse_mode="Markdown",
        )
        return True

    context.user_data.pop("awaiting_range", None)
    kind, rid = pending
    container = await _load_container(kind, rid)
    total = len(container.tracks)

    start = int(m.group(1)) if m.group(1) else 1
    end = int(m.group(2)) if m.group(2) else total
    start = max(1, min(start, total))
    end = max(start, min(end, total))

    await update.effective_message.reply_text(f"⬇️ ترک {start} تا {end}")
    await _download_range(update.effective_message, container, start - 1, end - start + 1)
    return True


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
            "🔢 محدوده دلخواه", callback_data=f"sp:plask:{kind}:{rid}"
        ),
        InlineKeyboardButton(
            f"⬇️ همه ({total})", callback_data=f"sp:pldl:{kind}:{rid}:0:{total}"
        ),
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

    last_start = max(0, ((total - 1) // _PAGE) * _PAGE)

    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton(
            "◀️", callback_data=f"sp:plpg:{kind}:{rid}:{max(0, offset - _PAGE)}"
        ))
    nav.append(InlineKeyboardButton(
        f"{offset // _PAGE + 1}/{last_start // _PAGE + 1}", callback_data="sp:noop"
    ))
    if offset + _PAGE < total:
        nav.append(InlineKeyboardButton(
            "▶️", callback_data=f"sp:plpg:{kind}:{rid}:{offset + _PAGE}"
        ))
    if len(nav) > 1:
        rows.append(nav)

    # Stepping ten pages at a time: a 4000-track playlist is 400 pages, and
    # walking it one page per tap is not navigation.
    if total > _PAGE * 5:
        jump = [
            InlineKeyboardButton("⏮", callback_data=f"sp:plpg:{kind}:{rid}:0"),
            InlineKeyboardButton(
                "⏪ ۱۰ صفحه",
                callback_data=f"sp:plpg:{kind}:{rid}:{max(0, offset - _PAGE * 10)}",
            ),
            InlineKeyboardButton(
                "۱۰ صفحه ⏩",
                callback_data=f"sp:plpg:{kind}:{rid}:{min(last_start, offset + _PAGE * 10)}",
            ),
            InlineKeyboardButton("⏭", callback_data=f"sp:plpg:{kind}:{rid}:{last_start}"),
        ]
        rows.append(jump)

    rows += _range_buttons(kind, rid, total)
    return InlineKeyboardMarkup(rows)


async def _send_container_menu(msg, kind: str, rid: str) -> None:
    container = await _load_container(kind, rid)
    total = len(container.tracks)
    if not total:
        await msg.reply_text("این لیست ترکی نداره.")
        return
    header = f"{_container_title(kind, container)}\n🎵 {total} ترک"
    if getattr(container, "truncated", False):
        header += (
            "\n⚠️ اسپاتیفای بدون اکانت فقط ۱۰۰ ترک اول رو می‌ده؛ بقیه در دسترس نیست."
        )

    keyboard = _page_keyboard(kind, rid, container, 0)
    cover = getattr(container, "cover_url", "")
    if cover:
        try:
            await msg.reply_photo(photo=cover, caption=header, reply_markup=keyboard)
            return
        except Exception as e:
            log.info("container cover failed (%s) - sending text menu", e)
    await msg.reply_text(header, reply_markup=keyboard)


# Tracks fetched at once. Downloads are network- and ffmpeg-bound, so running
# a few in parallel hides most of the wait; uploads still go out in playlist
# order. Higher values mostly just annoy YouTube.
_PARALLEL_DOWNLOADS = 4

# Chats that pressed "stop" mid-batch.
_cancelled: set[int] = set()


async def _download_range(msg, container, offset: int, count: int) -> None:
    import asyncio

    from utils import file_cache

    tracks = container.tracks[offset : offset + count]
    if not tracks:
        await msg.reply_text("چیزی تو این محدوده نیست.")
        return

    chat_id = msg.chat_id
    _cancelled.discard(chat_id)

    # One cover for the whole batch. Sending artwork per track turned a
    # 50-song playlist into 100 messages.
    cover = getattr(container, "cover_url", "") or next(
        (t.cover_url for t in tracks if t.cover_url), ""
    )
    if cover:
        try:
            await msg.reply_photo(photo=cover, caption=f"🎵 {len(tracks)} ترک")
        except Exception as e:
            log.info("batch cover failed: %s", e)

    status = await msg.reply_text(
        f"⬇️ شروع دانلود {len(tracks)} ترک…",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⏹ توقف", callback_data="sp:stop")]]
        ),
    )
    sem = asyncio.Semaphore(_PARALLEL_DOWNLOADS)

    async def fetch(meta):
        """Download only - uploading happens in order in the loop below."""
        if file_cache.get(f"audio:{meta.id}"):
            return None  # already on Telegram; nothing to fetch
        async with sem:
            try:
                return await sp.download_track(meta)
            except Exception as e:
                return e

    jobs = [asyncio.create_task(fetch(t)) for t in tracks]

    done = failed = 0
    stopped = False
    for i, (meta, job) in enumerate(zip(tracks, jobs), 1):
        if chat_id in _cancelled:
            stopped = True
            for pending in jobs[i - 1 :]:
                pending.cancel()
            break

        result = await job
        try:
            if isinstance(result, Exception):
                failed += 1
                await msg.reply_text(f"⚠️ رد شد: {meta.display} — {result}")
            elif result is None:
                cached = file_cache.get(f"audio:{meta.id}")
                ok = (
                    await _send_cached(msg, meta, cached, with_cover=False)
                    if cached
                    else False
                )
                done += ok
                failed += not ok
            else:
                ok = await _upload_track(msg, meta, result, with_cover=False)
                done += ok
                failed += not ok
        except Exception as e:
            failed += 1
            log.warning("send failed for %s: %s", meta.display, e)

        # Editing on every track would burn the rate limit on long playlists.
        if i % 3 == 0 or i == len(tracks):
            try:
                await status.edit_text(
                    f"⬇️ {i}/{len(tracks)} — ✅ {done}"
                    + (f" · ❌ {failed}" if failed else ""),
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("⏹ توقف", callback_data="sp:stop")]]
                    ),
                )
            except Exception:
                pass

    _cancelled.discard(chat_id)
    summary = ("⏹ متوقف شد — " if stopped else "") + f"✅ {done} ترک فرستاده شد"
    if failed:
        summary += f" · {failed} تا ناموفق"
    try:
        await status.edit_text(summary)
    except Exception:
        pass


def _cover_caption(meta, status: str = "") -> str:
    caption = f"🎵 *{meta.name}*\n👤 {', '.join(meta.artists)}"
    if meta.album:
        caption += f"\n💿 {meta.album}"
    if status:
        caption += f"\n\n{status}"
    return caption


async def _send_cover(msg, meta, status: str = ""):
    """Cover art as its own message, full resolution, with the platform links
    beneath it. Telegram compresses a photo far less than the 320x320 thumbnail
    the audio file is allowed to carry. Returns the sent message (or None) so
    the caller can update its caption instead of posting a separate status."""
    if not meta.cover_url:
        return None
    from handlers.lyrics_handler import platform_keyboard

    try:
        return await msg.reply_photo(
            photo=meta.cover_url,
            caption=_cover_caption(meta, status),
            parse_mode="Markdown",
            reply_markup=platform_keyboard(
                ", ".join(meta.artists), meta.name, sp.platform_links(meta)
            ),
        )
    except Exception as e:
        log.info("cover send failed for %s: %s", meta.display, e)
        return None


async def _upload_track(msg, meta, path, *, with_cover: bool = True) -> bool:
    """Send the cover message and then the audio itself."""
    from config import settings
    from handlers.lyrics_handler import lyrics_button
    from utils import file_cache
    from utils.helpers import prepare_telegram_thumb, safe_filename

    cache_key = f"audio:{meta.id}"
    if with_cover:
        await _send_cover(msg, meta)

    thumb_path = None
    if meta.cover_url:
        thumb_path = await prepare_telegram_thumb(
            meta.cover_url,
            settings.download_dir / "thumbs" / f"{safe_filename(meta.display)}.jpg",
        )

    try:
        with path.open("rb") as fh:
            sent = await msg.reply_audio(
                audio=fh,
                title=meta.name,
                performer=", ".join(meta.artists),
                duration=meta.duration_ms // 1000,
                thumbnail=thumb_path.open("rb") if thumb_path else None,
                reply_markup=lyrics_button(
                    ", ".join(meta.artists), meta.name, track_id=meta.id
                ),
            )
        if sent and sent.audio:
            file_cache.put(cache_key, sent.audio.file_id)
        stats.record_download(msg.chat_id, "music", meta.display)
        return True
    except Exception as e:
        log.exception("audio upload failed")
        await msg.reply_text(f"❌ آپلود ناموفق: {meta.display} — {e}")
        return False


async def _send_cached(msg, meta, file_id: str, *, with_cover: bool = True) -> bool:
    from handlers.lyrics_handler import lyrics_button

    if with_cover:
        await _send_cover(msg, meta)
    await msg.reply_audio(
        audio=file_id,
        title=meta.name,
        performer=", ".join(meta.artists),
        duration=meta.duration_ms // 1000,
        reply_markup=lyrics_button(", ".join(meta.artists), meta.name, track_id=meta.id),
    )
    stats.record_download(msg.chat_id, "music-cached", meta.display)
    return True


async def _send_and_download_track(msg, meta, *, quiet: bool = False) -> bool:
    from utils import file_cache

    cache_key = f"audio:{meta.id}"

    # Fast path: Telegram already has these bytes. Re-sending the file_id is
    # one API call - no download, no ffmpeg, no upload.
    cached = file_cache.get(cache_key)
    if cached:
        try:
            await sp.fill_cover(meta)
            return await _send_cached(msg, meta, cached)
        except Exception as e:
            log.info("cached file_id rejected (%s) - re-downloading", e)
            file_cache.drop(cache_key)

    # The cover goes out first, carrying the progress line in its caption.
    # A separate status message plus its edits and deletion cost three extra
    # round trips per track, which is most of the wait on a cached song.
    await sp.fill_cover(meta)
    cover_msg = await _send_cover(msg, meta, status="⬇️ در حال دانلود…")

    status = None
    if cover_msg is None and not quiet:
        status = await msg.reply_text(
            f"⬇️ دانلود: *{meta.display}*", parse_mode="Markdown"
        )

    try:
        path = await sp.download_track(meta)
    except Exception as e:
        if status:
            await status.edit_text(f"❌ {e}")
        else:
            await msg.reply_text(f"❌ {meta.display} — {e}")
        return False

    ok = await _upload_track(msg, meta, path, with_cover=False)

    if cover_msg is not None:
        try:
            await cover_msg.edit_caption(
                caption=_cover_caption(meta), parse_mode="Markdown"
            )
        except Exception:
            pass
    if status:
        try:
            await status.delete()
        except Exception:
            pass
    return ok


# ---------- callbacks ----------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "sp:noop":
        return

    if data == "sp:stop":
        _cancelled.add(query.message.chat_id)
        try:
            await query.answer("متوقف شد — ترک‌های در حال دانلود تموم می‌شن.")
        except Exception:
            pass
        return

    if data.startswith("sp:sim:"):
        track_id = data.split(":", 2)[2]
        status = await query.message.reply_text("🎧 دنبال آهنگ‌های شبیه می‌گردم…")
        try:
            meta = await sp.get_track_meta(track_id)
            tracks = await sp.similar_tracks(meta, limit=8)
        except Exception as e:
            await status.edit_text(f"❌ {e}")
            return
        if not tracks:
            await status.edit_text("چیزی شبیه این پیدا نکردم.")
            return
        await status.delete()
        await _send_tracklist(
            query.message,
            title=f"🎧 شبیه «{meta.display}»",
            tracks=tracks,
            bulk_callback=None,
        )
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

    if data.startswith("sp:plask:"):
        _, _, kind, rid = data.split(":", 3)
        container = await _load_container(kind, rid)
        total = len(container.tracks)
        context.user_data["awaiting_range"] = (kind, rid)
        await query.message.reply_text(
            f"🔢 محدوده رو بنویس (بین ۱ تا {total}).\n\n"
            "مثال:\n"
            "`500-600`  یعنی ترک ۵۰۰ تا ۶۰۰\n"
            "`3000-`    یعنی از ۳۰۰۰ تا آخر\n"
            "`-50`      یعنی ۵۰ تای اول",
            parse_mode="Markdown",
        )
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
