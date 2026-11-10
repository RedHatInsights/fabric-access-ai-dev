#!/usr/bin/env python3
"""Idempotently post context questions and find author answers to them."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

from post_review import api
from review_evidence import render_context_response_comments, validate_context_responses


QUESTION_MARKER = re.compile(r"<!-- dev-review-bot:missing-context:(Q-[0-9a-f]{12}) -->")
RESPONSE_MARKER = re.compile(r"<!-- dev-review-bot:context-response:(Q-[0-9a-f]{12}):([1-9]\d*) -->")
ANSWER_LINE = re.compile(r"^\s*Answer\s+(Q-[0-9a-f]{12})\s*:\s*(.+?)\s*$", re.IGNORECASE | re.DOTALL)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return parsed


def comments_for_pr(repo: str, pr: int) -> tuple[dict, list[dict]]:
    pull = api(f"repos/{repo}/pulls/{pr}")
    if not isinstance(pull, dict) or not isinstance(pull.get("user"), dict):
        raise RuntimeError("Could not read the PR author")
    comments = api(f"repos/{repo}/issues/{pr}/comments?per_page=100", paginate=True)
    if not isinstance(comments, list) or any(not isinstance(item, dict) for item in comments):
        raise RuntimeError("GitHub returned invalid PR comments")
    return pull, comments


def publish(repo: str, pr: int, head: str, comments_path: Path) -> dict:
    pull, existing = comments_for_pr(repo, pr)
    actor = api("user")
    bot_login = actor.get("login") if isinstance(actor, dict) else None
    if not isinstance(bot_login, str) or not bot_login:
        raise RuntimeError("Could not identify the bot account")
    if (pull.get("state") != "open" or not isinstance(pull.get("head"), dict)
            or pull["head"].get("sha") != head):
        raise RuntimeError("PR head changed before context comments could be posted")
    comments = json.loads(comments_path.read_text())
    if not isinstance(comments, list):
        raise ValueError("Context comments must be a JSON array")
    posted = unchanged = 0
    latest_comment_at = None
    endpoint = f"repos/{repo}/issues/{pr}/comments"
    for comment in comments:
        if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
            raise ValueError("Each context comment needs a body")
        body = comment["body"]
        question_markers = QUESTION_MARKER.findall(body)
        response_markers = RESPONSE_MARKER.findall(body)
        if len(question_markers) + len(response_markers) != 1:
            raise ValueError("Each context comment needs one question marker, one response marker, or one of each")
        if response_markers:
            question_id, answer_id = response_markers[0]
            marker = f"<!-- dev-review-bot:context-response:{question_id}:{answer_id} -->"
        else:
            marker = f"<!-- dev-review-bot:missing-context:{question_markers[0]} -->"
        if any(marker in (item.get("body") or "")
               and (item.get("user") or {}).get("login", "").casefold() == bot_login.casefold()
               for item in existing):
            unchanged += 1
            continue
        created = api(endpoint, "POST", {"body": body})
        if not isinstance(created, dict) or not isinstance(created.get("id"), int):
            raise RuntimeError("GitHub did not confirm a context comment")
        existing.append(created)
        posted += 1
        created_at = created.get("created_at")
        if isinstance(created_at, str) and (latest_comment_at is None or created_at > latest_comment_at):
            latest_comment_at = created_at
    return {"posted": posted, "unchanged": unchanged, "latest_comment_at": latest_comment_at}


def answer_candidates(repo: str, pr: int, since: str, through: str) -> list[dict]:
    start, end = timestamp(since), timestamp(through)
    if end < start:
        raise ValueError("Answer window ends before it starts")
    pull, comments = comments_for_pr(repo, pr)
    actor = api("user")
    bot_login = actor.get("login") if isinstance(actor, dict) else None
    if not isinstance(bot_login, str) or not bot_login:
        raise RuntimeError("Could not identify the bot account")
    author = (pull.get("user") or {}).get("login")
    if not isinstance(author, str) or not author:
        raise RuntimeError("Could not identify the PR author")

    questions = {}
    answered_ids = set()
    for comment in comments:
        body = comment.get("body") or ""
        login = (comment.get("user") or {}).get("login", "")
        if not isinstance(login, str) or login.casefold() != bot_login.casefold():
            continue
        for question_id in QUESTION_MARKER.findall(body):
            questions[question_id] = comment
        for question_id, _answer_id in RESPONSE_MARKER.findall(body):
            answered_ids.add(question_id)

    answers = []
    seen_answers = set()
    for comment in comments:
        body = comment.get("body")
        user = comment.get("user") or {}
        ident = comment.get("id")
        if (not isinstance(body, str) or not isinstance(ident, int) or isinstance(ident, bool)
                or user.get("type") != "User" or user.get("login", "").casefold() != author.casefold()
                or ident in seen_answers):
            continue
        match = ANSWER_LINE.match(body)
        if not match:
            continue
        question_id, answer = match.group(1).lower(), match.group(2).strip()
        if question_id not in questions or question_id in answered_ids or not answer:
            continue
        created_at = comment.get("created_at")
        try:
            created = timestamp(created_at)
        except (TypeError, ValueError):
            continue
        if not start <= created <= end:
            continue
        question_comment = questions[question_id]
        answers.append({
            "question_id": question_id,
            "question_comment_id": question_comment.get("id"),
            "question_url": question_comment.get("html_url"),
            "question_body": QUESTION_MARKER.sub("", question_comment.get("body") or "").strip(),
            "answer_id": ident,
            "answer_url": comment.get("html_url"),
            "login": author,
            "created_at": created_at,
            "answer": answer,
        })
        seen_answers.add(ident)
    answers.sort(key=lambda item: (timestamp(item["created_at"]), item["answer_id"]))
    selected = []
    resolved = set(answered_ids)
    for answer in answers:
        if answer["question_id"] not in resolved:
            selected.append(answer)
            resolved.add(answer["question_id"])
    return selected


def render_responses(manifest_path: Path, responses_path: Path, output_path: Path) -> list[dict]:
    manifest = json.loads(manifest_path.read_text())
    responses = validate_context_responses(json.loads(responses_path.read_text()), manifest)
    comments = render_context_response_comments(responses)
    output_path.write_text(json.dumps(comments, indent=2))
    return comments


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    post = subparsers.add_parser("publish", help="Post validated context question/response comments")
    post.add_argument("--repo", required=True)
    post.add_argument("--pr", required=True, type=int)
    post.add_argument("--head-sha", required=True)
    post.add_argument("--comments", required=True, type=Path)
    answers = subparsers.add_parser("answers", help="Find new answers from the PR author")
    answers.add_argument("--repo", required=True)
    answers.add_argument("--pr", required=True, type=int)
    answers.add_argument("--since", required=True)
    answers.add_argument("--through", required=True)
    render = subparsers.add_parser("render-responses", help="Validate and render context-answer comments")
    render.add_argument("--manifest", required=True, type=Path)
    render.add_argument("--responses", required=True, type=Path)
    render.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "publish":
            print(json.dumps(publish(args.repo, args.pr, args.head_sha, args.comments)))
        elif args.command == "answers":
            print(json.dumps({"answers": answer_candidates(args.repo, args.pr, args.since, args.through)}))
        else:
            print(json.dumps({"rendered": len(render_responses(args.manifest, args.responses, args.output)),
                              "output": str(args.output)}))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"Context question processing failed: {exc}\n")


if __name__ == "__main__":
    main()
