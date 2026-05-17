"""
telegram_handlers.py — All Telegram command and inline button callback handlers.
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

import config as cfg
import db
import reviewers as reviewers_mod
import state

logger = logging.getLogger(__name__)


def _resolve_submission(arg: str):
    """Resolve a CLI arg to (sub, error_message, ambiguous_matches).

    Accepts a numeric sub_id (optionally prefixed with '#') or a title keyword.
    Returns (sub_dict, None, None) on unique match,
            (None, error_text, None) when nothing matches,
            (None, None, matches_list) on keyword ambiguity.
    """
    arg = arg.strip().strip('"').strip("'")
    id_candidate = arg.lstrip("#")
    if id_candidate.isdigit():
        sub = db.get_submission_by_id(int(id_candidate))
        if not sub:
            return None, f"No submission found with ID #{id_candidate}.", None
        return sub, None, None

    matches = db.get_submission_by_title_keyword(arg)
    if not matches:
        return None, f"No active submission found matching '{arg}'.", None
    if len(matches) > 1:
        return None, None, matches
    return matches[0], None, None


# ── /getid ────────────────────────────────────────────────────────────────────

async def cmd_getid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id if user else "N/A"
    username = f"@{user.username}" if user and user.username else str(user_id)
    await update.message.reply_text(
        f"Chat ID: {chat_id}\nYour user ID: {user_id} ({username})"
    )


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    active = db.get_active_submissions()
    if not active:
        await update.message.reply_text("No active submissions right now.")
        return

    lines = ["Active Submissions:\n"]
    for sub in active:
        assignments = db.get_assignments_for_submission(sub["id"])
        reviewers = ", ".join(
            f"@{a['reviewer_tg_username']} ({a['status']})"
            for a in assignments
        )
        lines.append(
            f"#{sub['id']} — 《{sub['title']}》\n"
            f"Status: {sub['status']}\n"
            f"Reviewers: {reviewers or 'none'}\n"
        )

    await update.message.reply_text("\n".join(lines))


# ── /done <sub_id|keyword> ────────────────────────────────────────────────────

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not user.username:
        await update.message.reply_text(
            "You must have a Telegram username to use this command."
        )
        return

    if not context.args:
        await update.message.reply_text("Usage: /done <sub_id|keyword>")
        return

    target = " ".join(context.args).strip().strip('"').strip("'")

    sub, err, ambiguous = _resolve_submission(target)
    if ambiguous:
        listing = "\n".join(f"#{s['id']}: 《{s['title']}》" for s in ambiguous)
        await update.message.reply_text(
            f"Multiple submissions match '{target}':\n{listing}\n\n"
            f"Re-run with a sub_id (e.g. /done #{ambiguous[0]['id']})."
        )
        return
    if err:
        await update.message.reply_text(err)
        return

    config = cfg.load()
    answer = await state.handle_reviewer_done(
        sub_id=sub["id"],
        username=user.username,
        tg_user_id=user.id,
        bot=context.bot,
        config=config,
    )
    await update.message.reply_text(answer)


# ── /reject <sub_id|keyword> <reason> ─────────────────────────────────────────

async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not user.username:
        await update.message.reply_text(
            "You must have a Telegram username to use this command."
        )
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /reject <sub_id|keyword> <reason>")
        return

    target = context.args[0]
    reason = " ".join(context.args[1:])

    sub, err, ambiguous = _resolve_submission(target)
    if ambiguous:
        listing = "\n".join(f"#{s['id']}: 《{s['title']}》" for s in ambiguous)
        await update.message.reply_text(
            f"Multiple submissions match '{target}':\n{listing}\n\n"
            f"Re-run with a sub_id (e.g. /reject #{ambiguous[0]['id']} <reason>)."
        )
        return
    if err:
        await update.message.reply_text(err)
        return

    config = cfg.load()
    await state.handle_rejection_proposal(
        sub_id=sub["id"],
        proposed_by=user.username,
        reason=reason,
        bot=context.bot,
        config=config,
    )


# ── /second <sub_id|keyword> ──────────────────────────────────────────────────

async def cmd_second(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not user.username:
        await update.message.reply_text(
            "You must have a Telegram username to use this command."
        )
        return

    if not context.args:
        await update.message.reply_text("Usage: /second <sub_id|keyword>")
        return

    target = " ".join(context.args).strip()

    sub, err, ambiguous = _resolve_submission(target)
    if ambiguous:
        listing = "\n".join(f"#{s['id']}: 《{s['title']}》" for s in ambiguous)
        await update.message.reply_text(
            f"Multiple submissions match '{target}':\n{listing}\n\n"
            f"Re-run with a sub_id (e.g. /second #{ambiguous[0]['id']})."
        )
        return
    if err:
        await update.message.reply_text(err)
        return

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


# ── /drop <sub_id> @user ─────────────────────────────────────────────────────

async def cmd_drop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")

    if operator_user_id and (not user or user.id != operator_user_id):
        await update.message.reply_text("Only the operator can use /drop.")
        return

    if not context.args or len(context.args) != 2:
        await update.message.reply_text("Usage: /drop <sub_id> @user")
        return

    try:
        sub_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("sub_id must be a number.")
        return

    username = context.args[1].lstrip("@").strip()
    if not username:
        await update.message.reply_text("Username is required.")
        return

    answer = await state.handle_drop(
        sub_id=sub_id,
        username=username,
        bot=context.bot,
        config=config,
    )
    await update.message.reply_text(answer)


# ── /reviewers ────────────────────────────────────────────────────────────────

async def cmd_reviewers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current contents of reviewers.md (operator only)."""
    user = update.effective_user
    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")

    if operator_user_id and (not user or user.id != operator_user_id):
        await update.message.reply_text("Only the operator can use /reviewers.")
        return

    path = config.get("reviewers_file", "./reviewers.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        await update.message.reply_text(
            f"reviewers.md not found at {path}."
        )
        return
    except OSError as e:
        await update.message.reply_text(f"Failed to read {path}: {e}")
        return

    header = f"📋 {path} ({len(content)} chars)\n\n"
    # Telegram hard limit is 4096; leave headroom for the header/fences.
    chunk_size = 3800
    body = content or "(empty)"
    first = True
    for i in range(0, len(body), chunk_size):
        piece = body[i:i + chunk_size]
        prefix = header if first else ""
        await update.message.reply_text(f"{prefix}```\n{piece}\n```",
                                        parse_mode="Markdown")
        first = False


