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
from utils.i18n import t
from utils.limits import BoundedDict
from utils.url_router import RouteResult, SpotifyKind

log = logging.getLogger(__name__)


# ---------- URL entrypoint ----------

async def handle_url(
    update: Update, context: ContextTypes.DEFAULT_TYPE, route: RouteResult
) -> None:
    msg = update.effective_message
    kind = SpotifyKind(route.kind)

    if kind == SpotifyKind.TRACK:
        import time as _time

        # Acknowledge before doing anything. Fetching the track needs
        # Spotify's embed page, and until that returns there is nothing to put
        # in a photo caption - so the chat stayed empty for as long as that
        # took, which on a bad fetch is most of a minute. The YouTube handler
        # has always opened with a line like this; this one did not.
        ack = await msg.reply_text(t(msg.chat_id, "🔎 در حال گرفتن اطلاعات آهنگ…"))

        started = _time.monotonic()
        try:
            meta = await sp.get_track_meta(route.resource_id)
        except Exception as e:
            # The message is the exception's own, which arrives already
            # worded for the user; wrapping it in a translatable template
            # would just be that same string with a mark in front.
            await ack.edit_text(f"❌ {e}")
            return
        log.info("get_track_meta for %s took %.1fs - nothing can be shown "
                 "before this", route.resource_id, _time.monotonic() - started)

        try:
            await ack.delete()
        except Exception:
            pass
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
            t(update.effective_message.chat_id,
              "فرمت درست نیست. مثل `500-600` بنویس یا برای لغو /start بزن."),
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

    await update.effective_message.reply_text(
        t(update.effective_message.chat_id, "⬇️ ترک {start} تا {end}").format(start=start, end=end))
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
            await msg.reply_text(t(msg.chat_id, "نتیجه‌ای پیدا نکردم."))
            return
        await _send_tracklist(
            msg, title=t(msg.chat_id, "نتایج «{q}»").format(q=query), tracks=tracks, bulk_callback=None
        )
        return

    await _send_tracklist(
        msg,
        title=t(msg.chat_id, "نتایج «{q}»").format(q=query),
        tracks=tracks,
        bulk_callback=None,
        deep_query=query,
    )


# ---------- helpers ----------

# Callback data is capped at 64 bytes, so the deep-search query is kept here
# and referenced by a short hash.
_query_cache = BoundedDict(200)


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


def _button_label(track) -> str:
    """'Artist — Song · 3:24'. Duration disambiguates the remixes and live
    cuts that otherwise look like identical entries.

    Note the parameter is not called `t`: that is the translation function."""
    from utils.helpers import fmt_duration

    label = _truncate(f"{_source_icon(track.id)} {track.display}")
    if track.duration_ms:
        label += f" · {fmt_duration(track.duration_ms // 1000)}"
    return label


async def _send_tracklist(
    msg, *, title: str, tracks, bulk_callback: str | None, deep_query: str | None = None
) -> None:
    rows = [
        [InlineKeyboardButton(_button_label(tr), callback_data=f"sp:trk:{tr.id}")]
        for tr in tracks
    ]
    if bulk_callback:
        rows.append([InlineKeyboardButton(t(msg.chat_id, "⬇️ دانلود همه"), callback_data=bulk_callback)])
    if deep_query:
        key = _remember_query(deep_query)
        rows.append(
            [InlineKeyboardButton(t(msg.chat_id, "🔎 نتایج بیشتر (یوتیوب و ساندکلاد)"),
                                  callback_data=f"sp:more:{key}")]
        )
    await msg.reply_text(title, reply_markup=InlineKeyboardMarkup(rows))


# ---------- album / playlist browsing ----------

_PAGE = 10
# Playlists can hold thousands of tracks each; keep only a few.
_container_cache = BoundedDict(20)


async def _load_container(kind: str, rid: str):
    """Fetch an album/playlist once and keep it; paging must not re-scrape."""
    key = f"{kind}:{rid}"
    container = _container_cache.get(key)
    if container is None:
        container = await (
            sp.get_album_tracks(rid) if kind == "al" else sp.get_playlist_tracks(rid)
        )
        _container_cache[key] = container
    return container


def _container_title(kind: str, c) -> str:
    if kind == "al":
        return f"💿 {c.name} — {', '.join(c.artists)}"
    return f"📜 {c.name} (by {c.owner})"


