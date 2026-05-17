"""
db.py — SQLite schema and all database queries for the TEM review bot.
"""
import sqlite3
import os
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "./tem_bot.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gmail_message_id TEXT UNIQUE NOT NULL,
                gmail_thread_id TEXT,
                title TEXT NOT NULL,
                author_name TEXT,
                author_email TEXT NOT NULL,
                medium_url TEXT,
                email_subject TEXT,
                email_body TEXT,
                status TEXT NOT NULL DEFAULT 'pending_assignment',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                accepted_at TIMESTAMP,
                rejected_at TIMESTAMP,
                publish_date DATE,
                tg_status_message_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                reviewer_tg_username TEXT NOT NULL,
                reviewer_tg_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                responded_at TIMESTAMP,
                done_at TIMESTAMP,
                FOREIGN KEY (submission_id) REFERENCES submissions(id)
            );

            CREATE TABLE IF NOT EXISTS followups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                scheduled_at TIMESTAMP NOT NULL,
                sent_at TIMESTAMP,
                kind TEXT NOT NULL DEFAULT 'review',
                FOREIGN KEY (submission_id) REFERENCES submissions(id)
            );

            CREATE TABLE IF NOT EXISTS assignment_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER,
                reviewer_tg_username TEXT NOT NULL,
                assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS rejections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                proposed_by TEXT NOT NULL,
                reason TEXT NOT NULL,
                seconds TEXT NOT NULL DEFAULT '[]',
                tg_proposal_message_id INTEGER,
                proposed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (submission_id) REFERENCES submissions(id)
            );

            CREATE TABLE IF NOT EXISTS content_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER UNIQUE NOT NULL REFERENCES submissions(id),
                deadline TIMESTAMP NOT NULL,
                article_content TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status);
            CREATE INDEX IF NOT EXISTS idx_assignments_submission ON assignments(submission_id);
            CREATE INDEX IF NOT EXISTS idx_followups_pending
                ON followups(scheduled_at) WHERE sent_at IS NULL;
            CREATE INDEX IF NOT EXISTS idx_content_requests_deadline
                ON content_requests(deadline);
            CREATE INDEX IF NOT EXISTS idx_assignment_history_assigned_at
                ON assignment_history(assigned_at);
        """)

        # Additive migration: add article_content to existing content_requests
        # tables that predate multi-part content support.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(content_requests)")}
        if "article_content" not in cols:
            conn.execute(
                "ALTER TABLE content_requests ADD COLUMN "
                "article_content TEXT NOT NULL DEFAULT ''"
            )

        # Additive migration: add kind column to existing followups tables.
        fu_cols = {row[1] for row in conn.execute("PRAGMA table_info(followups)")}
        if "kind" not in fu_cols:
            conn.execute(
                "ALTER TABLE followups ADD COLUMN "
                "kind TEXT NOT NULL DEFAULT 'review'"
            )


# ── Submissions ──────────────────────────────────────────────────────────────

def insert_submission(gmail_message_id, gmail_thread_id, title, author_name,
                      author_email, medium_url, email_subject, email_body) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO submissions
               (gmail_message_id, gmail_thread_id, title, author_name,
                author_email, medium_url, email_subject, email_body)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (gmail_message_id, gmail_thread_id, title, author_name,
             author_email, medium_url, email_subject, email_body)
        )
        return cur.lastrowid


def get_submission_by_id(sub_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM submissions WHERE id = ?", (sub_id,)
        ).fetchone()


def get_submission_by_gmail_id(gmail_message_id: str):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM submissions WHERE gmail_message_id = ?",
            (gmail_message_id,)
        ).fetchone()


def get_submission_by_title_keyword(keyword: str):
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM submissions
               WHERE lower(title) LIKE lower(?)
               AND status NOT IN ('accepted', 'rejected', 'omitted')""",
            (f"%{keyword}%",)
        ).fetchall()


def get_active_submissions():
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM submissions WHERE status IN ('assigning', 'under_review')"
        ).fetchall()


def update_submission_status(sub_id: int, status: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE submissions SET status = ? WHERE id = ?", (status, sub_id)
        )


def set_submission_accepted(sub_id: int, publish_date):
    with get_conn() as conn:
        conn.execute(
            """UPDATE submissions
               SET status = 'accepted', accepted_at = CURRENT_TIMESTAMP, publish_date = ?
               WHERE id = ?""",
            (publish_date, sub_id)
        )


