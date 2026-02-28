"""
reviewers.py — Utilities for fetching/parsing the reviewer list.

The canonical source is the local reviewers.md file (path from config.yaml).
This module is a thin helper; llm.py reads the file directly.
"""
import re
import logging

logger = logging.getLogger(__name__)

_CATEGORY_RE = re.compile(r"^##\s+(.+)", re.MULTILINE)
_REVIEWER_LINE_RE = re.compile(r"Reviewers?:\s*(.+)", re.IGNORECASE)
_USERNAME_RE = re.compile(r"@?([\w]+)")


def parse_reviewers_md(content: str) -> dict:
    """
    Parse reviewers.md content into a structured dict.

    Expected format:
        ## Category Name
        ...description...
        Reviewers: @username1, @username2

    Returns:
        {
            "Category Name": {
                "description": "...",
                "reviewers": ["username1", "username2"]
            },
            ...
        }
    """
    result = {}
    sections = _CATEGORY_RE.split(content)
    # sections[0] is content before first ##; then alternating [category, body, ...]
    it = iter(sections[1:])
    for category in it:
        body = next(it, "")
        reviewer_match = _REVIEWER_LINE_RE.search(body)
        reviewers = []
        if reviewer_match:
            reviewers = _USERNAME_RE.findall(reviewer_match.group(1))
        description_lines = [
            line for line in body.splitlines()
            if line.strip() and not _REVIEWER_LINE_RE.match(line.strip())
        ]
        result[category.strip()] = {
            "description": " ".join(description_lines).strip(),
            "reviewers": reviewers,
        }
    return result


def get_all_reviewer_usernames(content: str) -> list[str]:
    """Return a flat deduplicated list of all reviewer usernames."""
    parsed = parse_reviewers_md(content)
    seen = set()
    result = []
    for cat in parsed.values():
        for u in cat["reviewers"]:
            if u not in seen:
                seen.add(u)
                result.append(u)
    return result