def _reviewers_path(config: dict) -> str:
    return config.get("reviewers_file", "./reviewers.md")


async def cmd_add_reviewer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Operator: /add_reviewer <category_keyword> <@username>"""
    user = update.effective_user
    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")
    if operator_user_id and (not user or user.id != operator_user_id):
        await update.message.reply_text("Only the operator can use /add_reviewer.")
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /add_reviewer <category_keyword> <@username>\n"
            "Example: /add_reviewer layer2 @alice\n"
            "Use /list_categories to see available categories."
        )
        return

    keyword = " ".join(context.args[:-1]).strip()
    username = context.args[-1].strip()

    path = _reviewers_path(config)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        await update.message.reply_text(f"Failed to read {path}: {e}")
        return

    try:
        new_content, matched = reviewers_mod.add_reviewer(content, keyword, username)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        await update.message.reply_text(f"Failed to write {path}: {e}")
        return

    await update.message.reply_text(
        f"✅ Added @{username.lstrip('@')} to 《{matched}》."
    )


async def cmd_remove_reviewer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Operator: /remove_reviewer <@username> — removes from all categories."""
    user = update.effective_user
    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")
    if operator_user_id and (not user or user.id != operator_user_id):
        await update.message.reply_text("Only the operator can use /remove_reviewer.")
        return

    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /remove_reviewer <@username>")
        return

    username = context.args[0].strip()

    path = _reviewers_path(config)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        await update.message.reply_text(f"Failed to read {path}: {e}")
        return

    try:
        new_content, affected = reviewers_mod.remove_reviewer(content, username)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        await update.message.reply_text(f"Failed to write {path}: {e}")
        return

    cats = ", ".join(f"《{c}》" for c in affected)
    await update.message.reply_text(
        f"✅ Removed @{username.lstrip('@')} from: {cats}"
    )


