"""
Sponsor-channel lock: require membership of a channel before the bot works.

Configured with REQUIRED_CHANNEL (e.g. @mychannel). The bot must be an
administrator of that channel, otherwise Telegram refuses to report
membership and the gate would lock everyone out - so a failed check is
treated as "let them through" and logged, rather than silently blocking all
users.

Admins in ADMIN_IDS always pass.
"""

from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from config import settings
from utils import i18n
from utils.limits import BoundedDict

log = logging.getLogger(__name__)

# Membership is stable enough that re-asking Telegram on every single update
# is wasteful; remember a pass for a few minutes.
_ok_until = BoundedDict(5000)
_TTL = 300.0

_JOINED = ("member", "administrator", "creator", "owner")

# The request each locked-out user was making when the gate stopped them, so
# joining resumes it instead of asking them to send it again.
_pending: BoundedDict = BoundedDict(2000)
_PENDING_TTL = 3600.0

# Why the last membership check could not be answered, "" when it could.
last_error: str = ""


async def diagnose(bot) -> list[str]:
    """Can this lock actually lock anything right now?

    Worth asking directly, because both ways it breaks are invisible from the
    outside. If the bot is not an admin of the channel the check throws and
    everyone is let through - a lock that never engages looks identical to one
    that everybody has passed. And an admin in ADMIN_IDS is exempt by design,
    so testing with your own account proves nothing either way.
    """
    if not settings.required_channel:
        return ["⚪️ قفل کانال: غیرفعال (REQUIRED_CHANNEL خالیه)"]

    lines = [f"🔒 قفل کانال: {settings.required_channel}"]

    try:
        chat = await bot.get_chat(settings.required_channel)
    except Exception as e:
        return lines + [
            f"   ❌ کانال پیدا نشد: {str(e)[:70]}",
            "   ↳ قفل عملا غیرفعاله — همه رد می‌شن.",
        ]

    try:
        me = await bot.get_chat_member(chat.id, (await bot.get_me()).id)
        if me.status in ("administrator", "creator"):
            lines.append("   ✅ بات ادمین کاناله — چک عضویت کار می‌کنه")
        else:
            lines += [
                f"   ❌ بات ادمین نیست (وضعیت: {me.status})",
                "   ↳ چک عضویت خطا می‌ده و همه رد می‌شن. بات رو ادمین کن.",
            ]
    except Exception as e:
        lines += [
            f"   ❌ وضعیت بات تو کانال خونده نشد: {str(e)[:70]}",
            "   ↳ قفل عملا غیرفعاله — همه رد می‌شن.",
        ]

    if last_error:
        lines.append(f"   ⚠️ آخرین خطای چک: {last_error[:70]}")
    if settings.admin_ids:
        lines.append(
            f"   ℹ️ {len(settings.admin_ids)} ادمین از قفل معافن — با اکانت خودت تست نکن"
        )
    return lines


def _channel_url() -> str:
    ch = settings.required_channel.lstrip("@")
    return f"https://t.me/{ch}"


def _prompt(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    fa = i18n.get_lang(chat_id) != i18n.EN
    text = (
        f"🔒 برای استفاده از بات، اول توی کانال زیر عضو شو:\n\n"
        f"{settings.required_channel}\n\n"
        "بعد از عضویت دکمه «عضو شدم» رو بزن."
        if fa
        else
        f"🔒 To use this bot, join our channel first:\n\n"
        f"{settings.required_channel}\n\n"
        "Then tap “I joined”."
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(
                "📢 عضویت در کانال" if fa else "📢 Join the channel", url=_channel_url()
            )],
            [InlineKeyboardButton(
                "✅ عضو شدم" if fa else "✅ I joined", callback_data="gate:check"
            )],
        ]
    )
    return text, kb


async def is_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    return await is_member_bot(context.bot, user_id)


