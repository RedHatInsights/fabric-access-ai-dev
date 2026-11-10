"""Find one labeled GitHub PR requiring a review or feedback check."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from urllib.parse import urlencode

from common import INSTANCE_ID, MEMORY_URL, http_get, load_project_repos, output_result
from review_config import ReviewConfigError, github_repo_url, load_config
from rereview_requests import fetch_comments, requests as rereview_requests

TASK_PREFIX = "review-only:github:"
ACTIVE = {"in_progress", "pr_open", "pr_changes"}
RESUMABLE = {"in_progress", "pr_changes"}
PR_FIELDS = "number,title,url,headRefOid,baseRefOid,headRefName,isDraft,author,updatedAt"
PR_STATE_FIELDS = "state,mergedAt,closedAt,headRefOid,baseRefOid,updatedAt,url,title,headRefName"


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


def view_pr_state(repo: str, number: int) -> dict:
    result = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json", PR_STATE_FIELDS],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"gh pr view {repo}#{number}: {result.stderr.strip()[:300]}")
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or data.get("state") not in {"OPEN", "CLOSED", "MERGED"}:
        raise RuntimeError(f"gh pr view {repo}#{number} returned an invalid state")
    return data


def _metadata(task: dict) -> dict:
    meta = task.get("metadata") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    return meta if isinstance(meta, dict) else {}


def _task_pr(task: dict, repos: dict) -> tuple[str, str, int] | None:
    key = task.get("external_key", "")
    if not key.startswith(TASK_PREFIX):
        return None
    target, separator, number = key[len(TASK_PREFIX):].rpartition("#")
    if not separator or not number.isdigit():
        return None
    for repo_key, entry in repos.items():
        try:
            repo = github_repo_url(entry["upstream"])
        except (KeyError, ReviewConfigError):
            continue
        if repo == target:
            return repo_key, repo, int(number)
    return None


def lifecycle_work(task: dict, repo_key: str, repo: str, number: int, pr: dict) -> dict | None:
    status = task.get("status")
    meta = _metadata(task)
    common = {
        "priority": 0,
        "task_key": task["external_key"],
        "task_exists": True,
        "task_active": status in ACTIVE,
        "repo_key": repo_key,
        "repo": repo,
        "number": number,
        "title": pr.get("title") or task.get("title", ""),
        "url": pr.get("url") or f"https://github.com/{repo}/pull/{number}",
        "head_sha": pr.get("headRefOid") or meta.get("last_reviewed_sha"),
        "base_sha": pr.get("baseRefOid") or meta.get("last_reviewed_base_sha"),
        "updated_at": pr.get("updatedAt", ""),
    }
    if pr.get("state") == "MERGED" or pr.get("mergedAt"):
        if status == "done" and meta.get("merged_at"):
            return None
        return {**common, "kind": "merge_complete", "merged_at": pr.get("mergedAt")}
    if pr.get("state") == "CLOSED":
        if status == "paused" and meta.get("pr_lifecycle") == "closed_unmerged":
            return None
        return {**common, "kind": "closed_unmerged"}
    if pr.get("state") == "OPEN":
        if status == "done" and not meta.get("merged_at"):
            return {**common, "kind": "reconcile_open", "priority": 3}
        if status == "paused" and meta.get("pr_lifecycle") == "closed_unmerged":
            return {**common, "kind": "reconcile_open", "priority": 3}
    return None


def candidate(pr: dict, repo_key: str, repo: str, task: dict | None,
              pending_rereview: bool = False) -> dict | None:
    if not isinstance(pr.get("number"), int):
        return None
    author = (pr.get("author") or {}).get("login", "")
    if author.lower() == os.environ.get("GH_USER_NAME", "").lower() and author:
        return None
    sha = pr.get("headRefOid")
    base_sha = pr.get("baseRefOid")
    if not isinstance(sha, str) or not sha or not isinstance(base_sha, str) or not base_sha:
        return None
    meta = _metadata(task or {})
    status = (task or {}).get("status")
    if pr.get("isDraft"):
        if status == "done" and not meta.get("merged_at"):
            kind, priority = "reconcile_open", 3
        else:
            return None
    elif status == "paused" and meta.get("paused_sha") == sha and meta.get("paused_base_sha") == base_sha:
        if pr.get("updatedAt", "") <= meta.get("last_seen_updated_at", "") and not pending_rereview:
            if meta.get("pr_lifecycle") == "closed_unmerged":
                kind, priority = "reconcile_open", 3
            else:
                return None
        else:
            kind, priority = "feedback_check", 2
    elif status in RESUMABLE:
        kind, priority = "resume", 0
    elif meta.get("last_reviewed_sha") != sha or meta.get("last_reviewed_base_sha") != base_sha:
        kind, priority = "review", 1
    elif pr.get("updatedAt", "") > meta.get("last_seen_updated_at", "") or pending_rereview:
        kind, priority = "feedback_check", 2
    elif status == "done" and not meta.get("merged_at"):
        kind, priority = "reconcile_open", 3
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
    boundary_probes: list[tuple[dict, dict, str, str]] = []
    open_pr_keys: set[str] = set()
    failed_repos: set[str] = set()
    for repo_key, entry in repos.items():
        try:
            repo = github_repo_url(entry["upstream"])
        except (KeyError, ReviewConfigError):
            continue  # The review-only instance handles GitHub repositories.
        try:
            prs = list_prs(repo, config["review_label"])
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            errors.append(f"{repo}: {exc}")
            failed_repos.add(repo)
            continue
        for pr in prs:
            key = task_key(repo, pr.get("number"))
            open_pr_keys.add(key)
            task = by_key.get(key)
            item = candidate(pr, repo_key, repo, task)
            if item is None and task and task.get("status") in {"done", "paused", "pr_open"}:
                # updatedAt has second precision: a new command can share the
                # last-seen timestamp and otherwise never enter feedback_check.
                probe = candidate(pr, repo_key, repo, task, pending_rereview=True)
                meta = _metadata(task)
                since, through = meta.get("last_seen_updated_at"), pr.get("updatedAt")
                if probe and probe["kind"] == "feedback_check" and since and through and since <= through:
                    boundary_probes.append((probe, meta, since, through))
            if item:
                found.append(item)
    for task in tasks:
        status = task.get("status")
        meta = _metadata(task)
        if status not in {"pr_open", "in_progress", "pr_changes", "paused"} and not (
            status == "done" and not meta.get("merged_at")
        ):
            continue
        task_ref = _task_pr(task, repos)
        if task_ref is None:
            continue
        repo_key, repo, number = task_ref
        if repo in failed_repos:
            continue
        if task_key(repo, number) in open_pr_keys:
            continue
        try:
            state = view_pr_state(repo, number)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            errors.append(f"{repo}#{number}: {exc}")
            continue
        item = lifecycle_work(task, repo_key, repo, number, state)
        if item:
            found.append(item)
    # Only probe timestamp-boundary comments when no higher-priority work is
    # ready. These requests are independently authorized by the runner.
    if not found:
        boundary_probes.sort(key=lambda entry: (entry[0]["updated_at"], entry[0]["repo"], entry[0]["number"]))
        for probe, meta, since, through in boundary_probes:
            try:
                seen_ids = meta.get("seen_rereview_comment_ids") or []
                if not isinstance(seen_ids, list) or any(type(ident) is not int or ident <= 0 for ident in seen_ids):
                    raise ValueError("Invalid seen_rereview_comment_ids")
                comments = fetch_comments(probe["repo"], probe["number"], since)
                if rereview_requests(comments, since, through, set(seen_ids)):
                    found.append(probe)
                    break
            except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
                errors.append(f"{probe['repo']}#{probe['number']}: {exc}")
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
