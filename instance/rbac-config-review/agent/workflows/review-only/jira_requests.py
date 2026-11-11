#!/usr/bin/env python3
"""Find explicit Jira-creation commands in top-level PR comments."""

from __future__ import annotations

import re
from datetime import datetime

COMMAND = re.compile(r"^/review-bot\s+jira\s+(F[1-9][0-9]*)$", re.IGNORECASE)


def timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Timestamps must be strings")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return parsed


def requests(comments: list[dict], since: str, through: str,
             seen_ids: set[int]) -> list[dict]:
    """Return exact, new commands; callers must verify org membership."""
    start, end = timestamp(since), timestamp(through)
    if end < start:
        raise ValueError("Request window ends before it starts")
    result = []
    emitted: set[int] = set()
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        ident, author = comment.get("id"), comment.get("user") or {}
        body = comment.get("body")
        match = COMMAND.fullmatch(body.strip()) if isinstance(body, str) else None
        if (not isinstance(ident, int) or isinstance(ident, bool) or ident <= 0
                or ident in seen_ids or ident in emitted
                or not isinstance(author, dict) or author.get("type") != "User"
                or not isinstance(author.get("login"), str) or not author["login"].strip()
                or match is None or comment.get("updated_at") != comment.get("created_at")):
            continue
        try:
            created_at = comment["created_at"]
            created = timestamp(created_at)
        except (KeyError, TypeError, ValueError):
            continue
        if start <= created <= end:
            result.append({
                "id": ident,
                "finding_id": match.group(1).upper(),
                "login": author["login"],
                "created_at": created_at,
            })
            emitted.add(ident)
    return sorted(result, key=lambda item: (timestamp(item["created_at"]), item["id"]))