def _range_buttons(chat_id: int, kind: str, rid: str, total: int) -> list[list[InlineKeyboardButton]]:
    """Bulk-download shortcuts. Only offer sizes the container actually has."""
    rows: list[list[InlineKeyboardButton]] = []
    presets = [n for n in (10, 30, 50, 100) if n < total]

    def chunk(buttons):
        return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]

    rows += chunk([
        InlineKeyboardButton(
            t(chat_id, "⬇️ {n} تای اول").format(n=n),
            callback_data=f"sp:pldl:{kind}:{rid}:0:{n}",
        )
        for n in presets
    ])
    rows += chunk([
        InlineKeyboardButton(
            t(chat_id, "⬇️ {n} تای آخر").format(n=n),
            callback_data=f"sp:pldl:{kind}:{rid}:{max(0, total - n)}:{n}",
        )
        for n in presets
    ])
    rows.append([
        InlineKeyboardButton(
            t(chat_id, "🔢 محدوده دلخواه"), callback_data=f"sp:plask:{kind}:{rid}"
        ),
        InlineKeyboardButton(
            t(chat_id, "⬇️ همه ({n})").format(n=total),
            callback_data=f"sp:pldl:{kind}:{rid}:0:{total}",
        ),
    ])
    return rows


def _page_keyboard(chat_id: int, kind: str, rid: str, container, offset: int) -> InlineKeyboardMarkup:
    total = len(container.tracks)
    page = container.tracks[offset : offset + _PAGE]

    rows = [
        [InlineKeyboardButton(
            f"{offset + i + 1}. {_button_label(tr)}", callback_data=f"sp:trk:{tr.id}"
        )]
        for i, tr in enumerate(page)
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
                t(chat_id, "⏪ ۱۰ صفحه"),
                callback_data=f"sp:plpg:{kind}:{rid}:{max(0, offset - _PAGE * 10)}",
            ),
            InlineKeyboardButton(
                t(chat_id, "۱۰ صفحه ⏩"),
                callback_data=f"sp:plpg:{kind}:{rid}:{min(last_start, offset + _PAGE * 10)}",
            ),
            InlineKeyboardButton("⏭", callback_data=f"sp:plpg:{kind}:{rid}:{last_start}"),
        ]
        rows.append(jump)

    rows += _range_buttons(chat_id, kind, rid, total)
    return InlineKeyboardMarkup(rows)


async def _send_container_menu(msg, kind: str, rid: str) -> None:
    container = await _load_container(kind, rid)
    total = len(container.tracks)
    if not total:
        await msg.reply_text(t(msg.chat_id, "این لیست ترکی نداره."))
        return
    header = f"{_container_title(kind, container)}\n🎵 {total} ترک"
    if getattr(container, "truncated", False):
        header += (
            "\n⚠️ اسپاتیفای بدون اکانت فقط ۱۰۰ ترک اول رو می‌ده؛ بقیه در دسترس نیست."
        )

    keyboard = _page_keyboard(msg.chat_id, kind, rid, container, 0)
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
# How many tracks are prepared ahead of the one being uploaded. The real
# concurrency cap is global (utils.limits); this only bounds how many tasks
# exist at once.
_PREFETCH = 6

# Chats that pressed "stop" mid-batch.
_cancelled: set[int] = set()


