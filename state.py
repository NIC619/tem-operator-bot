"""
state.py — State machine transitions and business logic for the TEM review bot.

All functions that change submission state live here. They call db.py for
persistence and return data/messages for the Telegram layer to post.
"""
import json
import logging
from datetime import datetime, timedelta

import pytz

import db
import llm

logger = logging.getLogger(__name__)


# ── Publish Date ─────────────────────────────────────────────────────────────

def compute_publish_date(timezone_str: str = "Asia/Taipei",
                         publish_time_str: str = "09:30") -> datetime:
    tz = pytz.timezone(timezone_str)
    now = datetime.now(tz)
    candidate = now + timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=Saturday, 6=Sunday
        candidate += timedelta(days=1)
    hour, minute = int(publish_time_str.split(":")[0]), int(publish_time_str.split(":")[1])
    publish_dt = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return publish_dt


# ── New Submission ────────────────────────────────────────────────────────────

async def handle_new_submission(email_data: dict, bot, config: dict) -> None:
    """
    Called by the Gmail poller when a new submission email arrives.
    If operator_user_id is set: insert as pending_content, DM operator for draft.
    Otherwise: proceed directly to LLM assignment.
    """
    # Avoid duplicate processing
    existing = db.get_submission_by_gmail_id(email_data["gmail_message_id"])
    if existing:
        logger.info("Submission %s already processed, skipping.",
                    email_data["gmail_message_id"])
        return

    sub_id = db.insert_submission(
        gmail_message_id=email_data["gmail_message_id"],
        gmail_thread_id=email_data.get("gmail_thread_id"),
        title=email_data["title"],
        author_name=email_data.get("author_name", ""),
        author_email=email_data["author_email"],
        medium_url=email_data.get("medium_url"),
        email_subject=email_data["email_subject"],
        email_body=email_data.get("email_body", ""),
    )
    logger.info("Inserted submission #%d: %s", sub_id, email_data["title"])

    operator_user_id = config["telegram"].get("operator_user_id")
    if not operator_user_id:
        logger.warning(
            "operator_user_id not set — skipping content request, assigning directly."
        )
        await _proceed_with_assignment(sub_id, email_data, "", bot, config)
        return

    # Set status to pending_content and DM the operator
    db.update_submission_status(sub_id, "pending_content")
    deadline = datetime.now() + timedelta(hours=24)
    db.insert_content_request(sub_id, deadline)

    tz = pytz.timezone(config["workflow"].get("publish_timezone", "Asia/Taipei"))
    deadline_local = deadline.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")

    dm_text = (
        f"📝 New submission #{sub_id}: 《{email_data['title']}》\n"
        f"Author: {email_data.get('author_name', '')} ({email_data['author_email']})\n\n"
        f"Please paste the article draft content so I can assign the best reviewers:\n"
        f"/content {sub_id} <paste full text here>\n\n"
        f"Or type /skip {sub_id} to assign based on title alone.\n"
        f"Deadline: {deadline_local} (24h)"
    )
    try:
        await bot.send_message(chat_id=operator_user_id, text=dm_text)
    except Exception as e:
        logger.error(
            "Failed to DM operator for submission #%d: %s — falling back to group notice.",
            sub_id, e,
        )
        group_chat_id = config["telegram"]["group_chat_id"]
        try:
            await bot.send_message(
                chat_id=group_chat_id,
                text=(
                    f"⚠️ Operator: please start a private chat with this bot and send:\n"
                    f"`/content {sub_id} <article text>`\n"
                    f"or `/skip {sub_id}` to assign based on title alone.\n"
                    f"(Submission #{sub_id}: 《{email_data['title']}》)"
                ),
                parse_mode="Markdown",
            )
        except Exception as group_err:
            logger.error("Failed to send group fallback notice: %s", group_err)