def set_submission_rejected(sub_id: int):
    with get_conn() as conn:
        conn.execute(
            """UPDATE submissions
               SET status = 'rejected', rejected_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (sub_id,)
        )


def delete_submission(sub_id: int) -> bool:
    """Hard delete a submission and every dependent row.

    Returns True if a submission row existed and was removed.
    """
    with get_conn() as conn:
        existed = conn.execute(
            "SELECT 1 FROM submissions WHERE id = ?", (sub_id,)
        ).fetchone() is not None
        if not existed:
            return False
        conn.execute("DELETE FROM rejections WHERE submission_id = ?", (sub_id,))
        conn.execute("DELETE FROM followups WHERE submission_id = ?", (sub_id,))
        conn.execute("DELETE FROM assignments WHERE submission_id = ?", (sub_id,))
        conn.execute("DELETE FROM assignment_history WHERE submission_id = ?", (sub_id,))
        conn.execute("DELETE FROM content_requests WHERE submission_id = ?", (sub_id,))
        conn.execute("DELETE FROM submissions WHERE id = ?", (sub_id,))
        return True


def set_tg_status_message_id(sub_id: int, message_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE submissions SET tg_status_message_id = ? WHERE id = ?",
            (message_id, sub_id)
        )


# ── Assignments ──────────────────────────────────────────────────────────────

def _norm_username(u: str) -> str:
    """Normalize Telegram usernames for DB storage/lookup (case-insensitive)."""
    return (u or "").strip().lstrip("@").lower()


def insert_assignment(submission_id: int, reviewer_tg_username: str) -> int:
    reviewer_tg_username = _norm_username(reviewer_tg_username)
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO assignments (submission_id, reviewer_tg_username)
               VALUES (?, ?)""",
            (submission_id, reviewer_tg_username)
        )
        # Also write to history for workload tracking
        conn.execute(
            """INSERT INTO assignment_history (submission_id, reviewer_tg_username)
               VALUES (?, ?)""",
            (submission_id, reviewer_tg_username)
        )
        return cur.lastrowid


def get_assignment(submission_id: int, reviewer_tg_username: str):
    reviewer_tg_username = _norm_username(reviewer_tg_username)
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM assignments
               WHERE submission_id = ? AND reviewer_tg_username = ?
               ORDER BY id DESC LIMIT 1""",
            (submission_id, reviewer_tg_username)
        ).fetchone()


def get_assignments_for_submission(submission_id: int):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM assignments WHERE submission_id = ?", (submission_id,)
        ).fetchall()


def get_confirmed_reviewers(sub_id: int):
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM assignments
               WHERE submission_id = ? AND status = 'confirmed'""",
            (sub_id,)
        ).fetchall()


def get_done_reviewers(sub_id: int):
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM assignments
               WHERE submission_id = ? AND status = 'done'""",
            (sub_id,)
        ).fetchall()


def update_assignment_status(submission_id: int, reviewer_tg_username: str,
                              status: str, reviewer_tg_id: int = None):
    reviewer_tg_username = _norm_username(reviewer_tg_username)
    with get_conn() as conn:
        if reviewer_tg_id is not None:
            conn.execute(
                """UPDATE assignments
                   SET status = ?, responded_at = CURRENT_TIMESTAMP, reviewer_tg_id = ?
                   WHERE submission_id = ? AND reviewer_tg_username = ?""",
                (status, reviewer_tg_id, submission_id, reviewer_tg_username)
            )
        else:
            conn.execute(
                """UPDATE assignments
                   SET status = ?, responded_at = CURRENT_TIMESTAMP
                   WHERE submission_id = ? AND reviewer_tg_username = ?""",
                (status, submission_id, reviewer_tg_username)
            )


def mark_assignment_done(submission_id: int, reviewer_tg_username: str,
                         reviewer_tg_id: int = None):
    reviewer_tg_username = _norm_username(reviewer_tg_username)
    with get_conn() as conn:
        if reviewer_tg_id is not None:
            conn.execute(
                """UPDATE assignments
                   SET status = 'done', done_at = CURRENT_TIMESTAMP, reviewer_tg_id = ?
                   WHERE submission_id = ? AND reviewer_tg_username = ?""",
                (reviewer_tg_id, submission_id, reviewer_tg_username)
            )
        else:
            conn.execute(
                """UPDATE assignments
                   SET status = 'done', done_at = CURRENT_TIMESTAMP
                   WHERE submission_id = ? AND reviewer_tg_username = ?""",
                (submission_id, reviewer_tg_username)
            )


def delete_pending_assignment(submission_id: int, reviewer_tg_username: str) -> bool:
    """Delete a single assignment row only if it's still in 'pending' status.

    Returns True if a row was deleted, False otherwise (not found or already
    confirmed/done/declined).
    """
    with get_conn() as conn:
        cur = conn.execute(
            """DELETE FROM assignments
               WHERE submission_id = ?
                 AND lower(reviewer_tg_username) = lower(?)
                 AND status = 'pending'""",
            (submission_id, reviewer_tg_username)
        )
        return cur.rowcount > 0


def clear_pending_assignments(submission_id: int):
    """Remove pending/declined assignments for operator override."""
    with get_conn() as conn:
        conn.execute(
            """DELETE FROM assignments
               WHERE submission_id = ? AND status IN ('pending', 'declined')""",
            (submission_id,)
        )


# ── Follow-ups ───────────────────────────────────────────────────────────────

def insert_followup(submission_id: int, scheduled_at: datetime,
                    kind: str = "review"):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO followups (submission_id, scheduled_at, kind) "
            "VALUES (?, ?, ?)",
            (submission_id, scheduled_at.isoformat(), kind)
        )


def get_pending_followups(now: datetime):
    with get_conn() as conn:
        return conn.execute(
            """SELECT f.*, s.title, s.status FROM followups f
               JOIN submissions s ON s.id = f.submission_id
               WHERE f.scheduled_at <= ? AND f.sent_at IS NULL
               AND s.status IN ('assigning', 'under_review')""",
            (now.isoformat(),)
        ).fetchall()


def mark_followup_sent(followup_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE followups SET sent_at = CURRENT_TIMESTAMP WHERE id = ?",
            (followup_id,)
        )


def clear_unsent_followups(submission_id: int, kind: str | None = None):
    """Delete unsent follow-ups for a submission, optionally filtered by kind."""
    with get_conn() as conn:
        if kind is None:
            conn.execute(
                "DELETE FROM followups WHERE submission_id = ? AND sent_at IS NULL",
                (submission_id,)
            )
        else:
            conn.execute(
                "DELETE FROM followups WHERE submission_id = ? "
                "AND sent_at IS NULL AND kind = ?",
                (submission_id, kind)
            )


# ── Assignment History ───────────────────────────────────────────────────────

def get_recent_assignment_history(days: int = 90):
    with get_conn() as conn:
        # assigned_at is SQLite's CURRENT_TIMESTAMP (UTC, "YYYY-MM-DD HH:MM:SS").
        # Match that format so string comparison works.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        return conn.execute(
            """SELECT ah.*, s.title FROM assignment_history ah
               LEFT JOIN submissions s ON s.id = ah.submission_id
               WHERE ah.assigned_at >= ?
               ORDER BY ah.assigned_at DESC""",
            (cutoff,)
        ).fetchall()


# ── Rejections ───────────────────────────────────────────────────────────────

def insert_rejection(submission_id: int, proposed_by: str, reason: str,
                     tg_proposal_message_id: int = None) -> int:
    proposed_by = _norm_username(proposed_by)
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO rejections
               (submission_id, proposed_by, reason, tg_proposal_message_id)
               VALUES (?, ?, ?, ?)""",
            (submission_id, proposed_by, reason, tg_proposal_message_id)
        )
        return cur.lastrowid