async def cmd_list_categories(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Operator: /list_categories — show category headings for /add_reviewer."""
    user = update.effective_user
    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")
    if operator_user_id and (not user or user.id != operator_user_id):
        await update.message.reply_text("Only the operator can use /list_categories.")
        return

    path = _reviewers_path(config)
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        await update.message.reply_text(f"Failed to read {path}: {e}")
        return

    cats = reviewers_mod.list_subcategories(content)
    if not cats:
        await update.message.reply_text("No categories found in reviewers.md.")
        return
    lines = "\n".join(f"• {c}" for c in cats)
    await update.message.reply_text(f"Categories:\n{lines}")


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

    total_len = db.append_content_request_text(sub_id, article_content)
    await update.message.reply_text(
        f"📝 Appended {len(article_content)} chars to 《{sub['title']}》 "
        f"(total: {total_len}).\n\n"
        f"Send more with /content {sub_id} <text> or finalize with "
        f"/content_done {sub_id}."
    )


# ── /content_done <sub_id> ───────────────────────────────────────────────────

async def cmd_content_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")

    if operator_user_id and (not user or user.id != operator_user_id):
        await update.message.reply_text("Only the operator can use /content_done.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /content_done <sub_id>")
        return

    try:
        sub_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("sub_id must be a number.")
        return

    sub = db.get_submission_by_id(sub_id)
    if not sub or sub["status"] != "pending_content":
        await update.message.reply_text(
            f"No pending content request for submission #{sub_id}."
        )
        return

    article_content = db.get_content_request_text(sub_id)
    if not article_content:
        await update.message.reply_text(
            f"No content buffered for #{sub_id}. Use /content first, "
            f"or /skip {sub_id} to proceed without content."
        )
        return

    await update.message.reply_text(
        f"✅ Finalizing content for 《{sub['title']}》 "
        f"({len(article_content)} chars). Assigning reviewers…"
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


# ── /omit <sub_id> [reason] ───────────────────────────────────────────────────

async def cmd_omit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Terminate a submission so the bot ignores it entirely.

    Used when the operator wants to drop a submission without running it
    through the review pipeline (e.g. a production email received while the
    bot is running in testing mode, or vice versa).
    """
    user = update.effective_user
    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")

    if operator_user_id and (not user or user.id != operator_user_id):
        await update.message.reply_text("Only the operator can use /omit.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /omit <sub_id> [reason]")
        return

    try:
        sub_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("sub_id must be a number.")
        return

    reason = " ".join(context.args[1:]).strip()

    sub = db.get_submission_by_id(sub_id)
    if not sub:
        await update.message.reply_text(f"Submission #{sub_id} not found.")
        return

    if sub["status"] in ("accepted", "rejected", "omitted"):
        await update.message.reply_text(
            f"Submission #{sub_id} is already {sub['status']}; nothing to omit."
        )
        return

    if db.has_content_request(sub_id):
        db.delete_content_request(sub_id)
    db.update_submission_status(sub_id, "omitted")

    suffix = f" — {reason}" if reason else ""
    await update.message.reply_text(
        f"🗑 Omitted 《{sub['title']}》 (#{sub_id}){suffix}. The bot will ignore it."
    )


# ── /delete <sub_id> ──────────────────────────────────────────────────────────

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Operator escape hatch: hard-delete a submission and every dependent row.

    Used to redo the review process from scratch when something has gone
    wrong. The bot will re-ingest the email on the next Gmail poll because
    the watermark is rewound past the deleted submission's created_at.
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    user = update.effective_user
    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")

    if operator_user_id and (not user or user.id != operator_user_id):
        await update.message.reply_text("Only the operator can use /delete.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /delete <sub_id>")
        return

    try:
        sub_id = int(context.args[0].lstrip("#"))
    except ValueError:
        await update.message.reply_text("sub_id must be a number.")
        return

    sub = db.get_submission_by_id(sub_id)
    if not sub:
        await update.message.reply_text(f"Submission #{sub_id} not found.")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🗑 Confirm delete", callback_data=f"confirm_delete_{sub_id}"
        )
    ]])
    await update.message.reply_text(
        f"⚠️ This will permanently delete submission #{sub_id} 《{sub['title']}》 "
        f"and all related assignments, follow-ups, rejections, and history.\n\n"
        f"The next Gmail poll will re-ingest the email and start the review "
        f"process over from scratch.",
        reply_markup=keyboard,
    )


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


# ── Button: confirm_delete_<sub_id> ──────────────────────────────────────────

async def cb_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from datetime import datetime, timezone

    query = update.callback_query
    parts = query.data.split("_")
    if len(parts) < 3:
        await query.answer()
        return

    try:
        sub_id = int(parts[-1])
    except ValueError:
        await query.answer()
        return

    user = query.from_user
    config = cfg.load()
    operator_user_id = config["telegram"].get("operator_user_id")
    if operator_user_id and user.id != operator_user_id:
        await query.answer("Only the operator can confirm deletion.", show_alert=True)
        return

    sub = db.get_submission_by_id(sub_id)
    if not sub:
        await query.answer("Already deleted.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    title = sub["title"]

    # Rewind the Gmail watermark so the next poll re-ingests this email.
    created_at = sub["created_at"]
    if created_at:
        try:
            dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            target_ts = dt.timestamp() - 60  # 1 minute buffer
            current = db.get_state("last_gmail_checked_ts")
            if current is None or float(current) > target_ts:
                db.set_state("last_gmail_checked_ts", str(target_ts))
        except ValueError:
            logger.warning("Could not parse created_at=%r for sub #%d", created_at, sub_id)

    db.delete_submission(sub_id)

    await query.answer("🗑 Deleted.")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    group_chat_id = config["telegram"].get("group_chat_id")
    if group_chat_id:
        await context.bot.send_message(
            chat_id=group_chat_id,
            text=(
                f"🗑 Submission #{sub_id} 《{title}》 has been removed by the "
                f"operator. It will be processed and the review will start "
                f"over from the beginning."
            ),
        )
