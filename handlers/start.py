"""/start and /help commands."""

from telegram import Update
from telegram.ext import ContextTypes

WELCOME = (
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
    "🎵 هر آهنگی که می‌فرستم دکمه‌ی «متن آهنگ» داره."
)


async def start_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(WELCOME)


async def help_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(WELCOME)
