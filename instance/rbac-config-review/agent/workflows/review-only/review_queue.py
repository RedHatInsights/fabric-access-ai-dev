"""Find one labeled GitHub PR requiring a review or feedback check."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from urllib.parse import urlencode

from common import INSTANCE_ID, MEMORY_URL, http_get, load_project_repos, output_result
from review_config import ReviewConfigError, github_repo_url, load_config

TASK_PREFIX = "review-only:github:"
ACTIVE = {"in_progress", "pr_open", "pr_changes"}
PR_FIELDS = "number,title,url,headRefOid,baseRefOid,headRefName,isDraft,author,updatedAt"


def task_key(repo: str, number: int) -> str:
    return f"{TASK_PREFIX}{repo}#{number}"


def load_tasks() -> list[dict]:
    """Page through task state; a failed memory read must not imply no reviews."""
    tasks: list[dict] = []
    offset = 0
    while True:
        params = {"exclude_status": "archived", "limit": 100, "offset": offset}
        if INSTANCE_ID:
            params["instance_id"] = INSTANCE_ID
        data = http_get(f"{MEMORY_URL}/api/tasks?{urlencode(params)}")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise RuntimeError("Memory task list unavailable")
        items = data["items"]
        tasks.extend(
            item for item in items
            if item.get("source_type") == "github"
            and item.get("external_key", "").startswith(TASK_PREFIX)
        )
        offset += len(items)
        if not items or offset >= data.get("total", offset):
            return tasks


def list_prs(repo: str, label: str) -> list[dict]:
    result = subprocess.run(
        [
            "gh", "pr", "list", "--repo", repo, "--label", label, "--state", "open",
            "--limit", "1000", "--json", PR_FIELDS,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"gh pr list {repo}: {result.stderr.strip()[:300]}")
    data = json.loads(result.stdout)
    if not isinstance(data, list):
        raise RuntimeError(f"gh pr list {repo} returned no list")
    return data


def candidate(pr: dict, repo_key: str, repo: str, task: dict | None) -> dict | None:
    if pr.get("isDraft") or not isinstance(pr.get("number"), int):
        return None
    author = (pr.get("author") or {}).get("login", "")
    if author.lower() == os.environ.get("GH_USER_NAME", "").lower() and author:
        return None
    sha = pr.get("headRefOid")
    base_sha = pr.get("baseRefOid")
    if not isinstance(sha, str) or not sha or not isinstance(base_sha, str) or not base_sha:
        return None
    meta = (task or {}).get("metadata") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    status = (task or {}).get("status")
    if status == "paused" and meta.get("paused_sha") == sha and meta.get("paused_base_sha") == base_sha:
        return None
    if status in ACTIVE:
        kind, priority = "resume", 0
    elif meta.get("last_reviewed_sha") != sha or meta.get("last_reviewed_base_sha") != base_sha:
        kind, priority = "review", 1
    elif pr.get("updatedAt", "") > meta.get("last_seen_updated_at", ""):
        kind, priority = "feedback_check", 2
    else:
        return None
    return {
        "kind": kind,
        "priority": priority,
        "task_key": task_key(repo, pr["number"]),
        "task_exists": task is not None,
        "task_active": status in ACTIVE,
        "repo_key": repo_key,
        "repo": repo,
        "number": pr["number"],
        "title": pr.get("title", ""),
        "url": pr.get("url", ""),
        "head_sha": sha,
        "base_sha": base_sha,
        "head_branch": pr.get("headRefName", ""),
        "updated_at": pr.get("updatedAt", ""),
    }


def pick_work(repos: dict, config: dict, tasks: list[dict]) -> tuple[dict | None, list[str]]:
    by_key = {task["external_key"]: task for task in tasks}
    found: list[dict] = []
    errors: list[str] = []
    for repo_key, entry in repos.items():
        try:
            repo = github_repo_url(entry["upstream"])
        except (KeyError, ReviewConfigError):
            continue  # The review-only instance handles GitHub repositories.
        try:
            prs = list_prs(repo, config["review_label"])
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            errors.append(f"{repo}: {exc}")
            continue
        for pr in prs:
            key = task_key(repo, pr.get("number"))
            item = candidate(pr, repo_key, repo, by_key.get(key))
            if item:
                found.append(item)
    found.sort(key=lambda item: (item["priority"], item["updated_at"], item["repo"], item["number"]))
    return (found[0] if found else None), errors


def main() -> None:
    try:
        config = load_config()
        repos = load_project_repos()
        if not repos:
            output_result("error", "No repositories in project-repos.json")
            return
        tasks = load_tasks()
        chosen, errors = pick_work(repos, config, tasks)
        if chosen is None:
            status = "error" if errors else "skip"
            detail = "; ".join(errors) if errors else "No labeled PR needs review or feedback handling"
            output_result(status, detail)
            return
        active_count = sum(task.get("status") in ACTIVE for task in tasks)
        if chosen["kind"] == "review" and not chosen["task_active"] and active_count >= 10:
            output_result("skip", f"Review instance at task capacity ({active_count}/10)")
            return
        chosen.pop("priority")
        output_result("start", json.dumps({"work": chosen, "discovery_errors": errors}))
    except (ReviewConfigError, RuntimeError, ValueError) as exc:
        print(f"Review queue error: {exc}", file=sys.stderr)
        output_result("error", str(exc))


if __name__ == "__main__":
    main()
