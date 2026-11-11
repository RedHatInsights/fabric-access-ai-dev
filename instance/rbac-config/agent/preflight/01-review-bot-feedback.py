#!/usr/bin/env python3
"""Wake the basic rbac-config agent for marked review-bot PR feedback."""

from __future__ import annotations

import gh_pr_status
from common import is_newer, output_result

REVIEW_BOT_AUTHOR = "platex-rehor-bot"
REVIEW_BOT_MARKERS = ("dev-review-bot", "fabric-access-finding", "fabric-access-review")
ACTIVE = {"in_progress", "pr_open", "pr_changes"}


def has_new_review_bot_feedback(enriched: dict) -> bool:
    last_addressed = enriched["task"].get("last_addressed") or ""
    for comment in enriched["pr_comments"]:
        author = comment.get("a")
        body = comment.get("b") or ""
        if (isinstance(author, str)
                and author.casefold() == REVIEW_BOT_AUTHOR
                and isinstance(body, str)
                and any(marker in body.casefold() for marker in REVIEW_BOT_MARKERS)
                and (not last_addressed or is_newer(comment.get("t") or "", last_addressed))):
            return True
    return False


def main() -> None:
    details = []
    for task in gh_pr_status.get_tasks():
        if task.get("status") not in ACTIVE:
            continue
        enriched = gh_pr_status.enrich_gh(task)
        if not enriched or not any(pr.get("state") == "OPEN" for pr in enriched["prs"]):
            continue
        if has_new_review_bot_feedback(enriched):
            details.append(gh_pr_status.fmt_task(enriched))

    if details:
        output_result(
            "start",
            "New review-bot feedback was posted by platex-rehor-bot. "
            "Handle it as external PR review feedback:\n\n" + "\n\n".join(details),
        )
    else:
        output_result("skip", "No new marked review-bot feedback on active GitHub PRs")


if __name__ == "__main__":
    main()
