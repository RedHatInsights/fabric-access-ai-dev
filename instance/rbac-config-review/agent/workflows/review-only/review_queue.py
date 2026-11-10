"""Find one labeled GitHub PR requiring a review or feedback check."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from common import INSTANCE_ID, MEMORY_URL, http_get, load_project_repos, output_result
from review_config import ReviewConfigError, github_repo_url, load_config
from review_ledger import (
    EmptyPendingReview,
    LedgerError,
    LedgerPayloadMissing,
    decode_body,
    public_ledger,
    restore_from_review,
    write_cycle_ledger,
)
from rereview_requests import fetch_comments, requests as rereview_requests

TASK_PREFIX = "review-only:github:"
ACTIVE = {"in_progress", "pr_open", "pr_changes"}
RESUMABLE = {"in_progress", "pr_changes"}
PR_FIELDS = "number,title,url,headRefOid,baseRefOid,headRefName,isDraft,author,updatedAt"
PR_STATE_FIELDS = "state,mergedAt,closedAt,headRefOid,baseRefOid,updatedAt,url,title,headRefName"


def task_key(repo: str, number: int) -> str:
    return f"{TASK_PREFIX}{repo}#{number}"


def load_tasks() -> list[dict]:
    """Load all task states so archived keys suppress duplicate task creation."""
    tasks: list[dict] = []
    offset = 0
    while True:
        params = {"limit": 100, "offset": offset}
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


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _ledger_recovery_id(chosen: dict, meta: dict) -> str:
    key = "|".join(str(value or "") for value in (
        chosen["repo"], chosen["number"], meta.get("review_id"),
        meta.get("last_reviewed_sha"), meta.get("last_reviewed_base_sha"),
        chosen["head_sha"], chosen["base_sha"],
    ))
    return "LR-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _request_ledger_recovery(chosen: dict, meta: dict) -> bool:
    """Ask the PR author before discarding history when every ledger copy is gone."""
    recovery_id = _ledger_recovery_id(chosen, meta)
    marker = f"<!-- dev-review-bot:ledger-recovery:{recovery_id} -->"
    comments = fetch_comments(chosen["repo"], chosen["number"])
    bot_login = _bot_login()
    requests = []
    for comment in comments:
        user = comment.get("user") or {}
        body = comment.get("body")
        login = user.get("login") if isinstance(user, dict) else None
        if (isinstance(login, str) and login.casefold() == bot_login.casefold()
                and isinstance(body, str) and marker in body):
            created = _parse_timestamp(comment.get("created_at"))
            if created is not None:
                requests.append((created, comment))

    if not requests:
        author = chosen.get("pr_author")
        if not isinstance(author, str) or not author.strip():
            raise RuntimeError("Cannot ask about ledger recovery because the PR author is unknown")
        body = (
            f"**Review history needs recovery ({recovery_id}).**\n\n"
            "I could not find a usable ledger in the status review, saved publication, or local ledger. "
            "A fresh review will restart the finding history and may assign new finding IDs. "
            f"Should I run a fresh re-review for @{author}?\n\n"
            f"Reply exactly `Review bot: yes {recovery_id}` to approve, or "
            f"`Review bot: no {recovery_id}` to leave this PR paused.\n\n{marker}"
        )
        result = subprocess.run(
            ["gh", "api", f"repos/{chosen['repo']}/issues/{chosen['number']}/comments",
             "--method", "POST", "--field", f"body={body}"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Cannot ask PR author about ledger recovery: {result.stderr.strip()[:300]}")
        chosen["ledger_recovery_state"] = "asked"
        return False

    request_at, _request_comment = min(requests, key=lambda item: item[0])
    pr_author = chosen.get("pr_author")
    if not isinstance(pr_author, str) or not pr_author.strip():
        chosen["ledger_recovery_state"] = "waiting"
        return False
    expected = {
        f"review bot: yes {recovery_id}": True,
        f"review bot: no {recovery_id}": False,
    }
    answers = []
    for comment in comments:
        user = comment.get("user") or {}
        created = _parse_timestamp(comment.get("created_at"))
        body = comment.get("body")
        if (not isinstance(user, dict) or user.get("type") != "User"
                or not isinstance(user.get("login"), str)
                or user["login"].casefold() != pr_author.casefold()
                or created is None or created <= request_at or not isinstance(body, str)):
            continue
        decision = expected.get(body.strip().casefold())
        if decision is not None:
            answers.append((created, decision))
    if not answers:
        chosen["ledger_recovery_state"] = "waiting"
        return False
    _answer_at, approved = max(answers, key=lambda item: item[0])
    chosen["ledger_recovery_state"] = "approved" if approved else "declined"
    return approved


def restore_prior_ledger(chosen: dict, task: dict | None) -> bool:
    """Restore the status-review ledger, then saved copies, before any model run."""
    if chosen.get("kind") not in {"review", "resume", "feedback_check"}:
        return True
    if task is None:
        chosen["update_count"] = 0
        return True
    meta = _metadata(task)
    had_review = bool(meta.get("review_id") or meta.get("last_reviewed_sha")
                      or meta.get("update_count", 0) or task.get("status") in {"pr_open", "pr_changes"})
    if not had_review:
        chosen["update_count"] = 0
        return True

    payload = None
    empty_pending: EmptyPendingReview | None = None
    review_id = meta.get("review_id")
    if isinstance(review_id, int) and not isinstance(review_id, bool) and review_id > 0:
        try:
            payload = restore_from_review(
                chosen["repo"], chosen["number"], review_id, _bot_login(), chosen["head_sha"]
            )
        except EmptyPendingReview as exc:
            empty_pending = exc
        except LedgerPayloadMissing:
            # Upgrade path for existing reviews: use an old local ledger once,
            # then the next published status comment carries the durable copy.
            payload = None
    elif review_id is not None:
        raise LedgerError("Tracked review ID is invalid")

    # The published status review is authoritative. An interrupted publication
    # is the next recoverable copy, followed by the legacy local ledger.
    if payload is None:
        pending = meta.get("pending_publication")
        pending_body = pending.get("publication_body") if isinstance(pending, dict) else None
        if isinstance(pending_body, str):
            try:
                pending_ledger = decode_body(pending_body, chosen["repo"], chosen["number"])
            except LedgerPayloadMissing:
                pending_ledger = None
            if (pending_ledger is not None
                    and pending_ledger["head_sha"] == chosen["head_sha"]
                    and pending_ledger["base_sha"] == chosen["base_sha"]):
                payload = pending_ledger

    if payload is None:
        legacy_path = meta.get("ledger_path")
        if isinstance(legacy_path, str) and Path(legacy_path).is_file():
            try:
                ledger = public_ledger(json.loads(Path(legacy_path).read_text()))
            except (OSError, json.JSONDecodeError) as exc:
                raise LedgerError(f"Could not read the legacy ledger: {exc}") from exc
            update_count = meta.get("update_count", 0)
            if not isinstance(update_count, int) or isinstance(update_count, bool) or update_count < 0:
                raise LedgerError("Legacy review update_count is invalid")
        else:
            if not _request_ledger_recovery(chosen, meta):
                return False
            chosen["update_count"] = 0
            chosen["ledger_recovery_approved"] = True
            if empty_pending is not None and not empty_pending.reusable:
                chosen["ignore_review_id"] = True
            return True
    else:
        ledger = payload["findings"]
        update_count = payload["update_count"]

    chosen["ledger_path"] = write_cycle_ledger(
        chosen["repo_key"], chosen["number"], chosen["head_sha"], chosen["base_sha"], ledger
    )
    chosen["update_count"] = update_count
    if empty_pending is not None and not empty_pending.reusable:
        chosen["ignore_review_id"] = True
    return True


def _bot_login() -> str:
    configured = os.environ.get("GH_USER_NAME", "").strip()
    if configured:
        return configured
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Cannot identify the GitHub bot account")
    return result.stdout.strip()


def _api_items(endpoint: str) -> list[dict]:
    result = subprocess.run(
        ["gh", "api", endpoint, "--paginate", "--slurp"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Cannot inspect PR feedback: {result.stderr.strip()[:300]}")
    pages = json.loads(result.stdout)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("GitHub returned invalid PR feedback pages")
    return [item for page in pages for item in page if isinstance(item, dict)]


def _external_feedback_key(item: dict, since: str, through: str,
                           key_prefix: str, seen_ids: set[str], bot_login: str) -> str | None:
    if key_prefix == "issue" and isinstance(item.get("body"), str):
        # A negative ledger-recovery answer closes that request without making
        # every later preflight treat the same control comment as new feedback.
        if re.fullmatch(r"Review bot: no LR-[0-9a-f]{12}", item["body"].strip(), re.IGNORECASE):
            return None
    user = item.get("user") or item.get("author") or {}
    login = user.get("login") if isinstance(user, dict) else None
    if (not isinstance(login, str) or not login.strip()
            or user.get("type") == "Bot"
            or login.casefold() == bot_login.casefold()):
        return None
    timestamp = (item.get("updated_at") or item.get("created_at")
                 or item.get("submitted_at"))
    ident = item.get("id")
    if (not isinstance(timestamp, str) or not since <= timestamp <= through
            or not isinstance(ident, int) or isinstance(ident, bool) or ident <= 0):
        return None
    key = f"{key_prefix}:{ident}"
    return None if key in seen_ids else key


def external_feedback_ids(repo: str, number: int, since: str, through: str,
                          seen_ids: set[str]) -> list[str]:
    """Return same-window human feedback IDs, excluding bot-only PR updates."""
    found: list[str] = []
    bot_login = _bot_login()
    sources = [
        ("issue", fetch_comments(repo, number, since)),
        ("review-comment", _api_items(f"repos/{repo}/pulls/{number}/comments?per_page=100")),
        ("review", _api_items(f"repos/{repo}/pulls/{number}/reviews?per_page=100")),
    ]
    for prefix, items in sources:
        for item in items:
            key = _external_feedback_key(item, since, through, prefix, seen_ids, bot_login)
            if key:
                found.append(key)
    return found


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
        "pr_author": author,
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
            if task and task.get("status") == "archived":
                # Keep the unique task key reserved. Operators can unarchive
                # this existing record when the PR should be reviewed again.
                continue
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
                meta = _metadata(task or {})
                seen_feedback = meta.get("seen_feedback_comment_ids") or []
                if (not isinstance(seen_feedback, list)
                        or any(not isinstance(ident, str) for ident in seen_feedback)):
                    errors.append(f"{repo}#{pr['number']}: Invalid seen_feedback_comment_ids")
                    continue
                since = meta.get("last_seen_updated_at") or meta.get("last_reviewed_at") or "1970-01-01T00:00:00Z"
                try:
                    feedback_ids = external_feedback_ids(
                        repo, pr["number"], since, item["updated_at"], set(seen_feedback)
                    ) if task and item["kind"] in {"review", "feedback_check"} else []
                    if item["kind"] != "feedback_check" or feedback_ids:
                        item["feedback_comment_ids"] = feedback_ids
                        found.append(item)
                except (RuntimeError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
                    if item["kind"] == "feedback_check":
                        errors.append(f"{repo}#{pr['number']}: {exc}")
                        continue
                    item["feedback_comment_ids"] = []
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
                seen_feedback = meta.get("seen_feedback_comment_ids") or []
                if (not isinstance(seen_feedback, list)
                        or any(not isinstance(ident, str) for ident in seen_feedback)):
                    raise ValueError("Invalid seen_feedback_comment_ids")
                feedback_ids = external_feedback_ids(
                    probe["repo"], probe["number"], since, through, set(seen_feedback)
                )
                if rereview_requests(comments, since, through, set(seen_ids)) or feedback_ids:
                    probe["feedback_comment_ids"] = feedback_ids
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
        task = next((item for item in tasks if item.get("external_key") == chosen["task_key"]), None)
        try:
            proceed = restore_prior_ledger(chosen, task)
        except (LedgerError, OSError, subprocess.TimeoutExpired) as exc:
            output_result("error", f"{chosen['repo']}#{chosen['number']}: {exc}")
            return
        if not proceed:
            state = chosen.get("ledger_recovery_state", "waiting")
            details = {
                "asked": "Asked the PR author whether to start a fresh review",
                "waiting": "Waiting for the PR author's ledger-recovery decision",
                "declined": "The PR author declined a fresh review",
            }.get(state, "Waiting for the PR author's ledger-recovery decision")
            output_result("skip", f"{chosen['repo']}#{chosen['number']}: {details}")
            return
        chosen.pop("priority")
        output_result("start", json.dumps({"work": chosen, "discovery_errors": errors}))
    except (ReviewConfigError, RuntimeError, ValueError) as exc:
        print(f"Review queue error: {exc}", file=sys.stderr)
        output_result("error", str(exc))


if __name__ == "__main__":
    main()
