#!/usr/bin/env python3
"""Preflight check: verify prerequisites and that bot PRs exist to consolidate."""

import json
import shutil
import subprocess
import sys


BOT_AUTHOR = "red-hat-konflux[bot]"


def check_tool(name: str, required: bool = True) -> bool:
    found = shutil.which(name) is not None
    status = "found" if found else "NOT FOUND"
    label = "REQUIRED" if required else "optional"
    print(f"  {name}: {status} ({label})")
    if required and not found:
        return False
    return True


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
                print(f"    Skipping #{pr['number']}: has DO NOT MERGE label")
                continue
            filtered.append(pr)
        return filtered
    except (json.JSONDecodeError, KeyError):
        return []


def main() -> int:
    print("Preflight: Bot Dependency Consolidation")
    print("=" * 40)

    # Check required tools
    print("\n[1/3] Checking tools...")
    ok = check_tool("gh", required=True)
    check_tool("go", required=False)
    check_tool("pipenv", required=False)
    check_tool("npm", required=False)

    if not ok:
        print("\nFAIL: missing required tool 'gh'. Install and authenticate with: gh auth login")
        return 1

    # Check git repo
    print("\n[2/3] Checking git repository...")
    if not check_git_repo():
        print("  FAIL: not in a git repository")
        return 1
    print("  OK")

    # Detect upstream and find bot PRs
    print("\n[3/3] Checking for bot PRs...")
    upstream_repo = detect_upstream_repo()
    if not upstream_repo:
        print("  FAIL: could not detect upstream repo. Use --repo to specify.")
        return 1
    print(f"  Repo: {upstream_repo}")

    prs = find_bot_prs(upstream_repo, BOT_AUTHOR)
    if not prs:
        print(f"  No open PRs found from {BOT_AUTHOR}")
        return 1

    print(f"  Found {len(prs)} open PR(s):")
    for pr in prs:
        print(f"    #{pr['number']}: {pr['title']}")

    if len(prs) < 2:
        print("\n  Only 1 PR found — nothing to consolidate")
        return 1

    print(f"\nPreflight PASSED: {len(prs)} PRs ready to consolidate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
