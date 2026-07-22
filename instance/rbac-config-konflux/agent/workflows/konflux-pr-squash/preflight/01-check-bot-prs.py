#!/usr/bin/env python3
"""Preflight check: verify prerequisites and that bot PRs exist to consolidate."""

import json
import shutil
import subprocess

from common import get_capacity, get_tasks, output_result


BOT_AUTHOR = "red-hat-konflux[bot]"
TASK_KEY_PREFIX = "konflux-pr-squash:"


def check_git_repo() -> bool:
    result = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True)
    return result.returncode == 0


def detect_upstream_repo() -> str:
    try:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return ""
    if result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            return data.get("nameWithOwner", "")
        except json.JSONDecodeError:
            pass
    return ""


def find_bot_prs(upstream_repo: str, bot_author: str) -> list[dict]:
    try:
        result = subprocess.run(
            ["gh", "pr", "list",
             "--repo", upstream_repo,
             "--author", bot_author,
             "--state", "open",
             "--json", "number,title,headRefName,url,labels"],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return []

    if result.returncode != 0:
        return []

    try:
        prs = json.loads(result.stdout)
        filtered = []
        for pr in prs:
            labels = [lbl.get("name", "").lower() for lbl in pr.get("labels", [])]
            if any("do not merge" in lbl or "do-not-merge" in lbl for lbl in labels):
                continue
            filtered.append(pr)
        return filtered
    except (json.JSONDecodeError, KeyError):
        return []


def main():
    # Phase 1: Check task system — avoid duplicate work and respect capacity
    tasks = get_tasks()
    active_n, max_n = get_capacity()
    active = [t for t in tasks if t.get("status") in ("in_progress", "pr_open", "pr_changes")]

    consolidation_tasks = [
        t for t in active
        if t.get("external_key", "").startswith(TASK_KEY_PREFIX)
    ]
    if consolidation_tasks:
        keys = ", ".join(t.get("external_key", "") for t in consolidation_tasks)
        output_result("skip", f"Consolidation already in progress: {keys}")
        return

    if active_n >= max_n:
        output_result("skip", f"At capacity ({active_n}/{max_n})")
        return

    # Phase 2: Check tools and environment
    if not shutil.which("gh"):
        output_result("skip", "Required tool 'gh' not found. Install and authenticate with: gh auth login")
        return

    if not check_git_repo():
        output_result("skip", "Not in a git repository")
        return

    # Phase 3: Discover bot PRs
    upstream_repo = detect_upstream_repo()
    if not upstream_repo:
        output_result("skip", "Could not detect upstream repo")
        return

    prs = find_bot_prs(upstream_repo, BOT_AUTHOR)
    if len(prs) < 2:
        output_result("skip", f"Only {len(prs)} open PR(s) from {BOT_AUTHOR} — need at least 2 to consolidate")
        return

    pr_summary = [{"number": pr["number"], "title": pr["title"], "branch": pr["headRefName"]} for pr in prs]
    output_result("start", json.dumps({
        "repo": upstream_repo,
        "bot_author": BOT_AUTHOR,
        "pr_count": len(prs),
        "prs": pr_summary,
        "task_key": f"{TASK_KEY_PREFIX}{upstream_repo}",
    }))


if __name__ == "__main__":
    main()
