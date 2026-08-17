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


async def recstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/recstatus - which recognition engines are usable right now."""
    if not _is_admin(update):
        return
    from modules import engines, recognize

    from utils import limits

    ok, detail = recognize.service_reachable()
    lines = [("✅ " if ok else "❌ ") + f"shazam: {detail}"]

    # Reachable and refusing us are different problems with the same symptom.
    # A TCP connect proves the host is up; it says nothing about whether the
    # endpoint will answer this IP with JSON or with a block page.
    if recognize.last_error:
        import time as _time

        mins = (_time.time() - recognize.last_error_at) / 60
        lines.append(f"   ⚠️ آخرین خطا ({mins:.0f} دقیقه پیش): {_md(recognize.last_error[:90])}")
        if "decode" in recognize.last_error.lower() or "403" in recognize.last_error:
            lines.append("   ↳ شزم به IP این سرور جواب JSON نمی‌ده. SHAZAM_PROXY رو ست کن.")
    if settings.shazam_proxy:
        kind = "socks" if settings.shazam_proxy.lower().startswith("socks") else "http"
        lines.append(f"   🔀 از پروکسی استفاده می‌شه ({kind})")

    lines += engines.status()

    # First, because it is the failure that does not look like itself: with
    # no room to write a window, ffmpeg fails, every window comes back empty,
    # and the user is told no music was found.
    disk = limits.disk_report(settings.download_dir)
    icon = "✅" if disk["free_mb"] > 500 else ("⚠️" if disk["free_mb"] > 150 else "❌")
    lines.append("")
    lines.append(
        f"{icon} دیسک: {disk['free_mb']}MB آزاد از {disk['total_mb']}MB"
    )
    lines.append(
        f"   ↳ دانلودها {disk['reclaimable_mb']}MB (سقف {disk['cap_mb']}MB)"
        f" · محافظت‌شده {disk['protected_mb']}MB"
    )
    if disk["free_mb"] <= 150:
        lines.append("   ↳ ⚠️ با این فضا تشخیص آهنگ کار نمی‌کنه. botctl clearcache")
    lines.append("")
    lines.append("ترتیب: " + " → ".join(("shazam",) + tuple(settings.recognition_engines)))
    await update.effective_message.reply_text("\n".join(lines))


async def srcstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/srcstatus - can each metadata source actually be reached right now.

    The Spotify Web API failing is invisible otherwise: the bot falls back to
    the embed page and everything keeps working except playlists past ~100
    tracks, which simply come back short with no error anywhere.
    """
    if not _is_admin(update):
        return
    import asyncio

    from modules import spotify_api

    lines = ["*وضعیت منابع*", ""]

    if not spotify_api.available():
        lines.append("⚪️ Spotify Web API: کلید تنظیم نشده (پلی‌لیست تا ۱۰۰ ترک)")
    else:
        def probe():
            try:
                spotify_api._get("https://api.spotify.com/v1/browse/new-releases",
                                 {"limit": 1})
                return ""
            except Exception:
                return spotify_api.blocked_reason() or "در دسترس نیست"

        why = await asyncio.to_thread(probe)
        if why:
            lines += [
                "❌ Spotify Web API: " + _md(why),
                "   ↳ پلی‌لیست‌های بالای ۱۰۰ ترک کار نمی‌کنن.",
            ]
        else:
            lines.append("✅ Spotify Web API: سالم")

    async def check(label, fn):
        try:
            return ("✅ " if await asyncio.to_thread(fn) else "❌ ") + label
        except Exception as e:
            return f"❌ {label}: {_md(str(e)[:60])}"

    from utils import http

    lines.append(await check(
        "Deezer", lambda: http.get("https://api.deezer.com/track/3135556",
                                   timeout=8).status_code == 200))
    lines.append(await check(
        "iTunes", lambda: http.get("https://itunes.apple.com/search",
                                   params={"term": "test", "limit": 1},
                                   timeout=8).status_code == 200))
    lines.append(await check(
        "Odesli", lambda: http.get("https://api.song.link/v1-alpha.1/links",
                                   params={"url": "spotify:track:0wwPcA6wtMf6HUMpIRdeP7"},
                                   timeout=12).status_code == 200))
    lines += _ig_direct_lines()

    from handlers import gate

    lines.append("")
    lines += [_md(line) for line in await gate.diagnose(context.bot)]

    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="Markdown")


