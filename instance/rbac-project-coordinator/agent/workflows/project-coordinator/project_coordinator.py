"""Read-only Jira preflight for the Fabric Access project coordinator.

Jira labels and links are the project state. The memory task only records the
last snapshot inspected by the agent, so an unchanged project costs no model
cycle. All Jira mutations happen in the workflow agent, after a fresh read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
from collections import defaultdict

from common import INSTANCE_ID, MEMORY_URL, http_get, load_project_repos, output_result
from jira_mcp import jira_call, jira_cleanup

PHASE_RE = re.compile(r"^phase:(\d{2})$")
JQL_VALUE_RE = re.compile(r"^[A-Za-z0-9:_-]+$")
POC_RE = re.compile(r"^\[POC\s+\d+\]", re.IGNORECASE)
COMPLETE_STATUSES = frozenset({"done", "closed", "resolved", "release pending"})
START_STATUSES = frozenset({"new", "backlog", "refinement", "to do"})
COORD_STATES = frozenset({"coord:needs-plan", "coord:active", "coord:blocked", "coord:complete"})
OWN_COMMENT_PREFIX = "### Coordinator update"
PAGE_SIZE = 50


def fields(issue: dict) -> dict:
    nested = issue.get("fields")
    return nested if isinstance(nested, dict) else issue


def value(issue: dict, field: str):
    return fields(issue).get(field, issue.get(field))


def key_of(issue: dict | str | None) -> str:
    if isinstance(issue, str):
        return issue
    if isinstance(issue, dict):
        return str(issue.get("key") or issue.get("issue_key") or "")
    return ""


def team_id(issue: dict, field_id: str) -> str:
    team = value(issue, field_id)
    if isinstance(team, dict):
        return str(team.get("id") or team.get("teamId") or "")
    return str(team) if team is not None else ""


def labels(issue: dict) -> list[str]:
    return list(value(issue, "labels") or [])


def parent_key(issue: dict) -> str:
    return key_of(value(issue, "parent")) or key_of(value(issue, "epic_key"))


def status_name(issue: dict) -> str:
    status = value(issue, "status") or {}
    return str(status.get("name", "") if isinstance(status, dict) else status)


def is_complete(issue: dict) -> bool:
    return status_name(issue).lower() in COMPLETE_STATUSES


def comments(issue: dict) -> list[dict]:
    direct = issue.get("comments")
    if isinstance(direct, list):
        items = direct
    else:
        comment_field = value(issue, "comment") or {}
        items = (comment_field.get("comments") or []) if isinstance(comment_field, dict) else []
    comment_field = value(issue, "comment") or {}
    total = comment_field.get("total") if isinstance(comment_field, dict) else None
    if isinstance(total, int) and total > len(items):
        raise RuntimeError(f"Jira returned only {len(items)} of {total} comments on {key_of(issue)}")
    return items


def load_tasks() -> dict[str, dict]:
    """Read every task for this instance; fail closed on memory API errors."""
    if not INSTANCE_ID:
        raise RuntimeError("BOT_INSTANCE_ID is required")
    tasks: dict[str, dict] = {}
    offset = 0
    while True:
        query = urllib.parse.urlencode({"instance_id": INSTANCE_ID, "limit": 50, "offset": offset})
        result = http_get(f"{MEMORY_URL}/api/tasks?{query}")
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise RuntimeError("Memory task list unavailable")
        page = result["items"]
        for task in page:
            tasks[task.get("external_key")] = task
        offset += len(page)
        if offset >= result.get("total", offset) or not page:
            return tasks


def comment_body(comment: dict) -> str:
    body = comment.get("body") or ""
    return body if isinstance(body, str) else json.dumps(body, sort_keys=True)


def search_all(jql: str, requested_fields: str = "*all") -> list[dict]:
    """Walk both Jira Cloud cursor pages and Server/DC offset pages."""
    issues: list[dict] = []
    offset = 0
    token = None
    seen_tokens: set[str] = set()
    for _ in range(100):
        args = {"jql": jql, "fields": requested_fields, "limit": PAGE_SIZE}
        if token:
            args["page_token"] = token
        elif offset:
            args["start_at"] = offset
        result = jira_call("jira_search", args)
        if not isinstance(result, dict) or result.get("isError") or result.get("success") is False:
            raise RuntimeError(f"Jira search failed: {jql}")
        page = result.get("issues")
        if not isinstance(page, list):
            raise RuntimeError(f"Jira search returned no issue list: {jql}")
        issues.extend(page)
        next_token = result.get("next_page_token") or result.get("nextPageToken")
        if next_token:
            if next_token in seen_tokens:
                raise RuntimeError("Jira search repeated a page token")
            seen_tokens.add(next_token)
            token = next_token
            continue
        total = result.get("total", -1)
        if isinstance(total, int) and total >= 0 and len(issues) < total:
            offset += len(page)
            if not page:
                raise RuntimeError("Jira search stopped before total")
            continue
        return issues
    raise RuntimeError("Jira search exceeded 100 pages")


def get_issue(issue_key: str) -> dict:
    issue = jira_call("jira_get_issue", {"issue_key": issue_key, "fields": "*all", "comment_limit": 100})
    if not isinstance(issue, dict) or issue.get("isError") or issue.get("success") is False:
        raise RuntimeError(f"Jira issue unavailable: {issue_key}")
    return issue


def linked_issues(issue: dict) -> tuple[set[str], set[str]]:
    """Return (blockers, related) from Jira's directional issue links."""
    blockers: set[str] = set()
    related: set[str] = set()
    for link in value(issue, "issuelinks") or []:
        link_type = link.get("type") or {}
        name = str(link_type.get("name") or "").lower()
        inward = link.get("inwardIssue")
        outward = link.get("outwardIssue")
        other = key_of(inward or outward)
        if not other:
            continue
        if "relate" in name:
            related.add(other)
        if inward and ("block" in name or "block" in str(link_type.get("inward") or "").lower()):
            blockers.add(other)
    return blockers, related