def get_active_rejection(submission_id: int):
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM rejections WHERE submission_id = ?
               ORDER BY proposed_at DESC LIMIT 1""",
            (submission_id,)
        ).fetchone()


def add_second_to_rejection(rejection_id: int, username: str):
    import json
    username = _norm_username(username)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT seconds FROM rejections WHERE id = ?", (rejection_id,)
        ).fetchone()
        seconds = json.loads(row["seconds"]) if row else []
        if username not in seconds:
            seconds.append(username)
        conn.execute(
            "UPDATE rejections SET seconds = ? WHERE id = ?",
            (json.dumps(seconds), rejection_id)
        )
        return seconds


def set_rejection_proposal_message_id(rejection_id: int, message_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE rejections SET tg_proposal_message_id = ? WHERE id = ?",
            (message_id, rejection_id)
        )


# ── Content Requests ─────────────────────────────────────────────────────────

def insert_content_request(submission_id: int, deadline: datetime):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO content_requests (submission_id, deadline) VALUES (?, ?)",
            (submission_id, deadline.isoformat())
        )


def get_expired_content_requests(now: datetime):
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM content_requests WHERE deadline <= ?",
            (now.isoformat(),)
        ).fetchall()


def delete_content_request(submission_id: int):
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM content_requests WHERE submission_id = ?",
            (submission_id,)
        )


def has_content_request(submission_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM content_requests WHERE submission_id = ?",
            (submission_id,)
        ).fetchone()
        return row is not None


def append_content_request_text(submission_id: int, chunk: str) -> int:
    """Append text to the buffered article content. Returns new total length."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT article_content FROM content_requests WHERE submission_id = ?",
            (submission_id,)
        ).fetchone()
        if row is None:
            return 0
        current = row["article_content"] or ""
        separator = "\n\n" if current else ""
        new_content = current + separator + chunk
        conn.execute(
            "UPDATE content_requests SET article_content = ? WHERE submission_id = ?",
            (new_content, submission_id)
        )
        return len(new_content)


def get_content_request_text(submission_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT article_content FROM content_requests WHERE submission_id = ?",
            (submission_id,)
        ).fetchone()
        return row["article_content"] if row else ""


# ── Bot State (persistent key-value) ─────────────────────────────────────────

def get_state(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_state(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