def _ig_direct_lines() -> list[str]:
    """Health of the Instagram Direct bridge.

    Worth its own block because every failure mode here is silent: a dead
    token, a dropped webhook subscription and an App Review that has not
    landed all look identical from the outside - the DMs simply stop arriving.
    """
    import time

    from modules import ig_direct, ig_graph

    snapshot = ig_direct.status()
    lines = ["", "*اینستاگرام دایرکت*"]

    if not snapshot["enabled"]:
        lines.append("⚪️ فعال نیست")
        return lines

    def ago(ts: float) -> str:
        if not ts:
            return "هیچ‌وقت"
        mins = (time.time() - ts) / 60
        return f"{mins:.0f} دقیقه پیش" if mins < 90 else f"{mins / 60:.0f} ساعت پیش"

    for name, state in snapshot["sources"].items():
        if not state["configured"]:
            continue
        if state["healthy"] is None:
            icon = "⚪️"
        else:
            icon = "✅" if state["healthy"] else "❌"
        role = "فعال" if state["running"] else "آماده‌باش"

        # These readings are taken by the health loop on its own timer, not by
        # this command - so a line can be minutes old. Saying when it was taken
        # is what separates "it is doing that now" from "that is the last thing
        # we saw", which is the difference that made a connected channel read
        # as still connecting.
        checked = state["last_check"]
        age = f" · بررسی {ago(checked)}" if checked and time.time() - checked > 120 else ""
        lines.append(f"{icon} {name} ({role}): {_md(state['detail'] or '-')}{age}")
        lines.append(f"   ↳ {state['events']} پیام · آخری {ago(state['last_event'])}")

    try:
        from modules import ig_private

        if ig_private.blocked_reason:
            import time as _t

            hours = (_t.time() - ig_private.blocked_at) / 3600
            lines.append(f"🚫 اکانت بلاک شده ({hours:.1f} ساعت پیش) — پولینگ متوقفه")
            lines.append("   ↳ " + _md(ig_private.blocked_reason[:110]))
            lines.append("   ↳ تو اپ اینستا لاگین کن، بعد: botctl igreset")
    except Exception:
        pass

    if snapshot["sources"].get("poll", {}).get("running"):
        try:
            from modules import ig_private

            timing = ig_private.timing()
            if timing["samples"]:
                lines.append(
                    f"⏱ تاخیر: آخری {timing['last_lag']}s · "
                    f"میانگین {timing['avg_lag']}s ({timing['samples']} نمونه)"
                )
            lines.append(f"   ↳ هر sweep {timing['sweep_ms']}ms طول می‌کشه")
        except Exception:
            pass

    if snapshot["sources"].get("web", {}).get("running"):
        try:
            from modules import ig_web

            r = ig_web.rate()
            icon = {"محتاطانه": "✅", "متعادل": "✅", "پرریسک": "⚠️"}.get(r["verdict"], "❌")
            lines.append(
                f"{icon} نرخ درخواست: {r['last_hour']} تو ساعت گذشته · "
                f"~{r['projected']} در روز ({r['verdict']})"
            )
            if r["hours_measured"] < 24:
                lines.append(f"   ↳ بر اساس {r['hours_measured']} ساعت اندازه‌گیری")
            if r["verdict"] in ("پرریسک", "بن‌آور"):
                lines.append("   ↳ IG_DM_MAX_INTERVAL رو ببر بالاتر")
        except Exception:
            pass

    try:
        from utils import exit_ip

        ip = exit_ip.status()
        if ip["current"]:
            held = ip["held_minutes"]
            age = f"{held:.0f} دقیقه" if held < 90 else f"{held / 60:.0f} ساعت"
            if ip["moves_24h"]:
                lines.append(
                    f"⚠️ IP خروجی: {_md(ip['current'])} · {ip['moves_24h']} بار "
                    f"جابه‌جا شد تو ۲۴ ساعت (الان {age} ثابت)"
                )
                lines.append("   ↳ کوکی به IP صادرکننده‌ش گره خورده — این عمرش رو کوتاه می‌کنه")
            else:
                lines.append(f"🌐 IP خروجی: {_md(ip['current'])} · {age} ثابت")
    except Exception:
        pass

    left = ig_graph.days_left()
    if left is not None:
        lines.append(
            ("⚠️ " if left < 10 else "🔑 ") + f"انقضای توکن: {left:.0f} روز دیگه"
        )
    if ig_graph.last_error:
        lines.append("⚠️ آخرین خطای تمدید: " + _md(ig_graph.last_error[:80]))

    lines.append(f"🔗 {snapshot['links']} اتصال فعال · {snapshot['pending']} در انتظار")
    if snapshot["public_url"]:
        lines.append("🌐 " + _md(snapshot["public_url"]))
    else:
        lines.append("⚠️ IG_PUBLIC_URL ست نشده — برای App Review لازمه")
    return lines


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