async def _proceed_with_assignment(sub_id: int, email_data: dict,
                                    article_content: str, bot, config: dict) -> None:
    """
    Call LLM, post group announcement, and send reviewer buttons.
    Called after content is received, skipped, or timed out.
    """
    try:
        assignment = await llm.pick_reviewers(email_data, config=config,
                                               article_content=article_content)
    except Exception as e:
        logger.error("LLM reviewer assignment failed: %s", e)
        group_chat_id = config["telegram"]["group_chat_id"]
        await bot.send_message(
            chat_id=group_chat_id,
            text=(
                f"⚠️ New submission received but automatic reviewer assignment failed.\n"
                f"《{email_data['title']}》\n"
                f"Please assign reviewers manually with /override {sub_id} @user1 @user2"
            ),
        )
        return

    category = assignment.get("category", "")
    reason_zh = assignment.get("reason_zh", "")
    # reviewer2 may be "" if only one reviewer is available for this category
    reviewers = [r.strip() for r in [assignment["reviewer1"], assignment.get("reviewer2", "")] if r and r.strip()]

    for r in reviewers:
        db.insert_assignment(sub_id, r)
    db.update_submission_status(sub_id, "assigning")

    group_chat_id = config["telegram"]["group_chat_id"]

    # Post announcement message
    medium_line = f"\n{email_data['medium_url']}" if email_data.get("medium_url") else ""
    reviewers_mention = " ".join(f"@{r}" for r in reviewers)
    announcement = (
        f"📬 New submission received\n\n"
        f"《{email_data['title']}》\n"
        f"Author: {email_data.get('author_name', '')} ({email_data['author_email']})"
        f"{medium_line}\n\n"
        f"Suggested reviewer(s) (based on topic: {category}):\n"
        f"{reviewers_mention}\n\n"
        f"Reason: {reason_zh}"
    )
    await bot.send_message(chat_id=group_chat_id, text=announcement)

    # Post reviewer request with inline buttons (one row per reviewer)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"✅ @{r} — Yes", callback_data=f"accept_{sub_id}_{r}"),
            InlineKeyboardButton(f"❌ @{r} — Can't", callback_data=f"decline_{sub_id}_{r}"),
        ]
        for r in reviewers
    ])
    msg = await bot.send_message(
        chat_id=group_chat_id,
        text=f"{reviewers_mention} — are you available to review 《{email_data['title']}》?",
        reply_markup=keyboard,
    )
    db.set_tg_status_message_id(sub_id, msg.message_id)


async def handle_content_provided(sub_id: int, article_content: str,
                                   bot, config: dict) -> None:
    """
    Called when the operator provides content (/content) or skips (/skip).
    Proceeds to LLM assignment.
    """
    sub = db.get_submission_by_id(sub_id)
    if not sub or sub["status"] != "pending_content":
        return

    db.delete_content_request(sub_id)

    email_data = {
        "gmail_message_id": sub["gmail_message_id"],
        "gmail_thread_id": sub["gmail_thread_id"],
        "title": sub["title"],
        "author_name": sub["author_name"],
        "author_email": sub["author_email"],
        "medium_url": sub["medium_url"],
        "email_subject": sub["email_subject"],
        "email_body": sub["email_body"],
    }
    await _proceed_with_assignment(sub_id, email_data, article_content, bot, config)


async def handle_content_timeout(sub_id: int, bot, config: dict) -> None:
    """
    Called by the scheduler when a content request deadline expires.
    Proceeds to LLM assignment without content.
    """
    sub = db.get_submission_by_id(sub_id)
    if not sub or sub["status"] != "pending_content":
        return  # Already handled by operator

    logger.info("Content request timed out for submission #%d, proceeding without content.", sub_id)
    db.delete_content_request(sub_id)

    email_data = {
        "gmail_message_id": sub["gmail_message_id"],
        "gmail_thread_id": sub["gmail_thread_id"],
        "title": sub["title"],
        "author_name": sub["author_name"],
        "author_email": sub["author_email"],
        "medium_url": sub["medium_url"],
        "email_subject": sub["email_subject"],
        "email_body": sub["email_body"],
    }
    await _proceed_with_assignment(sub_id, email_data, "", bot, config)


# ── Reviewer Accept / Decline ─────────────────────────────────────────────────

async def handle_reviewer_accept(sub_id: int, username: str, tg_user_id: int,
                                  bot, config: dict) -> str:
    """
    Returns a string to show in the callback answer (toast).
    Posts group messages and transitions state as needed.
    """
    assignment = db.get_assignment(sub_id, username)
    if not assignment:
        return "Assignment not found."

    if assignment["status"] != "pending":
        return "Already recorded!"

    db.update_assignment_status(sub_id, username, "confirmed", tg_user_id)

    all_assignments = db.get_assignments_for_submission(sub_id)
    active = [a for a in all_assignments if a["status"] != "declined"]
    still_pending = [a for a in active if a["status"] == "pending"]

    # Transition when every active slot is confirmed (works for 1 or 2 reviewers)
    if not still_pending and active:
        await _transition_to_under_review(sub_id, bot, config)

    return "✅ Confirmed! Thank you."


