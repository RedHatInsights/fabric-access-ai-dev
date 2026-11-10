#!/usr/bin/env python3
"""Publish a validated JSON review body without creating a Markdown report."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from review_ledger import append_to_body


def add_payload_fields(command: list[str], payload: dict) -> None:
    """Append scalar JSON fields for the runner's remote gh API bridge."""
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            raise ValueError("GitHub API payload keys must be non-empty strings")
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            raise ValueError("GitHub API payload fields must be scalar values")
        rendered = value if isinstance(value, str) else json.dumps(value)
        command.extend(["--field", f"{key}={rendered}"])


def api(endpoint: str, method: str = "GET", payload: dict | None = None,
        paginate: bool = False) -> object:
    command = ["gh", "api", endpoint]
    if paginate:
        command.extend(["--paginate", "--slurp"])
    if payload is not None:
        command.extend(["--method", method])
        add_payload_fields(command, payload)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"gh api failed: {endpoint}")
    data = json.loads(result.stdout)
    if paginate:
        if not isinstance(data, list) or any(not isinstance(page, list) for page in data):
            raise RuntimeError("GitHub returned invalid review pages")
        return [item for page in data for item in page]
    return data


def publish(repo: str, pr: int, head: str, base: str, publication: Path,
            review_id: int | None = None, comment_links: Path | None = None,
            ledger_path: Path | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or pr < 1:
        raise ValueError("Repository or PR number is invalid")
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (head, base)):
        raise ValueError("Review revision is invalid")
    if review_id is not None and review_id < 1:
        raise ValueError("Review ID must be positive")
    data = json.loads(publication.read_text())
    body = data.get("body") if isinstance(data, dict) else None
    marker = f"<!-- dev-review-bot:fabric-access-review:{head}:{base} -->"
    legacy_marker = f"<!-- fabric-access-review:{head}:{base} -->"
    if (not isinstance(body, str) or not body.strip()
            or body.count(marker) + body.count(legacy_marker) != 1):
        raise ValueError("Publication is missing its validated revision marker")
    body = body.replace(legacy_marker, marker)
    links = {}
    if comment_links:
        links = json.loads(comment_links.read_text())
        if not isinstance(links, dict) or any(
            not isinstance(ident, str) or not re.fullmatch(r"F[1-9]\d*", ident)
            or not isinstance(url, str) or not url.startswith("https://github.com/")
            for ident, url in links.items()
        ):
            raise ValueError("Finding comment links must map finding IDs to GitHub URLs")

    def replace_finding_link(match: re.Match[str]) -> str:
        label, ident = match.group(1), match.group(2)
        url = links.get(ident)
        return f"[{label}]({url})" if url else f"{label} (comment unavailable)"

    body = re.sub(r"\[([^\]]+)\]\(review-comment:(F[1-9]\d*)\)", replace_finding_link, body)
    if ledger_path:
        ledger = json.loads(ledger_path.read_text())
        review_numbers = re.findall(r"(?m)^## Review ([1-9]\d*)$", body)
        if len(review_numbers) != 1:
            raise ValueError("Publication must contain exactly one review number")
        body = append_to_body(body, repo, pr, head, base, int(review_numbers[0]) - 1, ledger)

    pr_state = api(f"repos/{repo}/pulls/{pr}")
    if (not isinstance(pr_state, dict) or pr_state.get("state") != "open"
            or not isinstance(pr_state.get("head"), dict)
            or not isinstance(pr_state.get("base"), dict)
            or pr_state["head"].get("sha") != head
            or pr_state["base"].get("sha") != base):
        raise RuntimeError("PR head or base changed before review publication")

    endpoint = f"repos/{repo}/pulls/{pr}/reviews"
    if review_id is None:
        actor = api("user")
        if not isinstance(actor, dict) or not isinstance(actor.get("login"), str):
            raise RuntimeError("Could not identify the GitHub reviewer")
        reviews = api(f"{endpoint}?per_page=100", paginate=True)
        matches = [item for item in reviews if isinstance(item, dict)
                   and isinstance(item.get("user"), dict)
                   and item["user"].get("login", "").casefold() == actor["login"].casefold()
                   and (marker in (item.get("body") or "")
                        or legacy_marker in (item.get("body") or ""))]
        if len(matches) > 1:
            raise RuntimeError("Multiple bot reviews contain this revision marker")
        if matches:
            existing = matches[0]
            if existing.get("state") == "PENDING" or not isinstance(existing.get("id"), int):
                raise RuntimeError("Matching review is not a submitted review")
            review_id = existing["id"]
            if existing.get("body") == body:
                return {"review_id": review_id, "review_url": existing.get("html_url"),
                        "action": "recovered"}

    if review_id is None:
        result = api(endpoint, "POST", {"commit_id": head, "event": "COMMENT", "body": body})
        action = "posted"
    else:
        result = api(f"{endpoint}/{review_id}", "PUT", {"body": body})
        action = "updated"
    if not isinstance(result, dict) or not isinstance(result.get("id"), int):
        raise RuntimeError("GitHub did not confirm review publication")
    if action == "updated" and result["id"] != review_id:
        raise RuntimeError("GitHub returned a different review ID")
    return {"review_id": result["id"], "review_url": result.get("html_url"), "action": action}


def prepare_publication(repo: str, pr: int, head: str, base: str,
                        publication: Path, ledger_path: Path) -> dict:
    """Embed the ledger before pending-publication state is recorded."""
    data = json.loads(publication.read_text())
    body = data.get("body") if isinstance(data, dict) else None
    if not isinstance(body, str):
        raise ValueError("Publication body is missing")
    ledger = json.loads(ledger_path.read_text())
    review_numbers = re.findall(r"(?m)^## Review ([1-9]\d*)$", body)
    if len(review_numbers) != 1:
        raise ValueError("Publication must contain exactly one review number")
    data["body"] = append_to_body(
        body, repo, pr, head, base, int(review_numbers[0]) - 1, ledger
    )
    publication.write_text(json.dumps(data, indent=2) + "\n")
    return {"prepared": True, "ledger_entries": len(ledger), "publication": str(publication)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--publication", required=True, type=Path)
    parser.add_argument("--review-id", type=int)
    parser.add_argument("--comment-links", type=Path,
                        help="Finding comment URLs created or found by post_inline_comments.py")
    parser.add_argument("--ledger", type=Path, required=True,
                        help="Validated ledger to embed in the status review for future cycles")
    parser.add_argument("--prepare-only", action="store_true",
                        help="Embed the ledger in the publication file without posting to GitHub")
    args = parser.parse_args()
    try:
        if args.prepare_only:
            if not args.ledger:
                parser.error("--prepare-only requires --ledger")
            print(json.dumps(prepare_publication(
                args.repo, args.pr, args.head_sha, args.base_sha, args.publication, args.ledger
            )))
            return
        print(json.dumps(publish(args.repo, args.pr, args.head_sha, args.base_sha,
                                 args.publication, args.review_id, args.comment_links, args.ledger)))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"Review publication failed: {exc}\n")


if __name__ == "__main__":
    main()
