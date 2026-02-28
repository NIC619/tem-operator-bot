# Git Hooks

This directory contains git hooks for the project. Git does not install hooks
automatically — run the setup command once after cloning.

## Setup

```bash
cp hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

## pre-commit

Blocks commits that contain secrets before they reach the repository.

**Checks performed:**

| Check | What it catches |
|---|---|
| Forbidden files | `.env`, `credentials.json`, `gmail_token.json` staged for commit |
| Telegram bot token | Pattern `123456789:ABCdef...` in any staged file |
| OpenAI API key | Pattern `sk-...` in any staged file |
| Generic secrets | Variable assignments like `TOKEN=<long value>` in any staged file |

`.env.example` is exempt from the generic secret check (placeholder values are fine).

**If the hook blocks a legitimate commit** (false positive):

```bash
git commit --no-verify
```

Use sparingly — only when you are certain no real secrets are present.