async def _download_range(msg, container, offset: int, count: int) -> None:
    import asyncio
    from pathlib import Path

    from config import settings
    from utils import file_cache, limits

    tracks = container.tracks[offset : offset + count]
    if not tracks:
        await msg.reply_text(t(msg.chat_id, "چیزی تو این محدوده نیست."))
        return

    chat_id = msg.chat_id
    if not limits.start_batch(chat_id):
        await msg.reply_text(
            t(chat_id, "⏳ یه دانلود گروهی دیگه از تو در حال اجراست. صبر کن تموم شه یا ⏹ توقف رو بزن.")
        )
        return
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

    stop_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(chat_id, "⏹ توقف"), callback_data="sp:stop")]]
    )
    status = await msg.reply_text(
        t(chat_id, "⬇️ شروع دانلود {n} ترک…").format(n=len(tracks)),
        reply_markup=stop_kb,
    )

    async def fetch(meta):
        """Download only - uploading happens in order in the loop below.
        The slot is global, so all users share one download budget."""
        if file_cache.get(f"audio:{meta.id}"):
            return None  # already on Telegram; nothing to fetch
        try:
            async with limits.download_slot(chat_id):
                return await sp.download_track(meta)
        except Exception as e:
            return e

    done = failed = 0
    stopped = False
    i = 0

    # Work through the playlist in windows instead of creating a task per
    # track: a 4000-track request used to spawn 4000 coroutines up front, all
    # of them holding metadata, before a single byte was downloaded.
    for start in range(0, len(tracks), _PREFETCH):
        if chat_id in _cancelled:
            stopped = True
            break

        window = tracks[start : start + _PREFETCH]
        jobs = [asyncio.create_task(fetch(m)) for m in window]

        for meta, job in zip(window, jobs):
            i += 1
            if chat_id in _cancelled:
                stopped = True
                for pending in jobs:
                    pending.cancel()
                break

            result = await job
            path = result if isinstance(result, Path) else None
            try:
                if isinstance(result, Exception):
                    failed += 1
                    await msg.reply_text(
                        t(chat_id, "⚠️ رد شد: {name} — {err}").format(
                            name=meta.display, err=result
                        )
                    )
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
            finally:
                # Telegram now holds the file and its id is cached, so the
                # local copy is dead weight - and 4000 of them is tens of GB.
                if path is not None:
                    path.unlink(missing_ok=True)

            # Editing on every track would burn the rate limit on long playlists.
            if i % 5 == 0 or i == len(tracks):
                try:
                    await status.edit_text(
                        f"⬇️ {i}/{len(tracks)} — ✅ {done}"
                        + (f" · ❌ {failed}" if failed else ""),
                        reply_markup=stop_kb,
                    )
                except Exception:
                    pass

        if stopped:
            break

    limits.sweep_downloads(settings.download_dir)
    _cancelled.discard(chat_id)
    limits.end_batch(chat_id)
    summary = (t(chat_id, "⏹ متوقف شد — ") if stopped else "")
    summary += t(chat_id, "✅ {n} ترک فرستاده شد").format(n=done)
    if failed:
        summary += t(chat_id, " · {n} تا ناموفق").format(n=failed)
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


def _cover_keyboard(meta, chat_id: int):
    """The buttons under the cover: artwork download, then the platform links.

    Built in one place because the caption is edited after the download
    finishes, and edit_caption without a reply_markup does not leave the old
    keyboard alone - it removes it. Rebuilding the same keyboard on every edit
    is what keeps the buttons there.
    """
    from handlers.lyrics_handler import cover_key, platform_keyboard

    return platform_keyboard(
        ", ".join(meta.artists), meta.name, sp.platform_links(meta),
        chat_id=chat_id,
        cover_key=cover_key(meta.cover_url, meta.display),
    )


async def _send_cover(msg, meta, status: str = ""):
    """Cover art as its own message, full resolution, with the platform links
    beneath it. Telegram compresses a photo far less than the 320x320 thumbnail
    the audio file is allowed to carry. Returns the sent message (or None) so
    the caller can update its caption instead of posting a separate status."""
    if not meta.cover_url:
        return None
    try:
        return await msg.reply_photo(
            photo=meta.cover_url,
            caption=_cover_caption(meta, status),
            parse_mode="Markdown",
            reply_markup=_cover_keyboard(meta, msg.chat_id),
        )
    except Exception as e:
        log.info("cover send failed for %s: %s", meta.display, e)
        return None


def _thumb_task(meta):
    """Build the 320x320 audio thumbnail. Returned as a task so the caller can
    let it run during the download instead of after it."""
    import asyncio

    from config import settings
    from utils.helpers import prepare_telegram_thumb, safe_filename

    if not meta.cover_url:
        return None
    return asyncio.create_task(
        prepare_telegram_thumb(
            meta.cover_url,
            settings.download_dir / "thumbs" / f"{safe_filename(meta.display)}.jpg",
        )
    )


