"""
Instagram Direct, Telegram side.

Two halves:

  * the screen the user drives - /igdirect shows the pairing token, the
    current link, and a one-tap unlink.
  * on_direct_message, which every inbox source funnels into. It resolves the
    sender to a chat, then runs the SAME modules/instagram.py download and the
    SAME upload path as a pasted link, so captions, the cookie-free route
    ladder and the disk sweeper all behave identically. Nothing about the
    download is forked for this feature.

Downloads take a slot from utils.limits like everything else. Someone who
shares thirty reels into the DM in one go queues behind their own two-slot
per-user cap instead of taking the server with them.
"""

from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import settings
from modules import ig_direct, ig_pairing
from modules import instagram as ig
from utils import limits
from utils.i18n import t
from utils.limits import BoundedDict

log = logging.getLogger(__name__)

_VIDEO_EXTS = {".mp4", ".mov"}

# Set once at startup; the DM path has no handler context to get a bot from.
_app = None

_account_name_cache: str = ""

# When each stranger was last told how to pair. Somebody who sends five
# messages before reading the reply should not get five identical DMs back -
# that reads as a broken bot and, on the unofficial path, looks exactly like
# the spam behaviour that gets an account banned.
_help_sent: BoundedDict = BoundedDict(1000)
_HELP_COOLDOWN = 3600.0


def bind(application) -> None:
    global _app
    _app = application


def _bot():
    if _app is None:
        raise RuntimeError("ig_direct_handler.bind() was never called")
    return _app.bot


def _escape_md(text: str) -> str:
    """Escape Telegram's legacy Markdown, same as handlers/admin._md."""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


async def account_name() -> str:
    """The @handle users should DM. Asked of Meta once, then remembered."""
    global _account_name_cache

    if _account_name_cache:
        return _account_name_cache
    if settings.ig_dm_username:
        _account_name_cache = settings.ig_dm_username
        return _account_name_cache
    try:
        from modules import ig_graph

        who = await ig_graph.me()
        _account_name_cache = str(who.get("username") or "")
    except Exception as e:
        log.info("ig direct: could not read the account username: %s", e)
    return _account_name_cache


# --------------------------------------------------------------------------
# Telegram screens
# --------------------------------------------------------------------------

async def _screen(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    if ig_pairing.is_linked(chat_id):
        since = ig_pairing.linked_at(chat_id)
        when = time.strftime("%Y-%m-%d", time.localtime(since)) if since else "?"
        text = t(chat_id, "📸 *اینستاگرام دایرکت*\n\n✅ وصله (از {date}).\n\nهرچی تو دایرکت اینستاگرام برای ما بفرستی — ریلز، پست، استوری — همین‌جا دانلودشده برات میاد.").format(date=when)
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(t(chat_id, "🔌 قطع اتصال"), callback_data="igd:unlink")]]
        )
        return text, kb

    text = t(
        chat_id,
        "📸 *اینستاگرام دایرکت*\n\n"
        "به‌جای کپی‌کردن لینک، مستقیم تو خود اینستاگرام برامون بفرست و "
        "همین‌جا تحویل بگیر.\n\n"
        "⚠️ *اول باید پیج ما رو فالو کنی.* اگه فالو نکنی، دایرکتت می‌ره تو "
        "بخش «درخواست پیام» اینستاگرام و ممکنه اصلا به دستمون نرسه.\n\n"
        "🔒 فقط شناسه‌ی عددی اکانت اینستاگرامت ذخیره می‌شه — نه اسم، نه پروفایل، "
        "نه پیام‌هات. قطع اتصال هم یه دکمه‌ست.",
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(chat_id, "🔗 وصل کردن اکانت"), callback_data="igd:pair")]]
    )
    return text, kb


