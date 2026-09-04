"""TikTok and Pinterest: one link, one file back.

These two share a handler because they ask the same question. There is no
quality menu (TikTok serves one rendition), no playlist, no search - a link
names exactly one thing, and the only job is to fetch it and send it.

Both go through yt-dlp's own extractors, which means they inherit the parts
already solved elsewhere: the proxy, the socket timeouts, the concurrent
fragment downloads.

TIKTOK NEEDS IMPERSONATION. Its extractor refuses to work without a TLS
fingerprint that matches the browser it claims to be, which in practice means
curl_cffi - and a version yt-dlp accepts. With a too-new one installed,
yt-dlp lists every impersonation target as "(unavailable)" and TikTok fails
with "Unexpected response from webpage request", which says nothing about the
real cause. That is worth naming in the error rather than making somebody
find it.

PINTEREST IS OFTEN NOT A VIDEO. Plenty of pins are a single image, and
yt-dlp handles those as a "video" with one image in it. Whatever comes back
is sent as what it actually is, read off the file, not off what was asked
for.
"""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from config import settings
from modules.youtube import ytdlp_run
from utils import file_cache, limits
from utils.helpers import run_in_thread, safe_filename
from utils.i18n import Localised, admin_note, localise, t
from utils.secrets import scrub
from utils.url_router import Platform, RouteResult

log = logging.getLogger(__name__)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}

# Names the platforms answer with when impersonation is missing. yt-dlp does
# not say "install curl_cffi" - it says the webpage was unexpected.
_IMPERSONATION_MARKERS = (
    "unexpected response from webpage request",
    "attempting impersonation",
    "unable to extract webpage",
)


def _looks_like_impersonation(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _IMPERSONATION_MARKERS)


# Pinterest answers this for any pin that is a picture, which is most of
# them. It is not a failure - there is simply no video, and the picture is
# sitting right there in the metadata.
_NO_VIDEO_MARKERS = ("no video formats found", "no video could be found",
                     "unable to extract video")


def _is_image_only(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in _NO_VIDEO_MARKERS)


def _canonical_pin(url: str) -> str:
    """Resolve a pin.it short link, and drop the invite code it arrives with.

    Measured: extracting from the short link gives ZERO thumbnails, and from
    the resolved one gives nine - the redirect has to be followed before
    there is anything to read. What it redirects to also carries an
    ?invite_code=..., which is a share token belonging to whoever sent it and
    has no business in a request this bot makes.
    """
    import re

    from utils import http

    if "pin.it/" not in url:
        return url
    try:
        response = http.get(url)
        resolved = str(response.url)
    except Exception as e:
        log.info("could not resolve %s (%s)", url, e)
        return url
    match = re.search(r"/pin/(\d+)", resolved)
    return f"https://www.pinterest.com/pin/{match.group(1)}/" if match else resolved


def _largest_image(url: str) -> str:
    """The full-size picture behind a pin.

    `process=False` is the whole trick. yt-dlp raises "No video formats
    found!" during its format-selection step, before returning anything -
    so the metadata that already contains the image is never handed over.
    Skipping that step gives the thumbnail list, and Pinterest's own
    /originals/ rendition is in it: measured on the pin that failed, the
    largest entry was 1024x1536 and 149KB, which IS the picture, not a
    preview of it.
    """
    from yt_dlp import YoutubeDL

    with YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
        info = ydl.extract_info(_canonical_pin(url), download=False,
                                process=False)
    thumbs = [t for t in (info or {}).get("thumbnails") or [] if t.get("url")]
    if not thumbs:
        raise Localised("چیزی برای دانلود پیدا نشد.")
    best = max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
    return str(best["url"])


