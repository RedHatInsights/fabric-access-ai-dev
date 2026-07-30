"""Preflight check: prevent duplicate Jira tickets for the same GlitchTip issue.

Fetches unresolved issues from GlitchTip via REST API, then searches Jira
for tickets labeled with the GlitchTip issue ID pattern (glitchtip-issue-{id}).
Duplicate IDs (with their owning ticket key) are written to a skip file for
the main script.
"""

import json
import os
import sys
import importlib.util

from common import get_capacity, output_result

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills"))

os.environ.setdefault("JIRA_MCP_URL", "")

spec = importlib.util.spec_from_file_location(
    "glitchtip",
    os.path.join(os.path.dirname(__file__), "..", "skills", "glitchtip-jira-integration.py"),
)
gt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gt)

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes") or not gt.JIRA_MCP_URL


def search_jira_for_glitchtip_issues(issue_ids: list[str]) -> dict:
    """Batch-search Jira for existing tickets matching any of the given issue IDs.

    Returns {glitchtip_issue_id: jira_ticket_key}.
    """
    duplicates = {}
    # JQL has a max clause length; batch into groups of 50
    batch_size = 50
    for i in range(0, len(issue_ids), batch_size):
        batch = [iid for iid in issue_ids[i:i + batch_size] if iid.isdigit()]
        if not batch:
            continue
        label_clauses = ", ".join(f'"glitchtip-issue-{iid}"' for iid in batch)
        jql = f'project = "{gt.JIRA_PROJECT_KEY}" AND labels in ({label_clauses})'
        result = gt.call_jira_mcp("jira_search", {"jql": jql, "limit": batch_size})
        content = result.get("result", {}).get("content", [])
        if content and isinstance(content, list):
            text = content[0].get("text", "[]")
            tickets = json.loads(text)
            for ticket in tickets:
                ticket_key = ticket.get("key", "")
                labels = ticket.get("fields", {}).get("labels", [])
                for label in labels:
                    if label.startswith("glitchtip-issue-"):
                        duplicates[label.replace("glitchtip-issue-", "")] = ticket_key
    return duplicates


def main():
    active_n, max_n = get_capacity()
    if active_n >= max_n:
        output_result("skip", f"At capacity ({active_n}/{max_n})")
        return

    print("Fetching projects from GlitchTip...")
    slug_map = gt.fetch_project_slugs()
    if not slug_map:
        output_result("skip", "No matching GlitchTip projects found.")
        return

    all_issues = []
    for slug, project_name in slug_map.items():
        issues = gt.fetch_unresolved_issues(slug)
        for issue in issues:
            issue["_project_name"] = project_name
        all_issues.extend(issues)

    if not all_issues:
        output_result("skip", "No unresolved GlitchTip issues to check.")
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
        output_result("start", json.dumps({"dry_run": True, "issue_count": len(all_issues)}))
        return

    issue_ids = [str(issue.get("id", "")) for issue in all_issues if issue.get("id")]
    print(f"Checking {len(issue_ids)} issue(s) against Jira for duplicates...")

    duplicates = search_jira_for_glitchtip_issues(issue_ids)

    if duplicates:
        for issue in all_issues:
            iid = str(issue.get("id", ""))
            if iid in duplicates:
                title = issue.get("title", "unknown")
                print(f"  DUPLICATE: GlitchTip issue {iid} ('{title}') -> {duplicates[iid]}")
        print(f"\n{len(duplicates)} duplicate(s) found. "
              "These issues will be skipped during ingestion.")
        existing = gt.load_skip_ids()
        existing.update(duplicates)
        gt.save_skip_ids(existing)
    else:
        print("No duplicates found. All issues are clear for ticket creation.")

    output_result("start", json.dumps({
        "issue_count": len(all_issues),
        "duplicate_count": len(duplicates),
        "new_count": len(all_issues) - len(duplicates),
    }))


if __name__ == "__main__":
    main()
