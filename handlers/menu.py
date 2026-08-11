"""
The command list Telegram shows behind the "Menu" button.

Two lists, not one. Telegram scopes commands, so the admin tools can be
registered only for the chats in ADMIN_IDS and are then genuinely invisible to
everybody else - not merely refused when typed, which is what /admin and
/broadcast were doing while sitting in plain sight of every user.

Registered on startup rather than kept in a static file: the admin list has to
follow ADMIN_IDS, which lives in .env and changes without a code change.
"""

from __future__ import annotations

import logging

from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from config import settings

log = logging.getLogger(__name__)

USER_COMMANDS_FA = [
    ("start", "منوی اصلی و راهنما"),
    ("igdirect", "اتصال پیج اینستاگرام — دایرکت بده، اینجا بگیر"),
    ("lang", "تغییر زبان / Change language"),
    ("help", "راهنما"),
]

USER_COMMANDS_EN = [
    ("start", "Main menu and help"),
    ("igdirect", "Connect Instagram — share by DM, collect here"),
    ("lang", "تغییر زبان / Change language"),
    ("help", "Help"),
]

# Appended to the user list for admins only.
ADMIN_COMMANDS = [
    ("admin", "پنل مدیریت و آمار"),
    ("broadcast", "ارسال همگانی"),
    ("srcstatus", "وضعیت منابع، دایرکت و قفل کانال"),
    ("igcheck", "وضعیت سشن اینستاگرام"),
    ("igtest", "تست مسیرهای دانلود اینستاگرام"),
    ("recstatus", "وضعیت موتورهای تشخیص آهنگ"),
    ("id", "آیدی عددی من"),
]


def _commands(pairs) -> list[BotCommand]:
    return [BotCommand(name, description) for name, description in pairs]


async def setup_commands(app) -> None:
    """Publish both lists. Never fatal - a bot that starts without a pretty
    menu is fine, one that refuses to start over it is not."""
    try:
        await app.bot.set_my_commands(
            _commands(USER_COMMANDS_FA), scope=BotCommandScopeDefault()
        )
        await app.bot.set_my_commands(
            _commands(USER_COMMANDS_EN), scope=BotCommandScopeDefault(), language_code="en"
        )
    except Exception as e:
        log.warning("could not publish the user command list: %s", e)
        return

    for admin_id in settings.admin_ids:
        try:
            await app.bot.set_my_commands(
                _commands(USER_COMMANDS_FA + ADMIN_COMMANDS),
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as e:
            # Usually just an admin who has never opened a chat with the bot.
            log.info("could not publish the admin command list to %s: %s", admin_id, e)

    log.info(
        "command menu published: %d user commands, %d admin(s) with %d",
        len(USER_COMMANDS_FA), len(settings.admin_ids),
        len(USER_COMMANDS_FA) + len(ADMIN_COMMANDS),
    )
