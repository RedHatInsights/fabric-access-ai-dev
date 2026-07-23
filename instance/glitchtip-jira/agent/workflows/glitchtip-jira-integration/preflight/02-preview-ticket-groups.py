"""Preflight check: preview how GlitchTip issues will be grouped into Jira tickets.

Shows the normalized error patterns, how many GlitchTip issues fall into each
group, total occurrences, and estimated priority. Runs before ticket creation
so you can verify grouping looks right and no flood of tickets will be created.

Required env vars:
  GLITCHTIP_ORG, GLITCHTIP_TOKEN

Optional:
  GLITCHTIP_URL                   (default: https://glitchtip.devshift.net)
  GLITCHTIP_PROJECTS              (comma-separated project slugs)
  JIRA_PROJECT_KEY                (for priority display; not required)
  GLITCHTIP_SKIP_FILE             (default: .glitchtip-skip-ids.json)
  MAX_TICKETS                     (default: 50 — simulates the ticket cap)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills"))

os.environ.setdefault("JIRA_MCP_URL", "")

import importlib.util
spec = importlib.util.spec_from_file_location(
    "glitchtip",
    os.path.join(os.path.dirname(__file__), "..", "skills", "glitchtip-jira-integration.py"),
)
gt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gt)

JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "")
MAX_TICKETS = int(os.environ.get("MAX_TICKETS", "50")) or 50


def main():
    print("=" * 70)
    print("PREFLIGHT: Ticket Group Preview")
    print("=" * 70)

    skip_ids = gt.load_skip_ids()
    if skip_ids:
        print(f"Skip file loaded: {len(skip_ids)} issue(s) will be excluded")

    print("\nFetching projects from GlitchTip...")
    slug_map = gt.fetch_project_slugs()
    if not slug_map:
        print("No matching projects found.")
        return
    print(f"Target projects: {', '.join(slug_map.values())}")

    total_issues = 0
    total_groups = 0
    total_skipped = 0
    all_groups = []

    for slug, project_name in slug_map.items():
        print(f"\n--- {project_name} ---")
        issues = gt.fetch_unresolved_issues(slug)
        if not issues:
            print("  No unresolved issues.")
            continue

        print(f"  Raw issues: {len(issues)}")

        filtered = [i for i in issues if str(i.get("id", "")) not in skip_ids]
        skipped = len(issues) - len(filtered)
        if skipped:
            print(f"  Skipped (already in Jira): {skipped}")

        total_issues += len(filtered)
        total_skipped += skipped

        groups = gt.group_issues(filtered)
        print(f"  Grouped into: {len(groups)} unique error pattern(s)")
        total_groups += len(groups)

        for group in groups:
            group["_project_name"] = project_name
        all_groups.extend(groups)

    if not all_groups:
        print("\nNo issues to process.")
        return

    all_groups.sort(key=lambda g: g["total_count"], reverse=True)

    print(f"\n{'=' * 70}")
    print(f"SUMMARY: {total_issues} issues -> {total_groups} tickets")
    if total_skipped:
        print(f"         {total_skipped} issues skipped (already have Jira tickets)")
    print(f"         MAX_TICKETS cap: {MAX_TICKETS}")
    would_create = min(total_groups, MAX_TICKETS)
    print(f"         Would create: {would_create} ticket(s)")
    print(f"{'=' * 70}")

    print(f"\n{'#':>4}  {'Issues':>6}  {'Occurrences':>11}  {'Priority':>8}  {'Project':<30}  Title")
    print(f"{'─' * 4}  {'─' * 6}  {'─' * 11}  {'─' * 8}  {'─' * 30}  {'─' * 50}")

    for i, group in enumerate(all_groups, 1):
        issue = group["representative"]
        project = group.get("_project_name", "unknown")
        title = issue.get("title", "unknown")
        group_size = len(group["issues"])
        total_count = group["total_count"]

        issue_copy = dict(issue)
        issue_copy["count"] = total_count
        priority = gt.compute_priority(issue_copy, project)

        cap_marker = " " if i <= MAX_TICKETS else "*"
        norm = gt.normalize_title(title)
        if len(norm) > 80:
            norm = norm[:77] + "..."

        print(f"{i:>4}{cap_marker} {group_size:>6}  {total_count:>11}  {priority:>8}  {project:<30}  {norm}")

    if total_groups > MAX_TICKETS:
        print(f"\n* Groups marked with * exceed MAX_TICKETS={MAX_TICKETS} and would NOT get tickets.")

    print(f"\nTop 5 groups by occurrence count:")
    for i, group in enumerate(all_groups[:5], 1):
        issue = group["representative"]
        title = issue.get("title", "unknown")
        if len(title) > 100:
            title = title[:97] + "..."
        print(f"  {i}. {group['total_count']:,} occurrences ({len(group['issues'])} issues): {title}")


if __name__ == "__main__":
    main()
