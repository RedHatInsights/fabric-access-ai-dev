#!/usr/bin/env python3
"""Post or refresh evidence-validated inline review comments idempotently."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path


def api(endpoint: str, method: str, payload: dict | None = None) -> object:
    command = ["gh", "api", endpoint]
    if payload is None:
        command.extend(["--paginate", "--slurp"])
    else:
        command.extend(["--method", method])
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as request:
            json.dump(payload, request)
            request.flush()
            command.extend(["--input", request.name])
            result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"gh api failed: {endpoint}")
        return json.loads(result.stdout) if result.stdout.strip() else {}

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"gh api failed: {endpoint}")
    pages = json.loads(result.stdout) if result.stdout.strip() else []
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise RuntimeError("GitHub returned an invalid inline-comment page list")
    return [comment for page in pages for comment in page]


def current_head(repo: str, pr: int) -> str:
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{pr}", "--jq", ".head.sha"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Could not verify the PR head")
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="GitHub owner/repository")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--comments", required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be owner/repository")
    if args.pr < 1 or not re.fullmatch(r"[0-9a-f]{40}", args.head_sha):
        parser.error("--pr and --head-sha are invalid")

    comments = json.loads(args.comments.read_text())
    if not isinstance(comments, list):
        parser.error("--comments must contain a JSON array")
    if not comments:
        print(json.dumps({"posted": 0, "updated": 0, "unchanged": 0}))
        return

    for comment in comments:
        if not isinstance(comment, dict):
            parser.error("Each inline comment must be a JSON object")
        ident = comment.get("id")
        body = comment.get("body")
        if not isinstance(ident, str) or not re.fullmatch(r"F[1-9]\d*", ident):
            parser.error("Each inline comment needs an ID in the F1, F2, ... format")
        if not isinstance(body, str) or not body.startswith(f"**[{ident}]** "):
            parser.error(f"Inline comment {ident} must start with its finding ID")
        labels = ("Location", "Trigger", "Impact", "Evidence", "Suggested fix")
        if any(f"**{label}:**" not in body for label in labels):
            parser.error(f"Inline comment {ident} must include all bold finding labels")
        if f"<!-- fabric-access-finding:{ident} -->" not in body:
            parser.error(f"Inline comment {ident} is missing its idempotency marker")
        if not isinstance(comment.get("path"), str) or not comment["path"]:
            parser.error(f"Inline comment {ident} needs a path")
        if not isinstance(comment.get("line"), int) or comment["line"] < 1:
            parser.error(f"Inline comment {ident} needs a positive line")
        if comment.get("side") not in {"LEFT", "RIGHT"}:
            parser.error(f"Inline comment {ident} needs a LEFT or RIGHT side")

    comment_ids = [comment["id"] for comment in comments]
    if len(comment_ids) != len(set(comment_ids)):
        parser.error("Inline comment finding IDs must be unique")
    if current_head(args.repo, args.pr) != args.head_sha:
        raise RuntimeError("PR head changed before inline comments could be posted")
    endpoint = f"repos/{args.repo}/pulls/{args.pr}/comments"
    existing = api(endpoint, "GET")
    if not isinstance(existing, list):
        raise RuntimeError("GitHub returned an invalid inline-comment list")
    posted = updated = unchanged = 0
    for comment in comments:
        ident = comment["id"]
        marker = f"<!-- fabric-access-finding:{ident} -->"
        candidates = [
            item for item in existing
            if marker in (item.get("body") or "")
            and item.get("commit_id") == args.head_sha
            and item.get("path") == comment["path"]
        ]
        exact = next((
            item for item in candidates
            if item.get("line") == comment["line"] and item.get("side") == comment["side"]
        ), None)
        if candidates and exact is None:
            raise RuntimeError(f"Finding {ident} already has a current-head comment at a different location")
        if exact:
            if exact.get("body") == comment["body"]:
                unchanged += 1
                continue
            api(
                f"repos/{args.repo}/pulls/comments/{exact['id']}",
                "PATCH",
                {"body": comment["body"]},
            )
            updated += 1
            continue
        created = api(endpoint, "POST", {
            "commit_id": args.head_sha,
            "path": comment["path"],
            "line": comment["line"],
            "side": comment["side"],
            "body": comment["body"],
        })
        if not isinstance(created, dict) or not created.get("id"):
            raise RuntimeError(f"GitHub did not confirm inline comment {ident}")
        existing.append(created)
        posted += 1

    print(json.dumps({"posted": posted, "updated": updated, "unchanged": unchanged}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Inline comment posting failed: {exc}") from exc
