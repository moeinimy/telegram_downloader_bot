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
    instagram_handler,
    lyrics_handler,
    recognize_handler,
    router,
    spotify_handler,
    start,
    youtube_handler,
)

log = logging.getLogger("main")


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
    )

    # Route through a local Bot API server when configured - lifts the
    # 50MB upload cap of the public api.telegram.org to 2GB.
    if settings.bot_api_base_url:
        base = settings.bot_api_base_url.rstrip("/")
        builder = (
            builder.base_url(f"{base}/bot")
            .base_file_url(f"{base}/file/bot")
            .local_mode(True)
        )
        log.info("Using local Bot API server at %s", base)

    app = builder.build()

    # Runs before everything else (group -1) and never blocks other handlers;
    # keeps the usage table current without touching each flow.
    app.add_handler(TypeHandler(Update, admin.track_update), group=-1)

    # Commands
    app.add_handler(CommandHandler("start", start.start_cmd))
    app.add_handler(CommandHandler("help", start.help_cmd))
    app.add_handler(CommandHandler(["admin", "stats"], admin.admin_cmd))
    app.add_handler(CommandHandler(["id", "whoami"], admin.whoami_cmd))

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
    app.add_handler(CallbackQueryHandler(spotify_handler.on_callback, pattern=r"^sp:"))
    app.add_handler(CallbackQueryHandler(lyrics_handler.on_callback, pattern=r"^lyr:"))
    app.add_handler(CallbackQueryHandler(admin.on_callback, pattern=r"^adm:"))

    # Global error log
    app.add_error_handler(_on_error)
    return app


async def _on_error(update: object, context) -> None:
    log.exception("Unhandled exception", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "💥 یه خطای غیرمنتظره پیش اومد. دوباره امتحان کن."
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
