"""
Lyrics button: every music file the bot sends carries a "lyrics" inline
button. Pressing it looks the song up on lrclib.net and replies with the
text (chunked to fit Telegram's 4096-char message limit).

Callback data is capped at 64 bytes, so artist/title pairs are stored in an
in-process cache keyed by a short hash. Cache is lost on restart, which only
means old buttons answer with "session expired".
"""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import quote_plus

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from modules.lyrics import fetch_lyrics
from utils.i18n import t
from utils.limits import BoundedDict

log = logging.getLogger(__name__)

_cache = BoundedDict(2000)

# Cover urls behind a short key, for the same reason the lyrics cache exists:
# callback_data is capped at 64 bytes and an artwork url is routinely longer
# than that on its own.
_covers = BoundedDict(2000)


def cover_key(cover_url: str, name: str) -> str:
    """Register a cover for the download button. Empty when there is none."""
    if not cover_url:
        return ""
    key = hashlib.md5(cover_url.encode("utf-8")).hexdigest()[:12]
    _covers[key] = (cover_url, name)
    return key


def lyrics_button(
    artist: str,
    title: str,
    links: dict[str, str] | None = None,
    *,
    track_id: str | None = None,
    chat_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Keyboard attached to the audio file: lyrics, plus recommendations when
    we know which catalogue track this is.

    `links` is accepted and ignored so older call sites keep working; platform
    links now live under the cover image (see platform_keyboard)."""
    key = hashlib.md5(f"{artist}|{title}".encode("utf-8")).hexdigest()[:12]
    _cache[key] = (artist, title)

    row = [InlineKeyboardButton(t(chat_id, "📜 متن آهنگ"), callback_data=f"lyr:{key}")]
    if track_id:
        row.append(
            InlineKeyboardButton(t(chat_id, "🎧 شبیه این"), callback_data=f"sp:sim:{track_id}")
        )
    rows = [row]
    if track_id:
        # Remixes, live cuts and edits of this exact song. They are refused as a
        # download source on purpose, so this is the way to reach them.
        rows.append([
            InlineKeyboardButton(
                t(chat_id, "🎚 نسخه‌های دیگه"), callback_data=f"sp:ver:{track_id}"
            ),
            # The best the source actually has, in its native codec. Labelled
            # "بالاترین کیفیت" rather than FLAC because the sources are lossy
            # and a FLAC made from them would be a bigger file with identical
            # audio - it says FLAC in the reply only when it really is.
            InlineKeyboardButton(
                t(chat_id, "💎 بالاترین کیفیت"), callback_data=f"sp:hq:{track_id}"
            ),
        ])
    return InlineKeyboardMarkup(rows)


def platform_keyboard(
    artist: str, title: str, links: dict[str, str] | None = None,
    *, chat_id: int | None = None, cover_key: str = "",
) -> InlineKeyboardMarkup:
    """Links to the song on each major service, shown under the cover art.
    Real URLs are used where the source gave us one; the rest fall back to
    that platform's search page, which always resolves to something.

    `cover_key` adds a button that sends the artwork as a file. The photo
    above these buttons is whatever Telegram compressed it to; the file is
    the largest version the CDN will serve, which is usually several times
    the resolution.
    """
    q = quote_plus(f"{artist} {title}".strip())
    links = links or {}
    extra: list[list[InlineKeyboardButton]] = []
    if cover_key:
        extra.append([InlineKeyboardButton(
            t(chat_id, "🖼 دانلود کاور (کیفیت اصلی)") if chat_id is not None
            else "🖼 دانلود کاور (کیفیت اصلی)",
            callback_data=f"cov:{cover_key}",
        )])
    return InlineKeyboardMarkup(
        extra + [
            [
                InlineKeyboardButton(
                    "🟢 Spotify",
                    url=links.get("spotify") or f"https://open.spotify.com/search/{q}",
                ),
                InlineKeyboardButton(
                    "🔴 YouTube",
                    url=links.get("youtube")
                    or f"https://www.youtube.com/results?search_query={q}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🍎 Apple Music",
                    url=links.get("apple") or f"https://music.apple.com/search?term={q}",
                ),
                InlineKeyboardButton(
                    "🟠 SoundCloud",
                    url=links.get("soundcloud") or f"https://soundcloud.com/search?q={q}",
                ),
            ],
        ]
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]

    pair = _cache.get(key)
    if not pair:
        await query.message.reply_text(t(query.message.chat_id, "⌛ سشن منقضی شده. آهنگ رو دوباره بگیر."))
        return

    artist, title = pair
    status = await query.message.reply_text(t(query.message.chat_id, "🔎 دنبال متن آهنگ می‌گردم…"))

    try:
        text = await fetch_lyrics(artist, title)
    except Exception as e:
        log.warning("lyrics lookup failed: %s", e)
        text = None

    if not text:
        await status.edit_text(t(query.message.chat_id, "😕 متنی برای «{artist} — {title}» پیدا نکردم.").format(artist=artist, title=title))
        return

    await status.delete()
    full = f"🎵 {artist} — {title}\n\n{text}"
    for i in range(0, len(full), 4000):
        await query.message.reply_text(full[i : i + 4000])


async def on_cover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the artwork as a file, at the largest size the CDN will serve.

    The photo this button sits under is whatever Telegram compressed it to,
    and the thumbnail embedded in the audio file is capped at 320x320. Neither
    is the actual artwork, which is why this sends a document rather than
    another photo - a photo would be recompressed on the way out and the
    button would do nothing visible.
    """
    query = update.callback_query
    await query.answer()

    entry = _covers.get(query.data.split(":", 1)[1])
    if not entry:
        await query.message.reply_text(
            t(query.message.chat_id, "⌛ سشن منقضی شده. آهنگ رو دوباره بگیر."))
        return

    url, name = entry
    status = await query.message.reply_text(
        t(query.message.chat_id, "🖼 دنبال بهترین نسخه‌ی کاور می‌گردم…"))

    import asyncio
    import io

    from utils import artwork
    from utils.helpers import safe_filename

    def _fetch() -> tuple[str, bytes] | None:
        # Rewriting the size only helps when the url has one. A Spotify image
        # is capped at 640 and a YouTube thumbnail is a video frame, so for
        # those the larger cover lives at a different source, not a different
        # url - and the same release is almost always in Apple's catalogue at
        # 3000x3000. The original stays in the list either way.
        sources = [url]
        if not artwork.is_upgradable(url):
            better = artwork.upgrade_source(name)
            if better:
                sources.insert(0, better)
        return artwork.best(*sources)

    try:
        found = await asyncio.to_thread(_fetch)
    except Exception as e:
        log.info("cover fetch failed for %s: %s", name, e)
        found = None

    if not found:
        await status.edit_text(t(query.message.chat_id, "😕 کاور رو نتونستم بگیرم."))
        return

    found_url, blob = found
    ext = "png" if found_url.lower().rsplit(".", 1)[-1] == "png" else "jpg"
    size = _dimensions(blob)

    buffer = io.BytesIO(blob)
    buffer.name = f"{safe_filename(name) or 'cover'}.{ext}"

    caption = f"🖼 {name}"
    if size:
        caption += f"\n{size[0]}×{size[1]} · {len(blob) / 1024:.0f}KB"
    else:
        caption += f"\n{len(blob) / 1024:.0f}KB"

    try:
        await query.message.reply_document(document=buffer, caption=caption)
        await status.delete()
    except Exception as e:
        log.info("cover upload failed for %s: %s", name, e)
        await status.edit_text(t(query.message.chat_id, "😕 کاور رو نتونستم بفرستم."))


def _dimensions(blob: bytes) -> tuple[int, int] | None:
    """Pixel size, for the caption. Pillow is already a dependency for the
    thumbnails, but a cover that cannot be parsed is still worth sending."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(blob)) as im:
            return im.size
    except Exception:
        return None
