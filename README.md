# TEM Review Bot

A Telegram bot that automates the editorial review workflow for the [Taipei Ethereum Meetup (TEM) Medium column](https://medium.com/taipei-ethereum-meetup). It watches a Gmail inbox for article submissions, assigns reviewers via Telegram, follows up periodically, and sends acceptance/rejection emails to authors.

---

## How It Works

```
New submission email
        │
        ▼
Bot DMs operator: "paste the draft content or /skip"
        │
        ├── /content <sub_id> <text>   (operator pastes draft)
        ├── /skip <sub_id>             (skip, use title only)
        └── 24h timeout                (auto-proceed)
        │
        ▼
LLM picks 1–2 reviewers (using content if provided)
Bot posts in Telegram group → inline buttons [✅ Yes] [❌ Can't]
        │
        ▼  (all reviewers confirm)
Status → Under Review
Author notified by email
Follow-up messages every 14 days
        │
        ▼  (all reviewers mark done)
Publish date computed (next weekday)
Author notified by email → ACCEPTED

At any point: /reject → 2 seconds → operator confirms → REJECTED
```

> If `operator_user_id` is not set in `config.yaml`, the content-request step is skipped and the bot assigns reviewers directly from the email subject and body.

---

## Setup

### 1. Dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token
2. Add the bot to the TEM reviewer group (make it an admin so it can post)

### 3. Gmail OAuth

1. Go to [Google Cloud Console](https://console.cloud.google.com/) → create a project → enable the Gmail API
2. Create OAuth 2.0 credentials (Desktop app type) → download as `credentials.json` → place in project root
3. First run opens a browser for consent → saves `gmail_token.json` automatically

### 4. Environment

```bash
cp .env.example .env
```

Edit `.env`:
```
TELEGRAM_BOT_TOKEN=your_bot_token
OPENAI_API_KEY=your_openai_key
GMAIL_CREDENTIALS_JSON_PATH=./credentials.json
GMAIL_TOKEN_PATH=./gmail_token.json
```

### 5. Configuration

Edit `config.yaml`:

```yaml
telegram:
  group_chat_id: null        # ← fill in after step 6
  operator_user_id: null     # ← fill in after step 6
  poll_interval_seconds: 15

gmail:
  poll_interval_seconds: 37
  submission_label: "工作/TEM/有獎徵稿"   # Gmail label to filter by (null = all inbox)
  subject_prefix: "TEM 專欄投稿："         # Subject prefix to filter by (null = all)

workflow:
  followup_interval_days: 14
  publish_time: "09:30"
  publish_timezone: "Asia/Taipei"
```

### 6. First run — get IDs

```bash
python main.py
```

- Gmail OAuth browser window opens → approve → `gmail_token.json` saved
- Send `/getid` in the TEM reviewer group → copy the **chat ID** into `config.yaml → telegram.group_chat_id`
- Your **user ID** is also shown → copy into `telegram.operator_user_id`
- Restart the bot

### 7. Reviewers

Edit `reviewers.md` with real reviewer Telegram usernames and categories. The LLM reads this file directly to pick appropriate reviewers for each submission.

---

## Running

```bash
python main.py
```

For persistent background running on Mac Mini:

```bash
npm install -g pm2
pm2 start main.py --interpreter python3 --name tem-bot
pm2 save
pm2 startup
```

---

## Telegram Commands

| Command | Who | What it does |
|---------|-----|-------------|
| `/getid` | Anyone | Shows current chat ID and your user ID |
| `/status` | Anyone | Lists all active submissions and their state |
| `/done <keyword>` | Reviewer | Marks your review as done (e.g. `/done fusaka`) |
| `/reject <keyword> <reason>` | Anyone | Proposes rejecting a submission |
| `/second <keyword>` | Anyone | Seconds a rejection proposal (2 needed) |
| `/override <sub_id> @user1 [@user2]` | Operator | Manually assigns reviewers |
| `/content <sub_id> <text>` | Operator | Provides article draft text for reviewer assignment |
| `/skip <sub_id>` | Operator | Skips content request; assigns based on title alone |

Inline buttons appear automatically — reviewers tap to accept/decline and to mark done.

---

## Submission Email Format

Emails are picked up if they match **both** filters (either can be set to `null` to disable):

- **Label**: `工作/TEM/有獎徵稿` (applied in Gmail)
- **Subject prefix**: `TEM 專欄投稿：`

The bot extracts author name, email, article title, and Medium URL (if present) from the email.

---

## Project Structure

```
├── main.py               # Entry point
├── config.yaml           # Tunables
├── .env                  # Secrets (never commit)
├── .env.example          # Secret template
├── requirements.txt
├── db.py                 # SQLite schema + queries
├── state.py              # State machine + business logic
├── gmail_client.py       # Gmail OAuth, polling, sending
├── llm.py                # OpenAI reviewer assignment
├── telegram_handlers.py  # Telegram commands + button handlers
├── scheduler.py          # Background jobs
├── config.py             # Config loader
├── reviewers.py          # reviewers.md parser utility
├── reviewers.md          # Reviewer list (edit this)
├── credentials.json      # Gmail OAuth client secret (never commit)
├── gmail_token.json      # Gmail access token (auto-created, never commit)
├── tem_bot.db            # SQLite database (auto-created)
└── CONTEXT.md            # Implementation notes and change log
```

---

## Database

SQLite (`tem_bot.db`), 6 tables:

| Table | Purpose |
|-------|---------|
| `submissions` | One row per email submission |
| `assignments` | Reviewer assignments per submission |
| `followups` | Scheduled follow-up messages |
| `assignment_history` | 90-day history for LLM workload balancing |
| `rejections` | Rejection proposals + seconds |
| `bot_state` | Persistent key-value store (e.g. last Gmail poll timestamp) |
| `content_requests` | Pending operator content requests with 24h deadline |

---

## Notes

- The bot uses Telegram **usernames** (no `@`) as canonical reviewer IDs. Reviewers must have a username set.
- Inline buttons are reviewer-specific — only the tagged reviewer can click their own button.
- Gmail sends are run in a thread (`asyncio.to_thread`) so they don't block Telegram responses.
- The Gmail poll timestamp is persisted to the DB so emails are never missed across restarts.
- The operator must have started a private chat with the bot before content-request DMs will work. If not, the bot falls back to a group-chat notice.
- Occasional `ConnectTimeout` errors to Telegram are transient network issues and self-recover — logged as warnings, not errors.
