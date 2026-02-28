"""
telegram_handlers.py — All Telegram command and inline button callback handlers.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

import config as cfg
import db
import state

logger = logging.getLogger(__name__)


# ── /getid ────────────────────────────────────────────────────────────────────

async def cmd_getid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id if user else "N/A"
    username = f"@{user.username}" if user and user.username else str(user_id)
    await update.message.reply_text(
        f"Chat ID: `{chat_id}`\nYour user ID: `{user_id}` ({username})",
        parse_mode="Markdown",
    )


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    active = db.get_active_submissions()
    if not active:
        await update.message.reply_text("No active submissions right now.")
        return

    lines = ["*Active Submissions:*\n"]
    for sub in active:
        assignments = db.get_assignments_for_submission(sub["id"])
        reviewers = ", ".join(
            f"@{a['reviewer_tg_username']} ({a['status']})"
            for a in assignments
        )
        lines.append(
            f"*#{sub['id']}* — 《{sub['title']}》\n"
            f"Status: `{sub['status']}`\n"
            f"Reviewers: {reviewers or 'none'}\n"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── /done <keyword> ───────────────────────────────────────────────────────────

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not user.username:
        await update.message.reply_text(
            "You must have a Telegram username to use this command."
        )
        return

    if not context.args:
        await update.message.reply_text("Usage: /done <keyword>")
        return

    keyword = " ".join(context.args).strip().strip('"').strip("'")
    matches = db.get_submission_by_title_keyword(keyword)

    if not matches:
        await update.message.reply_text(
            f"No active submission found matching '{keyword}'."
        )
        return

    if len(matches) > 1:
        listing = "\n".join(f"#{s['id']}: 《{s['title']}》" for s in matches)
        await update.message.reply_text(
            f"Multiple submissions match '{keyword}':\n{listing}\n\n"
            f"Please be more specific."
        )
        return

    sub = matches[0]
    config = cfg.load()
    answer = await state.handle_reviewer_done(
        sub_id=sub["id"],
        username=user.username,
        tg_user_id=user.id,
        bot=context.bot,
        config=config,
    )
    await update.message.reply_text(answer)


# ── /reject <keyword> <reason> ────────────────────────────────────────────────

async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not user.username:
        await update.message.reply_text(
            "You must have a Telegram username to use this command."
        )
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /reject <keyword> <reason>")
        return

    keyword = context.args[0]
    reason = " ".join(context.args[1:])

    matches = db.get_submission_by_title_keyword(keyword)
    if not matches:
        await update.message.reply_text(
            f"No active submission found matching '{keyword}'."
        )
        return

    if len(matches) > 1:
        listing = "\n".join(f"#{s['id']}: 《{s['title']}》" for s in matches)
        await update.message.reply_text(
            f"Multiple submissions match '{keyword}':\n{listing}\n\n"
            f"Please be more specific."
        )
        return

    sub = matches[0]
    config = cfg.load()
    await state.handle_rejection_proposal(
        sub_id=sub["id"],
        proposed_by=user.username,
        reason=reason,
        bot=context.bot,
        config=config,
    )


# ── /second <keyword> ─────────────────────────────────────────────────────────

async def cmd_second(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not user.username:
        await update.message.reply_text(
            "You must have a Telegram username to use this command."
        )
        return

    if not context.args:
        await update.message.reply_text("Usage: /second <keyword>")
        return

    keyword = " ".join(context.args).strip()
    matches = db.get_submission_by_title_keyword(keyword)

    if not matches:
        await update.message.reply_text(
            f"No active submission found matching '{keyword}'."
        )
        return

    if len(matches) > 1:
        listing = "\n".join(f"#{s['id']}: 《{s['title']}》" for s in matches)
        await update.message.reply_text(
            f"Multiple submissions match '{keyword}':\n{listing}\n\n"
            f"Please be more specific."
        )
        return

    sub = matches[0]
    config = cfg.load()
    answer = await state.handle_second(
        sub_id=sub["id"],
        username=user.username,
        bot=context.bot,
        config=config,
    )
    await update.message.reply_text(answer)


# ── /override <sub_id> @user1 @user2 ─────────────────────────────────────────

async def cmd_override(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")

    if operator_user_id and (not user or user.id != operator_user_id):
        await update.message.reply_text("Only the operator can use /override.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /override <sub_id> @user1 [@user2]"
        )
        return

    try:
        sub_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("sub_id must be a number.")
        return

    # Strip @ from usernames
    new_reviewers = [a.lstrip("@") for a in context.args[1:]]

    answer = await state.handle_override(
        sub_id=sub_id,
        new_reviewers=new_reviewers,
        bot=context.bot,
        config=config,
    )
    await update.message.reply_text(answer)


# ── /content <sub_id> <text> ──────────────────────────────────────────────────

async def cmd_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")

    if operator_user_id and (not user or user.id != operator_user_id):
        await update.message.reply_text("Only the operator can use /content.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /content <sub_id> <article text>")
        return

    try:
        sub_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("sub_id must be a number.")
        return

    article_content = " ".join(context.args[1:]).strip()
    if not article_content:
        await update.message.reply_text("Article content cannot be empty.")
        return

    sub = db.get_submission_by_id(sub_id)
    if not sub:
        await update.message.reply_text(f"Submission #{sub_id} not found.")
        return

    if sub["status"] != "pending_content":
        await update.message.reply_text(
            f"No pending content request for submission #{sub_id}."
        )
        return

    if not db.has_content_request(sub_id):
        await update.message.reply_text(
            f"No pending content request for submission #{sub_id}."
        )
        return

    await update.message.reply_text(
        f"✅ Content received for 《{sub['title']}》. Assigning reviewers now…"
    )
    await state.handle_content_provided(sub_id, article_content, context.bot, config)


# ── /skip <sub_id> ────────────────────────────────────────────────────────────

async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")

    if operator_user_id and (not user or user.id != operator_user_id):
        await update.message.reply_text("Only the operator can use /skip.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /skip <sub_id>")
        return

    try:
        sub_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("sub_id must be a number.")
        return

    sub = db.get_submission_by_id(sub_id)
    if not sub:
        await update.message.reply_text(f"Submission #{sub_id} not found.")
        return

    if sub["status"] != "pending_content":
        await update.message.reply_text(
            f"No pending content request for submission #{sub_id}."
        )
        return

    if not db.has_content_request(sub_id):
        await update.message.reply_text(
            f"No pending content request for submission #{sub_id}."
        )
        return

    await update.message.reply_text(
        f"⏭ Skipped content for 《{sub['title']}》. Assigning reviewers based on title…"
    )
    await state.handle_content_provided(sub_id, "", context.bot, config)


# ── Button: accept_<sub_id>_<username> ───────────────────────────────────────

async def cb_accept(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    parts = query.data.split("_", 2)  # accept / sub_id / username
    if len(parts) != 3:
        await query.answer()
        return

    _, sub_id_str, target_username = parts
    sub_id = int(sub_id_str)

    user = query.from_user
    if not user.username or user.username.lower() != target_username.lower():
        await query.answer(
            "This button is for @" + target_username + " only.", show_alert=True
        )
        return

    # Answer immediately — Telegram requires a response within 30 seconds.
    # The detailed result is posted to the group chat by state.py.
    await query.answer("✅ Confirmed! Thank you.")
    config = cfg.load()
    await state.handle_reviewer_accept(
        sub_id=sub_id,
        username=target_username,
        tg_user_id=user.id,
        bot=context.bot,
        config=config,
    )


# ── Button: decline_<sub_id>_<username> ──────────────────────────────────────

async def cb_decline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    parts = query.data.split("_", 2)
    if len(parts) != 3:
        await query.answer()
        return

    _, sub_id_str, target_username = parts
    sub_id = int(sub_id_str)

    user = query.from_user
    if not user.username or user.username.lower() != target_username.lower():
        await query.answer(
            "This button is for @" + target_username + " only.", show_alert=True
        )
        return

    await query.answer("Noted. Looking for a replacement.")
    config = cfg.load()
    await state.handle_reviewer_decline(
        sub_id=sub_id,
        username=target_username,
        tg_user_id=user.id,
        bot=context.bot,
        config=config,
    )


# ── Button: done_<sub_id>_<username> ─────────────────────────────────────────

async def cb_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    parts = query.data.split("_", 2)
    if len(parts) != 3:
        await query.answer()
        return

    _, sub_id_str, target_username = parts
    sub_id = int(sub_id_str)

    user = query.from_user
    if not user.username or user.username.lower() != target_username.lower():
        await query.answer(
            "This button is for @" + target_username + " only.", show_alert=True
        )
        return

    await query.answer("✅ Review marked as done!")
    config = cfg.load()
    await state.handle_reviewer_done(
        sub_id=sub_id,
        username=target_username,
        tg_user_id=user.id,
        bot=context.bot,
        config=config,
    )


# ── Button: confirm_rejection_<sub_id> ───────────────────────────────────────

async def cb_confirm_rejection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    parts = query.data.split("_", 2)  # confirm / rejection / sub_id
    # callback_data format: confirm_rejection_<sub_id>
    # Split on first two underscores: ["confirm", "rejection", "<sub_id>"]
    data_parts = query.data.split("_")
    if len(data_parts) < 3:
        await query.answer()
        return

    sub_id = int(data_parts[-1])
    user = query.from_user
    config = cfg.load()

    operator_user_id = config["telegram"].get("operator_user_id")
    if operator_user_id and user.id != operator_user_id:
        await query.answer("Only the operator can confirm rejection.", show_alert=True)
        return

    await query.answer("🚫 Rejection confirmed.")
    await state.handle_confirm_rejection(
        sub_id=sub_id,
        operator_tg_id=user.id,
        bot=context.bot,
        config=config,
    )
