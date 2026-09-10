#!/usr/bin/env python3
"""Preflight check: verify prerequisites and that bot PRs exist to consolidate.

Reads repos from project-repos.json and checks each for open bot PRs.
"""

import html
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
                "number,title,headRefName,url,labels,body",
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


def _version(value: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?(?:[-+][\w.-]+)?", value)
    if not match:
        return None
    major, minor, patch, build = (int(part or 0) for part in match.groups())
    return major, minor, patch, build


# {1,3} (not {1,2}) so 4-segment versions like the date-suffixed stub packages
# (types-pyyaml `6.0.12.20260906`) match as one token instead of truncating at
# the third segment — a truncated match makes old/new compare equal even when
# the (date) build segment actually changed, so the bump silently vanishes and
# _tier_from_versions never sees a difference.
_VERSION_TOKEN = r"v?\d+(?:\.\d+){1,3}(?:[-+][\w.-]+)?"

# Renovate/Mintmaker PR bodies embed the current->target version in a table
# cell, typically as backtick-quoted values joined by an arrow (`1.2.3` ->
# `1.4.0`), but bare arrows and "from X to Y" phrasing also show up. Try each
# in order and take the first match.
_BODY_VERSION_PATTERNS = [
    re.compile(rf"`({_VERSION_TOKEN})`\s*(?:->|→)\s*`({_VERSION_TOKEN})`", re.IGNORECASE),
    re.compile(rf"({_VERSION_TOKEN})\s*(?:->|→)\s*({_VERSION_TOKEN})", re.IGNORECASE),
    re.compile(rf"\bfrom\s+({_VERSION_TOKEN})\s+to\s+({_VERSION_TOKEN})\b", re.IGNORECASE),
]


def _body_versions(body: str) -> tuple[str, str] | None:
    """Extract the (old, new) version pair from a bot PR body, if present.

    Title-only classification can't tell major/minor/patch apart when the
    title only states the target version (e.g. "Update dependency X to
    v2.69.1") — this is the common Renovate/Mintmaker title format. The PR
    body's changelog table almost always states both the current and target
    version, so fall back to it before giving up and calling the tier
    "unknown".

    Renovate/Mintmaker often renders the changelog table as raw HTML rather
    than markdown (`<code>1.2.3</code> -&gt; <code>1.4.0</code>` instead of
    `` `1.2.3` -> `1.4.0` ``), which breaks every pattern above since the tag
    text sits between the version and the arrow instead of whitespace. Strip
    tags and unescape entities first so the patterns see plain "1.2.3 -> 1.4.0"
    regardless of markup.
    """
    if not body:
        return None
    text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    for pattern in _BODY_VERSION_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1), match.group(2)
    return None


_MANIFEST_FILES = {
    "python": ("Pipfile",),
    "npm": ("package.json",),
    "go": ("go.mod",),
}


def _diff_versions(repo_nwo: str, pr_number: int, ecosystem: str) -> tuple[str, str] | None:
    """Extract the real (old, new) version by diffing the PR's manifest file.

    Titles like "Update dependency X to vY" only state the target version, and
    body changelog tables are inconsistently formatted or missing entirely
    (see _body_versions). The PR diff's hunk for the actual dependency
    manifest always contains the true before/after version strings, so this
    is tried before falling back to parsing prose in title/body.
    """
    manifest_names = _MANIFEST_FILES.get(ecosystem)
    if not manifest_names:
        return None

    try:
        result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", repo_nwo],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    in_manifest = False
    old_version = new_version = None
    for line in result.stdout.split("\n"):
        if line.startswith("+++") or line.startswith("---"):
            in_manifest = any(line.rstrip().endswith(name) for name in manifest_names)
            continue
        if not in_manifest:
            continue
        if old_version is None and line.startswith("-") and not line.startswith("---"):
            match = re.search(_VERSION_TOKEN, line)
            if match:
                old_version = match.group(0)
        elif new_version is None and line.startswith("+") and not line.startswith("+++"):
            match = re.search(_VERSION_TOKEN, line)
            if match:
                new_version = match.group(0)
        if old_version and new_version:
            break

    if old_version and new_version:
        return old_version, new_version
    return None


