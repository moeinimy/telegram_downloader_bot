"""/start, /help and language selection."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from utils import i18n

WELCOME_FA = (
    "👋 سلام! هرچی بفرستی برات می‌گیرم.\n\n"
    "🎧 *آهنگ رو نمی‌شناسی؟*\n"
    "ویدیو، ویس یا لینک بفرست — گوش می‌دم، آهنگش رو پیدا می‌کنم و "
    "با کاور و متن ترانه برات می‌فرستم.\n\n"
    "🔗 *لینک‌هایی که می‌شناسم:*\n"
    "• یوتیوب — انتخاب کیفیت یا فقط MP3\n"
    "• اینستاگرام — ریلز و پست\n"
    "• اسپاتیفای — ترک، آلبوم، پلی‌لیست، آرتیست\n"
    "• ساندکلاد — ترک و پلی‌لیست\n\n"
    "🔎 *دنبال یه آهنگی؟*\n"
    "فقط اسمش رو تایپ کن (مثلا `Drake Jaded`) تا نتایج رو با دکمه بدم.\n\n"
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
    "• Instagram — reels and posts\n"
    "• Spotify — track, album, playlist, artist\n"
    "• SoundCloud — track and playlist\n\n"
    "🔎 *Looking for a song?*\n"
    "Just type its name (e.g. `Drake Jaded`) and pick from the results.\n\n"
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