async def _upload_track(msg, meta, path, *, with_cover: bool = True, thumb=None) -> bool:
    """Send the cover message and then the audio itself."""
    from handlers.lyrics_handler import lyrics_button
    from utils import file_cache

    cache_key = f"audio:{meta.id}"
    if with_cover:
        await _send_cover(msg, meta)

    if thumb is None:
        thumb = _thumb_task(meta)
    thumb_path = await thumb if thumb is not None else None

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
        await msg.reply_text(t(msg.chat_id, "❌ آپلود ناموفق: {name} — {err}").format(name=meta.display, err=e))
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
        reply_markup=lyrics_button(
                    ", ".join(meta.artists), meta.name,
                    track_id=meta.id, chat_id=msg.chat_id),
    )
    stats.record_download(msg.chat_id, "music-cached", meta.display)
    return True


async def _send_best_quality(query, track_id: str) -> None:
    """The best audio the source actually has, sent as a file.

    Sent as a document rather than audio on purpose: Telegram re-encodes and
    caps what it streams as music, and the whole point of this button is to
    hand over the bytes untouched.

    The reply says what it really is. "FLAC" is only claimed when the source
    was lossless - YouTube and SoundCloud are not, and transcoding lossy audio
    into FLAC produces a file several times larger carrying identical sound.
    """
    from utils import limits
    from utils.helpers import safe_filename

    chat_id = query.message.chat_id
    status = await query.message.reply_text(t(chat_id, "💎 دنبال بهترین نسخه می‌گردم…"))

    try:
        meta = await sp.get_track_meta(track_id)
        async with limits.download_slot(chat_id):
            path, codec, lossless = await sp.download_best(meta)
    except Exception as e:
        await status.edit_text(t(chat_id, "❌ خطا: {err}").format(err=e))
        return

    size_mb = path.stat().st_size / 1e6
    if lossless:
        note = t(chat_id, "💎 *{name}*\n\nبدون فشرده‌سازی ({codec}) · {size:.1f}MB")
    else:
        note = t(
            chat_id,
            "💎 *{name}*\n\nبهترین نسخه‌ی موجود: {codec} · {size:.1f}MB\n\n"
            "منبع خودش فشرده‌ست، پس نسخه‌ی بدون افت وجود نداره — "
            "تبدیلش به FLAC فقط حجم رو زیاد می‌کرد بدون اینکه کیفیت اضافه شه.",
        )

    try:
        with path.open("rb") as handle:
            await query.message.reply_document(
                document=handle,
                filename=f"{safe_filename(meta.display)}.{path.suffix.lstrip('.')}",
                caption=note.format(name=meta.display, codec=codec or "?", size=size_mb),
                parse_mode="Markdown",
            )
        await status.delete()
    except Exception as e:
        await status.edit_text(t(chat_id, "❌ آپلود ناموفق: {err}").format(err=e))