async def is_member_bot(bot, user_id: int) -> bool:
    """The check itself, addressed by bot rather than by handler context.

    The Instagram Direct bridge has no Update and therefore no context - it is
    triggered by a DM - but a paired user who left the channel must still hit
    the same gate as everyone else.
    """
    if not settings.required_channel:
        return True
    if user_id in settings.admin_ids:
        return True
    if _ok_until.get(user_id, 0) > time.monotonic():
        return True

    try:
        member = await bot.get_chat_member(settings.required_channel, user_id)
        joined = member.status in _JOINED
    except Exception as e:
        # Usually "chat not found" or the bot is not an admin there. Blocking
        # everyone because of a misconfiguration is worse than not gating.
        #
        # But failing open silently is how a lock that was never working looks
        # exactly like a lock that is working: everybody gets through either
        # way. Remember why, so /srcstatus can say so out loud.
        global last_error
        last_error = str(e)
        log.warning(
            "membership check failed for %s in %s: %s - letting the user through",
            user_id, settings.required_channel, e,
        )
        return True

    last_error = ""

    if joined:
        _ok_until[user_id] = time.monotonic() + _TTL
    return joined


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler (group -1). Stops the update when the user
    has not joined."""
    if not settings.required_channel:
        return
    user = update.effective_user
    if not user or user.is_bot:
        return

    # Let the "I joined" button and the language picker through - they are the
    # only two things a locked-out user needs to be able to press. Everything
    # else, /start included, waits behind the gate, which is what makes the
    # lock apply to people who started the bot long before it was turned on.
    data = (update.callback_query.data or "") if update.callback_query else ""
    if data.startswith(("gate:", "lang:")):
        return

    if await is_member(context, user.id):
        return

    # Hold on to what they were actually trying to do. Being told to join a
    # channel mid-request and then having to find and resend the link is the
    # kind of friction that loses the user at exactly the wrong moment.
    # Messages only. A callback query expires within about a minute, so
    # replaying one after a trip to the channel would just fail on answer().
    if update.effective_message and not update.callback_query:
        _pending[user.id] = (time.monotonic(), update)

    text, kb = _prompt(user.id)
    try:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(text, reply_markup=kb)
        elif update.effective_message:
            await update.effective_message.reply_text(text, reply_markup=kb)
    except Exception as e:
        log.info("could not send join prompt: %s", e)

    # Nothing else should process this update.
    raise ApplicationHandlerStop


async def _resume(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Re-run whatever the user was blocked on. True if there was something.

    The update is fed back through the application, so it takes exactly the
    path it would have taken had the gate not been there - no duplicate
    routing logic that could drift from the real one. It cannot loop: by the
    time this runs, membership has been confirmed and cached, so guard returns
    early on the second pass.
    """
    held = _pending.pop(user_id, None)
    if not held:
        return False

    stamp, update = held
    if time.monotonic() - stamp > _PENDING_TTL:
        return False

    try:
        await context.application.process_update(update)
        return True
    except Exception as e:
        log.warning("could not resume the held request for %s: %s", user_id, e)
        return False


async def on_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The '✅ I joined' button."""
    query = update.callback_query
    user_id = query.from_user.id
    _ok_until.pop(user_id, None)  # force a fresh check

    if await is_member(context, user_id):
        await query.answer()
        fa = i18n.get_lang(user_id) != i18n.EN
        resumed = await _resume(context, user_id)
        try:
            if resumed:
                await query.edit_message_text(
                    "✅ عضویتت تایید شد. درخواست قبلیت رو ادامه می‌دم…"
                    if fa else "✅ Membership confirmed. Picking up where you left off…"
                )
            else:
                await query.edit_message_text(
                    "✅ عضویتت تایید شد. حالا می‌تونی استفاده کنی."
                    if fa else "✅ Membership confirmed. You're good to go."
                )
        except Exception:
            pass
        return

    fa = i18n.get_lang(user_id) != i18n.EN
    await query.answer(
        "هنوز عضو نشدی." if fa else "You haven't joined yet.", show_alert=True
    )
