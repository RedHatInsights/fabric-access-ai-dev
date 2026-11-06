#!/usr/bin/env python3
"""Build read-only, revision-pinned context for one GitHub PR."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from impact_analysis import make_impact_report
from review_config import github_markdown_url, github_repo_url, load_config

STOP_WORDS = {
    "about", "after", "before", "config", "const", "false", "function",
    "import", "return", "string", "tests", "true", "value",
}
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{5,}")


class ContextError(RuntimeError):
    pass


def run(args: list[str], *, timeout: int = 90) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise ContextError(f"{' '.join(args[:4])} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def gh_json(endpoint: str) -> dict:
    data = json.loads(run(["gh", "api", endpoint]))
    if not isinstance(data, dict):
        raise ContextError(f"GitHub API did not return an object: {endpoint}")
    return data


def diff_terms(patch: str, limit: int = 24) -> list[str]:
    """Extract specific identifiers from changed lines for related-repo search."""
    counts: Counter[str] = Counter()
    for line in patch.splitlines():
        if not line.startswith(("+", "-")) or line.startswith(("+++", "---")):
            continue
        for term in TOKEN.findall(line[1:]):
            if term.lower() in STOP_WORDS or len(term) > 100:
                continue
            if not any(mark in term for mark in (".", "/", "_", "-", ":")):
                continue
            counts[term] += 1
    return [term for term, _ in counts.most_common(limit)]


def update_related_repo(url: str, destination: Path) -> str:
    """Shallow-clone or refresh a trusted GitHub repository, then pin HEAD."""
    if not (destination / ".git").exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--depth", "1", url + ".git", str(destination)], timeout=120)
    else:
        run(["git", "-C", str(destination), "fetch", "--depth", "1", "origin", "HEAD"], timeout=120)
        run(["git", "-C", str(destination), "checkout", "--detach", "FETCH_HEAD"], timeout=30)
    return run(["git", "-C", str(destination), "rev-parse", "HEAD"]).strip()


def update_pr_checkout(repo: str, pr_number: int, expected_head: str, destination: Path) -> str:
    """Fetch the upstream PR ref into a read-only local checkout."""
    if not (destination / ".git").exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--depth", "1", f"https://github.com/{repo}.git", str(destination)], timeout=120)
    run([
        "git", "-C", str(destination), "fetch", "--depth", "1",
        "origin", f"pull/{pr_number}/head",
    ], timeout=120)
    run(["git", "-C", str(destination), "checkout", "--detach", "FETCH_HEAD"], timeout=30)
    actual = run(["git", "-C", str(destination), "rev-parse", "HEAD"]).strip()
    if actual != expected_head:
        raise ContextError("Fetched PR head does not match preflight SHA")
    return actual


def find_matches(directory: Path, terms: list[str]) -> list[str]:
    if not terms:
        return []
    args = ["git", "-C", str(directory), "grep", "-n", "-I", "-m", "10", "-F"]
    for term in terms:
        args.extend(["-e", term])
    result = subprocess.run(args + ["HEAD"], capture_output=True, text=True, timeout=45, check=False)
    if result.returncode not in (0, 1):
        raise ContextError(f"git grep failed in {directory}: {result.stderr.strip()[:200]}")
    return result.stdout.splitlines()[:60]


def fetch_doc(url: str) -> tuple[str, str]:
    """Return Markdown text and a URL pinned to the resolved Git commit."""
    repo, ref, path = github_markdown_url(url)
    commit = gh_json(f"repos/{repo}/commits/{quote(ref, safe='')}").get("sha")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ContextError(f"Could not resolve docs ref for {url}")
    data = gh_json(f"repos/{repo}/contents/{quote(path, safe='/')}?ref={commit}")
    if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
        raise ContextError(f"Markdown file unavailable or too large: {url}")
    content = base64.b64decode(data["content"], validate=False).decode("utf-8")
    permalink = f"https://github.com/{repo}/blob/{commit}/{quote(path, safe='/')}"
    return content, permalink


def build(repo_key: str, pr_number: int, expected_head: str, expected_base: str, output_root: Path) -> dict:
    config = load_config()
    project_repos = json.loads((Path.cwd() / "project-repos.json").read_text())
    entry = project_repos.get(repo_key)
    if not isinstance(entry, dict):
        raise ContextError(f"Repository not in project-repos.json: {repo_key}")
    target = github_repo_url(entry["upstream"])
    pr = json.loads(run([
        "gh", "pr", "view", str(pr_number), "--repo", target, "--json",
        "number,title,url,body,headRefOid,baseRefOid,updatedAt",
    ]))
    if pr.get("headRefOid") != expected_head or pr.get("baseRefOid") != expected_base:
        raise ContextError("PR head or base changed since preflight; retry on the next cycle")
    patch = run(["gh", "pr", "diff", str(pr_number), "--repo", target, "--patch"], timeout=120)
    if len(patch) > 2_000_000:
        raise ContextError("PR diff exceeds 2 MB; review manually or split the PR")
    terms = diff_terms(patch)
    snapshot = output_root / "prs" / (
        f"{repo_key.replace('/', '__')}--{pr_number}--{expected_head[:12]}--{expected_base[:12]}"
    )
    snapshot.mkdir(parents=True, exist_ok=True)
    patch_path = snapshot / "pr.diff"
    patch_path.write_text(patch)
    target_path = output_root / "targets" / f"{target.replace('/', '__')}--{pr_number}"
    update_pr_checkout(target, pr_number, expected_head, target_path)
    context = config["repositories"].get(repo_key, {})
    related_results: list[dict] = []
    for related in context.get("related", []):
        repo = github_repo_url(related["url"])
        destination = output_root / "repos" / repo.replace("/", "__")
        result = {"id": related["id"], "repo": repo, "relationship": related["relationship"]}
        try:
            sha = update_related_repo(related["url"], destination)
            result.update({
                "path": str(destination),
                "sha": sha,
                "url": f"https://github.com/{repo}/tree/{sha}",
                "matches": find_matches(destination, terms),
            })
        except (ContextError, subprocess.TimeoutExpired, OSError) as exc:
            result["error"] = str(exc)
        related_results.append(result)
    doc_results: list[dict] = []
    for doc in context.get("docs", []):
        result = {"id": doc["id"], "configured_url": doc["url"]}
        try:
            content, permalink = fetch_doc(doc["url"])
            path = snapshot / f"{doc['id']}.md"
            path.write_text(content)
            result.update({"path": str(path), "url": permalink, "bytes": len(content.encode())})
        except (ContextError, subprocess.TimeoutExpired, OSError, UnicodeError, ValueError) as exc:
            result["error"] = str(exc)
        doc_results.append(result)
    manifest = {
        "pr": {
            "repo_key": repo_key,
            "repo": target,
            "number": pr_number,
            "title": pr.get("title", ""),
            "url": pr.get("url", ""),
            "body": pr.get("body", ""),
            "head_sha": expected_head,
            "base_sha": pr.get("baseRefOid", ""),
            "checkout_path": str(target_path),
            "diff_path": str(patch_path),
            "search_terms": terms,
        },
        "related": related_results,
        "docs": doc_results,
    }
    impact_path = snapshot / "impact.json"
    impact_path.write_text(json.dumps(make_impact_report(manifest, patch), indent=2))
    manifest["impact_path"] = str(impact_path)
    manifest_path = snapshot / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return {"manifest_path": str(manifest_path), **manifest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-key", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--output-root", type=Path, default=Path("data/review-context"))
    args = parser.parse_args()
    try:
        print(json.dumps(build(args.repo_key, args.pr, args.head_sha, args.base_sha, args.output_root), indent=2))
    except (ContextError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"Context build failed: {exc}\n")


if __name__ == "__main__":
    main()