async def igdirect_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not settings.ig_direct_enabled:
        await update.effective_message.reply_text(
            t(chat_id, "📸 اینستاگرام دایرکت هنوز روی این سرور فعال نشده.")
        )
        return
    text, kb = await _screen(chat_id)
    await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    action = query.data.split(":", 2)[1]

    if action == "pair":
        token = ig_pairing.issue(chat_id)
        handle = await account_name()
        # An Instagram handle like ig_bot_official has an odd number of
        # underscores, which makes Telegram reject the whole Markdown message -
        # and the failure is swallowed, so the button just looks dead.
        target = f"@{_escape_md(handle)}" if handle else t(chat_id, "اکانت اینستاگرام بات")

        # A tappable link, because the alternative is copying a handle out of
        # a message and searching Instagram for it by hand.
        kb = None
        if handle:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                t(chat_id, "📸 باز کردن پیج در اینستاگرام"),
                url=f"https://instagram.com/{handle}",
            )]])

        await query.message.reply_text(
            t(
                chat_id,
                "🔗 *وصل کردن اکانت*\n\n"
                "۱. پیج {target} رو باز کن و *فالو کن*\n"
                "(بدون فالو، پیامت می‌ره تو «درخواست پیام» و ممکنه به دستمون نرسه)\n\n"
                "۲. *دایرکت* همون پیج رو باز کن و این کد رو *به عنوان پیام* بفرست:\n\n"
                "`{token}`\n\n"
                "همین. چند ثانیه بعد همین‌جا تایید می‌گیری.\n\n"
                "⏳ این کد {minutes} دقیقه اعتبار داره و یک‌بار مصرفه.",
            ).format(target=target, token=token, minutes=ig_pairing.TOKEN_TTL // 60),
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    if action == "unlink":
        removed = ig_pairing.unlink(chat_id)
        await query.message.reply_text(
            t(chat_id, "🔌 اتصال قطع شد. شناسه‌ی اینستاگرامت پاک شد.")
            if removed
            else t(chat_id, "اتصالی برقرار نبود.")
        )
        return



# --------------------------------------------------------------------------
# The DM side
# --------------------------------------------------------------------------

_PAIR_HELP_FA = (
    "سلام! برای اینکه چیزایی که اینجا می‌فرستی رو تو تلگرام برات دانلود کنم، "
    "اول باید اکانتت رو وصل کنی:\n\n"
    "۱. این پیج رو فالو کن\n"
    "۲. تو تلگرام بات رو باز کن و /igdirect بزن\n"
    "۳. دکمه «وصل کردن اکانت» رو بزن\n"
    "۴. کدی که می‌ده رو همین‌جا تو دایرکت برام بفرست"
)


async def on_direct_message(dm: ig_direct.DirectMessage) -> None:
    """Every inbox source ends up here. Already de-duplicated upstream."""
    identity = dm.identity()
    chat_id = ig_pairing.chat_for(identity)

    # 1. A pairing token from someone we do not know yet.
    if chat_id is None and dm.text and ig_pairing.looks_like_token(dm.text):
        await _redeem(dm, identity)
        return

    # 2. Still a stranger. Say how to pair rather than dropping it silently -
    #    from their side an ignored DM is indistinguishable from a dead bot -
    #    but at most once an hour.
    if chat_id is None:
        if time.time() - _help_sent.get(identity, 0.0) > _HELP_COOLDOWN:
            _help_sent[identity] = time.time()
            await ig_direct.reply_dm(dm, _PAIR_HELP_FA)
        return

    # 3. Paired. Either a share we can act on, ordinary chat, or a share we
    #    failed to read - and the three must not look alike.
    shortcode = dm.shortcode()
    if not shortcode and not dm.media_url and not dm.media_id:
        if dm.text.strip():
            return  # they are just talking to us

        # An attachment arrived and nothing was extracted from it. Dropping
        # this silently is why the shared-story failure took a user report to
        # find: from both sides it looked like nothing had been sent.
        log.warning("ig direct: unreadable share from %s - raw=%s", dm.source, dm.raw)
        await ig_direct.reply_dm(
            dm, "این رو نتونستم بخونم. لینک خود پست رو برام بفرست تا دانلودش کنم."
        )
        return

    await _fetch_and_send(dm, chat_id, shortcode)


async def _redeem(dm: ig_direct.DirectMessage, identity: str) -> None:
    chat_id = ig_pairing.redeem(dm.text, identity)
    if chat_id is None:
        await ig_direct.reply_dm(
            dm,
            "❌ این کد معتبر نیست یا منقضی شده.\n\n"
            "تو تلگرام /igdirect بزن و یه کد تازه بگیر.",
        )
        return

    await ig_direct.reply_dm(dm, "✅ وصل شد! از این به بعد هرچی اینجا بفرستی، تو تلگرام برات میاد.")
    try:
        await _bot().send_message(
            chat_id,
            t(chat_id, "✅ اکانت اینستاگرامت وصل شد.\n\nحالا هر ریلز یا پستی رو تو دایرکت اینستاگرام برامون بفرست تا همین‌جا دانلودشده تحویل بگیری."),
        )
    except Exception as e:
        log.warning("ig direct: could not confirm pairing in chat %s: %s", chat_id, e)


async def _fetch_and_send(dm: ig_direct.DirectMessage, chat_id: int, shortcode: str) -> None:
    from handlers import gate, instagram_handler

    bot = _bot()

    # The sponsor gate applies here too. Pairing once must not become a way
    # around a channel lock the pasted-link flow enforces.
    if not await gate.is_member_bot(bot, chat_id):
        await ig_direct.reply_dm(
            dm,
            t(chat_id, "برای استفاده از بات باید اول تو کانال ما عضو بشی. تو تلگرام /start بزن."),
        )
        return

    try:
        status = await bot.send_message(
            chat_id, t(chat_id, "📥 از دایرکت اینستاگرام گرفتمش — دارم دانلود می‌کنم…")
        )
    except Exception as e:
        # Blocked, or the chat is gone. The pairing can never be useful again.
        log.warning("ig direct: chat %s unreachable (%s) - dropping the pairing", chat_id, e)
        ig_pairing.unlink_identity(dm.identity())
        return

    if not shortcode:
        # Everything downstream is a guess from here, and the guess is what
        # keeps failing. Record what the message actually looked like so the
        # next failure names the field the media was hiding in.
        log.warning(
            "ig direct: no shortcode from %s message %s - url=%r raw=%s",
            dm.source, dm.mid, dm.media_url[:120], dm.raw,
        )

    from modules import ig_private, ig_web

    try:
        async with limits.download_slot(chat_id):
            if dm.media_id and (ig_web.usable() or ig_private.usable()):
                # The pk came off the DM itself; a shortcode is something we
                # computed from it. Prefer the exact address - and it is the
                # ONLY one that works for a story, which has no shortcode.
                files = await ig.fetch_by_pk(dm.media_id)
            elif shortcode:
                files = await ig.fetch_post(shortcode)
            else:
                # No permalink and no media id: all we have is a signed CDN
                # url that expires within minutes, so it is fetched now.
                files = await ig.fetch_direct_url(dm.media_url, dm.mid or str(int(dm.timestamp)))
    except Exception as e:
        await status.edit_text(t(chat_id, "❌ خطا: {err}").format(err=e))
        return

    permalink = dm.permalink or (
        f"https://www.instagram.com/reel/{shortcode}/" if shortcode else ""
    )

    caption = permalink.split("?")[0] or None
    if shortcode:
        from handlers import ig_post_menu

        caption = await ig_post_menu.caption_for(chat_id, shortcode, permalink)

    try:
        await instagram_handler.deliver(bot, chat_id, files, caption=caption)
        await status.delete()
    except Exception as e:
        await status.edit_text(t(chat_id, "❌ آپلود ناموفق: {err}").format(err=e))
        return

    limits.sweep_downloads(settings.download_dir)

    video = next((f for f in files if f.suffix.lower() in _VIDEO_EXTS), None)
    if shortcode:
        # The same strip a pasted link gets. A share should not be a
        # second-class way of asking for the same post.
        await instagram_handler.post_menu(
            bot, chat_id, shortcode, permalink, video, offer_music=bool(video)
        )


# --------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------

async def on_startup(application) -> None:
    """Called from main's post_init once the Application is running."""
    bind(application)

    if not settings.ig_direct_enabled:
        log.info("ig direct: disabled (nothing configured)")
        return

    if settings.has_ig_webhook:
        from modules import ig_graph

        ig_graph.set_alert(_alert_admins)
        ig_graph.start_refresh_loop()

    if settings.has_ig_web:
        from modules import ig_web

        # A warning from Instagram is the one chance to back off before it
        # becomes an action, and it is worth waking somebody for.
        ig_web.set_alert(_alert_admins)

    if settings.has_ig_private:
        try:
            from modules import ig_private

            ig_private.set_alert(_alert_admins)
        except Exception:
            pass

    await ig_direct.start(on_direct_message)


async def on_shutdown(application) -> None:
    from modules import ig_graph

    ig_graph.stop_refresh_loop()
    await ig_direct.stop()


async def _alert_admins(text: str) -> None:
    """A dead Instagram token is silent otherwise: the webhook simply stops
    delivering and nothing in the bot looks wrong."""
    if _app is None:
        return
    for admin_id in settings.admin_ids:
        try:
            await _app.bot.send_message(admin_id, text)
        except Exception as e:
            log.info("could not alert admin %s: %s", admin_id, e)
