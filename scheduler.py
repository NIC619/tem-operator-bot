"""
scheduler.py — APScheduler jobs: Gmail polling and follow-up checker.
"""
import logging
import time
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config as cfg
import db
import state

logger = logging.getLogger(__name__)

_DB_KEY = "last_gmail_checked_ts"


def _load_last_checked_ts() -> float:
    """Load persisted timestamp from DB, falling back to 24h ago on first run."""
    saved = db.get_state(_DB_KEY)
    if saved:
        return float(saved)
    # First ever run: look back 24 hours so we don't miss recent submissions
    fallback = time.time() - 86400
    logger.info(
        "No persisted Gmail timestamp found — scanning last 24h (since %s).",
        datetime.fromtimestamp(fallback).strftime("%Y-%m-%d %H:%M:%S"),
    )
    return fallback


def _save_last_checked_ts(ts: float) -> None:
    db.set_state(_DB_KEY, str(ts))


def start_scheduler(bot) -> None:
    config = cfg.load()
    poll_interval = config["gmail"].get("poll_interval_seconds", 300)

    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        _poll_gmail,
        trigger="interval",
        seconds=poll_interval,
        args=[bot],
        id="gmail_poll",
        replace_existing=True,
    )

    scheduler.add_job(
        _check_followups,
        trigger="interval",
        hours=1,
        args=[bot],
        id="followup_checker",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Scheduler started. Gmail poll every %ds, follow-up check every 1h.",
        poll_interval,
    )


async def _poll_gmail(bot) -> None:
    from gmail_client import GmailClient

    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")
    last_checked_ts = _load_last_checked_ts()

    try:
        gmail = GmailClient()
        submissions = gmail.poll_new_submissions(
            last_checked_ts,
            subject_prefix=config["gmail"].get("subject_prefix"),
            submission_label=config["gmail"].get("submission_label"),
        )
        _save_last_checked_ts(time.time())

        for email_data in submissions:
            try:
                await state.handle_new_submission(email_data, bot, config)
            except Exception as e:
                logger.error("Error handling new submission: %s", e)

    except Exception as e:
        logger.error("Gmail polling failed: %s", e)
        # Notify operator in private chat
        if operator_user_id:
            try:
                await bot.send_message(
                    chat_id=operator_user_id,
                    text=f"⚠️ Gmail polling error: {e}",
                )
            except Exception as notify_err:
                logger.error("Failed to notify operator: %s", notify_err)


async def _check_followups(bot) -> None:
    config = cfg.load()
    now = datetime.now()

    try:
        due = db.get_pending_followups(now)
        for followup in due:
            try:
                await state.send_followup(followup, bot, config)
            except Exception as e:
                logger.error(
                    "Error sending follow-up for submission #%s: %s",
                    followup["submission_id"],
                    e,
                )
    except Exception as e:
        logger.error("Follow-up checker failed: %s", e)