async def handle_reviewer_decline(sub_id: int, username: str, tg_user_id: int,
                                   bot, config: dict) -> str:
    assignment = db.get_assignment(sub_id, username)
    if not assignment:
        return "Assignment not found."

    if assignment["status"] not in ("pending",):
        return "Already recorded!"

    db.update_assignment_status(sub_id, username, "declined", tg_user_id)

    sub = db.get_submission_by_id(sub_id)
    group_chat_id = config["telegram"]["group_chat_id"]

    # Find a replacement
    existing_assignments = db.get_assignments_for_submission(sub_id)
    excluded = [a["reviewer_tg_username"] for a in existing_assignments]

    email_data = {
        "gmail_message_id": sub["gmail_message_id"],
        "title": sub["title"],
        "author_name": sub["author_name"],
        "author_email": sub["author_email"],
        "medium_url": sub["medium_url"],
        "email_subject": sub["email_subject"],
        "email_body": sub["email_body"],
    }

    try:
        assignment_result = await llm.pick_replacement_reviewer(
            email_data=email_data,
            declined_username=username,
            excluded_usernames=excluded,
        )
        new_reviewer = assignment_result["reviewer1"].strip()
        if not new_reviewer or new_reviewer.lower() in [e.lower() for e in excluded]:
            raise ValueError(f"LLM returned excluded or empty reviewer: '{new_reviewer}'")
        db.insert_assignment(sub_id, new_reviewer)

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"✅ @{new_reviewer} — Yes",
                callback_data=f"accept_{sub_id}_{new_reviewer}"
            ),
            InlineKeyboardButton(
                f"❌ @{new_reviewer} — Can't",
                callback_data=f"decline_{sub_id}_{new_reviewer}"
            ),
        ]])
        await bot.send_message(
            chat_id=group_chat_id,
            text=(
                f"⚠️ @{username} is not available for 《{sub['title']}》.\n\n"
                f"Suggested replacement: @{new_reviewer}"
            ),
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error("Replacement assignment failed: %s", e)
        # Build a pre-filled /override command showing already-confirmed reviewers
        confirmed = db.get_confirmed_reviewers(sub_id)
        confirmed_mentions = " ".join(f"@{a['reviewer_tg_username']}" for a in confirmed)
        override_example = f"/override {sub_id} {confirmed_mentions} @new_reviewer".strip()
        await bot.send_message(
            chat_id=group_chat_id,
            text=(
                f"⚠️ @{username} is not available for 《{sub['title']}》 "
                f"and no replacement could be found automatically.\n\n"
                f"Please assign a replacement manually:\n"
                f"`{override_example}`\n\n"
                f"Replace `@new_reviewer` with the actual username. The number `{sub_id}` is the submission ID."
            ),
            parse_mode="Markdown",
        )

    return "Noted. Looking for a replacement."


# ── Transition to Under Review ────────────────────────────────────────────────

async def _transition_to_under_review(sub_id: int, bot, config: dict) -> None:
    from gmail_client import GmailClient
    import config as cfg

    import asyncio
    sub = db.get_submission_by_id(sub_id)
    confirmed = db.get_confirmed_reviewers(sub_id)
    reviewers = [a["reviewer_tg_username"] for a in confirmed]

    db.update_submission_status(sub_id, "under_review")

    group_chat_id = config["telegram"]["group_chat_id"]

    # Schedule first follow-up
    followup_days = config["workflow"]["followup_interval_days"]
    next_followup = datetime.now() + timedelta(days=followup_days)
    db.insert_followup(sub_id, next_followup)

    # Send "under review" email to submitter (run blocking Gmail call in thread)
    try:
        gmail = GmailClient()
        await asyncio.to_thread(gmail.send_under_review_email, dict(sub))
    except Exception as e:
        logger.error("Failed to send under-review email: %s", e)

    # Post group message with done buttons (one per reviewer)
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"✅ Mark my review as done — @{r}",
            callback_data=f"done_{sub_id}_{r}"
        )]
        for r in reviewers
    ])
    reviewer_lines = "\n".join(f"Reviewer {i+1}: @{r}" for i, r in enumerate(reviewers))
    count_word = "All reviewers" if len(reviewers) > 1 else "Reviewer"
    msg = await bot.send_message(
        chat_id=group_chat_id,
        text=(
            f"✅ {count_word} confirmed for 《{sub['title']}》\n"
            f"{reviewer_lines}\n\n"
            f"Submission status updated to \"Under Review\". Author has been notified.\n\n"
            f"When you've finished your review, click the button below or "
            f"type /done <keyword> (use any word from the title)"
        ),
        reply_markup=keyboard,
    )
    db.set_tg_status_message_id(sub_id, msg.message_id)


