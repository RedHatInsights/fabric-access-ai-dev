#!/usr/bin/env python3
"""Preflight check: verify prerequisites and that bot PRs exist to consolidate.

Reads repos from project-repos.json and checks each for open bot PRs.
"""

import json
import subprocess

from common import get_capacity, get_tasks, load_project_repos, output_result, upstream_repo


BOT_AUTHOR = "red-hat-konflux[bot]"
TASK_KEY_PREFIX = "konflux-pr-squash:"


def find_bot_prs(repo_nwo: str, bot_author: str) -> list[dict]:
    try:
        result = subprocess.run(
            ["gh", "pr", "list",
             "--repo", repo_nwo,
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

    if active_n >= max_n:
        output_result("skip", f"At capacity ({active_n}/{max_n})")
        return

    # Phase 2: Load repos from project-repos.json
    project_repos = load_project_repos()
    if not project_repos:
        output_result("skip", "No repos found in project-repos.json")
        return

    # Phase 3: Check each repo for bot PRs
    repos_with_prs = []

    for repo_name, repo_config in project_repos.items():
        repo_nwo, host = upstream_repo(repo_name)
        if not repo_nwo or host != "github":
            continue

        task_key = f"{TASK_KEY_PREFIX}{repo_nwo}"
        already_active = any(
            t.get("external_key", "").startswith(task_key)
            for t in active
        )
        if already_active:
            print(f"  Skipping {repo_nwo}: consolidation already in progress")
            continue

        prs = find_bot_prs(repo_nwo, BOT_AUTHOR)
        if len(prs) >= 2:
            pr_summary = [{"number": pr["number"], "title": pr["title"], "branch": pr["headRefName"]} for pr in prs]
            repos_with_prs.append({
                "repo": repo_nwo,
                "bot_url": repo_config.get("url", ""),
                "pr_count": len(prs),
                "prs": pr_summary,
                "task_key": task_key,
            })

    if not repos_with_prs:
        output_result("skip", f"No repos with 2+ open PRs from {BOT_AUTHOR}")
        return

    output_result("start", json.dumps({
        "bot_author": BOT_AUTHOR,
        "repos": repos_with_prs,
    }))


if __name__ == "__main__":
    main()
