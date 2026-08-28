"""/start, /help and language selection."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from utils import i18n

WELCOME_FA = (
    "👋 سلام! هرچی بفرستی برات می‌گیرم.\n\n"
    "🎧 *آهنگ رو نمی‌شناسی؟*\n"
    "ویدیو، ویس یا لینک بفرست — گوش می‌دم، پیداش می‌کنم و "
    "با کاور و متن ترانه برات می‌فرستم.\n\n"
    "🔗 *لینک‌هایی که می‌شناسم:*\n"
    "• یوتیوب — انتخاب کیفیت یا فقط MP3\n"
    "• اینستاگرام — ریلز، پست، استوری، هایلایت، عکس پروفایل\n"
    "• اسپاتیفای — ترک، آلبوم، پلی‌لیست، آرتیست\n"
    "• ساندکلاد — ترک و پلی‌لیست\n\n"
    "🔎 *دنبال یه آهنگی؟*\n"
    "فقط اسمش رو تایپ کن (مثلا `Drake Jaded`).\n"
    "آلبوم و پلی‌لیست رو هم می‌تونی تیکه‌تیکه بگیری — ۱۰ تای اول، "
    "محدوده‌ی دلخواه، یا همه.\n\n"
    "⚡️ *تو هر چتی:* بنویس `@MoeinimyDL_bot` و بعدش اسم آهنگ — "
    "بدون باز کردن بات.\n\n"
    "🎵 هر آهنگی که می‌فرستم دکمه‌ی «متن آهنگ» و «شبیه این» داره.\n"
    "📸 اینستاگرام دایرکت: /igdirect — تو خود اینستا بفرست، اینجا تحویل بگیر.\n"
    "🌐 تغییر زبان: /lang"
)

WELCOME_EN = (
    "👋 Hi! Send me anything and I'll fetch it.\n\n"
    "🎧 *Don't know the song?*\n"
    "Send a video, a voice note or a link — I'll listen, identify the track "
    "and send it back with cover art and lyrics.\n\n"
    "🔗 *Links I understand:*\n"
    "• YouTube — pick a quality, or audio only\n"
    "• Instagram — reels, posts, stories, highlights, profile pictures\n"
    "• Spotify — track, album, playlist, artist\n"
    "• SoundCloud — track and playlist\n\n"
    "🔎 *Looking for a song?*\n"
    "Just type its name (e.g. `Drake Jaded`).\n"
    "Albums and playlists come in pieces too — the first 10, a range you "
    "choose, or all of it.\n\n"
    "⚡️ *In any chat:* type `@MoeinimyDL_bot` then the song — no need to "
    "open the bot.\n\n"
    "🎵 Every track comes with “Lyrics” and “Similar” buttons.\n"
    "📸 Instagram Direct: /igdirect — share inside Instagram, collect it here.\n"
    "🌐 Change language: /lang"
)


def _picker() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang:fa"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang:en"),
        ]]
    )


def welcome_for(chat_id: int) -> str:
    return WELCOME_EN if i18n.get_lang(chat_id) == i18n.EN else WELCOME_FA


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    # `?start=trk_<id>` - the link under an inline card for a track Telegram
    # has never seen. Without this the button opens the bot and shows the
    # welcome text, which reads as the link being broken: somebody pressed
    # "get the song" and got a menu.
    arg = (context.args or [None])[0] if context else None
    if arg and arg.startswith("trk_"):
        from handlers import spotify_handler

        await spotify_handler.deliver_track(update.effective_message, arg[4:])
        return
    # Ask once, on the very first /start; afterwards go straight to the help
    # text so returning users are not nagged.
    if not i18n.has_lang(chat_id):
        await update.message.reply_text(
            "🌐 زبان رو انتخاب کن / Choose your language",
            reply_markup=_picker(),
        )
        return
    await update.message.reply_markdown(welcome_for(chat_id))


async def help_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(welcome_for(update.effective_chat.id))


async def lang_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🌐 زبان رو انتخاب کن / Choose your language", reply_markup=_picker()
    )


async def on_lang_callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    lang = query.data.split(":", 1)[1]
    chat_id = query.message.chat_id
    i18n.set_lang(chat_id, lang)

    confirm = "✅ Language set to English." if lang == i18n.EN else "✅ زبان روی فارسی تنظیم شد."
    try:
        await query.edit_message_text(confirm)
    except Exception:
        pass
    await query.message.reply_markdown(welcome_for(chat_id))
