"""
Telegram multi-platform downloader bot — entry point.

Run:
    python main.py
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from config import settings, setup_logging
from handlers import (
    admin,
    gate,
    ig_direct_handler,
    instagram_handler,
    lyrics_handler,
    recognize_handler,
    router,
    spotify_handler,
    start,
    youtube_handler,
)

log = logging.getLogger("main")


def _local_api_reachable(base: str, attempts: int = 10, delay: float = 3.0) -> bool:
    """A TCP connect is enough: the server answers 404 on / but that still
    proves it is listening.

    Retried, because on boot the bot can easily win the race against the
    Bot API container, and because a bot that was moved to a local server has
    been logged out of the cloud API - there is nothing useful to fall back
    to, so waiting beats failing.
    """
    import socket
    import time as _time
    from urllib.parse import urlparse

    parsed = urlparse(base)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    for attempt in range(1, attempts + 1):
        try:
            with socket.create_connection((host, port), timeout=3.0):
                return True
        except OSError:
            if attempt < attempts:
                log.warning(
                    "Local Bot API not up yet (%d/%d) - retrying in %.0fs",
                    attempt, attempts, delay,
                )
                _time.sleep(delay)
    return False


def build_app() -> Application:
    builder = (
        Application.builder()
        .token(settings.telegram_token)
        # Without this, updates are processed strictly one-at-a-time; a
        # single slow download blocks every other user and chat.
        .concurrent_updates(True)
        # PTB's default media write timeout is 20s, which aborts uploads
        # of large video/audio files midway. Allow up to 10 minutes.
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .media_write_timeout(600)
        .pool_timeout(30)
        # The Instagram Direct bridge is not driven by updates: its webhook
        # listener and standby poller are started once the loop is running,
        # and torn down with it.
        .post_init(ig_direct_handler.on_startup)
        .post_shutdown(ig_direct_handler.on_shutdown)
    )

    # Route through a local Bot API server when configured - lifts the
    # 50MB upload cap of the public api.telegram.org to 2GB.
    if settings.bot_api_base_url:
        base = settings.bot_api_base_url.rstrip("/")
        if _local_api_reachable(base):
            builder = (
                builder.base_url(f"{base}/bot")
                .base_file_url(f"{base}/file/bot")
                .local_mode(True)
            )
            log.info("Using local Bot API server at %s", base)
        else:
            # Enabling a local server logs the bot out of api.telegram.org, so
            # the public API answers "Logged out" and the fallback is useless.
            # Say that plainly instead of leaving a confusing traceback.
            log.error(
                "Local Bot API at %s is unreachable. This bot was moved to a "
                "local server, so the public API will reject it with "
                "'Logged out'. Start the container: botctl diag / option 9.",
                base,
            )

    app = builder.build()

    # These two MUST sit in different groups. Within one group PTB runs only
    # the first handler that matches and then moves to the next group - and a
    # TypeHandler(Update) matches everything. Both were in group -1, so
    # track_update consumed the group and gate.guard was never reached: the
    # channel lock silently let every user through, for as long as both
    # handlers had been registered.
    #
    # Statistics first, deliberately. A user who is about to be turned away at
    # the gate is still a user worth counting.
    app.add_handler(TypeHandler(Update, admin.track_update), group=-2)

    # Sponsor-channel lock, if configured. Its own group, before any feature
    # handler, so it can stop the update.
    if settings.required_channel:
        app.add_handler(TypeHandler(Update, gate.guard), group=-1)
        log.info("Channel lock active: %s", settings.required_channel)

    # Commands
    app.add_handler(CommandHandler("start", start.start_cmd))
    app.add_handler(CommandHandler("help", start.help_cmd))
    app.add_handler(CommandHandler(["admin", "stats"], admin.admin_cmd))
    app.add_handler(CommandHandler(["id", "whoami"], admin.whoami_cmd))
    app.add_handler(CommandHandler(["lang", "language"], start.lang_cmd))
    app.add_handler(CommandHandler("broadcast", admin.broadcast_cmd))
    app.add_handler(CommandHandler("igcheck", admin.igcheck_cmd))
    app.add_handler(CommandHandler("igtest", admin.igtest_cmd))
    app.add_handler(CommandHandler("recstatus", admin.recstatus_cmd))
    app.add_handler(CommandHandler("srcstatus", admin.srcstatus_cmd))
    app.add_handler(CommandHandler("igdirect", ig_direct_handler.igdirect_cmd))

    # Any video/audio the user sends or forwards -> identify its music.
    # Document.* covers files sent "as file" (no compression), which arrive as
    # documents rather than video/audio and would otherwise be ignored.
    app.add_handler(
        MessageHandler(
            filters.VIDEO
            | filters.AUDIO
            | filters.VOICE
            | filters.VIDEO_NOTE
            | filters.Document.VIDEO
            | filters.Document.AUDIO,
            recognize_handler.on_media,
        )
    )

    # Free text / URLs
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, router.handle_text)
    )

    # Inline button callbacks — namespaced by prefix so each module handles its own
    app.add_handler(CallbackQueryHandler(youtube_handler.on_callback, pattern=r"^yt:"))
    app.add_handler(CallbackQueryHandler(instagram_handler.on_callback, pattern=r"^ig:"))
    app.add_handler(CallbackQueryHandler(ig_direct_handler.on_callback, pattern=r"^igd:"))
    app.add_handler(CallbackQueryHandler(spotify_handler.on_callback, pattern=r"^sp:"))
    app.add_handler(CallbackQueryHandler(lyrics_handler.on_callback, pattern=r"^lyr:"))
    app.add_handler(CallbackQueryHandler(admin.on_callback, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(recognize_handler.on_pick, pattern=r"^rec:"))
    app.add_handler(CallbackQueryHandler(start.on_lang_callback, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(gate.on_check, pattern=r"^gate:"))

    # Global error log
    app.add_error_handler(_on_error)
    return app


async def _on_error(update: object, context) -> None:
    log.exception("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            from utils.i18n import t

            await update.effective_message.reply_text(
                t(update.effective_chat.id,
                  "💥 یه خطای غیرمنتظره پیش اومد. دوباره امتحان کن.")
            )
        except Exception:
            pass


def main() -> None:
    setup_logging()
    log.info("Starting bot. Download dir: %s", settings.download_dir)
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
