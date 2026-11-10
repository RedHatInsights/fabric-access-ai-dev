#!/usr/bin/env python3
"""GH PR status for this workflow's tasks only (pr-label:* keys)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gh_pr_status
from labeled_prs import own_tasks

_has_new_feedback = gh_pr_status.has_new_feedback
_REVIEW_BOT_AUTHOR = "platex-rehor-bot"
_REVIEW_BOT_MARKERS = ("dev-review-bot", "fabric-access-finding", "fabric-access-review")


def has_new_feedback(enriched):
    """Count marked comments from this instance's review bot as new feedback."""
    last_addressed = enriched["task"].get("last_addressed", "")
    for comment in enriched["pr_comments"]:
        author = comment.get("a", "?")
        body = comment.get("b") or ""
        is_review_bot_finding = (
            isinstance(author, str)
            and author.casefold() == _REVIEW_BOT_AUTHOR
            and isinstance(body, str)
            and any(marker in body.casefold() for marker in _REVIEW_BOT_MARKERS)
        )
        created_at = (comment.get("t") or "")[:16]
        if is_review_bot_finding and (not last_addressed or created_at > last_addressed[:16]):
            return True
    return _has_new_feedback(enriched)


gh_pr_status.has_new_feedback = has_new_feedback
_orig_get_tasks = gh_pr_status.get_tasks
gh_pr_status.get_tasks = lambda: own_tasks(_orig_get_tasks())
gh_pr_status.main()
