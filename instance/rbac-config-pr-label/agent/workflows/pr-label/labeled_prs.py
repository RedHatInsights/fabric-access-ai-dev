"""Find open GitHub PRs labeled for the bot and emit preflight start/skip."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from common import (
    get_capacity,
    get_tasks,
    load_project_repos,
    output_result,
    save_state,
    upstream_repo,
)

TASK_KEY_PREFIX = "pr-label:"
# GitHub PR label — independent of BOT_LABEL (that env is the Jira ticket label).
PR_LABEL = os.environ.get("BOT_PR_LABEL") or "dev-bot"
_GH_PR_JSON = (
    "number,title,url,headRefName,author,isCrossRepository,maintainerCanModify"
)
_FOREIGN_BRANCH_PREFIXES = ("bot/", "chore/consolidate-")
_FOREIGN_AUTHORS = frozenset(
    {
        "platex-rehor-bot",
        "red-hat-konflux[bot]",
        "red-hat-konflux",
    }
)


def task_key(owner_repo: str, number: int) -> str:
    return f"{TASK_KEY_PREFIX}{owner_repo}#{number}"


def is_tracked(key: str, tasks: list[dict]) -> bool:
    return any(t.get("external_key") == key for t in tasks)


def own_tasks(tasks: list[dict]) -> list[dict]:
    """Keep only this workflow's tasks so a shared instance_id cannot mix in Jira/Konflux work."""
    return [t for t in tasks if str(t.get("external_key", "")).startswith(TASK_KEY_PREFIX)]


def _author_login(pr: dict) -> str:
    author = pr.get("author") or {}
    if isinstance(author, dict):
        return author.get("login", "?")
    return str(author) if author else "?"


def is_foreign_pr(pr: dict) -> bool:
    """True if this PR belongs to the Jira bot or Konflux squash workflow."""
    branch = (pr.get("headRefName") or "").lower()
    if branch.startswith(_FOREIGN_BRANCH_PREFIXES):
        return True
    author = _author_login(pr).lower()
    bot_user = (os.environ.get("GH_USER_NAME") or "").lower()
    if bot_user and author == bot_user:
        return True
    return author in _FOREIGN_AUTHORS


def github_repos(project_repos: dict) -> list[tuple[str, str]]:
    found = []
    for name in project_repos:
        nwo, host = upstream_repo(name)
        if nwo and host == "github":
            found.append((name, nwo))
    return found


def can_push(pr: dict) -> bool:
    if pr.get("isCrossRepository"):
        return bool(pr.get("maintainerCanModify"))
    return True


def gh_pr_list(owner_repo: str, label: str) -> list[dict]:
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                owner_repo,
                "--label",
                label,
                "--state",
                "open",
                "--json",
                _GH_PR_JSON,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        print(f"  ERR gh pr list timed out for {owner_repo}", file=sys.stderr)
        return []

    if result.returncode != 0:
        err = (result.stderr or "").strip()[:200]
        print(f"  ERR gh pr list {owner_repo}: {err}", file=sys.stderr)
        return []

    try:
        return json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        print(f"  ERR gh pr list {owner_repo}: invalid JSON", file=sys.stderr)
        return []


def main() -> None:
    tasks = own_tasks(get_tasks())
    active_n = len([t for t in tasks if t.get("status") in ("in_progress", "pr_open", "pr_changes")])
    _, max_n = get_capacity()
    if active_n >= max_n:
        output_result("skip", f"At capacity ({active_n}/{max_n})")
        return

    project_repos = load_project_repos()
    if not project_repos:
        output_result("skip", "No repos found in project-repos.json")
        return

    gh_repos = github_repos(project_repos)
    if not gh_repos:
        output_result("skip", "No GitHub repos in project-repos.json")
        return

    candidates = []
    tracked = 0
    foreign = 0
    for _name, nwo in gh_repos:
        for pr in gh_pr_list(nwo, PR_LABEL):
            number = pr.get("number")
            if not number:
                continue
            if is_foreign_pr(pr):
                foreign += 1
                print(
                    f"  Skipping {nwo}#{number}: owned by Jira/Konflux bot "
                    f"(branch={pr.get('headRefName')}, author={_author_login(pr)})",
                    file=sys.stderr,
                )
                continue
            key = task_key(nwo, number)
            if is_tracked(key, tasks):
                tracked += 1
                continue
            fork = bool(pr.get("isCrossRepository"))
            candidates.append(
                {
                    "repo": nwo,
                    "number": number,
                    "title": pr.get("title", ""),
                    "url": pr.get("url", ""),
                    "branch": pr.get("headRefName", ""),
                    "author": _author_login(pr),
                    "is_fork": fork,
                    "can_push": can_push(pr),
                    "task_key": key,
                }
            )

    if not candidates:
        bits = []
        if tracked:
            bits.append(f"{tracked} already tracked")
        if foreign:
            bits.append(f"{foreign} owned by other-bot workflows")
        extra = f" ({', '.join(bits)})" if bits else ""
        output_result("skip", f"No open PRs with label {PR_LABEL!r}{extra}")
        return

    save_state({"labeled_prs": len(candidates)})
    output_result(
        "start",
        json.dumps({"label": PR_LABEL, "prs": candidates}, indent=2),
    )


if __name__ == "__main__":
    main()
