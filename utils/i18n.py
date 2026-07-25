"""
Two-language UI (Persian / English).

The Persian text is the key. Anything missing from the English map falls back
to Persian rather than to an empty string or a raw key, so a translation gap
degrades to "one message is still Persian" instead of breaking the screen.

Call sites pass a chat id; the preference is cached in memory and persisted
in the stats database.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

FA, EN = "fa", "en"

_cache: dict[int, str] = {}
_lock = threading.Lock()


def get_lang(chat_id: int | None) -> str:
    if not chat_id:
        return FA
    with _lock:
        cached = _cache.get(chat_id)
    if cached:
        return cached
    try:
        from modules import stats

        lang = stats.get_lang(chat_id) or FA
    except Exception:
        lang = FA
    with _lock:
        _cache[chat_id] = lang
    return lang


def set_lang(chat_id: int, lang: str) -> None:
    lang = EN if lang == EN else FA
    with _lock:
        _cache[chat_id] = lang
    try:
        from modules import stats

        stats.set_lang(chat_id, lang)
    except Exception as e:
        log.warning("could not persist language for %s: %s", chat_id, e)


def has_lang(chat_id: int) -> bool:
    """True once the user has actually chosen, so /start can ask only once."""
    try:
        from modules import stats

        return bool(stats.get_lang(chat_id))
    except Exception:
        return False


def t(chat_id: int | None, text: str) -> str:
    """Translate one UI string for this chat."""
    if get_lang(chat_id) != EN:
        return text
    return _EN.get(text, text)


# --------------------------------------------------------------------------
# Persian -> English. Keys are the exact literals used in the handlers;
# {placeholders} are filled with .format() by the caller after translation.
# --------------------------------------------------------------------------

_EN: dict[str, str] = {
    # --- start / help ---
    "🌐 زبان رو انتخاب کن / Choose your language":
        "🌐 زبان رو انتخاب کن / Choose your language",
    "✅ زبان روی فارسی تنظیم شد.": "✅ Language set to English.",

    # --- search ---
    "نتیجه‌ای پیدا نکردم.": "Nothing found.",
    "نتایج «{q}»": "Results for “{q}”",
    "🔎 نتایج بیشتر (یوتیوب و ساندکلاد)": "🔎 More results (YouTube & SoundCloud)",
    "🔎 دنبال ریمیکس‌ها و نسخه‌های دیگه می‌گردم… (چند ثانیه طول می‌کشه)":
        "🔎 Looking for remixes and other versions… (takes a few seconds)",
    "چیز بیشتری پیدا نکردم.": "Nothing more found.",
    "🔎 نتایج بیشتر «{q}»": "🔎 More results for “{q}”",
    "🎤 Top tracks": "🎤 Top tracks",

    # --- track download ---
    "⬇️ دانلود: *{name}*": "⬇️ Downloading: *{name}*",
    "⬇️ در حال دانلود…": "⬇️ Downloading…",
    "📤 آپلود به تلگرام…": "📤 Uploading to Telegram…",
    "❌ آپلود ناموفق: {name} — {err}": "❌ Upload failed: {name} — {err}",
    "📜 متن آهنگ": "📜 Lyrics",
    "🎧 شبیه این": "🎧 Similar",
    "🎧 دنبال آهنگ‌های شبیه می‌گردم…": "🎧 Looking for similar tracks…",
    "چیزی شبیه این پیدا نکردم.": "Nothing similar found.",
    "🎧 شبیه «{name}»": "🎧 Similar to “{name}”",
    "🔎 دنبال متن آهنگ می‌گردم…": "🔎 Looking for lyrics…",
    "😕 متنی برای «{artist} — {title}» پیدا نکردم.":
        "😕 No lyrics found for “{artist} — {title}”.",
    "⌛ سشن منقضی شده. آهنگ رو دوباره بگیر.":
        "⌛ Session expired. Request the track again.",

    # --- playlists / albums ---
    "این لیست ترکی نداره.": "This list has no tracks.",
    "🎵 {n} ترک": "🎵 {n} tracks",
    "\n⚠️ اسپاتیفای بدون اکانت فقط ۱۰۰ ترک اول رو می‌ده؛ بقیه در دسترس نیست.":
        "\n⚠️ Without credentials Spotify only returns the first 100 tracks.",
    "⬇️ {n} تای اول": "⬇️ First {n}",
    "⬇️ {n} تای آخر": "⬇️ Last {n}",
    "⬇️ همه ({n})": "⬇️ All ({n})",
    "🔢 محدوده دلخواه": "🔢 Custom range",
    "⏪ ۱۰ صفحه": "⏪ 10 pages",
    "۱۰ صفحه ⏩": "10 pages ⏩",
    "⏹ توقف": "⏹ Stop",
    "متوقف شد — ترک‌های در حال دانلود تموم می‌شن.":
        "Stopping — downloads already in flight will finish.",
    "چیزی تو این محدوده نیست.": "Nothing in that range.",
    "⬇️ شروع دانلود {n} ترک…": "⬇️ Starting {n} tracks…",
    "⚠️ رد شد: {name} — {err}": "⚠️ Skipped: {name} — {err}",
    "✅ {n} ترک فرستاده شد": "✅ Sent {n} tracks",
    "⏹ متوقف شد — ": "⏹ Stopped — ",
    " · {n} تا ناموفق": " · {n} failed",
    "⌛ سشن منقضی شده. دوباره سرچ کن.": "⌛ Session expired. Search again.",
    "🔢 محدوده رو بنویس (بین ۱ تا {total}).\n\nمثال:\n`500-600`  یعنی ترک ۵۰۰ تا ۶۰۰\n`3000-`    یعنی از ۳۰۰۰ تا آخر\n`-50`      یعنی ۵۰ تای اول":
        "🔢 Send a range (1 to {total}).\n\nExamples:\n`500-600`  tracks 500 to 600\n`3000-`    from 3000 to the end\n`-50`      the first 50",
    "فرمت درست نیست. مثل `500-600` بنویس یا برای لغو /start بزن.":
        "Bad format. Write it like `500-600`, or /start to cancel.",
    "⬇️ ترک {start} تا {end}": "⬇️ Tracks {start} to {end}",

    # --- youtube ---
    "🔎 در حال گرفتن اطلاعات ویدیو…": "🔎 Fetching video info…",
    "❌ نتونستم اطلاعات ویدیو رو بگیرم: {err}": "❌ Could not fetch video info: {err}",
    "🎵 Audio (MP3)": "🎵 Audio (MP3)",
    "🎧 پیدا کردن آهنگ ویدیو (Shazam)": "🎧 Identify the music (Shazam)",
    "⌛ سشن منقضی شده. دوباره لینک رو بفرست.":
        "⌛ Session expired. Send the link again.",
    "⬇️ شروع دانلود…": "⬇️ Starting download…",
    "❌ دانلود ناموفق: {err}": "❌ Download failed: {err}",
    "📤 در حال آپلود…": "📤 Uploading…",
    "❌ آپلود ناموفق: {err}": "❌ Upload failed: {err}",
    "⚠️ فایل {size}MB شد که از حد مجاز تلگرام ({limit}MB) بزرگ‌تره. یه کیفیت پایین‌تر انتخاب کن.":
        "⚠️ The file is {size}MB, over Telegram's {limit}MB limit. Pick a lower quality.",

    # --- recognition ---
    "🎧 در حال گوش دادن به ویدیو و پیدا کردن آهنگ…":
        "🎧 Listening to the video to identify the music…",
    "❌ نتونستم صدای ویدیو رو بگیرم: {err}": "❌ Could not get the video's audio: {err}",
    "🎧 اول ویدیو جواب نداد، وسطش رو گوش می‌دم…":
        "🎧 No match at the start — trying the middle…",
    "🎧 در حال پیدا کردن آهنگ…": "🎧 Identifying the music…",
    "❌ خطا در تشخیص آهنگ: {err}": "❌ Recognition error: {err}",
    "😕 آهنگی تشخیص ندادم.\n\nمعمولا یعنی صدای موزیک زیر حرف/افکت گم شده یا تیکه خیلی کوتاهه. یه بخش بلندتر که موزیکش واضح‌تره بفرست، یا اسم آهنگ رو تایپ کن.":
        "😕 Couldn't identify any music.\n\nUsually that means the music sits under speech or effects, or the clip is too short. Send a longer part where the music is clearer, or just type the song name.",
    "🎧 مطمئن نیستم — این احتمالات رو پیدا کردم:":
        "🎧 Not certain — here's what I found:",
    "🎧 این رو پیدا کردم ولی مطمئن نیستم:":
        "🎧 Found this, but I'm not certain:",
    "اگه درسته بزن روش. اگه نه، اسم آهنگ رو تایپ کن یا یه تیکه‌ی بلندتر/واضح‌تر از ویدیو بفرست.":
        "Tap it if that's right. Otherwise type the song name, or send a longer/clearer part of the video.",
    "✅ پیدا شد: *{artist} — {title}*\n⬇️ در حال دانلود…":
        "✅ Found: *{artist} — {title}*\n⬇️ Downloading…",
    "🎵 آهنگ: {artist} — {title}\n😕 ولی برای دانلود پیداش نکردم.":
        "🎵 Track: {artist} — {title}\n😕 But I couldn't find it to download.",
    "⌛ سشن منقضی شده. دوباره ویدیو رو بفرست.":
        "⌛ Session expired. Send the video again.",
    "📥 در حال دریافت فایل…": "📥 Fetching the file…",
    "❌ دریافت فایل ناموفق: {err}": "❌ Could not fetch the file: {err}",

    # --- routing / generic ---
    "🤔 لینک رو نشناختم. یوتوب / اینستا / اسپاتیفای ساپورت میشه.":
        "🤔 Unrecognised link. YouTube, Instagram, Spotify and SoundCloud are supported.",
    "❌ خطا: {err}": "❌ Error: {err}",
    "💥 یه خطای غیرمنتظره پیش اومد. دوباره امتحان کن.":
        "💥 Something went wrong. Please try again.",

    # --- instagram / soundcloud ---
    "☁️ پلی‌لیست ساندکلاد": "☁️ SoundCloud playlist",
    "🎧 پیدا کردن آهنگ این ویدیو": "🎧 Identify this video's music",
}