@run_in_thread(heavy=True)
def _fetch(url: str, target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(target / "%(id)s.%(ext)s"),
        "noplaylist": True,
        # A pin or a clip is one item. Without this, a link that happens to
        # point at a board would start downloading somebody's entire board.
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
    }
    try:
        ytdlp_run(opts, lambda ydl: ydl.download([url]), kind="simple")
    except Exception as e:
        if not _is_image_only(e):
            raise
        log.info("no video at %s - taking the picture instead", url)
        _save_image(_largest_image(url), target)

    files = sorted(p for p in target.iterdir()
                   if p.is_file() and p.suffix.lower() in _IMAGE_EXTS | _VIDEO_EXTS)
    if not files:
        raise Localised("چیزی برای دانلود پیدا نشد.")
    return files


def _save_image(url: str, target: Path) -> Path:
    from utils import http

    response = http.get(url, headers={"Referer": "https://www.pinterest.com/"})
    response.raise_for_status()
    ctype = (response.headers.get("content-type") or "").lower()
    if not ctype.startswith("image"):
        # 200 is not the same as "this is a picture": a refused or expired
        # link comes back as an html page with a 200, and saving that hands
        # the user a .jpg that no viewer will open.
        raise Localised("چیزی برای دانلود پیدا نشد.")
    suffix = ".png" if "png" in ctype else ".webp" if "webp" in ctype else ".jpg"
    dest = target / f"pin{suffix}"
    dest.write_bytes(response.content)
    return dest


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     result: RouteResult) -> None:
    msg = update.effective_message
    chat_id = msg.chat_id
    label = ("تیک‌تاک" if result.platform == Platform.TIKTOK else "پینترست")

    cache_key = f"simple:{result.url}"
    cached = file_cache.get(cache_key)
    if cached:
        try:
            sent = await msg.reply_video(video=cached, supports_streaming=True)
            _remember(sent)
            return
        except Exception as e:
            # Only an id Telegram has actually disowned is worth dropping.
            if "wrong file identifier" in str(e).lower():
                file_cache.drop(cache_key)
            else:
                log.info("cached send failed for %s: %s", result.url, e)

    status = await msg.reply_text(
        t(chat_id, "⬇️ در حال گرفتن از {site}…").format(site=label))

    user = update.effective_user
    target = (settings.download_dir / "simple"
              / safe_filename(result.url)[-60:])
    try:
        async with limits.download_slot(user.id if user else 0):
            files = await _fetch(result.url, target)
    except Exception as e:
        if _looks_like_impersonation(e) and result.platform == Platform.TIKTOK:
            await status.edit_text(
                t(chat_id, "❌ تیک‌تاک این ویدیو رو به سرور نداد.")
                + admin_note(chat_id,
                             "معمولا یعنی curl_cffi نصب نیست یا نسخه‌ش با "
                             "yt-dlp نمی‌خونه.\nروی سرور:  botctl fixcurl"))
            return
        await status.edit_text("❌ " + scrub(localise(chat_id, e)))
        return

    try:
        await _send(msg, status, files, cache_key)
    finally:
        for f in files:
            f.unlink(missing_ok=True)


def _remember(sent) -> None:
    """So a time range typed next cuts what was just sent."""
    try:
        from handlers import cut_handler

        cut_handler.remember(sent)
    except Exception:
        pass


async def _send(msg, status, files: list[Path], cache_key: str) -> None:
    from handlers.cut_handler import cut_button
    from utils import archive

    sent = None
    for path in files:
        with path.open("rb") as fh:
            if path.suffix.lower() in _VIDEO_EXTS:
                sent = await msg.reply_video(
                    video=fh, supports_streaming=True,
                    reply_markup=cut_button(msg.chat_id))
            else:
                sent = await msg.reply_photo(photo=fh)

    await status.delete()
    if sent is None:
        return
    _remember(sent)

    # Only a single video is worth caching by url: a multi-file pin would
    # need a list, and the id kept would silently be the last one.
    if len(files) == 1 and getattr(sent, "video", None):
        file_cache.put(cache_key, sent.video.file_id)
        try:
            await archive.mirror(msg.get_bot(), "video", sent.video.file_id,
                                 caption=cache_key)
        except Exception:
            pass