# ── Reviewer Done ─────────────────────────────────────────────────────────────

async def handle_reviewer_done(sub_id: int, username: str, tg_user_id: int,
                                bot, config: dict) -> str:
    sub = db.get_submission_by_id(sub_id)
    if not sub:
        return "Submission not found."

    if sub["status"] not in ("under_review", "assigning"):
        return "This submission is not currently under review."

    assignment = db.get_assignment(sub_id, username)
    if not assignment:
        return f"You (@{username}) are not assigned to this submission."

    if assignment["status"] == "done":
        return "Already recorded!"

    db.mark_assignment_done(sub_id, username, tg_user_id)

    done_reviewers = db.get_done_reviewers(sub_id)
    confirmed_reviewers = db.get_confirmed_reviewers(sub_id)
    # After marking done, re-fetch
    all_assignments = db.get_assignments_for_submission(sub_id)
    # Active reviewers = confirmed + done (not declined, not still pending)
    active = [a for a in all_assignments if a["status"] in ("confirmed", "done")]
    done_list = [a for a in active if a["status"] == "done"]

    group_chat_id = config["telegram"]["group_chat_id"]

    # All active reviewers finished → accept
    if active and len(done_list) >= len(active):
        await _transition_to_accepted(sub_id, done_list, bot, config)
    else:
        still_pending = [
            a["reviewer_tg_username"] for a in active
            if a["status"] == "confirmed" and a["reviewer_tg_username"] != username
        ]
        if still_pending:
            waiting = ", ".join(f"@{u}" for u in still_pending)
            await bot.send_message(
                chat_id=group_chat_id,
                text=(
                    f"✅ @{username} has finished their review of 《{sub['title']}》.\n\n"
                    f"Waiting on {waiting} to complete theirs."
                ),
            )

    return "✅ Review marked as done!"


async def _transition_to_accepted(sub_id: int, done_assignments: list,
                                   bot, config: dict) -> None:
    from gmail_client import GmailClient

    sub = db.get_submission_by_id(sub_id)
    publish_dt = compute_publish_date(
        timezone_str=config["workflow"].get("publish_timezone", "Asia/Taipei"),
        publish_time_str=config["workflow"].get("publish_time", "09:30"),
    )
    publish_date_str = publish_dt.strftime("%Y-%m-%d")

    db.set_submission_accepted(sub_id, publish_date_str)

    group_chat_id = config["telegram"]["group_chat_id"]
    await bot.send_message(
        chat_id=group_chat_id,
        text=(
            f"🎉 Both reviews complete for 《{sub['title']}》!\n\n"
            f"Scheduled to publish: {publish_date_str} at 09:30 (Asia/Taipei)\n\n"
            f"Author has been notified."
        ),
    )

    try:
        import asyncio
        gmail = GmailClient()
        await asyncio.to_thread(gmail.send_acceptance_email, dict(sub), publish_date_str)
    except Exception as e:
        logger.error("Failed to send acceptance email: %s", e)


# ── Rejection Flow ────────────────────────────────────────────────────────────

async def handle_rejection_proposal(sub_id: int, proposed_by: str,
                                     reason: str, bot, config: dict) -> None:
    sub = db.get_submission_by_id(sub_id)
    group_chat_id = config["telegram"]["group_chat_id"]

    rejection_id = db.insert_rejection(sub_id, proposed_by, reason)

    title_keyword = sub["title"].split()[0].lower() if sub["title"] else str(sub_id)
    msg = await bot.send_message(
        chat_id=group_chat_id,
        text=(
            f"🚫 @{proposed_by} has proposed rejecting 《{sub['title']}》\n\n"
            f"Reason: {reason}\n\n"
            f"Two more people need to second this. "
            f"Type /second {title_keyword} to agree.\n"
            f"(0/2 seconds so far)"
        ),
    )
    db.set_rejection_proposal_message_id(rejection_id, msg.message_id)


