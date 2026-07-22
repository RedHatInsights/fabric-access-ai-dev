#!/usr/bin/env python3
"""
Bot Dependency Consolidation Tool

Consolidates multiple dependency update PRs from red-hat-konflux[bot]
into a single PR for easier review and merging.
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class BotPR:
    """Information about a bot-created PR."""
    number: int
    title: str
    branch: str
    url: str
    files: list = field(default_factory=list)


@dataclass
class ConsolidationResult:
    """Result of the consolidation operation."""
    success: bool
    pr_url: str = ""
    pr_urls: list = field(default_factory=list)  # Multiple PRs when split by ecosystem
    consolidated_count: int = 0
    error: str = ""


class DependencyConsolidator:
    """Consolidates bot dependency PRs into a single PR."""

    BOT_AUTHOR = "red-hat-konflux[bot]"
    DEFAULT_UPSTREAM = "origin"

    def __init__(
        self,
        upstream_repo: str = "",
        dry_run: bool = False,
        bot_author: str = "",
        close_originals: bool = True,
        regenerate_locks: bool = True,
    ):
        self.upstream_repo = upstream_repo
        self.dry_run = dry_run
        self.bot_author = bot_author or self.BOT_AUTHOR
        self.close_originals = close_originals
        self.regenerate_locks = regenerate_locks
        self.bot_prs: list[BotPR] = []

    def run(self) -> ConsolidationResult:
        """Main entry point for dependency consolidation."""
        print(f"\n{'='*60}")
        print("Bot Dependency Consolidation Tool")
        print(f"{'='*60}\n")

        if not self.upstream_repo:
            self.upstream_repo = self._detect_upstream_repo()

        print(f"[Step 1] Finding {self.bot_author} PRs in {self.upstream_repo}...")
        self.bot_prs = self._find_bot_prs()

        print(f"  Found {len(self.bot_prs)} open PR(s):")
        for pr in self.bot_prs:
            print(f"    #{pr.number}: {pr.title}")

        # Group PRs by ecosystem to create separate branches/PRs
        print("\n[Step 2] Grouping PRs by ecosystem...")
        grouped_prs = self._group_prs_by_ecosystem()

        for ecosystem, prs in grouped_prs.items():
            print(f"  {ecosystem}: {len(prs)} PR(s)")

        # Process each ecosystem separately
        all_pr_urls = []
        total_consolidated = 0
        all_merged_prs = []

        for ecosystem, prs in grouped_prs.items():
            if len(prs) == 0:
                continue

            print(f"\n{'='*60}")
            print(f"Processing {ecosystem} dependencies ({len(prs)} PRs)")
            print(f"{'='*60}")

            # Store original bot_prs and replace with just this ecosystem's PRs
            original_bot_prs = self.bot_prs
            self.bot_prs = prs

            result = self._process_ecosystem(ecosystem)

            # Restore original
            self.bot_prs = original_bot_prs

            if result.success and result.pr_url:
                all_pr_urls.append(result.pr_url)
                total_consolidated += result.consolidated_count
                all_merged_prs.extend(prs[:result.consolidated_count])

        if not all_pr_urls:
            return ConsolidationResult(success=False, error="Failed to create any PRs")

        # Close originals if requested (after all PRs created)
        if self.close_originals and all_merged_prs:
            print(f"\n[Final] Closing original bot PRs...")
            self._close_original_prs(all_merged_prs, ", ".join(all_pr_urls))

        return ConsolidationResult(
            success=True,
            pr_url=all_pr_urls[0] if len(all_pr_urls) == 1 else "",
            pr_urls=all_pr_urls,
            consolidated_count=total_consolidated,
        )

    def _group_prs_by_ecosystem(self) -> dict[str, list[BotPR]]:
        """Group PRs by their dependency ecosystem."""
        groups: dict[str, list[BotPR]] = {
            "python": [],
            "npm": [],
            "go": [],
        }

        for pr in self.bot_prs:
            dep_type, _ = self._detect_dep_type(pr)
            if dep_type in groups:
                groups[dep_type].append(pr)
            else:
                groups["python"].append(pr)

        # Remove empty groups
        return {k: v for k, v in groups.items() if v}

    def _process_ecosystem(self, ecosystem: str) -> ConsolidationResult:
        """Process a single ecosystem's PRs."""
        if len(self.bot_prs) < 1:
            return ConsolidationResult(success=True, consolidated_count=0)

        print(f"\n[Step 1] Creating {ecosystem} consolidation branch...")
        branch_name = self._create_consolidation_branch(ecosystem)
        if not branch_name:
            return ConsolidationResult(
                success=False, error=f"Failed to create {ecosystem} consolidation branch"
            )

        print(f"\n[Step 2] Applying changes from {len(self.bot_prs)} PRs...")
        merged_prs = self._merge_bot_pr_changes(branch_name)

        if not merged_prs:
            self._cleanup_branch(branch_name)
            return ConsolidationResult(
                success=False, error=f"Failed to merge any {ecosystem} PR changes"
            )

        print(f"\n[Step 3] Consolidated {len(merged_prs)} {ecosystem} PRs")

        if self.dry_run:
            print(f"\n[Dry Run] Skipping push and PR creation for {ecosystem}")
            self._cleanup_branch(branch_name)
            return ConsolidationResult(
                success=True, consolidated_count=len(merged_prs)
            )

        print(f"\n[Step 4] Pushing {ecosystem} consolidation branch...")
        if not self._push_branch(branch_name):
            return ConsolidationResult(success=False, error="Failed to push branch")

        print(f"\n[Step 5] Creating {ecosystem} consolidated PR...")
        result = self._create_consolidated_pr(branch_name, merged_prs, ecosystem)

        return result

    def _run_git(self, *args, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + list(args), capture_output=True, text=True, check=check
        )

    def _run_pipenv_lock(self, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        """Run pipenv lock to regenerate the lock file."""
        return subprocess.run(
            ["pipenv", "lock"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=300,
        )

    def _run_npm_install(self, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
        """Run npm install to regenerate the lock file."""
        result = subprocess.run(
            ["npm", "install"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=300,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["npm", "install", "--legacy-peer-deps"],
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=300,
            )
        return result

    def _run_gh(self, *args, timeout: int = 60) -> subprocess.CompletedProcess:
        """Run gh CLI command with timeout to prevent hanging on network issues."""
        return subprocess.run(
            ["gh"] + list(args), capture_output=True, text=True, timeout=timeout
        )

    def _detect_upstream_repo(self) -> str:
        try:
            result = self._run_gh("repo", "view", "--json", "nameWithOwner")
        except subprocess.TimeoutExpired:
            return ""
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                return data.get("nameWithOwner", "")
            except json.JSONDecodeError:
                pass
        return ""

    def _find_bot_prs(self) -> list[BotPR]:
        try:
            result = self._run_gh(
                "pr", "list",
                "--repo", self.upstream_repo,
                "--author", self.bot_author,
                "--state", "open",
                "--json", "number,title,headRefName,url,labels",
            )
        except subprocess.TimeoutExpired:
            print("  Timeout listing PRs from GitHub (60s)")
            return []

        if result.returncode != 0:
            print(f"  Error listing PRs: {result.stderr}")
            return []

        try:
            prs_data = json.loads(result.stdout)
            bot_prs = []
            for pr in prs_data:
                # Skip PRs with "DO NOT MERGE" label (case-insensitive)
                labels = [lbl.get("name", "").lower() for lbl in pr.get("labels", [])]
                if any("do not merge" in lbl or "do-not-merge" in lbl for lbl in labels):
                    print(f"    Skipping #{pr['number']}: has DO NOT MERGE label")
                    continue
                bot_prs.append(BotPR(
                    number=pr["number"],
                    title=pr["title"],
                    branch=pr["headRefName"],
                    url=pr["url"],
                ))
            return bot_prs
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  Error parsing PR data: {e}")
            return []

    def _create_consolidation_branch(self, ecosystem: str = "") -> Optional[str]:
        # Determine which remote to use - prefer 'upstream', fall back to 'origin'
        remote = "origin"
        remote_check = self._run_git("remote", "get-url", "upstream")
        if remote_check.returncode == 0:
            remote = "upstream"

        # Fetch all refs from remote to ensure we have the absolute latest
        print(f"  Fetching latest from {remote}...")
        fetch_result = self._run_git("fetch", remote)
        if fetch_result.returncode != 0:
            print(f"  Warning: git fetch failed: {fetch_result.stderr.strip()}")

        # Determine default branch (try main, then master)
        base_ref = None
        for branch in ["main", "master"]:
            result = self._run_git("rev-parse", f"{remote}/{branch}")
            if result.returncode == 0:
                base_ref = result.stdout.strip()
                print(f"  Using {remote}/{branch} as base")
                break

        if not base_ref:
            print(f"  Error: Could not find {remote}/main or {remote}/master")
            return None

        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        if ecosystem:
            branch_name = f"chore/consolidate-{ecosystem}-deps-{timestamp}"
        else:
            branch_name = f"chore/consolidate-deps-{timestamp}"

        result = self._run_git("checkout", "-b", branch_name, base_ref)
        if result.returncode != 0:
            print(f"  Error creating branch: {result.stderr}")
            return None

        print(f"  Created branch: {branch_name}")
        return branch_name

    def _detect_dep_type_from_diff(self, pr_number: int) -> tuple[str, str]:
        """Detect dependency type and directory by checking which files the PR modifies.

        Returns (dep_type, directory) tuple. Directory is relative path or "." for root.
        """
        import os

        diff_result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", self.upstream_repo, "--name-only"],
            capture_output=True,
            text=True,
        )
        if diff_result.returncode != 0:
            return ("unknown", ".")

        files = diff_result.stdout.strip().split("\n")
        for f in files:
            basename = os.path.basename(f)
            dirname = os.path.dirname(f) or "."

            if basename in ("go.mod", "go.sum"):
                return ("go", dirname)
            if basename in ("Pipfile", "Pipfile.lock"):
                return ("python", dirname)
            if basename in ("package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml"):
                return ("npm", dirname)
            if basename in ("pyproject.toml", "poetry.lock"):
                return ("python", dirname)
            if basename == "requirements.txt":
                return ("python", dirname)
        return ("unknown", ".")

    def _detect_dep_type(self, pr: BotPR) -> tuple[str, str]:
        """Detect which dependency system this PR targets.

        Primary: check PR diff to see which files are modified.
        Fallback: infer from title patterns if diff check fails.

        Returns (dep_type, directory) tuple.
        """
        # Primary: check the actual PR diff to see which files are modified
        # This is most reliable for repos with multiple ecosystems (e.g., Pipfile + package.json)
        dep_type, directory = self._detect_dep_type_from_diff(pr.number)
        if dep_type != "unknown":
            return (dep_type, directory)

        # Fallback: infer from title patterns (assume root directory)
        title = pr.title.lower()

        # npm/node packages start with @ or have typical npm patterns
        if "@" in title and ("typespec" in title or "types/" in title):
            return ("npm", ".")

        # Go modules have domain-like paths
        if "module " in title and ("github.com/" in title or "golang.org/" in title):
            return ("go", ".")

        # Check for grpc which could be Go or Python - look at package name pattern
        if "grpcio" in title or "grpcio-status" in title:
            return ("python", ".")

        # Last resort: default to python for typical package names
        if "update dependency" in title or "fix(deps)" in title:
            return ("python", ".")

        return ("unknown", ".")

    def _merge_bot_pr_changes(self, branch_name: str) -> list[BotPR]:
        """Apply changes from bot PRs using semantic dependency updates.

        For each PR, extract the dependency name/version from title or diff,
        then directly update the manifest file (go.mod, Pipfile, package.json).
        Lock files are regenerated once at the end.
        """
        import os

        merged = []
        original_go_version = self._get_go_version() if self._has_go_mod() else None

        # Track which directories need lock/tidy commands for each ecosystem
        go_dirs: set[str] = set()
        python_dirs: set[str] = set()
        npm_dirs: set[str] = set()

        for pr in self.bot_prs:
            print(f"  Processing PR #{pr.number}: {pr.title[:50]}...")

            dep_type, dep_dir = self._detect_dep_type(pr)
            applied = False

            # Check if the target file exists in the detected directory
            pipfile_path = os.path.join(dep_dir, "Pipfile")
            package_json_path = os.path.join(dep_dir, "package.json")
            go_mod_path = os.path.join(dep_dir, "go.mod")

            if dep_type == "go" and os.path.exists(go_mod_path):
                dep_spec = self._extract_go_dep_from_title(pr.title)
                if not dep_spec:
                    print(f"    Title parsing failed, checking PR content...")
                    dep_spec = self._extract_go_dep_from_pr_content(pr.number)
                    if not dep_spec:
                        print(f"    Warning: Could not extract dep from title or PR content, skipping")
                        continue

                print(f"    Running go get {dep_spec} in {dep_dir}...")
                get_result = subprocess.run(
                    ["go", "get", dep_spec],
                    capture_output=True,
                    text=True,
                    cwd=dep_dir if dep_dir != "." else None,
                )
                if get_result.returncode != 0:
                    print(f"    Warning: go get failed: {get_result.stderr.strip()}")
                    continue
                if original_go_version and dep_dir == ".":
                    self._restore_go_version(original_go_version)
                applied = True
                go_dirs.add(dep_dir)

            elif dep_type == "python" and os.path.exists(pipfile_path):
                package, version = self._extract_python_dep_from_title(pr.title)
                if not package:
                    print(f"    Title parsing failed, checking PR content...")
                    package, version = self._extract_python_dep_from_pr_content(pr.number)
                    if not package:
                        print(f"    Warning: Could not extract dep from title or PR content, skipping")
                        continue

                print(f"    Updating {package} to {version} in {dep_dir}...")
                if not self._update_pipfile_dep(package, version, pipfile_path):
                    print(f"    Warning: Could not find {package} in {pipfile_path}, skipping")
                    continue
                applied = True
                python_dirs.add(dep_dir)

            elif dep_type == "npm" and os.path.exists(package_json_path):
                package, version = self._extract_npm_dep_from_title(pr.title)
                if not package:
                    print(f"    Title parsing failed, checking PR content...")
                    package, version = self._extract_npm_dep_from_pr_content(pr.number)
                    if not package:
                        print(f"    Warning: Could not extract npm dep from title or PR content, skipping")
                        continue

                print(f"    Updating {package} to {version} in {dep_dir}...")
                if not self._update_package_json_dep(package, version, package_json_path):
                    print(f"    Warning: Could not find {package} in {package_json_path}, skipping")
                    continue
                applied = True
                npm_dirs.add(dep_dir)

            else:
                # For unknown types or mismatched projects, try patch application
                try:
                    diff_result = self._run_gh("pr", "diff", str(pr.number), "--repo", self.upstream_repo)
                except subprocess.TimeoutExpired:
                    print(f"    Warning: Timeout getting diff for PR #{pr.number}")
                    continue
                if diff_result.returncode != 0:
                    print(f"    Warning: Could not get diff for PR #{pr.number}")
                    continue

                apply_result = subprocess.run(
                    ["git", "apply", "--3way"],
                    input=diff_result.stdout,
                    capture_output=True,
                    text=True,
                )
                if apply_result.returncode != 0:
                    print(f"    Warning: Patch failed for PR #{pr.number}, skipping")
                    self._run_git("checkout", "--", ".")
                    continue
                applied = True

            if applied:
                merged.append(pr)
                print(f"    Applied successfully")

        # Run final lock/tidy commands for each directory that was updated (if enabled)
        if self.regenerate_locks:
            for go_dir in go_dirs:
                print(f"  Running go mod tidy in {go_dir}...")
                subprocess.run(["go", "mod", "tidy"], capture_output=True, cwd=go_dir if go_dir != "." else None)
                if original_go_version and go_dir == ".":
                    self._restore_go_version(original_go_version)

            for python_dir in python_dirs:
                print(f"  Running pipenv lock in {python_dir}...")
                cwd = python_dir if python_dir != "." else None

                # Try WSL first (matches dev environment), fall back to native
                lock_result = self._run_pipenv_lock(cwd)
                if lock_result.returncode != 0:
                    print(f"    Warning: pipenv lock failed: {lock_result.stderr[:200]}")

            for npm_dir in npm_dirs:
                print(f"  Running npm install in {npm_dir}...")
                npm_result = self._run_npm_install(npm_dir if npm_dir != "." else None)
                if npm_result.returncode != 0:
                    print(f"    Warning: npm install failed: {npm_result.stderr[:200]}")

        # Commit all changes
        if merged:
            self._run_git("add", "-A")
            pr_list = "\n".join(f"- #{pr.number}: {pr.title}" for pr in merged)
            commit_msg = f"chore(deps): consolidate {len(merged)} dependency updates\n\nConsolidated PRs:\n{pr_list}"
            self._run_git("commit", "-m", commit_msg)

        return merged

    def _has_go_mod(self) -> bool:
        """Check if this is a Go project with go.mod."""
        import os
        return os.path.exists("go.mod")

    def _get_go_version(self) -> str:
        """Get the current go version directive from go.mod."""
        import re
        try:
            with open("go.mod") as f:
                content = f.read()
            match = re.search(r"^go\s+([\d.]+)", content, re.MULTILINE)
            return match.group(1) if match else ""
        except (FileNotFoundError, IOError):
            return ""

    def _restore_go_version(self, version: str):
        """Restore the go version directive in go.mod to the specified version."""
        import re
        try:
            with open("go.mod") as f:
                content = f.read()
            new_content = re.sub(r"^go\s+[\d.]+", f"go {version}", content, count=1, flags=re.MULTILINE)
            if new_content != content:
                with open("go.mod", "w") as f:
                    f.write(new_content)
                print(f"  Restored go version to {version}")
        except (FileNotFoundError, IOError) as e:
            print(f"  Warning: Could not restore go version: {e}")

    def _extract_go_dep_from_title(self, title: str) -> str:
        """Extract Go dependency spec (module@version) from PR title."""
        import re
        match = re.search(
            r"(?:update|fix)\s+(?:module\s+)?([\w./-]+)\s+to\s+(v[\d.]+(?:-[\w.]+)?)",
            title,
            re.IGNORECASE,
        )
        if match:
            module, version = match.groups()
            return f"{module}@{version}"
        return ""

    def _extract_go_dep_from_pr_content(self, pr_number: int) -> str:
        """Extract Go dependency spec from PR diff when title parsing fails."""
        import re

        try:
            diff_result = self._run_gh("pr", "diff", str(pr_number), "--repo", self.upstream_repo)
        except subprocess.TimeoutExpired:
            return ""
        if diff_result.returncode != 0:
            return ""

        diff = diff_result.stdout

        for line in diff.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                match = re.match(
                    r"^\+\s+([\w./-]+)\s+(v[\d.]+(?:-[\w.+-]+)?)",
                    line,
                )
                if match:
                    module, version = match.groups()
                    if "." in module and "/" in module:
                        return f"{module}@{version}"

        return ""

    def _extract_python_dep_from_title(self, title: str) -> tuple[str, str]:
        """Extract Python dependency name and version from PR title."""
        import re
        match = re.search(
            r"(?:update|fix)\s+dependency\s+([\w_-]+)\s+to\s+(?:v)?([\d.]+(?:[a-zA-Z][\w.]*)?)",
            title,
            re.IGNORECASE,
        )
        if match:
            package, version = match.groups()
            return (package, version)
        return ("", "")

    def _extract_python_dep_from_pr_content(self, pr_number: int) -> tuple[str, str]:
        """Extract Python dependency from PR diff when title parsing fails."""
        import re

        try:
            diff_result = self._run_gh("pr", "diff", str(pr_number), "--repo", self.upstream_repo)
        except subprocess.TimeoutExpired:
            return ("", "")
        if diff_result.returncode != 0:
            return ("", "")

        diff = diff_result.stdout

        for line in diff.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                match = re.match(
                    r'^\+\s*([\w_-]+)\s*=\s*["\'](?:[<>=~!]*)?(\d[\d.]*(?:[a-zA-Z][\w.]*)?)["\']',
                    line,
                )
                if match:
                    return match.groups()

        return ("", "")

    def _update_pipfile_dep(self, package: str, version: str, path: str = "Pipfile") -> bool:
        """Update a dependency in Pipfile to the specified version."""
        import re

        try:
            with open(path) as f:
                content = f.read()

            pattern = rf'^(\s*{re.escape(package)}\s*=\s*["\'])([<>=~!]*)[\d.]+(?:[a-zA-Z][\w.]*)?(["\'])'
            new_content = re.sub(
                pattern,
                rf"\g<1>\g<2>{version}\3",
                content,
                flags=re.MULTILINE | re.IGNORECASE,
            )

            if new_content == content:
                # Try alternate format: package = {version = "..."}
                pattern = rf'^(\s*{re.escape(package)}\s*=\s*\{{\s*version\s*=\s*["\'])([<>=~!]*)[\d.]+(?:[a-zA-Z][\w.]*)?(["\'])'
                new_content = re.sub(
                    pattern,
                    rf"\g<1>\g<2>{version}\3",
                    content,
                    flags=re.MULTILINE | re.IGNORECASE,
                )

            if new_content != content:
                with open(path, "w") as f:
                    f.write(new_content)
                return True
            return False
        except (FileNotFoundError, IOError) as e:
            print(f"    Warning: Could not update {path}: {e}")
            return False

    def _extract_npm_dep_from_title(self, title: str) -> tuple[str, str]:
        """Extract npm dependency name and version from PR title."""
        import re
        match = re.search(
            r"(?:update|fix)\s+dependency\s+(@?[\w./-]+)\s+to\s+(?:v)?(\^?~?[\d.]+(?:-[\w.]+)?)",
            title,
            re.IGNORECASE,
        )
        if match:
            package, version = match.groups()
            return (package, version)
        return ("", "")

    def _extract_npm_dep_from_pr_content(self, pr_number: int) -> tuple[str, str]:
        """Extract npm dependency from PR diff when title parsing fails."""
        import re

        try:
            diff_result = self._run_gh("pr", "diff", str(pr_number), "--repo", self.upstream_repo)
        except subprocess.TimeoutExpired:
            return ("", "")
        if diff_result.returncode != 0:
            return ("", "")

        diff = diff_result.stdout
        skip_keys = {"version", "name", "description", "main", "scripts", "author", "license", "type"}

        for line in diff.split("\n"):
            if line.startswith("+") and not line.startswith("+++"):
                match = re.match(
                    r'^\+\s*"(@?[\w./-]+)"\s*:\s*"(\^?~?[\d.]+(?:-[\w.]+)?)"',
                    line,
                )
                if match:
                    package, version = match.groups()
                    if package.lower() not in skip_keys:
                        return (package, version)

        return ("", "")

    def _update_package_json_dep(self, package: str, version: str, path: str = "package.json") -> bool:
        """Update a dependency in package.json to the specified version in all locations."""
        try:
            with open(path) as f:
                data = json.load(f)

            updated = False
            for dep_type in ["dependencies", "devDependencies", "peerDependencies"]:
                if dep_type in data and package in data[dep_type]:
                    data[dep_type][package] = version
                    updated = True

            if updated:
                with open(path, "w") as f:
                    json.dump(data, f, indent=2)
                    f.write("\n")
                return True
            return False
        except (FileNotFoundError, IOError, json.JSONDecodeError) as e:
            print(f"    Warning: Could not update {path}: {e}")
            return False

    def _push_branch(self, branch_name: str) -> bool:
        result = self._run_git("push", "-u", "origin", branch_name)
        if result.returncode != 0:
            print(f"  Push failed: {result.stderr}")
            return False
        print("  Push successful")
        return True

    def _create_consolidated_pr(
        self, branch_name: str, merged_prs: list[BotPR], ecosystem: str = ""
    ) -> ConsolidationResult:
        ecosystem_label = f" {ecosystem}" if ecosystem else ""
        title = f"chore(deps): consolidate {len(merged_prs)}{ecosystem_label} dependency updates"

        pr_list = "\n".join(
            f"- #{pr.number}: {pr.title}" for pr in merged_prs
        )

        body = f"""## Summary

Consolidates {len(merged_prs)}{ecosystem_label} dependency update PRs from `{self.bot_author}` into a single PR for easier review.

## Consolidated PRs

{pr_list}

## Why consolidate?

- Reduces CI/CD load from multiple small PRs
- Easier to review related dependency updates together
- Single merge commit instead of many

## Testing

- [ ] CI passes
- [ ] No breaking changes from dependency updates
"""

        try:
            result = self._run_gh(
                "pr", "create",
                "--repo", self.upstream_repo,
                "--title", title,
                "--body", body,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            print("  Timeout creating PR (120s)")
            return ConsolidationResult(success=False, error="Timeout creating PR")

        if result.returncode != 0:
            print(f"  Error creating PR: {result.stderr}")
            return ConsolidationResult(success=False, error=result.stderr)

        pr_url = result.stdout.strip()
        print(f"  Created PR: {pr_url}")

        return ConsolidationResult(
            success=True, pr_url=pr_url, consolidated_count=len(merged_prs)
        )

    def _close_original_prs(self, merged_prs: list[BotPR], consolidated_pr_url: str):
        for pr in merged_prs:
            comment = f"Consolidated into {consolidated_pr_url}"
            try:
                self._run_gh(
                    "pr", "comment", str(pr.number),
                    "--repo", self.upstream_repo,
                    "--body", comment,
                )
            except subprocess.TimeoutExpired:
                print(f"    Timeout commenting on PR #{pr.number}")

            try:
                self._run_gh(
                    "pr", "close", str(pr.number),
                    "--repo", self.upstream_repo,
                )
                print(f"  Closed PR #{pr.number}")
            except subprocess.TimeoutExpired:
                print(f"    Timeout closing PR #{pr.number}")

    def _cleanup_branch(self, branch_name: str):
        self._run_git("checkout", "-")
        self._run_git("branch", "-D", branch_name)


def main():
    parser = argparse.ArgumentParser(
        description="Consolidate bot dependency PRs into a single PR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # Auto-detect repo, consolidate konflux PRs
  %(prog)s --repo owner/repo            # Specify upstream repo
  %(prog)s --bot dependabot[bot]        # Use different bot author
  %(prog)s --dry-run                    # Preview without creating PR
  %(prog)s --keep-originals              # Keep original PRs open (default: close them)
        """,
    )

    parser.add_argument(
        "--repo", "-r",
        dest="upstream_repo",
        default="",
        help="Upstream repository (owner/repo). Auto-detected if not specified.",
    )

    parser.add_argument(
        "--bot", "-b",
        dest="bot_author",
        default="",
        help=f"Bot author to filter PRs (default: {DependencyConsolidator.BOT_AUTHOR})",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview consolidation without creating PR",
    )

    parser.add_argument(
        "--close-originals",
        action="store_true",
        default=True,
        help="Close original bot PRs after creating consolidated PR (default: close)",
    )

    parser.add_argument(
        "--keep-originals",
        action="store_true",
        help="Keep original bot PRs open after creating consolidated PR",
    )

    parser.add_argument(
        "--no-regenerate-locks",
        action="store_true",
        help="Skip regenerating lock files (pipenv lock, npm install, go mod tidy). "
             "By default, lock files are regenerated.",
    )

    args = parser.parse_args()

    consolidator = DependencyConsolidator(
        upstream_repo=args.upstream_repo,
        dry_run=args.dry_run,
        bot_author=args.bot_author,
        close_originals=args.close_originals and not args.keep_originals,
        regenerate_locks=not args.no_regenerate_locks,
    )

    result = consolidator.run()

    print("\n" + "=" * 60)
    if result.success:
        if result.consolidated_count > 0:
            print(f"Done! Consolidated {result.consolidated_count} PRs")
            if result.pr_url:
                print(f"URL: {result.pr_url}")
        else:
            print("No PRs to consolidate")
    else:
        print(f"Failed: {result.error}")
        sys.exit(1)


if __name__ == "__main__":
    main()