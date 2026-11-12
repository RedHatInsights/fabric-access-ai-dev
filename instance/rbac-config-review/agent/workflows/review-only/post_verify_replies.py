#!/usr/bin/env python3
"""Acknowledge each completed verification request after its status review is updated."""

from __future__ import annotations

import argparse
import json
import re
import subprocess

from post_review import api
from public_comment_safety import ensure_public_content
from rereview_requests import VERIFY_COMMAND
from review_comment_footer import status_url, with_footer


def publish(repo: str, pr: int, head: str, base: str, review_id: int,
            command_ids: list[int]) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or pr < 1:
        raise ValueError("Repository or PR number is invalid")
    if (not re.fullmatch(r"[0-9a-f]{40}", head)
            or not re.fullmatch(r"[0-9a-f]{40}", base)
            or review_id < 1):
        raise ValueError("Verification revision or review ID is invalid")
    if (not command_ids or any(type(ident) is not int or ident < 1 for ident in command_ids)
            or len(command_ids) != len(set(command_ids))):
        raise ValueError("Verification needs unique positive command IDs")

    pull = api(f"repos/{repo}/pulls/{pr}")
    if (not isinstance(pull, dict) or pull.get("state") != "open"
            or not isinstance(pull.get("head"), dict)
            or not isinstance(pull.get("base"), dict)
            or pull["head"].get("sha") != head
            or pull["base"].get("sha") != base):
        raise RuntimeError("PR head or base changed before verification replies")
    actor = api("user")
    bot_login = actor.get("login") if isinstance(actor, dict) else None
    if not isinstance(bot_login, str) or not bot_login:
        raise RuntimeError("Could not identify the GitHub reviewer")
    review = api(f"repos/{repo}/pulls/{pr}/reviews/{review_id}")
    body = review.get("body") if isinstance(review, dict) else None
    marker = f"<!-- dev-review-bot:fabric-access-review:{head}:{base} -->"
    verification_lines = (re.findall(r"(?m)^\*\*Last verification:\*\* .+$", body)
                          if isinstance(body, str) else [])
    if (not isinstance(review, dict) or review.get("state") != "COMMENTED"
            or not isinstance(review.get("user"), dict)
            or not isinstance(review["user"].get("login"), str)
            or review["user"]["login"].casefold() != bot_login.casefold()
            or not isinstance(body, str) or body.count(marker) != 1
            or len(verification_lines) != 1
            or any(f"(https://github.com/{repo}/pull/{pr}#issuecomment-{ident})"
                   not in verification_lines[0]
                   for ident in command_ids)):
        raise RuntimeError("Tracked status review does not confirm this verification")

    endpoint = f"repos/{repo}/issues/{pr}/comments"
    comments = api(f"{endpoint}?per_page=100", paginate=True)
    if not isinstance(comments, list) or any(not isinstance(item, dict) for item in comments):
        raise RuntimeError("GitHub returned invalid PR comments")
    by_id = {item.get("id"): item for item in comments}
    for ident in command_ids:
        request = by_id.get(ident)
        if (not isinstance(request, dict)
                or not isinstance(request.get("user"), dict)
                or request["user"].get("type") != "User"
                or not isinstance(request.get("body"), str)
                or request["body"].strip().casefold() != VERIFY_COMMAND.casefold()
                or request.get("updated_at") != request.get("created_at")):
            raise RuntimeError(f"Verification request {ident} is missing or was edited")

    posted = unchanged = 0
    for ident in command_ids:
        reply_marker = f"<!-- dev-review-bot:verify-response:{ident} -->"
        if any(isinstance(item.get("body"), str) and reply_marker in item["body"]
               and isinstance(item.get("user"), dict)
               and isinstance(item["user"].get("login"), str)
               and item["user"]["login"].casefold() == bot_login.casefold()
               for item in comments):
            unchanged += 1
            continue
        reply = (
            f"Verification completed for [request #{ident}]"
            f"(https://github.com/{repo}/pull/{pr}#issuecomment-{ident}) on "
            f"[head `{head[:12]}`](https://github.com/{repo}/commit/{head}). "
            f"The [review status](https://github.com/{repo}/pull/{pr}#pullrequestreview-{review_id}) "
            "now records the updated finding statuses and last verification."
            f"\n\n{reply_marker}"
        )
        reply = with_footer(reply, repo, pr, status_url(repo, pr, review_id))
        ensure_public_content(reply, "Verification response")
        created = api(endpoint, "POST", {"body": reply})
        if not isinstance(created, dict) or not isinstance(created.get("id"), int):
            raise RuntimeError("GitHub did not confirm a verification response")
        comments.append(created)
        posted += 1
    return {"posted": posted, "unchanged": unchanged}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--review-id", required=True, type=int)
    parser.add_argument("--command-id", required=True, action="append", type=int)
    args = parser.parse_args()
    try:
        print(json.dumps(publish(args.repo, args.pr, args.head_sha, args.base_sha,
                                 args.review_id, args.command_id)))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"Verification response failed: {exc}\n")


if __name__ == "__main__":
    main()
