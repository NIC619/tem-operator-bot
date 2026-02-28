"""
llm.py — OpenAI-based reviewer assignment for the TEM review bot.
"""
import json
import logging
import os

from openai import AsyncOpenAI

import db

logger = logging.getLogger(__name__)

_client = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


# ── Reviewer file ─────────────────────────────────────────────────────────────

def _load_reviewers_markdown(config: dict) -> str:
    """Load the reviewers.md file. Reloaded on every call (file is small)."""
    path = config.get("reviewers_file", "./reviewers.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning("Reviewers file not found at %s", path)
        return "(No reviewer list found)"


# ── Workload helpers ──────────────────────────────────────────────────────────

def _build_history_text() -> str:
    rows = db.get_recent_assignment_history(days=90)
    if not rows:
        return "(No recent assignment history)"
    lines = []
    seen = set()
    for row in rows[:10]:
        key = (row["submission_id"], row["reviewer_tg_username"])
        if key in seen:
            continue
        seen.add(key)
        date_str = row["assigned_at"][:10] if row["assigned_at"] else "?"
        title = row["title"] or f"Submission #{row['submission_id']}"
        lines.append(f"- {date_str}: 《{title}》 → @{row['reviewer_tg_username']}")
    return "\n".join(lines) or "(No recent assignment history)"


def _build_workload_summary() -> str:
    rows = db.get_recent_assignment_history(days=90)
    counts: dict[str, int] = {}
    for row in rows:
        u = row["reviewer_tg_username"]
        counts[u] = counts.get(u, 0) + 1
    if not counts:
        return "(No workload data)"
    return "\n".join(f"- @{u}: {n} 篇" for u, n in sorted(counts.items()))


# ── Core assignment ───────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
你是 Ethereum Meetup Taipei (TEM) Medium 專欄的 Reviewer 分配助手。

你的任務：
1. 分析投稿文章的主題
2. 從 Reviewer 列表中選出最適合的 1～2 位 Reviewer
3. 參考近期分配紀錄，避免重複指派同一位 Reviewer
4. 如果有多位合適的 Reviewer，優先選擇近期分配次數較少的人
5. 如果該類別只有 1 位合適的 Reviewer，只選 1 位即可，reviewer2 留空字串 ""
6. 簡要說明為什麼選擇這些人

## Reviewer 列表
{reviewer_list_markdown}

## 回覆格式 (務必遵守)

請用以下 JSON 格式回覆，不要加任何其他文字或 markdown：
{{"reviewer1": "tg_username", "reviewer2": "tg_username_or_empty", "category": "主要類別", "reason_zh": "用中文簡要說明為什麼選擇，包含工作量平衡說明 (2-3句話)"}}\
"""

_USER_PROMPT = """\
投稿主題：{email_subject}

寄件人：{author_name} ({author_email})

信件內容：{email_body}

文章內容（如有）：{article_content}

## 近期分配紀錄（最近90天）
{history_text}

## 近期 Reviewer 工作量統計
{workload_summary}

請根據以上資訊，選出最適合且近期工作量較低的 2 位 Reviewer。\
"""


async def pick_reviewers(email_data: dict, config: dict = None) -> dict:
    """
    Call OpenAI to pick 2 reviewers for a new submission.
    Returns dict with keys: reviewer1, reviewer2, category, reason_zh
    """
    if config is None:
        import config as cfg
        config = cfg.load()

    reviewer_md = _load_reviewers_markdown(config)
    history_text = _build_history_text()
    workload_summary = _build_workload_summary()

    system_prompt = _SYSTEM_PROMPT.format(reviewer_list_markdown=reviewer_md)
    user_prompt = _USER_PROMPT.format(
        email_subject=email_data.get("email_subject", ""),
        author_name=email_data.get("author_name", ""),
        author_email=email_data.get("author_email", ""),
        email_body=(email_data.get("email_body", "") or "")[:3000],
        article_content="",
        history_text=history_text,
        workload_summary=workload_summary,
    )

    client = _get_client()
    response = await client.chat.completions.create(
        model="gpt-4o",
        temperature=0.3,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw = response.choices[0].message.content.strip()
    result = _parse_json_response(raw)
    logger.info("LLM picked reviewers: %s, %s (category: %s)",
                result.get("reviewer1"), result.get("reviewer2"), result.get("category"))
    return result


async def pick_replacement_reviewer(email_data: dict, declined_username: str,
                                     excluded_usernames: list[str],
                                     config: dict = None) -> dict:
    """
    Pick a single replacement reviewer when someone declines.
    Returns dict with key reviewer1 (the replacement).
    """
    if config is None:
        import config as cfg
        config = cfg.load()

    reviewer_md = _load_reviewers_markdown(config)
    history_text = _build_history_text()
    workload_summary = _build_workload_summary()

    excluded_str = ", ".join(f"@{u}" for u in excluded_usernames) if excluded_usernames else "none"
    extra_constraint = (
        f"\n\n## 限制條件\n"
        f"已拒絕的 Reviewer: @{declined_username}，請不要再次選擇此人。\n"
        f"排除以下人員（已確認或已拒絕）: {excluded_str}。\n"
        f"只需回覆 1 位 Reviewer (放在 reviewer1 欄位，reviewer2 留空字串)。"
    )

    system_prompt = _SYSTEM_PROMPT.format(reviewer_list_markdown=reviewer_md)
    user_prompt = (
        _USER_PROMPT.format(
            email_subject=email_data.get("email_subject", ""),
            author_name=email_data.get("author_name", ""),
            author_email=email_data.get("author_email", ""),
            email_body=(email_data.get("email_body", "") or "")[:3000],
            article_content="",
            history_text=history_text,
            workload_summary=workload_summary,
        )
        + extra_constraint
    )

    client = _get_client()
    response = await client.chat.completions.create(
        model="gpt-4o",
        temperature=0.3,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw = response.choices[0].message.content.strip()
    result = _parse_json_response(raw)
    logger.info("LLM picked replacement reviewer: %s", result.get("reviewer1"))
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_json_response(raw: str) -> dict:
    """Strip markdown fences and parse JSON."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip().rstrip("`").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse LLM JSON response: %s\nRaw: %s", e, raw)
        raise ValueError(f"LLM returned invalid JSON: {e}") from e
