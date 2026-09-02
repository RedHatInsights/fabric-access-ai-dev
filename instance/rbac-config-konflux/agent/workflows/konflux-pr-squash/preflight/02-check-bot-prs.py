#!/usr/bin/env python3
"""Preflight check: verify prerequisites and that bot PRs exist to consolidate.

Reads repos from project-repos.json and checks each for open bot PRs.
"""

import json
import re
import subprocess
import sys
from collections import defaultdict

from common import get_capacity, get_tasks, load_project_repos, output_result, upstream_repo

BOT_AUTHOR = "red-hat-konflux[bot]"
TASK_KEY_PREFIX = "konflux-pr-squash:"


def find_bot_prs(repo_nwo: str, bot_author: str) -> list[dict]:
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo_nwo,
                "--author",
                bot_author,
                "--state",
                "open",
                "--json",
                "number,title,headRefName,url,labels",
            ],
            capture_output=True,
            text=True,
            timeout=60,
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
            if "abandoned" in pr.get("title", "").lower():
                continue
            filtered.append(pr)
        return filtered
    except (json.JSONDecodeError, KeyError):
        return []


def has_open_consolidation_pr(repo_nwo: str) -> bool:
    """Check GitHub directly for an already-open consolidation PR/branch.

    This is a defense-in-depth check independent of the task system: the task
    that de-dupes runs is only recorded *after* a consolidated PR is pushed
    (see CLAUDE.md), so a crash or a failed task_add between "PR pushed" and
    "task recorded" leaves no trace in the task store. Without this check,
    the next preflight run sees the same still-open original bot PRs
    (originals are kept open via --keep-originals) and consolidates them
    again, producing a duplicate PR.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo_nwo,
                "--state",
                "open",
                "--search",
                "chore(deps): consolidate in:title",
                "--json",
                "number,title,headRefName",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        # Fail closed: if we can't verify, don't risk creating a duplicate.
        return True

    if result.returncode != 0:
        return True

    try:
        prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return True

    for pr in prs:
        title = pr.get("title", "")
        branch = pr.get("headRefName", "")
        if title.startswith("chore(deps): consolidate") or branch.startswith("chore/consolidate-"):
            return True

    return False


def _ecosystem(title: str) -> str:
    """Infer dependency ecosystem from Mintmaker PR title."""
    title_lower = title.lower()
    if "github.com/" in title_lower or "golang.org/" in title_lower or "module " in title_lower:
        return "go"
    if any(token in title_lower for token in ("npm", "node", "package.json", "yarn", "pnpm")):
        return "npm"
    return "python"


def _version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+][\w.-]+)?", value)
    if not match:
        return None
    major, minor, patch = (int(part or 0) for part in match.groups())
    return major, minor, patch


def _tier(title: str) -> str:
    """Classify bump tier conservatively from title-only preflight data."""
    title_lower = title.lower()
    if re.search(r"(?:^|[\s(:])[^\s:]+!:", title_lower) or "breaking" in title_lower:
        return "major"
    if "digest" in title_lower or "sha" in title_lower:
        return "patch"

    path_bump = re.search(r"/v(\d+)\b.*?\bto\s+v?(\d+)", title_lower)
    if path_bump and path_bump.group(1) != path_bump.group(2):
        return "major"

    versions = re.findall(r"v?\d+(?:\.\d+){1,2}(?:[-+][\w.-]+)?", title_lower)
    if len(versions) >= 2:
        old, new = (_version(value) for value in versions[-2:])
        if old and new:
            if old[0] != new[0] or (old[0] == 0 and old[1] != new[1]):
                return "major"
            if old[1] != new[1]:
                return "minor"
            if old[2] != new[2]:
                return "patch"

    # 0.x target bumps are breaking by project policy. Other target-only
    # versions are unknown because source version is unavailable preflight.
    target_versions = [_version(value) for value in versions]
    if target_versions and target_versions[-1] and target_versions[-1][0] == 0:
        return "major"
    return "unknown"


def _consolidatable_groups(prs: list[dict]) -> list[dict]:
    """Return only ecosystem/tier groups containing at least two PRs."""
    groups: defaultdict[tuple[str, str], list[dict]] = defaultdict(list)
    for pr in prs:
        group = (_ecosystem(pr.get("title", "")), _tier(pr.get("title", "")))
        if group[1] != "unknown":
            groups[group].append(pr)

    return [
        {"ecosystem": ecosystem, "tier": tier, "prs": grouped_prs}
        for (ecosystem, tier), grouped_prs in groups.items()
        if len(grouped_prs) >= 2
    ]


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
        already_active = any(t.get("external_key", "").startswith(task_key) for t in active)
        if already_active:
            print(f"  Skipping {repo_nwo}: consolidation already in progress", file=sys.stderr)
            continue

        if has_open_consolidation_pr(repo_nwo):
            print(f"  Skipping {repo_nwo}: an open consolidation PR already exists", file=sys.stderr)
            continue

        prs = find_bot_prs(repo_nwo, BOT_AUTHOR)
        groups = _consolidatable_groups(prs)
        if groups:
            eligible_prs = [pr for group in groups for pr in group["prs"]]
            pr_summary = [
                {"number": pr["number"], "title": pr["title"], "branch": pr["headRefName"]} for pr in eligible_prs
            ]
            repos_with_prs.append(
                {
                    "repo": repo_nwo,
                    "bot_url": repo_config.get("url", ""),
                    "pr_count": len(eligible_prs),
                    "prs": pr_summary,
                    "groups": [
                        {
                            "ecosystem": group["ecosystem"],
                            "tier": group["tier"],
                            "pr_count": len(group["prs"]),
                        }
                        for group in groups
                    ],
                    "task_key": task_key,
                }
            )

    if not repos_with_prs:
        output_result("skip", f"No repos with 2+ open PRs in same ecosystem+tier from {BOT_AUTHOR}")
        return

    output_result(
        "start",
        json.dumps(
            {
                "bot_author": BOT_AUTHOR,
                "repos": repos_with_prs,
            }
        ),
    )


if __name__ == "__main__":
    main()
