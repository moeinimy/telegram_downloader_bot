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
    "🎚 نسخه‌های دیگه": "🎚 Other versions",
    "💎 بالاترین کیفیت": "💎 Best quality",
    "💎 دنبال بهترین نسخه می‌گردم…": "💎 Looking for the best version…",
    "💎 *{name}*\n\nبدون فشرده‌سازی ({codec}) · {size:.1f}MB":
        "💎 *{name}*\n\nLossless ({codec}) · {size:.1f}MB",
    "💎 *{name}*\n\nبهترین نسخه‌ی موجود: {codec} · {size:.1f}MB\n\n"
    "منبع خودش فشرده‌ست، پس نسخه‌ی بدون افت وجود نداره — "
    "تبدیلش به FLAC فقط حجم رو زیاد می‌کرد بدون اینکه کیفیت اضافه شه.":
        "💎 *{name}*\n\nBest available: {codec} · {size:.1f}MB\n\n"
        "The source is itself compressed, so there is no lossless version to "
        "get — converting it to FLAC would only make the file bigger without "
        "adding any quality.",
    "🎚 دنبال نسخه‌های دیگه می‌گردم…": "🎚 Looking for other versions…",
    "نسخه دیگه‌ای از این آهنگ پیدا نکردم.": "No other version of this track found.",
    "🎚 نسخه‌های «{name}»": "🎚 Versions of “{name}”",
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
    "⏹ متوقف شد — ترک‌های در حال دانلود تموم می‌شن.":
        "⏹ Stopping — downloads already in flight will finish.",
    "⏹ در حال توقف…": "⏹ Stopping…",
    "⏳ یکم آروم‌تر! حدود {n} ثانیه دیگه دوباره بفرست.": "⏳ Easy there! Try again in about {n} seconds.",
    "\n\n⏳ ترافیک زیاده، برای اینکه اکانت اینستاگرام بلاک نشه فعلا کندتر چک می\u200cکنم — تحویل چند ده ثانیه دیرتره.": "\n\n⏳ Traffic is high. To keep the Instagram account from being blocked I am checking more slowly for now, so delivery is a few tens of seconds later.",
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
    "🖼 دانلود کاور (کیفیت اصلی)": "🖼 Download cover (full size)",
    "🔎 در حال گرفتن اطلاعات آهنگ…": "🔎 Fetching track info…",
    "🎬 موزیک ویدیو": "🎬 Music video",
    "🎬 دنبال موزیک ویدیو می‌گردم…": "🎬 Looking for the music video…",
    "😕 موزیک ویدیویی برای این آهنگ پیدا نکردم. خیلی از ترک‌ها ویدیو ندارن؛ "
    "چیزی که پیدا شد یا لیریک ویدیو بود یا آهنگ دیگه‌ای.":
        "😕 No music video found for this track. Many songs don't have one; "
        "what turned up was either a lyric video or a different song.",
    "📺 معروف‌ترین ویدیوهای این کانال": "📺 Most popular from this channel",
    "📺 دنبال معروف‌ترین ویدیوهای کانال می‌گردم…": "📺 Finding the channel's top videos…",
    "😕 لیست ویدیوهای این کانال رو نتونستم بگیرم.": "😕 Couldn't list this channel's videos.",
    "😕 ویدیوی دیگه‌ای از این کانال پیدا نشد.": "😕 No other videos found on this channel.",
    "📺 معروف‌ترین ویدیوهای *{name}*": "📺 Most popular from *{name}*",
    "🖼 دنبال بهترین نسخه‌ی کاور می‌گردم…": "🖼 Looking for the largest cover…",
    "😕 کاور رو نتونستم بگیرم.": "😕 Couldn't fetch the cover.",
    "😕 کاور رو نتونستم بفرستم.": "😕 Couldn't send the cover.",
    "📤 در حال آپلود… ({size}MB)": "📤 Uploading… ({size}MB)",
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

    # --- buttons and limits added later ---
    "⬇️ دانلود همه": "⬇️ Download all",
    "⏳ یه دانلود گروهی دیگه از تو در حال اجراست. صبر کن تموم شه یا ⏹ توقف رو بزن.":
        "⏳ You already have a batch download running. Wait for it, or press ⏹ Stop.",
    "🎵 موزیک": "🎵 Music",
    "⚡ موزیک (از کش)": "⚡ Music (cached)",
    "🎧 صدای یوتیوب": "🎧 YouTube audio",
    "🎬 ویدیو یوتیوب": "🎬 YouTube video",

    # --- instagram / soundcloud ---
    "⬇️ در حال دانلود از اینستاگرام…": "⬇️ Downloading from Instagram…",
    "⬇️ گرفتن از اینستا…": "⬇️ Fetching from Instagram…",
    "🤔 نوع لینک اینستا رو نشناختم.": "🤔 Unrecognised Instagram link type.",
    "🎧 پیدا کردن آهنگ این ویدیو": "🎧 Identify this video's music",
    "آهنگ این ویدیو رو برات پیدا کنم؟": "Want me to identify the music in this video?",
    "📸 عکس پروفایل": "📸 Profile picture",
    "📖 استوری‌ها": "📖 Stories",
    "⌛ فایل ویدیو دیگه موجود نیست. دوباره لینک رو بفرست.":
        "⌛ That video is no longer available. Send the link again.",
    "اکشن نامعتبر.": "Invalid action.",
    "🔎 در حال خوندن پلی‌لیست ساندکلاد…": "🔎 Reading the SoundCloud playlist…",
    "ترکی تو این پلی‌لیست پیدا نکردم.": "No tracks found in that playlist.",
    "🔎 در حال گرفتن اطلاعات ترک…": "🔎 Fetching track info…",
    "☁️ پلی‌لیست ساندکلاد": "☁️ SoundCloud playlist",

    # --- Instagram access errors ---
    # These come out of modules/instagram.py already phrased for the user, and
    # are translated at the handler boundary.
    "😕 این پست رو نتونستم بگیرم.\n\n"
    "خود اینستاگرام بعضی پست‌ها رو محدود می‌کنه (محدودیت سنی، "
    "علامت «حساس»، یا اکانت خصوصی) و اونا رو فقط به کاربر لاگین‌کرده نشون می‌ده.\n\n"
    "یه پست دیگه امتحان کن — معمولا مشکلی پیش نمیاد.":
        "😕 I couldn't fetch this post.\n\n"
        "Instagram restricts some posts (age limits, a “sensitive” flag, or a "
        "private account) and only shows those to logged-in users.\n\n"
        "Try another post — most work fine.",
    "اینستاگرام این پست رو نداد. معمولا یعنی کوکی‌های اکانت منقضی شدن "
    "یا پست خصوصی/حذف شده‌ست.\n\n"
    "کوکی‌های تازه بگیر و با «botctl → گزینه ۱۰» ست کن.":
        "Instagram refused this post. Usually the account cookies have expired, "
        "or the post is private or deleted.\n\n"
        "Get fresh cookies and set them with botctl -> option 10.",
    "سشن اینستاگرام دیگه معتبر نیست. کوکی‌ها رو از مرورگر دوباره بگیر "
    "و تو سرور با «botctl → گزینه ۱۰» آپدیتشون کن.":
        "The Instagram session is no longer valid. Take the cookies from your "
        "browser again and update them with botctl -> option 10.",
    "این پست خصوصیه یا حذف شده.": "This post is private or has been deleted.",
    "اینستاگرام اکانت رو موقتا محدود کرده. تو مرورگر وارد اکانت شو، "
    "اگه پیام تایید اومد تاییدش کن و چند ساعت بعد دوباره امتحان کن.":
        "Instagram has temporarily restricted the account. Log into it in a "
        "browser, confirm any security prompt, and try again in a few hours.",
    "اینستاگرام ریت‌لیمیت کرده. حدود یک ساعت صبر کن و دوباره امتحان کن.":
        "Instagram is rate-limiting us. Wait about an hour and try again.",
    "این کاربر الان استوری فعالی نداره.":
        "This user has no active stories right now.",

    # --- instagram direct (DM -> Telegram bridge) ---
    "📸 اینستاگرام دایرکت هنوز روی این سرور فعال نشده.":
        "📸 Instagram Direct is not enabled on this server yet.",
    "📸 *اینستاگرام دایرکت*\n\n"
    "به‌جای کپی‌کردن لینک، مستقیم تو خود اینستاگرام برامون بفرست و "
    "همین‌جا تحویل بگیر.\n\n"
    "🔒 فقط شناسه‌ی عددی اکانت اینستاگرامت ذخیره می‌شه — نه اسم، نه پروفایل، "
    "نه پیام‌هات. قطع اتصال هم یه دکمه‌ست.":
        "📸 *Instagram Direct*\n\n"
        "Instead of copying links, share straight to us inside Instagram and "
        "pick it up here.\n\n"
        "🔒 Only your Instagram account's numeric id is stored — no name, no "
        "profile, none of your messages. Unlinking is one tap.",
    "📸 *اینستاگرام دایرکت*\n\n✅ وصله (از {date}).\n\nهرچی تو دایرکت اینستاگرام برای ما بفرستی — ریلز، پست، استوری — همین‌جا دانلودشده برات میاد.":
        "📸 *Instagram Direct*\n\n✅ Connected (since {date}).\n\nAnything you share to our Instagram DMs — reels, posts, stories — comes back here already downloaded.",
    "🔗 وصل کردن اکانت": "🔗 Connect my account",
    "🔌 قطع اتصال": "🔌 Disconnect",
    "اکانت اینستاگرام بات": "the bot's Instagram account",
    "🔗 *وصل کردن اکانت*\n\n"
    "۱. برو به اینستاگرام\n"
    "۲. به {target} دایرکت بده\n"
    "۳. دقیقا همین کد رو بفرست:\n\n"
    "`{token}`\n\n"
    "⏳ این کد {minutes} دقیقه اعتبار داره و یک‌بار مصرفه.":
        "🔗 *Connect your account*\n\n"
        "1. Open Instagram\n"
        "2. Send a DM to {target}\n"
        "3. Send exactly this code:\n\n"
        "`{token}`\n\n"
        "⏳ It is valid for {minutes} minutes and can only be used once.",
    "🔌 اتصال قطع شد. شناسه‌ی اینستاگرامت پاک شد.":
        "🔌 Disconnected. Your Instagram id has been deleted.",
    "اتصالی برقرار نبود.": "Nothing was connected.",
    "✅ اکانت اینستاگرامت وصل شد.\n\nحالا هر ریلز یا پستی رو تو دایرکت اینستاگرام برامون بفرست تا همین‌جا دانلودشده تحویل بگیری.":
        "✅ Your Instagram account is connected.\n\nShare any reel or post to our Instagram DMs and it will arrive here, already downloaded.",
    "📥 از دایرکت اینستاگرام گرفتمش — دارم دانلود می‌کنم…":
        "📥 Got it from your Instagram DM — downloading…",
    "برای استفاده از بات باید اول تو کانال ما عضو بشی. تو تلگرام /start بزن.":
        "You need to join our channel before using the bot. Send /start in Telegram.",

    # --- instagram post menu ---
    "چیکار دیگه‌ای برات بکنم؟": "Anything else?",
    "👁 مشاهده در اینستاگرام": "👁 View on Instagram",
    "🔄 بروزرسانی اطلاعات": "🔄 Refresh post info",
    "🎬 همه کیفیت‌ها": "🎬 All qualities",
    "🎧 فقط صدا": "🎧 Audio only",
    "🔗 لینک مستقیم": "🔗 Direct link",
    "📝 کپشن": "📝 Caption",
    "📊 *اطلاعات پست*\n\n{stats}": "📊 *Post info*\n\n{stats}",
    "اطلاعاتی در دسترس نیست.": "No information available.",
    "این پست کپشن نداره.": "This post has no caption.",
    "لینک مستقیمی پیدا نکردم.": "I couldn't find a direct link.",
    "🔗 *لینک مستقیم*\n\n{links}\n\n⏳ این لینک‌ها امضا شدن و تا چند ساعت بیشتر کار نمی‌کنن.":
        "🔗 *Direct link*\n\n{links}\n\n⏳ These are signed URLs and stop working after a few hours.",
    "این پست ویدیو نیست یا فقط یه کیفیت داره.":
        "This post isn't a video, or has only one quality.",
    "🎬 کیفیت رو انتخاب کن:": "🎬 Pick a quality:",
    "⬇️ در حال دانلود {label}…": "⬇️ Downloading {label}…",
    "🎧 در حال جدا کردن صدا…": "🎧 Extracting the audio…",
    "این پست ویدیو نیست.": "This post isn't a video.",
    "💬 زیرنویس ویدیو (آزمایشی)": "💬 Video subtitles (beta)",
    "💬 دارم به ویدیو گوش می‌دم…": "💬 Listening to the video…",
    "💬 دارم به ویدیو گوش می‌دم… (روی این سرور چند دقیقه طول می‌کشه)":
        "💬 Listening to the video… (this takes a few minutes on this server)",
    "💬 *زیرنویس* (زبان: {lang})\n\n": "💬 *Subtitles* (language: {lang})\n\n",
    "💬 حرفی توش نشنیدم — احتمالا فقط موزیکه.":
        "💬 I couldn't hear any speech — it's probably music only.",
    "💬 زیرنویس هنوز روی این سرور نصب نشده.\n\nادمین: `botctl whisper`":
        "💬 Subtitles are not installed on this server yet.\n\nAdmin: `botctl whisper`",

    # --- instagram direct: pairing screens ---
    "📸 *اینستاگرام دایرکت*\n\n"
    "به‌جای کپی‌کردن لینک، مستقیم تو خود اینستاگرام برامون بفرست و "
    "همین‌جا تحویل بگیر.\n\n"
    "⚠️ *اول باید پیج ما رو فالو کنی.* اگه فالو نکنی، دایرکتت می‌ره تو "
    "بخش «درخواست پیام» اینستاگرام و ممکنه اصلا به دستمون نرسه.\n\n"
    "🔒 فقط شناسه‌ی عددی اکانت اینستاگرامت ذخیره می‌شه — نه اسم، نه پروفایل، "
    "نه پیام‌هات. قطع اتصال هم یه دکمه‌ست.":
        "📸 *Instagram Direct*\n\n"
        "Instead of copying links, share straight to us inside Instagram and "
        "pick it up here.\n\n"
        "⚠️ *You must follow our page first.* Without that, your DM lands in "
        "Instagram's message requests and may never reach us.\n\n"
        "🔒 Only your Instagram account's numeric id is stored — no name, no "
        "profile, none of your messages. Unlinking is one tap.",
    "🔗 *وصل کردن اکانت*\n\n"
    "۱. پیج {target} رو باز کن و *فالو کن*\n"
    "(بدون فالو، پیامت می‌ره تو «درخواست پیام» و ممکنه به دستمون نرسه)\n\n"
    "۲. *دایرکت* همون پیج رو باز کن و این کد رو *به عنوان پیام* بفرست:\n\n"
    "`{token}`\n\n"
    "همین. چند ثانیه بعد همین‌جا تایید می‌گیری.\n\n"
    "⏳ این کد {minutes} دقیقه اعتبار داره و یک‌بار مصرفه.":
        "🔗 *Connect your account*\n\n"
        "1. Open {target} and *follow* it\n"
        "(without that your message lands in Instagram's message requests and "
        "may never reach us)\n\n"
        "2. Open that page's *DMs* and send this code *as a message*:\n\n"
        "`{token}`\n\n"
        "That's it. You'll get a confirmation here within seconds.\n\n"
        "⏳ It is valid for {minutes} minutes and can only be used once.",
    "📸 باز کردن پیج در اینستاگرام": "📸 Open the page on Instagram",

    # --- recognition service ---
    "⏳ سرویس تشخیص آهنگ الان جواب نمی‌ده. چند دقیقه دیگه دوباره امتحان کن.":
        "⏳ The music-recognition service is not responding right now. "
        "Please try again in a few minutes.",
    "⚠️ این فایل {size} مگابایته و تلگرام اجازه نمی‌ده بات فایل‌های "
    "بزرگ‌تر از ۲۰ مگ رو بگیره.\n\n"
    "یه تیکه کوتاه‌تر از ویدیو بفرست.":
        "⚠️ This file is {size}MB and Telegram does not let bots fetch files "
        "over 20MB.\n\nSend a shorter part of the video.",
}
