"""
main.py — Entry point: initialises DB, registers Telegram handlers, starts scheduler.

PTB v21 manages its own event loop via app.run_polling() — do NOT wrap in asyncio.run().
Use post_init to run setup code inside PTB's event loop.
"""
import logging

from dotenv import load_dotenv
from telegram.ext import Application, ApplicationBuilder, CommandHandler, CallbackQueryHandler
from telegram.request import HTTPXRequest

import config as cfg
import db
from scheduler import start_scheduler
from telegram_handlers import (
    cmd_getid,
    cmd_status,
    cmd_done,
    cmd_reject,
    cmd_second,
    cmd_override,
    cb_accept,
    cb_decline,
    cb_done,
    cb_confirm_rejection,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Called by PTB after the app is initialised, within its event loop."""
    db.init_db()
    logger.info("Database initialised.")
    start_scheduler(application.bot)


async def error_handler(update: object, context) -> None:
    """Log all PTB errors (network timeouts, bad requests, etc.) without crashing."""
    from telegram.error import TimedOut, NetworkError
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        # Transient network issue — log at WARNING, bot will recover automatically
        logger.warning("Telegram network error (will retry automatically): %s", err)
    else:
        logger.error("Unhandled PTB error: %s", err, exc_info=err)


def main() -> None:
    token = cfg.TELEGRAM_BOT_TOKEN
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

    # Trigger Gmail OAuth consent flow now, synchronously, before the async
    # event loop starts. run_local_server() blocks until the browser callback
    # completes. On subsequent runs it just loads the saved token silently.
    from gmail_client import GmailClient
    logger.info("Initialising Gmail client (OAuth flow if first run)...")
    GmailClient()
    logger.info("Gmail client ready.")

    # Use separate HTTP clients for getUpdates vs all other API calls so the
    # long-polling connection never blocks scheduler-initiated sendMessage calls.
    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .request(HTTPXRequest(
            connection_pool_size=8,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=15,
        ))
        .get_updates_request(HTTPXRequest(
            connection_pool_size=1,
            read_timeout=30,
        ))
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("getid", cmd_getid))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("second", cmd_second))
    app.add_handler(CommandHandler("override", cmd_override))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(cb_accept, pattern=r"^accept_"))
    app.add_handler(CallbackQueryHandler(cb_decline, pattern=r"^decline_"))
    app.add_handler(CallbackQueryHandler(cb_done, pattern=r"^done_"))
    app.add_handler(CallbackQueryHandler(cb_confirm_rejection, pattern=r"^confirm_rejection_"))
    app.add_error_handler(error_handler)

    tg_poll_interval = cfg.load()["telegram"].get("poll_interval_seconds", 30)
    logger.info("Bot starting, polling for updates (interval: %ds)...", tg_poll_interval)
    app.run_polling(poll_interval=tg_poll_interval)


if __name__ == "__main__":
    main()
