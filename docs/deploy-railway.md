# Deploying to Railway

The bot is built to run unchanged on Railway. A persistent Volume holds
the SQLite DB and the two non-secret-but-private config files
(`config.yaml`, `reviewers.md`) plus the Gmail OAuth files. Secrets go
in Railway environment variables.

## 1. Generate the Gmail token locally (one-time)

Railway containers are headless — the OAuth browser flow cannot run
there. Generate `gmail_token.json` on your laptop first:

```bash
python main.py      # browser opens; approve; token.json is saved
# stop the bot with Ctrl+C once you see "Gmail client ready."
```

## 2. Create a Railway project + volume

1. Railway dashboard → New Project → Deploy from GitHub repo.
2. In the service settings → Volumes → New Volume → mount path `/data`.

## 3. Seed the volume files via base64 env vars (first deploy only)

Railway's container filesystem is empty on first boot and there's no
way to SSH in *before* the service has ever run. The bot has a small
bootstrap that decodes four base64 env vars into the volume on startup
(writes only if the target file is missing, so it's a no-op on later
boots).

On your local machine, generate the four base64 blobs:

```bash
base64 -i config.yaml       | pbcopy   # paste into CONFIG_YAML_B64
base64 -i reviewers.md      | pbcopy   # paste into REVIEWERS_MD_B64
base64 -i credentials.json  | pbcopy   # paste into GMAIL_CREDENTIALS_B64
base64 -i gmail_token.json  | pbcopy   # paste into GMAIL_TOKEN_B64
```

Paste each into Railway → Variables. Make sure your local
`config.yaml` has `reviewers_file: /data/reviewers.md` before you
encode it.

After the first successful deploy, you can remove the four `_B64`
variables — the files now live on the volume.

## 4. Set environment variables

In the Railway service → Variables tab:

| Variable | Value |
|----------|-------|
| `TELEGRAM_BOT_TOKEN` | your bot token |
| `OPENAI_API_KEY` | your OpenAI key |
| `TELEGRAM_GROUP_CHAT_ID` | e.g. `-1001234567890` |
| `TELEGRAM_OPERATOR_USER_ID` | your Telegram user ID |
| `TELEGRAM_POLL_INTERVAL_SECONDS` | e.g. `3` |
| `GMAIL_POLL_INTERVAL_SECONDS` | e.g. `300` |
| `WORKFLOW_FOLLOWUP_INTERVAL_DAYS` | e.g. `14` |
| `CONFIG_PATH` | `/data/config.yaml` |
| `DB_PATH` | `/data/tem_bot.db` |
| `GMAIL_CREDENTIALS_JSON_PATH` | `/data/credentials.json` |
| `GMAIL_TOKEN_PATH` | `/data/gmail_token.json` |
| `REVIEWERS_MD_PATH` | `/data/reviewers.md` |
| `HEADLESS` | `1` |
| `CONFIG_YAML_B64` | base64 of local `config.yaml` (first deploy only) |
| `REVIEWERS_MD_B64` | base64 of local `reviewers.md` (first deploy only) |
| `GMAIL_CREDENTIALS_B64` | base64 of `credentials.json` (first deploy only) |
| `GMAIL_TOKEN_B64` | base64 of `gmail_token.json` (first deploy only) |

`HEADLESS=1` makes the bot refuse the OAuth browser flow and fail
loudly if the token is missing, instead of hanging. The five tunables
above override the corresponding values in `config.yaml` when set.

## 5. Deploy

Railway auto-builds from `railway.json` (Nixpacks + `python main.py`).
Check the logs: you should see `Gmail client ready.` then
`Bot starting, polling for updates…`.

## 6. Editing reviewers / config later

`railway shell` is **not** an SSH session — it just wraps a local
shell with the service's env vars. Use `railway ssh` for a real shell
inside the running container.

### Option 1 — `railway ssh` (preferred, once the service is healthy)

```bash
railway ssh
# inside the container:
nano /data/reviewers.md
# or, without a text editor:
cat > /data/reviewers.md <<'EOF'
<paste new content>
EOF
```

- `reviewers.md` is **reread on every LLM call** — no restart needed.
- `config.yaml` is **cached in memory** after first load — after
  editing, restart the service (Deployments → Restart).

### Option 2 — Railway volume file browser (if your plan exposes it)

Service → Volumes → click the volume → edit files in place.

### Option 3 — re-bootstrap fallback (no SSH available)

```bash
# locally:
base64 -i reviewers.md | pbcopy
```

Then in Railway:

1. Set (or update) `REVIEWERS_MD_B64` with the new blob.
2. `railway ssh` → `rm /data/reviewers.md` (the bootstrap skips
   existing files, so you must delete first).
3. Redeploy. Bootstrap rewrites the file.
4. Remove `REVIEWERS_MD_B64` from Variables once done.

Same pattern applies to `config.yaml`, `credentials.json`, and
`gmail_token.json` via their respective `_B64` vars.

## Which env vars are permanent vs. one-shot

| Kind | Examples | When |
|------|----------|------|
| Permanent | `CONFIG_PATH`, `DB_PATH`, `GMAIL_CREDENTIALS_JSON_PATH`, `GMAIL_TOKEN_PATH`, `REVIEWERS_MD_PATH`, `HEADLESS` | Always keep set — they tell the bot to read/write on the `/data` volume instead of the ephemeral container disk |
| One-shot | `CONFIG_YAML_B64`, `REVIEWERS_MD_B64`, `GMAIL_CREDENTIALS_B64`, `GMAIL_TOKEN_B64` | Delete from Variables after the first successful deploy. They re-run the bootstrap only when the target file is missing |

## Backups

SQLite on a Railway Volume has no automatic backups. Add a cron (or
a scheduled GitHub Action) that copies `/data/tem_bot.db` to object
storage daily. For zero-data-loss streaming, consider
[litestream](https://litestream.io) replicating to S3.
