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
from utils.limits import BoundedDict

log = logging.getLogger(__name__)

_PAGE = 8

def _md(text: str) -> str:
    """Escape Telegram's legacy Markdown.

    A username like `moein_imy` has an odd number of underscores, which makes
    Telegram reject the whole message - and the failure was swallowed, so the
    user-list button simply appeared to do nothing.
    """
    out = str(text or "")
    for ch in ("_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


_KIND_LABELS = {
    "music": "🎵 موزیک",
    "music-cached": "⚡ موزیک (از کش)",
    "yt-audio": "🎧 صدای یوتیوب",
    "yt-video": "🎬 ویدیو یوتیوب",
}


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
            [InlineKeyboardButton("📣 ارسال همگانی", callback_data="adm:bchelp")],
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
    # Always render these sections. Hiding them when empty looked like a
    # missing feature rather than "nothing recorded yet".
    lines += ["", "*بر اساس نوع:*"]
    lines += (
        [f"   • {_KIND_LABELS.get(k, k or '?')}: {c}" for k, c in d["by_kind"]]
        if d["by_kind"]
        else ["   — هنوز دانلودی ثبت نشده"]
    )

    lines += ["", "*پرتکرارترین‌ها:*"]
    lines += (
        [f"   {i}. {_md(title[:38])} ({c})" for i, (title, c) in enumerate(d["top_tracks"], 1)]
        if d["top_tracks"]
        else ["   — هنوز چیزی نیست"]
    )

    lines += ["", f"🕐 {time.strftime('%H:%M:%S')}"]
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
        lines.append(
            f"`{uid}` — {_md(handle)}\n   ⬇️ {dls} · ⚡ {actions} · 🕐 {_ago(last)}"
        )
        # Button labels are plain text, so they must NOT be escaped.
        buttons.append(
            [InlineKeyboardButton(f"{handle} ({dls})"[:60], callback_data=f"adm:u:{uid}")]
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
        f"👤 *{_md(fname or 'بدون نام')}*",
        f"🆔 `{uid}`",
        f"🔗 @{_md(uname)}" if uname else "🔗 یوزرنیم نداره",
        "",
        f"⬇️ کل دانلودها: *{total}*",
        f"⚡ تعاملات: {actions}",
        f"📅 اولین بار: {_ago(first)} پیش",
        f"🕐 آخرین بار: {_ago(last)} پیش",
    ]
    if recent:
        lines += ["", "*آخرین دانلودها:*"]
        lines += [f"   • [{k}] {_md(title[:40])}" for k, title, _ in recent]
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


async def igcheck_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report whether the Instagram session actually works, so a dead cookie
    is visible directly instead of being guessed from a failed download."""
    if not _is_admin(update):
        return
    from modules import instagram as ig

    msg = await update.effective_message.reply_text("🔎 چک کردن سشن اینستاگرام…")
    try:
        ok, detail = await ig.check_session()
    except Exception as e:
        ok, detail = False, str(e)
    await msg.edit_text(("✅ " if ok else "❌ ") + detail)


async def igtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/igtest [shortcode] - which cookie-free routes work from THIS server."""
    if not _is_admin(update):
        return
    from modules import instagram as ig

    sc = (context.args[0] if context.args else "Bt4k7fjnRRl").strip()
    for prefix in ("https://www.instagram.com/p/", "https://www.instagram.com/reel/"):
        if sc.startswith(prefix):
            sc = sc[len(prefix):].strip("/").split("?")[0]
    msg = await update.effective_message.reply_text("🔎 تست روش‌های بدون کوکی…")
    try:
        await msg.edit_text(await ig.diagnose(sc))
    except Exception as e:
        await msg.edit_text(f"❌ {e}")


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
    if data.startswith("adm:bc:"):
        await _run_broadcast(query, context, data.split(":", 2)[2])
        return
    if data == "adm:bccancel":
        await query.edit_message_text("لغو شد.")
        return

    if data == "adm:bchelp":
        await query.message.reply_text(
            "📣 *ارسال همگانی*\n\n"
            "• `/broadcast متن پیام`\n"
            "• یا روی یه پیام ریپلای کن و `/broadcast` بزن",
            parse_mode="Markdown",
        )
        return

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
        if "not modified" in str(e).lower():
            return  # refresh with no new data; nothing to do
        # Anything else is a real failure - retry as plain text so the panel
        # still works, and log loudly rather than looking like a dead button.
        log.warning("admin panel Markdown failed (%s) - resending as plain text", e)
        try:
            await query.edit_message_text(text, reply_markup=kb)
        except Exception as e2:
            log.error("admin panel edit failed: %s", e2)


# ---------------- broadcast ----------------

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/broadcast <text>, or reply to a message with /broadcast to send that."""
    if not _is_admin(update):
        return

    msg = update.effective_message
    text = " ".join(context.args) if context.args else ""
    source = msg.reply_to_message

    if not text and not source:
        await msg.reply_text(
            "📣 *ارسال همگانی*\n\n"
            "دو راه:\n"
            "• `/broadcast متن پیام`\n"
            "• یا روی یه پیام (حتی عکس/فایل) ریپلای کن و `/broadcast` بزن\n\n"
            "قبل از ارسال، پیش‌نمایش و تایید می‌گیرم.",
            parse_mode="Markdown",
        )
        return

    key = str(update.effective_user.id)
    _pending_broadcast[key] = (
        source.message_id if source else None,
        source.chat_id if source else None,
        text,
    )
    total = stats.user_count()
    preview = text or "(همون پیامی که ریپلای کردی)"
    await msg.reply_text(
        f"📣 برای *{total}* کاربر ارسال بشه؟\n\n{preview[:500]}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ بفرست", callback_data=f"adm:bc:{key}"),
                InlineKeyboardButton("❌ لغو", callback_data="adm:bccancel"),
            ]]
        ),
    )


_pending_broadcast = BoundedDict(20)


async def _run_broadcast(query, context, key: str) -> None:
    import asyncio

    pending = _pending_broadcast.pop(key, None)
    if not pending:
        await query.edit_message_text("⌛ منقضی شد. دوباره /broadcast بزن.")
        return

    src_msg_id, src_chat_id, text = pending
    user_ids = [row[0] for row in stats.list_users(limit=100000, offset=0)]
    status = await query.message.reply_text(f"📣 ارسال به {len(user_ids)} کاربر…")

    sent = blocked = failed = 0
    for i, uid in enumerate(user_ids, 1):
        try:
            if src_msg_id and src_chat_id:
                await context.bot.copy_message(
                    chat_id=uid, from_chat_id=src_chat_id, message_id=src_msg_id
                )
            else:
                await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception as e:
            detail = str(e).lower()
            if "blocked" in detail or "deactivated" in detail or "not found" in detail:
                blocked += 1
            else:
                failed += 1
                log.info("broadcast to %s failed: %s", uid, e)

        # Telegram allows roughly 30 messages/second; stay well under it or
        # the whole run gets throttled.
        await asyncio.sleep(0.05)
        if i % 25 == 0:
            try:
                await status.edit_text(
                    f"📣 {i}/{len(user_ids)} — ✅ {sent} · 🚫 {blocked} · ⚠️ {failed}"
                )
            except Exception:
                pass

    await status.edit_text(
        f"📣 تموم شد\n✅ رسید: {sent}\n🚫 بلاک کرده/حذف شده: {blocked}\n⚠️ خطا: {failed}"
    )


async def track_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs for every update (group -1) purely to keep the user table fresh."""
    user = update.effective_user
    if not user or user.is_bot:
        return
    # Browsing the panel is not bot usage - counting it made the refresh
    # button bump the interaction total on every press.
    query = update.callback_query
    if query and (query.data or "").startswith("adm:"):
        return
    stats.touch_user(user.id, user.username, user.first_name)
