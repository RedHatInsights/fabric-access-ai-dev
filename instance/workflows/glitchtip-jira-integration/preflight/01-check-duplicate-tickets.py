"""Preflight check: prevent duplicate Jira tickets for the same GlitchTip issue.

Fetches unresolved issues from GlitchTip via REST API, then searches Jira
for tickets labeled with the GlitchTip issue ID pattern (glitchtip-issue-{id}).
Duplicate IDs are written to a skip file for the main script.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error


GLITCHTIP_URL = os.environ.get("GLITCHTIP_URL", "https://glitchtip.devshift.net").rstrip("/")
GLITCHTIP_ORG = os.environ.get("GLITCHTIP_ORG", "")
GLITCHTIP_TOKEN = os.environ.get("GLITCHTIP_TOKEN", "")
JIRA_MCP_URL = os.environ.get("JIRA_MCP_URL", "")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes") or not JIRA_MCP_URL

GLITCHTIP_PROJECTS = set(
    p.strip() for p in os.environ.get("GLITCHTIP_PROJECTS", "").split(",") if p.strip()
)

MAX_PAGINATION_PAGES = 50
MAX_RETRIES = 3
RETRY_BACKOFF = 2


def _request_with_retry(req: urllib.request.Request) -> bytes:
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.read(), resp
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF ** (attempt + 1)
                print(f"  Retrying after HTTP {e.code} (attempt {attempt + 1}/{MAX_RETRIES}, waiting {wait}s)...")
                time.sleep(wait)
                continue
            raise


def glitchtip_get(path: str) -> any:
    url = f"{GLITCHTIP_URL}/api/0/{path}"
    headers = {"Accept": "application/json"}
    if GLITCHTIP_TOKEN:
        headers["Authorization"] = f"Bearer {GLITCHTIP_TOKEN}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    body, _ = _request_with_retry(req)
    return json.loads(body)


def glitchtip_get_paginated(path: str) -> list:
    results = []
    url = f"{GLITCHTIP_URL}/api/0/{path}"
    headers = {"Accept": "application/json"}
    if GLITCHTIP_TOKEN:
        headers["Authorization"] = f"Bearer {GLITCHTIP_TOKEN}"
    seen_urls = set()
    for _ in range(MAX_PAGINATION_PAGES):
        if not url or url in seen_urls:
            break
        seen_urls.add(url)
        req = urllib.request.Request(url, headers=headers, method="GET")
        body, resp = _request_with_retry(req)
        page = json.loads(body)
        if isinstance(page, list):
            results.extend(page)
        else:
            results.append(page)
            break
        link_header = resp.getheader("Link", "")
        url = _parse_next_link(link_header)
    return results


def _parse_next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        if 'rel="next"' in part and 'results="true"' in part:
            start = part.index("<") + 1
            end = part.index(">")
            return part[start:end]
    return None


def call_jira_mcp(tool_name: str, arguments: dict) -> dict:
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }).encode()
    req = urllib.request.Request(
        JIRA_MCP_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    body, _ = _request_with_retry(req)
    return json.loads(body)


def search_jira_for_glitchtip_issues(issue_ids: list[str]) -> set:
    """Batch-search Jira for existing tickets matching any of the given issue IDs."""
    duplicates = set()
    # JQL has a max clause length; batch into groups of 50
    batch_size = 50
    for i in range(0, len(issue_ids), batch_size):
        batch = issue_ids[i:i + batch_size]
        label_clauses = ", ".join(f'"glitchtip-issue-{iid}"' for iid in batch)
        jql = f'project = "{JIRA_PROJECT_KEY}" AND labels in ({label_clauses})'
        result = call_jira_mcp("jira_search", {"jql": jql, "limit": batch_size})
        content = result.get("result", {}).get("content", [])
        if content and isinstance(content, list):
            text = content[0].get("text", "[]")
            tickets = json.loads(text)
            for ticket in tickets:
                labels = ticket.get("fields", {}).get("labels", [])
                for label in labels:
                    if label.startswith("glitchtip-issue-"):
                        duplicates.add(label.replace("glitchtip-issue-", ""))
    return duplicates


def fetch_project_slugs() -> dict:
    projects = glitchtip_get_paginated(f"organizations/{GLITCHTIP_ORG}/projects/")
    slug_map = {}
    for p in projects:
        name = p.get("name", "")
        slug = p.get("slug", "")
        if not GLITCHTIP_PROJECTS or name in GLITCHTIP_PROJECTS or slug in GLITCHTIP_PROJECTS:
            slug_map[slug] = name
    return slug_map


def fetch_unresolved_issues(project_slug: str) -> list:
    return glitchtip_get_paginated(
        f"projects/{GLITCHTIP_ORG}/{project_slug}/issues/?query=is:unresolved"
    )


def main():
    print("Fetching projects from GlitchTip...")
    slug_map = fetch_project_slugs()
    if not slug_map:
        print("No matching projects found.")
        return

    all_issues = []
    for slug, project_name in slug_map.items():
        issues = fetch_unresolved_issues(slug)
        for issue in issues:
            issue["_project_name"] = project_name
        all_issues.extend(issues)

    if not all_issues:
        print("No unresolved GlitchTip issues to check.")
        return

    if DRY_RUN:
        print(f"[DRY RUN] Found {len(all_issues)} issue(s) across {len(slug_map)} project(s):")
        for issue in all_issues:
            issue_id = issue.get("id", "?")
            title = issue.get("title", "unknown")
            count = issue.get("count", 0)
            project = issue.get("_project_name", "unknown")
            print(f"  [{project}] #{issue_id} ({count} occurrences): {title}")
        print("\n[DRY RUN] Skipping Jira duplicate check.")
        return

    issue_ids = [str(issue.get("id", "")) for issue in all_issues if issue.get("id")]
    print(f"Checking {len(issue_ids)} issue(s) against Jira for duplicates...")

    duplicate_ids = search_jira_for_glitchtip_issues(issue_ids)

    if duplicate_ids:
        for issue in all_issues:
            iid = str(issue.get("id", ""))
            if iid in duplicate_ids:
                title = issue.get("title", "unknown")
                print(f"  DUPLICATE: GlitchTip issue {iid} ('{title}')")
        print(f"\n{len(duplicate_ids)} duplicate(s) found. "
              "These issues will be skipped during ingestion.")
        skip_file = os.environ.get("GLITCHTIP_SKIP_FILE", ".glitchtip-skip-ids.json")
        with open(skip_file, "w") as f:
            json.dump(list(duplicate_ids), f)
    else:
        print("No duplicates found. All issues are clear for ticket creation.")


if __name__ == "__main__":
    main()
