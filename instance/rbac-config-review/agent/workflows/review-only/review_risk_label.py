#!/usr/bin/env python3
"""Apply one validated, revision-bound risk label to a GitHub PR."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from public_comment_safety import ensure_public_content


RISK_LABELS = {
    "low": {
        "name": "low-risk",
        "color": "0e8a16",
        "description": "Low breaking risk; auto-merge may be considered after required checks and repository rules pass.",
    },
    "medium": {
        "name": "medium-risk",
        "color": "fbca04",
        "description": "Medium breaking risk; one human approval is required before merge.",
    },
    "high": {
        "name": "high-risk",
        "color": "b60205",
        "description": "High breaking risk; two human approvals are required before merge.",
    },
}


def _status(result: subprocess.CompletedProcess) -> int | None:
    response = result.stdout + "\n" + result.stderr
    match = re.search(r"(?m)^HTTP/\S+\s+(\d{3})\b", response)
    return int(match.group(1)) if match else None


def _api(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", "api", *args], capture_output=True, text=True,
                          timeout=30, check=False)


def _require_success(result: subprocess.CompletedProcess, message: str) -> None:
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "GitHub returned no response"
        raise RuntimeError(f"{message}: {detail[:300]}")


def _ensure_label(repo: str, level: str) -> str:
    config = RISK_LABELS[level]
    path = f"repos/{repo}/labels/{quote(config['name'], safe='')}"
    lookup = _api(["--include", path])
    status = _status(lookup)
    if status == 404:
        created = _api([
            f"repos/{repo}/labels", "--method", "POST",
            "--field", f"name={config['name']}",
            "--field", f"color={config['color']}",
            "--field", f"description={config['description']}",
        ])
        if created.returncode == 0:
            return config["name"]
        # A concurrent cycle may have created this label after the lookup.
        verified = _api(["--include", path])
        if verified.returncode or _status(verified) != 200:
            detail = created.stderr.strip() or created.stdout.strip() or "GitHub returned no response"
            raise RuntimeError(f"Cannot create risk label for {repo}: {detail[:300]}")
        # Fall through and update the concurrently created label to our policy.
        lookup, status = verified, 200
    if lookup.returncode or status != 200:
        detail = lookup.stderr.strip() or lookup.stdout.strip() or "GitHub returned no response"
        raise RuntimeError(f"Cannot check risk label for {repo}: {detail[:300]}")

    update = _api([
        path, "--method", "PATCH",
        "--field", f"color={config['color']}",
        "--field", f"description={config['description']}",
    ])
    _require_success(update, f"Cannot update risk label for {repo}")
    return config["name"]


def apply(repo: str, pr: int, head: str, base: str, assessment_path: Path) -> dict:
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or pr < 1 or not re.fullmatch(r"[0-9a-f]{40}", head)
            or not re.fullmatch(r"[0-9a-f]{40}", base)):
        raise ValueError("Repository, PR, or revision is invalid")
    assessment = json.loads(assessment_path.read_text())
    level = assessment.get("risk") if isinstance(assessment, dict) else None
    explanation = assessment.get("explanation") if isinstance(assessment, dict) else None
    evidence_files = assessment.get("evidence_files") if isinstance(assessment, dict) else None
    if (not isinstance(level, str) or level not in RISK_LABELS
            or not isinstance(explanation, str) or not explanation.strip()
            or not isinstance(evidence_files, list) or not 1 <= len(evidence_files) <= 8
            or any(not isinstance(path, str) or not path.strip() for path in evidence_files)
            or assessment.get("head_sha") != head or assessment.get("base_sha") != base
            or assessment.get("stale") is True):
        raise ValueError("Risk assessment needs a valid level and explanation")
    ensure_public_content(explanation, "Risk assessment")

    pull_result = _api([f"repos/{repo}/pulls/{pr}"])
    _require_success(pull_result, "Cannot read PR before risk labeling")
    pull = json.loads(pull_result.stdout)
    if (not isinstance(pull, dict) or pull.get("state") != "open"
            or not isinstance(pull.get("head"), dict) or pull["head"].get("sha") != head
            or not isinstance(pull.get("base"), dict) or pull["base"].get("sha") != base):
        raise RuntimeError("PR head or base changed before risk labeling")

    selected = _ensure_label(repo, level)
    add = _api([
        f"repos/{repo}/issues/{pr}/labels", "--method", "POST",
        "--field", f"labels[]={selected}",
    ])
    _require_success(add, f"Cannot add {selected} to {repo}#{pr}")
    for other_level, config in RISK_LABELS.items():
        if other_level == level:
            continue
        result = subprocess.run(
            ["gh", "api", "--include",
             f"repos/{repo}/issues/{pr}/labels/{quote(config['name'], safe='')}",
             "--method", "DELETE"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode and _status(result) != 404:
            detail = result.stderr.strip() or result.stdout.strip() or "GitHub returned no response"
            raise RuntimeError(f"Cannot remove stale risk label from {repo}#{pr}: {detail[:300]}")
    return {"label": selected, "risk": level, "head_sha": head, "base_sha": base}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--assessment", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(apply(args.repo, args.pr, args.head_sha, args.base_sha,
                               args.assessment)))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"Risk label update failed: {exc}\n")


if __name__ == "__main__":
    main()
