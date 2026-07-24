"""/start and /help commands."""

from telegram import Update
from telegram.ext import ContextTypes

WELCOME = (
    "👋 سلام! من یه ربات دانلودر چندپلتفرمی هستم.\n\n"
    "🔹 *YouTube*: لینک ویدیو بفرست تا کیفیت یا فرمت صوتی رو انتخاب کنی.\n"
    "🔹 *Instagram*: لینک پست / ریلز / استوری / پروفایل رو بفرست.\n"
    "🔹 *Spotify*: لینک ترک / آلبوم / پلی‌لیست / آرتیست بفرست،\n"
    "   یا فقط *اسم آهنگ یا آرتیست* رو تایپ کن تا برات سرچ کنم.\n\n"
    "📥 فقط لینک یا اسم رو بفرست؛ بقیه‌ش با من."
)


async def start_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(WELCOME)


async def help_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_markdown(WELCOME)