def cyclic_dependencies(blockers_by_key: dict[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> bool:
        if key in visiting:
            return True
        if key in visited:
            return False
        visiting.add(key)
        for blocker in blockers_by_key.get(key, set()):
            if blocker in blockers_by_key and visit(blocker):
                return True
        visiting.remove(key)
        visited.add(key)
        return False

    return any(visit(key) for key in blockers_by_key)


def summarize_child(issue: dict, control_key: str) -> dict:
    blockers, related = linked_issues(issue)
    issue_labels = labels(issue)
    assignee = value(issue, "assignee")
    sprint = value(issue, "customfield_10020")
    return {
        "key": key_of(issue),
        "summary": value(issue, "summary") or "",
        "status": status_name(issue),
        "phase_labels": sorted(label for label in issue_labels if PHASE_RE.fullmatch(label)),
        "repo_labels": sorted(label for label in issue_labels if label.startswith("repo:")),
        "implementation_labeled": False,  # filled by build_snapshot
        "blockers": sorted(blockers),
        "relates_to_control": control_key in related,
        "assignee": bool(assignee),
        "sprint": bool(sprint),
        "description": str(value(issue, "description") or "")[:2000],
        "poc": bool(POC_RE.match(str(value(issue, "summary") or ""))),
    }


def evaluate(control: dict, children: list[dict], repo_names: set[str], implementation_label: str, include_backlog: bool) -> dict:
    """Mechanical plan checks. The agent still judges requirement quality."""
    control_key = key_of(control)
    control_labels = labels(control)
    gaps: list[str] = []
    state_labels = [label for label in control_labels if label in COORD_STATES]
    if len(state_labels) > 1:
        gaps.append("Control ticket has multiple coordinator state labels")
    if any(PHASE_RE.fullmatch(label) for label in control_labels):
        gaps.append("Phase labels belong on implementation tickets, not the control ticket")
    if implementation_label in control_labels:
        gaps.append("Control ticket has the implementation bot label")

    allowed_repos = {label[5:] for label in control_labels if label.startswith("repo:")}
    if not allowed_repos:
        gaps.append("Control ticket needs one or more allowed repo: labels")
    unknown = allowed_repos - repo_names
    if unknown:
        gaps.append(f"Control ticket has unknown repos: {', '.join(sorted(unknown))}")

    tickets = [summarize_child(issue, control_key) for issue in children]
    for ticket, issue in zip(tickets, children):
        ticket["implementation_labeled"] = implementation_label in labels(issue)
    implementation = [ticket for ticket in tickets if not ticket["poc"]]
    for ticket in tickets:
        if ticket["poc"] and (ticket["phase_labels"] or ticket["implementation_labeled"]):
            gaps.append(f"POC ticket {ticket['key']} has a phase or implementation label")
    by_key = {ticket["key"]: ticket for ticket in implementation}
    phases: dict[str, list[dict]] = defaultdict(list)
    for ticket in implementation:
        phase_labels = ticket["phase_labels"]
        if not phase_labels:
            gaps.append(f"{ticket['key']} has no phase label")
        if len(phase_labels) > 1:
            gaps.append(f"{ticket['key']} has multiple phase labels")
        if len(phase_labels) == 1:
            phases[phase_labels[0]].append(ticket)

    if not phases.get("phase:01"):
        gaps.append("No same-Team implementation ticket has phase:01")
    blockers_by_key = {ticket["key"]: set(ticket["blockers"]) for ticket in implementation}
    if cyclic_dependencies(blockers_by_key):
        gaps.append("Implementation ticket dependency links form a cycle")

    for ticket in implementation:
        internal_blockers = set(ticket["blockers"]) & set(by_key)
        if ticket["phase_labels"] and not internal_blockers and not ticket["relates_to_control"]:
            gaps.append(f"Root ticket {ticket['key']} must relate to the control ticket")

    incomplete_phases = sorted(
        phase for phase, phase_tickets in phases.items() if any(ticket["status"].lower() not in COMPLETE_STATUSES for ticket in phase_tickets)
    )
    current_phase = incomplete_phases[0] if incomplete_phases else None
    ready: list[dict] = []
    waiting: list[str] = []
    if current_phase and not gaps:
        for ticket in phases[current_phase]:
            if ticket["status"].lower() in COMPLETE_STATUSES or ticket["implementation_labeled"]:
                continue
            blockers = set(ticket["blockers"])
            external = blockers - set(by_key)
            incomplete = {key for key in blockers & set(by_key) if by_key[key]["status"].lower() not in COMPLETE_STATUSES}
            if external or incomplete:
                waiting.append(ticket["key"])
                continue
            child_repos = {label[5:] for label in ticket["repo_labels"]}
            if child_repos and (child_repos - allowed_repos or child_repos - repo_names):
                gaps.append(f"{ticket['key']} has a repo outside the control ticket's allowed repos")
                continue
            if ticket["status"].lower() not in START_STATUSES or ticket["assignee"]:
                gaps.append(f"{ticket['key']} is not unassigned in a not-started status")
                continue
            if not ticket["sprint"] and not include_backlog:
                gaps.append(f"{ticket['key']} is outside the active sprint and backlog intake is disabled")
                continue
            ready.append({"key": ticket["key"], "repo_candidates": sorted(child_repos or allowed_repos)})

    return {
        "state": state_labels[0] if len(state_labels) == 1 else None,
        "mode": "poc" if "coord-mode:poc" in control_labels else "normal",
        "allowed_repos": sorted(allowed_repos),
        "current_phase": current_phase,
        "gaps": gaps,
        "ready": ready if not gaps else [],
        "waiting": waiting,
        "tickets": tickets,
    }


def fingerprint(parent: dict, control: dict, children: list[dict], team_field_id: str) -> str:
    """Ignore coordinator comments/state labels to avoid waking on own reports."""
    human_comments = [
        {"id": str(comment.get("id") or ""), "body": comment_body(comment)}
        for comment in comments(control)
        if not comment_body(comment).startswith(OWN_COMMENT_PREFIX)
    ]
    payload = {
        "parent": {"key": key_of(parent), "summary": value(parent, "summary"), "description": value(parent, "description"), "status": status_name(parent)},
        "control": {
            "key": key_of(control),
            "summary": value(control, "summary"),
            "description": value(control, "description"),
            "parent": parent_key(control),
            "team": team_id(control, team_field_id),
            "labels": sorted(label for label in labels(control) if label not in COORD_STATES),
            "comments": human_comments,
        },
        "children": [
            {
                "key": key_of(child), "summary": value(child, "summary"), "description": value(child, "description"),
                "status": status_name(child), "team": team_id(child, team_field_id),
                "labels": sorted(labels(child)), "links": value(child, "issuelinks") or [],
                "assignee": value(child, "assignee"), "sprint": value(child, "customfield_10020"),
            }
            for child in sorted(children, key=key_of)
        ],
    }
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def main() -> None:
    project = os.environ.get("BOT_JIRA_PROJECT", "RHCLOUD")
    control_label = os.environ.get("BOT_LABEL", "")
    team_field_id = os.environ.get("BOT_TEAM_FIELD_ID", "")
    expected_team_id = os.environ.get("BOT_TEAM_ID", "")
    implementation_label = os.environ.get("BOT_IMPLEMENTATION_LABEL", "")
    include_backlog = os.environ.get("BOT_INCLUDE_BACKLOG", "").lower() in {"1", "true", "yes"}
    required = {"BOT_LABEL": control_label, "BOT_TEAM_FIELD_ID": team_field_id, "BOT_TEAM_ID": expected_team_id, "BOT_IMPLEMENTATION_LABEL": implementation_label}
    missing = [name for name, item in required.items() if not item]
    if missing:
        output_result("error", f"Missing configuration: {', '.join(missing)}")
        return
    if not all(JQL_VALUE_RE.fullmatch(item) for item in (project, control_label, expected_team_id, implementation_label)):
        output_result("error", "Invalid Jira project, label, or Team ID configuration")
        return
    if not re.fullmatch(r"customfield_\d+", team_field_id):
        output_result("error", "BOT_TEAM_FIELD_ID must be a customfield_ number")
        return

    try:
        candidates = search_all(f'project = "{project}" AND labels = "{control_label}" ORDER BY key ASC', "key,labels,parent")
        tasks = load_tasks()
        repo_names = set(load_project_repos())
        actionable = []
        for candidate in candidates:
            control_key = key_of(candidate)
            control = get_issue(control_key)
            actual_team = team_id(control, team_field_id)
            if actual_team and actual_team != expected_team_id:
                continue
            parent = parent_key(control)
            if not parent:
                task = tasks.get(control_key) or {}
                digest = fingerprint({}, control, [], team_field_id)
                if (task.get("metadata") or {}).get("snapshot_hash") != digest:
                    actionable.append({
                        "control_key": control_key,
                        "team_id": expected_team_id,
                        "team_field_id": team_field_id,
                        "snapshot_hash": digest,
                        "error": "Control ticket has no parent issue",
                    })
                continue
            parent_issue = get_issue(parent)
            siblings = search_all(f'parent = "{parent}" ORDER BY key ASC')
            children = [issue for issue in siblings if key_of(issue) != control_key and team_id(issue, team_field_id) == expected_team_id]
            other_controls = [issue for issue in siblings if key_of(issue) != control_key and control_label in labels(issue) and team_id(issue, team_field_id) == expected_team_id]
            assessment = evaluate(control, children, repo_names, implementation_label, include_backlog)
            if not actual_team:
                assessment["gaps"].append("Control ticket has no Jira Team ID")
                assessment["ready"] = []
            if other_controls:
                assessment["gaps"].append("Parent has multiple same-Team control tickets")
                assessment["ready"] = []
            digest = fingerprint(parent_issue, control, children, team_field_id)
            task = tasks.get(control_key) or {}
            old_digest = (task.get("metadata") or {}).get("snapshot_hash")
            if not old_digest or old_digest != digest or not assessment["state"]:
                actionable.append({
                    "control_key": control_key,
                    "parent_key": parent,
                    "team_id": expected_team_id,
                    "team_field_id": team_field_id,
                    "snapshot_hash": digest,
                    "first_run": not bool(old_digest),
                    "new_comments": [
                        {"id": str(comment.get("id") or ""), "body": comment_body(comment), "created": comment.get("created")}
                        for comment in comments(control)
                        if not comment_body(comment).startswith(OWN_COMMENT_PREFIX)
                        and str(comment.get("id") or "") not in set((task.get("metadata") or {}).get("processed_comment_ids") or [])
                    ],
                    **assessment,
                })
        if actionable:
            output_result("start", json.dumps({"projects": actionable, "implementation_label": implementation_label}, default=str))
        else:
            output_result("skip", f"No changed {project} control tickets for Team {expected_team_id}")
    except Exception as error:
        output_result("error", str(error))
    finally:
        jira_cleanup()


if __name__ == "__main__":
    main()