async def _send_and_download_track(msg, meta, *, quiet: bool = False) -> bool:
    from utils import file_cache

    cache_key = f"audio:{meta.id}"

    # Fast path: Telegram already has these bytes. Re-sending the file_id is
    # one API call - no download, no ffmpeg, no upload.
    cached = file_cache.get(cache_key)
    if cached:
        try:
            await sp.fill_details(meta)
            return await _send_cached(msg, meta, cached)
        except Exception as e:
            log.info("cached file_id rejected (%s) - re-downloading", e)
            file_cache.drop(cache_key)

    # The cover goes out first, carrying the progress line in its caption.
    # A separate status message plus its edits and deletion cost three extra
    # round trips per track, which is most of the wait on a cached song.
    import asyncio
    import time as _time

    # The cover used to wait for fill_details, which is several lookups against
    # iTunes and Deezer - so a pasted link sat with no reply at all for half a
    # minute, and the bot looked dead rather than busy.
    #
    # The embed page has already supplied the name, artists, album and a cover,
    # which is everything this message needs. It goes out now and is corrected
    # afterwards; the caption is re-edited at the end of this function anyway,
    # so being early costs nothing and being late cost thirty seconds.
    cover_msg = await _send_cover(msg, meta, status=t(msg.chat_id, "⬇️ در حال دانلود…"))

    started = _time.monotonic()
    await sp.fill_details(meta)
    log.info("fill_details for %s took %.1fs", meta.display, _time.monotonic() - started)

    # Start fetching immediately and post the cover while it runs. Awaiting the
    # cover first made a Telegram photo round trip block the download from even
    # beginning, for no reason - the two are independent.
    fetch = asyncio.create_task(sp.download_track(meta))
    thumb = _thumb_task(meta)  # also runs during the download

    status = None
    if cover_msg is None and not quiet:
        status = await msg.reply_text(
            t(msg.chat_id, "⬇️ دانلود: *{name}*").format(name=meta.display),
            parse_mode="Markdown",
        )

    try:
        path = await fetch
    except Exception as e:
        # The thumbnail is now pointless; leaving it pending orphans a task.
        if thumb is not None:
            thumb.cancel()
        err = t(msg.chat_id, str(e))
        if status:
            await status.edit_text(f"❌ {err}")
        else:
            await msg.reply_text(f"❌ {meta.display} — {err}")
        return False

    ok = await _upload_track(msg, meta, path, with_cover=False, thumb=thumb)

    if cover_msg is not None:
        try:
            # The keyboard has to be passed again. Editing a caption without
            # one does not keep the buttons that were there - it clears them,
            # so the artwork and platform buttons vanished the moment the
            # download finished.
            await cover_msg.edit_caption(
                caption=_cover_caption(meta), parse_mode="Markdown",
                reply_markup=_cover_keyboard(meta, msg.chat_id),
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
            await query.answer(t(query.message.chat_id, "متوقف شد — ترک‌های در حال دانلود تموم می‌شن."))
        except Exception:
            pass
        return

    if data.startswith("sp:sim:"):
        track_id = data.split(":", 2)[2]
        status = await query.message.reply_text(t(query.message.chat_id, "🎧 دنبال آهنگ‌های شبیه می‌گردم…"))
        try:
            meta = await sp.get_track_meta(track_id)
            tracks = await sp.similar_tracks(meta, limit=8)
        except Exception as e:
            await status.edit_text(f"❌ {e}")
            return
        if not tracks:
            await status.edit_text(t(query.message.chat_id, "چیزی شبیه این پیدا نکردم."))
            return
        await status.delete()
        await _send_tracklist(
            query.message,
            title=t(query.message.chat_id, "🎧 شبیه «{name}»").format(name=meta.display),
            tracks=tracks,
            bulk_callback=None,
        )
        return

    if data.startswith("sp:hq:"):
        await _send_best_quality(query, data.split(":", 2)[2])
        return

    if data.startswith("sp:ver:"):
        track_id = data.split(":", 2)[2]
        status = await query.message.reply_text(
            t(query.message.chat_id, "🎚 دنبال نسخه‌های دیگه می‌گردم…")
        )
        try:
            meta = await sp.get_track_meta(track_id)
            tracks = await sp.other_versions(meta, limit=8)
        except Exception as e:
            await status.edit_text(f"❌ {e}")
            return
        if not tracks:
            await status.edit_text(
                t(query.message.chat_id, "نسخه دیگه‌ای از این آهنگ پیدا نکردم.")
            )
            return
        await status.delete()
        await _send_tracklist(
            query.message,
            title=t(query.message.chat_id, "🎚 نسخه‌های «{name}»").format(name=meta.display),
            tracks=tracks,
            bulk_callback=None,
        )
        return

    if data.startswith("sp:plpg:"):
        _, _, kind, rid, offset = data.split(":", 4)
        container = await _load_container(kind, rid)
        try:
            await query.edit_message_reply_markup(
                reply_markup=_page_keyboard(
                    query.message.chat_id, kind, rid, container, int(offset)
                )
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
            await query.message.reply_text(t(query.message.chat_id, "⌛ سشن منقضی شده. دوباره سرچ کن."))
            return
        status = await query.message.reply_text(
            t(query.message.chat_id, "🔎 دنبال ریمیکس‌ها و نسخه‌های دیگه می‌گردم… (چند ثانیه طول می‌کشه)")
        )
        try:
            tracks = await sp.deep_search(search_query, limit=12)
        except Exception as e:
            await status.edit_text(f"❌ {e}")
            return
        if not tracks:
            await status.edit_text(t(query.message.chat_id, "چیز بیشتری پیدا نکردم."))
            return
        await status.delete()
        await _send_tracklist(
            query.message,
            title=t(query.message.chat_id, "🔎 نتایج بیشتر «{q}»").format(q=search_query),
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