def _tier_from_versions(old: str, new: str) -> str | None:
    old_version, new_version = _version(old), _version(new)
    if not old_version or not new_version:
        return None
    if old_version[0] != new_version[0] or (old_version[0] == 0 and old_version[1] != new_version[1]):
        return "major"
    if old_version[1] != new_version[1]:
        return "minor"
    if old_version[2] != new_version[2] or old_version[3] != new_version[3]:
        return "patch"
    return None


def _tier(title: str, body: str = "", diff_versions: tuple[str, str] | None = None) -> str:
    """Classify bump tier, preferring the real diff versions over title/body prose.

    `diff_versions` (when available) is the actual old/new version pulled
    straight from the PR's manifest diff (see `_diff_versions`) and takes
    priority over title/body text parsing, which only ever sees what the bot
    chose to write in prose and can't tell tiers apart when the title states
    just the target version.
    """
    title_lower = title.lower()
    if re.search(r"(?:^|[\s(:])[^\s:]+!:", title_lower) or "breaking" in title_lower:
        return "major"
    if "digest" in title_lower or "sha" in title_lower:
        return "patch"

    path_bump = re.search(r"/v(\d+)\b.*?\bto\s+v?(\d+)", title_lower)
    if path_bump and path_bump.group(1) != path_bump.group(2):
        return "major"

    if diff_versions:
        tier = _tier_from_versions(*diff_versions)
        if tier:
            return tier

    versions = re.findall(_VERSION_TOKEN, title_lower)
    if len(versions) >= 2:
        tier = _tier_from_versions(*versions[-2:])
        if tier:
            return tier

    body_versions = _body_versions(body)
    if body_versions:
        tier = _tier_from_versions(*body_versions)
        if tier:
            return tier

    # 0.x target bumps are breaking by project policy. Other target-only
    # versions are unknown because no source version could be determined
    # from the title or body.
    target_versions = [_version(value) for value in versions]
    if target_versions and target_versions[-1] and target_versions[-1][0] == 0:
        return "major"
    return "unknown"


def _consolidatable_groups(prs: list[dict], repo_nwo: str = "") -> list[dict]:
    """Group PRs into consolidation batches of at least two PRs each.

    Majors are always isolated: they only ever batch with other majors of the
    same ecosystem, never with minor/patch PRs, no matter how that affects
    batch size — a major bump needs its own breaking-change investigation
    (see CLAUDE.md), and folding it into an otherwise-safe patch batch would
    force that investigation onto the whole batch.

    Minor and patch PRs combine into a single ecosystem-wide batch whenever
    both are present — the combined batch is always a superset of either
    tier alone, so this consolidates strictly more than treating them as two
    separate (and possibly sub-threshold) tier batches. The combined batch is
    tagged "minor" so it still gets the more cautious minor-bump handling
    (code-change investigation) rather than being treated as a bare patch
    batch. When only one of minor/patch is present, it's grouped on its own
    as before.
    """
    tiered: defaultdict[tuple[str, str], list[dict]] = defaultdict(list)
    for pr in prs:
        title = pr.get("title", "")
        ecosystem = _ecosystem(title)
        diff_versions = _diff_versions(repo_nwo, pr["number"], ecosystem) if repo_nwo else None
        tier = _tier(title, pr.get("body", ""), diff_versions)
        if tier != "unknown":
            tiered[(ecosystem, tier)].append(pr)

    groups = []
    for ecosystem in {eco for eco, _ in tiered}:
        major_prs = tiered.get((ecosystem, "major"), [])
        minor_prs = tiered.get((ecosystem, "minor"), [])
        patch_prs = tiered.get((ecosystem, "patch"), [])

        if len(major_prs) >= 2:
            groups.append({"ecosystem": ecosystem, "tier": "major", "prs": major_prs})

        if minor_prs and patch_prs and len(minor_prs) + len(patch_prs) >= 2:
            groups.append({"ecosystem": ecosystem, "tier": "minor", "prs": minor_prs + patch_prs})
        else:
            if len(minor_prs) >= 2:
                groups.append({"ecosystem": ecosystem, "tier": "minor", "prs": minor_prs})
            if len(patch_prs) >= 2:
                groups.append({"ecosystem": ecosystem, "tier": "patch", "prs": patch_prs})

    return groups


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
        groups = _consolidatable_groups(prs, repo_nwo)
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
