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


# ── Structured editing ───────────────────────────────────────────────────────

_SUBCATEGORY_RE = re.compile(r"^###\s+(.+)$", re.MULTILINE)
_REVIEWERS_LINE_RE = re.compile(
    r"(^\*\*Reviewers?:\*\*\s*)(.*)$",
    re.IGNORECASE | re.MULTILINE,
)


def list_subcategories(content: str) -> list[str]:
    """Return the `### ...` subcategory names in file order."""
    return [m.group(1).strip() for m in _SUBCATEGORY_RE.finditer(content)]


def _find_subcategory_span(content: str, keyword: str) -> tuple[int, int, str]:
    """
    Locate the subcategory whose heading contains `keyword` (case-insensitive).
    Returns (start_offset_of_body, end_offset_of_body, matched_heading).
    Raises ValueError on no match or ambiguous match.
    """
    kw = keyword.strip().lower()
    matches = [m for m in _SUBCATEGORY_RE.finditer(content)
               if kw in m.group(1).strip().lower()]
    if not matches:
        raise ValueError(f"No subcategory heading matches '{keyword}'.")
    if len(matches) > 1:
        names = ", ".join(m.group(1).strip() for m in matches)
        raise ValueError(
            f"Keyword '{keyword}' matches multiple categories: {names}. "
            f"Use a more specific keyword."
        )
    m = matches[0]
    body_start = m.end()
    next_heading = _SUBCATEGORY_RE.search(content, pos=body_start)
    body_end = next_heading.start() if next_heading else len(content)
    return body_start, body_end, m.group(1).strip()


def add_reviewer(content: str, category_keyword: str, username: str) -> tuple[str, str]:
    """
    Add `username` to the Reviewers line of the subcategory matching
    `category_keyword`. Returns (new_content, matched_category).
    Raises ValueError on no/ambiguous match, or if already present.
    """
    username = username.strip().lstrip("@")
    if not username:
        raise ValueError("Username is empty.")

    body_start, body_end, heading = _find_subcategory_span(content, category_keyword)
    body = content[body_start:body_end]

    match = _REVIEWERS_LINE_RE.search(body)
    if not match:
        raise ValueError(f"No `**Reviewers:**` line found under '{heading}'.")

    current = [u.strip() for u in match.group(2).split(",") if u.strip()]
    if any(u.lower() == username.lower() for u in current):
        raise ValueError(f"@{username} is already a reviewer in '{heading}'.")

    current.append(username)
    new_line = f"{match.group(1)}{', '.join(current)}"
    new_body = body[:match.start()] + new_line + body[match.end():]
    return content[:body_start] + new_body + content[body_end:], heading


def remove_reviewer(content: str, username: str) -> tuple[str, list[str]]:
    """
    Remove `username` from every Reviewers line in `content`.
    Returns (new_content, list_of_categories_affected).
    Raises ValueError if the username isn't found anywhere.
    """
    username = username.strip().lstrip("@")
    if not username:
        raise ValueError("Username is empty.")

    affected: list[str] = []
    out: list[str] = []
    last = 0
    headings = list(_SUBCATEGORY_RE.finditer(content))

    def heading_for(offset: int) -> str:
        current = "(top-level)"
        for h in headings:
            if h.start() > offset:
                break
            current = h.group(1).strip()
        return current

    for m in _REVIEWERS_LINE_RE.finditer(content):
        current = [u.strip() for u in m.group(2).split(",") if u.strip()]
        filtered = [u for u in current if u.lower() != username.lower()]
        if len(filtered) == len(current):
            continue  # not in this line
        affected.append(heading_for(m.start()))
        out.append(content[last:m.start()])
        out.append(f"{m.group(1)}{', '.join(filtered)}")
        last = m.end()

    if not affected:
        raise ValueError(f"@{username} not found in any category.")

    out.append(content[last:])
    return "".join(out), affected
