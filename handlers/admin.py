"""
Admin panel: /admin (or /stats) shows usage; a paged user list drills into
individual users.

Restricted to the ids in ADMIN_IDS. With nothing configured the panel is
disabled entirely rather than open to everyone.
"""

from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import settings
from modules import stats

log = logging.getLogger(__name__)

_PAGE = 8


def _is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in settings.admin_ids)


def _ago(ts: int) -> str:
    if not ts:
        return "-"
    delta = max(int(time.time()) - int(ts), 0)
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 لیست کاربران", callback_data="adm:users:0")],
            [InlineKeyboardButton("🔄 بروزرسانی", callback_data="adm:home")],
        ]
    )


def _summary_text() -> str:
    d = stats.summary()
    lines = [
        "📊 *پنل مدیریت*",
        "",
        f"👥 کاربران: *{d['users']}*",
        f"   • فعال امروز: {d['users_day']}",
        f"   • فعال این هفته: {d['users_week']}",
        f"   • جدید این هفته: {d['new_week']}",
        "",
        f"⬇️ دانلودها: *{d['downloads']}*",
        f"   • امروز: {d['downloads_day']}",
        f"   • این هفته: {d['downloads_week']}",
        "",
        f"⚡ کل تعاملات: {d['actions']}",
    ]
    if d["by_kind"]:
        lines.append("")
        lines.append("*بر اساس نوع:*")
        lines += [f"   • {k or '?'}: {c}" for k, c in d["by_kind"]]
    if d["top_tracks"]:
        lines.append("")
        lines.append("*پرتکرارترین‌ها:*")
        lines += [f"   {i}. {t[:38]} ({c})" for i, (t, c) in enumerate(d["top_tracks"], 1)]
    return "\n".join(lines)


def _users_view(offset: int) -> tuple[str, InlineKeyboardMarkup]:
    total = stats.user_count()
    rows = stats.list_users(_PAGE, offset)
    if not rows:
        return "هنوز کاربری ثبت نشده.", _panel_keyboard()

    lines = [f"👥 *کاربران* ({offset + 1}-{offset + len(rows)} از {total})", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for uid, uname, fname, last, actions, dls in rows:
        handle = f"@{uname}" if uname else (fname or "بدون نام")
        lines.append(f"`{uid}` — {handle}\n   ⬇️ {dls} · ⚡ {actions} · 🕐 {_ago(last)}")
        buttons.append(
            [InlineKeyboardButton(f"{handle} ({dls})", callback_data=f"adm:u:{uid}")]
        )

    nav = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton("◀️", callback_data=f"adm:users:{max(0, offset - _PAGE)}")
        )
    nav.append(InlineKeyboardButton("🏠", callback_data="adm:home"))
    if offset + _PAGE < total:
        nav.append(
            InlineKeyboardButton("▶️", callback_data=f"adm:users:{offset + _PAGE}")
        )
    buttons.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def _user_detail_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    found = stats.user_detail(user_id)
    back = InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ برگشت", callback_data="adm:users:0")]]
    )
    if not found:
        return "کاربر پیدا نشد.", back

    (uid, uname, fname, first, last, actions), recent, total = found
    lines = [
        f"👤 *{fname or 'بدون نام'}*",
        f"🆔 `{uid}`",
        f"🔗 @{uname}" if uname else "🔗 یوزرنیم نداره",
        "",
        f"⬇️ کل دانلودها: *{total}*",
        f"⚡ تعاملات: {actions}",
        f"📅 اولین بار: {_ago(first)} پیش",
        f"🕐 آخرین بار: {_ago(last)} پیش",
    ]
    if recent:
        lines += ["", "*آخرین دانلودها:*"]
        lines += [f"   • [{k}] {t[:40]}" for k, t, _ in recent]
    return "\n".join(lines), back


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not settings.admin_ids:
        await update.effective_message.reply_text(
            "پنل مدیریت غیرفعاله. تو سرور آیدی عددیت رو تو ADMIN_IDS بذار:\n"
            "`botctl` → گزینه ۷ (ویرایش .env)",
            parse_mode="Markdown",
        )
        return
    if not _is_admin(update):
        return  # stay silent for non-admins
    await update.effective_message.reply_text(
        _summary_text(), parse_mode="Markdown", reply_markup=_panel_keyboard()
    )


async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lets the owner discover the id to put in ADMIN_IDS."""
    user = update.effective_user
    await update.effective_message.reply_text(
        f"🆔 آیدی عددی تو: `{user.id}`", parse_mode="Markdown"
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_admin(update):
        return

    data = query.data
    if data == "adm:home":
        text, kb = _summary_text(), _panel_keyboard()
    elif data.startswith("adm:users:"):
        text, kb = _users_view(int(data.split(":")[2]))
    elif data.startswith("adm:u:"):
        text, kb = _user_detail_view(int(data.split(":")[2]))
    else:
        return

    try:
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        # "message is not modified" on a refresh with no new data
        log.debug("admin panel edit skipped: %s", e)


async def track_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs for every update (group -1) purely to keep the user table fresh."""
    user = update.effective_user
    if user and not user.is_bot:
        stats.touch_user(user.id, user.username, user.first_name)
