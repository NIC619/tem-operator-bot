"""
gmail_client.py — Gmail OAuth setup, polling, email parsing, and sending.
"""
import base64
import email as email_lib
import logging
import os
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

_MEDIUM_URL_RE = re.compile(r"https?://(?:www\.)?medium\.com/[^\s\"'>]+")
_REPLY_SUBJECT_RE = re.compile(r"^(Re|Fwd|FW|RE|FWD):\s*", re.IGNORECASE)


class GmailClient:
    def __init__(self):
        credentials_path = os.environ.get(
            "GMAIL_CREDENTIALS_JSON_PATH", "./credentials.json"
        )
        token_path = os.environ.get("GMAIL_TOKEN_PATH", "./gmail_token.json")
        self.service = _build_service(credentials_path, token_path)

    # ── Auth ──────────────────────────────────────────────────────────────────

    # ── Polling ───────────────────────────────────────────────────────────────

    def poll_new_submissions(self, last_checked_timestamp: float,
                             subject_prefix: str = None,
                             submission_label: str = None) -> list[dict]:
        """
        Query Gmail for messages received after last_checked_timestamp (Unix epoch).
        Filters by subject_prefix and/or submission_label when provided.
        Returns list of parsed submission dicts, skipping replies/threads.
        """
        after_ts = int(last_checked_timestamp)
        parts = [f"after:{after_ts}"]

        if submission_label:
            # Nested labels: use unquoted label:path/to/label — quotes break slash parsing
            parts.append(f"label:{submission_label}")
        else:
            parts.append("in:inbox")

        if subject_prefix:
            parts.append(f'subject:"{subject_prefix}"')

        query = " ".join(parts)
        logger.info("Gmail query: %s", query)

        try:
            result = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, maxResults=50)
                .execute()
            )
        except HttpError as e:
            logger.error("Gmail API error listing messages: %s", e)
            return []

        messages = result.get("messages", [])
        logger.info("Gmail query returned %d message(s).", len(messages))
        submissions = []

        for msg_ref in messages:
            try:
                parsed = self._fetch_and_parse(msg_ref["id"])
                if parsed:
                    submissions.append(parsed)
            except Exception as e:
                logger.error("Error processing message %s: %s", msg_ref["id"], e)

        return submissions

    def _fetch_and_parse(self, message_id: str) -> dict | None:
        msg = (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "")
        logger.info("Checking message %s: subject=%r", message_id, subject)

        # Skip replies — if In-Reply-To header is present it's part of a thread
        if "In-Reply-To" in headers:
            logger.info("  → skipped (is a reply)")
            return None

        # Skip if thread has more than 1 message (i.e. we've already replied)
        thread_id = msg.get("threadId", message_id)
        thread = (
            self.service.users()
            .threads()
            .get(userId="me", id=thread_id, format="minimal")
            .execute()
        )
        thread_len = len(thread.get("messages", []))
        if thread_len > 1:
            logger.info("  → skipped (thread has %d messages, already processed)", thread_len)
            return None

        subject_raw = headers.get("Subject", "")
        subject_clean = _REPLY_SUBJECT_RE.sub("", subject_raw).strip()

        from_raw = headers.get("From", "")
        author_name, author_email = _parse_from_header(from_raw)

        body = _extract_body(msg["payload"])
        medium_url = _find_medium_url(body) or _find_medium_url(subject_raw)

        return {
            "gmail_message_id": message_id,
            "gmail_thread_id": thread_id,
            "title": subject_clean or subject_raw,
            "author_name": author_name,
            "author_email": author_email,
            "medium_url": medium_url,
            "email_subject": subject_raw,
            "email_body": body,
            "message_id_header": headers.get("Message-ID", ""),
        }

    # ── Sending ───────────────────────────────────────────────────────────────

    def send_under_review_email(self, sub) -> None:
        body = (
            f"Hi {sub['author_name'] or 'there'},\n\n"
            f"Thank you for submitting your article 《{sub['title']}》 "
            f"to the TEM Medium column.\n\n"
            f"Your submission is currently under review. "
            f"We will follow up once the review is complete.\n\n"
            f"Best,\nTEM Editorial Team"
        )
        self._send_reply(sub, body)

    def send_acceptance_email(self, sub, publish_date_str: str) -> None:
        body = (
            f"Hi {sub['author_name'] or 'there'},\n\n"
            f"Great news — your article 《{sub['title']}》 has been accepted "
            f"for publication on the TEM Medium column.\n\n"
            f"It is scheduled to publish on {publish_date_str} at 9:30 AM (Taiwan time).\n\n"
            f"Please make sure the article draft on Medium is ready before then. "
            f"If you need to make any changes, please do so before the scheduled date.\n\n"
            f"Thank you for your contribution!\n\n"
            f"Best,\nTEM Editorial Team"
        )
        self._send_reply(sub, body)

    def send_rejection_email(self, sub, rejection_reason: str) -> None:
        reason_block = f"\n{rejection_reason}\n" if rejection_reason else ""
        body = (
            f"Hi {sub['author_name'] or 'there'},\n\n"
            f"Thank you for submitting your article 《{sub['title']}》 "
            f"to the TEM Medium column.\n\n"
            f"After careful review, we are unable to accept this submission at this time.\n"
            f"{reason_block}\n"
            f"We encourage you to revise and resubmit in the future. "
            f"If you have questions, feel free to reach out.\n\n"
            f"Best,\nTEM Editorial Team"
        )
        self._send_reply(sub, body)

    def _send_reply(self, sub, body_text: str) -> None:
        subject = f"Re: {sub['email_subject']}"

        mime_msg = MIMEMultipart()
        mime_msg["To"] = sub["author_email"]
        mime_msg["Subject"] = subject
        mime_msg.attach(MIMEText(body_text, "plain", "utf-8"))

        # Thread headers so it appears as a reply in the submitter's inbox
        if sub["gmail_message_id"]:
            original_msg_id = _get_original_message_id_header(
                self.service, sub["gmail_message_id"]
            )
            if original_msg_id:
                mime_msg["In-Reply-To"] = original_msg_id
                mime_msg["References"] = original_msg_id

        raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        body_payload = {"raw": raw}

        if sub["gmail_thread_id"]:
            body_payload["threadId"] = sub["gmail_thread_id"]

        try:
            self.service.users().messages().send(
                userId="me", body=body_payload
            ).execute()
            logger.info("Sent reply email to %s for submission #%s",
                        sub["author_email"], sub.get("id", "?"))
        except HttpError as e:
            logger.error("Gmail send error: %s", e)
            raise


# ── Auth helper ───────────────────────────────────────────────────────────────

def _build_service(credentials_path: str, token_path: str):
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_from_header(from_raw: str) -> tuple[str, str]:
    """Parse 'Display Name <email@example.com>' into (name, email)."""
    match = re.match(r'"?([^"<]+)"?\s*<([^>]+)>', from_raw.strip())
    if match:
        return match.group(1).strip(), match.group(2).strip()
    # Plain email with no display name
    email_match = re.match(r"[\w.+-]+@[\w.-]+", from_raw)
    if email_match:
        return "", email_match.group(0)
    return "", from_raw.strip()


def _extract_body(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    if mime_type.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = _extract_body(part)
            if text:
                return text

    return ""


def _find_medium_url(text: str) -> str | None:
    match = _MEDIUM_URL_RE.search(text)
    return match.group(0) if match else None


def _get_original_message_id_header(service, gmail_message_id: str) -> str | None:
    """Fetch the RFC Message-ID header from the original email."""
    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=gmail_message_id, format="metadata",
                 metadataHeaders=["Message-ID"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        return headers.get("Message-ID")
    except Exception as e:
        logger.warning("Could not fetch Message-ID header: %s", e)
        return None