async def handle_second(sub_id: int, username: str, bot, config: dict) -> str:
    rejection = db.get_active_rejection(sub_id)
    if not rejection:
        return "No active rejection proposal for this submission."

    if username == rejection["proposed_by"]:
        return "You can't second your own rejection proposal."

    rejection_id = rejection["id"]
    seconds = db.add_second_to_rejection(rejection_id, username)

    sub = db.get_submission_by_id(sub_id)
    group_chat_id = config["telegram"]["group_chat_id"]
    seconds_text = ", ".join(f"@{s}" for s in seconds)
    count = len(seconds)

    # Edit the original proposal message
    text = (
        f"🚫 @{rejection['proposed_by']} has proposed rejecting 《{sub['title']}》\n\n"
        f"Reason: {rejection['reason']}\n\n"
        f"/second supporters: {seconds_text}\n"
        f"({count}/2 seconds)"
    )

    if count >= 2:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "✅ Confirm and send rejection email",
                callback_data=f"confirm_rejection_{sub_id}"
            )
        ]])
        await bot.edit_message_text(
            chat_id=group_chat_id,
            message_id=rejection["tg_proposal_message_id"],
            text=text,
            reply_markup=keyboard,
        )
        return "Second recorded. Waiting for operator confirmation."
    else:
        await bot.edit_message_text(
            chat_id=group_chat_id,
            message_id=rejection["tg_proposal_message_id"],
            text=text,
        )
        return f"Second recorded ({count}/2)."


async def handle_confirm_rejection(sub_id: int, operator_tg_id: int,
                                    bot, config: dict) -> str:
    from gmail_client import GmailClient

    operator_user_id = config["telegram"].get("operator_user_id")
    if operator_user_id and operator_tg_id != operator_user_id:
        return "Only the operator can confirm rejection."

    rejection = db.get_active_rejection(sub_id)
    sub = db.get_submission_by_id(sub_id)

    db.set_submission_rejected(sub_id)

    group_chat_id = config["telegram"]["group_chat_id"]
    await bot.send_message(
        chat_id=group_chat_id,
        text=f"🚫 《{sub['title']}》 has been rejected. Author has been notified.",
    )

    try:
        import asyncio
        gmail = GmailClient()
        reason = rejection["reason"] if rejection else ""
        await asyncio.to_thread(gmail.send_rejection_email, dict(sub), reason)
    except Exception as e:
        logger.error("Failed to send rejection email: %s", e)

    return "Rejection confirmed."


# ── Operator Override ─────────────────────────────────────────────────────────

async def handle_override(sub_id: int, new_reviewers: list[str],
                           bot, config: dict) -> str:
    sub = db.get_submission_by_id(sub_id)
    if not sub:
        return f"Submission #{sub_id} not found."

    db.clear_pending_assignments(sub_id)
    for username in new_reviewers:
        db.insert_assignment(sub_id, username)
    db.update_submission_status(sub_id, "assigning")

    group_chat_id = config["telegram"]["group_chat_id"]
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    rows = []
    for username in new_reviewers:
        rows.append([
            InlineKeyboardButton(
                f"✅ @{username} — Yes",
                callback_data=f"accept_{sub_id}_{username}"
            ),
            InlineKeyboardButton(
                f"❌ @{username} — Can't",
                callback_data=f"decline_{sub_id}_{username}"
            ),
        ])

    keyboard = InlineKeyboardMarkup(rows)
    reviewers_str = " ".join(f"@{u}" for u in new_reviewers)
    await bot.send_message(
        chat_id=group_chat_id,
        text=(
            f"🔧 Reviewer override for 《{sub['title']}》\n\n"
            f"{reviewers_str} — are you available to review 《{sub['title']}》?"
        ),
        reply_markup=keyboard,
    )
    return f"Override applied. New reviewers: {reviewers_str}"


# ── Follow-up ─────────────────────────────────────────────────────────────────

async def send_followup(followup_row, bot, config: dict) -> None:
    sub_id = followup_row["submission_id"]
    sub = db.get_submission_by_id(sub_id)
    if not sub or sub["status"] != "under_review":
        return

    confirmed = db.get_confirmed_reviewers(sub_id)
    all_assignments = db.get_assignments_for_submission(sub_id)
    active = [a for a in all_assignments if a["status"] in ("confirmed", "done")]

    if not active:
        return

    reviewers = [a["reviewer_tg_username"] for a in active]

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"✅ Mark my review as done — @{r}",
            callback_data=f"done_{sub_id}_{r}"
        )]
        for r in reviewers
    ])

    reviewers_mention = " ".join(f"@{r}" for r in reviewers)
    group_chat_id = config["telegram"]["group_chat_id"]
    await bot.send_message(
        chat_id=group_chat_id,
        text=(
            f"👋 Friendly check-in for 《{sub['title']}》\n\n"
            f"{reviewers_mention} — how's the review coming along?\n\n"
            f"Tap your button when you're done, or let us know if you need more time."
        ),
        reply_markup=keyboard,
    )

    db.mark_followup_sent(followup_row["id"])

    followup_days = config["workflow"]["followup_interval_days"]
    next_followup = datetime.now() + timedelta(days=followup_days)
    db.insert_followup(sub_id, next_followup)
