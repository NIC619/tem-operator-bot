"""
main.py — Entry point: initialises DB, registers Telegram handlers, starts scheduler.

PTB v21 manages its own event loop via app.run_polling() — do NOT wrap in asyncio.run().
Use post_init to run setup code inside PTB's event loop.
"""
import base64
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, BotCommandScopeChat
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
    cmd_content,
    cmd_content_done,
    cmd_skip,
    cmd_omit,
    cmd_reviewers,
    cmd_add_reviewer,
    cmd_remove_reviewer,
    cmd_list_categories,
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


# Volume file bootstrap: on platforms like Railway the /data volume starts
# empty and there's no SSH path in before the service first runs. If these
# *_B64 env vars are set and the target file is missing, write it on boot.
# Once the volume holds the files, the env vars can be removed.
_BOOTSTRAP_FILES = [
    ("CONFIG_YAML_B64",       "CONFIG_PATH",                 "./config.yaml"),
    ("REVIEWERS_MD_B64",      "REVIEWERS_MD_PATH",           "./reviewers.md"),
    ("GMAIL_CREDENTIALS_B64", "GMAIL_CREDENTIALS_JSON_PATH", "./credentials.json"),
    ("GMAIL_TOKEN_B64",       "GMAIL_TOKEN_PATH",            "./gmail_token.json"),
]


def _bootstrap_volume_files() -> None:
    for env_b64, env_path, default_path in _BOOTSTRAP_FILES:
        b64 = os.environ.get(env_b64)
        if not b64:
            continue
        target = Path(os.environ.get(env_path, default_path))
        if target.exists() and not os.environ.get("BOOTSTRAP_OVERWRITE"):
            logger.info("Bootstrap: %s already exists, skipping.", target)
            continue
        b64 = "".join(b64.split())  # tolerate whitespace/newlines from env var UIs
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception as e:
            raise RuntimeError(f"{env_b64} is not valid base64: {e}") from e
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        try:
            target.chmod(0o600)
        except OSError:
            pass
        logger.info("Bootstrap: wrote %s (%d bytes) from %s", target, len(data), env_b64)


PUBLIC_COMMANDS = [
    BotCommand("getid", "Show this chat's ID and your user ID"),
    BotCommand("status", "List active submissions and their state"),
    BotCommand("done", "Mark your review as done: /done <keyword>"),
    BotCommand("reject", "Propose rejecting: /reject <sub_id|keyword> <reason>"),
    BotCommand("second", "Second a rejection: /second <sub_id|keyword>"),
    BotCommand("override", "Operator: reassign reviewers /override <sub_id> @user1 [@user2]"),
]

OPERATOR_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("content", "Append draft text: /content <sub_id> <text>"),
    BotCommand("content_done", "Finalize buffered content: /content_done <sub_id>"),
    BotCommand("skip", "Skip content request: /skip <sub_id>"),
    BotCommand("omit", "Drop a submission: /omit <sub_id> [reason]"),
    BotCommand("reviewers", "Show current reviewers.md contents"),
    BotCommand("list_categories", "Show reviewer category headings"),
    BotCommand("add_reviewer", "Add a reviewer: /add_reviewer <category> @user"),
    BotCommand("remove_reviewer", "Remove a reviewer: /remove_reviewer @user"),
]


async def post_init(application: Application) -> None:
    """Called by PTB after the app is initialised, within its event loop."""
    db.init_db()
    logger.info("Database initialised.")
    await application.bot.set_my_commands(PUBLIC_COMMANDS)
    operator_id = cfg.load()["telegram"].get("operator_user_id")
    if operator_id:
        await application.bot.set_my_commands(
            OPERATOR_COMMANDS, scope=BotCommandScopeChat(chat_id=operator_id)
        )
    logger.info("Bot command menu registered.")
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
    # Seed volume files from base64 env vars if they're missing. Runs before
    # config.load() and GmailClient() so subsequent steps find the files.
    _bootstrap_volume_files()

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
    app.add_handler(CommandHandler("content", cmd_content))
    app.add_handler(CommandHandler("content_done", cmd_content_done))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("omit", cmd_omit))
    app.add_handler(CommandHandler("reviewers", cmd_reviewers))
    app.add_handler(CommandHandler("add_reviewer", cmd_add_reviewer))
    app.add_handler(CommandHandler("remove_reviewer", cmd_remove_reviewer))
    app.add_handler(CommandHandler("list_categories", cmd_list_categories))

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
