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
import subprocess
import urllib.parse
from collections import defaultdict
from pathlib import Path

from common import INSTANCE_ID, MEMORY_URL, http_get, load_project_repos, output_result
from jira_mcp import jira_call, jira_cleanup

# Vault-sourced settings arrive under nonreserved FABRIC_ names (rendered
# from the tier-2 `fabric-access-rehor` Secret into sandbox.env); legacy
# deployments still supply the reserved BOT_ names through app-interface
# parameters. In the sandbox the FABRIC_ delivery is required: a missing
# value raises instead of silently using a legacy one. Outside the sandbox
# the BOT_ fallback stays until cutover.
FABRIC_PREFIX = "FABRIC_"
LEGACY_PREFIX = "BOT_"


def fabric_setting(suffix: str, environ: dict[str, str] | None = None) -> str:
    """Prefer the sandbox FABRIC_ delivery of a setting; fall back to BOT_.

    Sandbox mode (REHOR_INPUT_SOURCE=sandbox) requires the FABRIC_ name and
    never falls back; legacy mode falls back to the BOT_ alias.
    """
    env = os.environ if environ is None else environ
    value = env.get(FABRIC_PREFIX + suffix, "").strip()
    if value:
        return value
    if env.get("REHOR_INPUT_SOURCE", "").strip() == "sandbox":
        raise ValueError(f"Missing required sandbox setting: FABRIC_{suffix}")
    return env.get(LEGACY_PREFIX + suffix, "").strip()

PHASE_RE = re.compile(r"^phase:(\d{2})$")
JQL_VALUE_RE = re.compile(r"^[A-Za-z0-9:_-]+$")
COMPLETE_STATUSES = frozenset({"done", "closed", "resolved", "release pending"})
START_STATUSES = frozenset({"new", "backlog", "refinement", "to do"})
UNASSIGNED_MARKERS = frozenset({"", "unassigned", "unassigned user"})
PLANNER_COMMAND = re.compile(r"PLANNER:[ \t]*/coord[ \t]+plan(?:[ \t]+(.+))?", re.IGNORECASE)
PLANNER_HEADING = "PLANNER: Planner update - Technical plan"
PLANNER_RECEIPT = "Planner request comment:"
PLANNER_REQUEST_DIR = Path("data/planner-requests")
COMMENT_ID = re.compile(r"[1-9][0-9]*")
COORD_STATES = frozenset(
    {
        "dev-bot-coordinator:needs-plan",
        "dev-bot-coordinator:active",
        "dev-bot-coordinator:blocked",
        "dev-bot-coordinator:complete",
    }
)
OWN_COMMENT_PREFIX = "Coordinator update"
COORDINATOR_ROLE_PREFIX = "COORDINATOR:"
PAGE_SIZE = 50


def fields(issue: dict) -> dict:
    nested = issue.get("fields")
    return nested if isinstance(nested, dict) else issue


def value(issue: dict, field: str):
    return fields(issue).get(field, issue.get(field))


def is_assigned(assignee: object) -> bool:
    """Treat Jira's explicit Unassigned placeholder like an empty assignee."""
    if assignee is None:
        return False
    if isinstance(assignee, str):
        return assignee.strip().casefold() not in UNASSIGNED_MARKERS
    if isinstance(assignee, list):
        return any(is_assigned(item) for item in assignee)
    if isinstance(assignee, dict):
        identifier_keys = ("accountId", "account_id", "name", "key", "username", "emailAddress", "id")
        identifiers = [
            assignee.get(key)
            for key in identifier_keys
            if isinstance(assignee.get(key), (str, int))
            and not isinstance(assignee.get(key), bool)
            and str(assignee.get(key)).strip()
        ]
        if identifiers:
            return any(
                str(identifier).strip().casefold() not in UNASSIGNED_MARKERS
                for identifier in identifiers
            )
        for display_name_key in ("displayName", "display_name"):
            display_name = assignee.get(display_name_key)
            if (
                isinstance(display_name, str)
                and display_name.strip().casefold() in UNASSIGNED_MARKERS
            ):
                return False
        for key in ("value", "user", "assignee"):
            if assignee.get(key) is not None:
                return is_assigned(assignee[key])
        return bool(assignee)
    return bool(assignee)


def key_of(issue: dict | str | None) -> str:
    if isinstance(issue, str):
        return issue
    if isinstance(issue, dict):
        return str(issue.get("key") or issue.get("issue_key") or "")
    return ""


def team_id(issue: dict, field_id: str) -> str:
    """Read Jira Team IDs across the field shapes returned by Jira/MCP."""
    def extract(raw, *, allow_scalar: bool = True) -> str:
        if isinstance(raw, dict):
            for key in ("id", "teamId", "team_id", "teamID"):
                item = raw.get(key)
                if item is not None and not isinstance(item, (dict, list)):
                    result = str(item).strip()
                    if result:
                        return result
            for key in ("team", "option", "value", "data"):
                item = raw.get(key)
                if isinstance(item, (dict, list)):
                    result = extract(item, allow_scalar=False)
                    if result:
                        return result
                elif isinstance(item, (int, float)) and not isinstance(item, bool):
                    return str(item)
                elif isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_-]+", item.strip()):
                    return item.strip()
            return ""
        if isinstance(raw, list):
            ids = {extract(item, allow_scalar=False) for item in raw}
            ids.discard("")
            return next(iter(ids)) if len(ids) == 1 else ""
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return str(raw)
        if isinstance(raw, str):
            item = raw.strip()
            if item.startswith(("{", "[")):
                try:
                    parsed = json.loads(item)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    return extract(parsed, allow_scalar=False)
            return item if allow_scalar else ""
        return ""

    return extract(value(issue, field_id))


def labels(issue: dict) -> list[str]:
    return list(value(issue, "labels") or [])


def implementation_children(siblings: list[dict], control: dict, field_id: str) -> list[dict]:
    control_team = team_id(control, field_id)
    if not control_team:
        return []
    return [
        issue for issue in siblings
        if key_of(issue) != key_of(control)
        and team_id(issue, field_id) == control_team
    ]


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
    if isinstance(body, str):
        stripped = body.strip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                body = parsed
            else:
                return body
        else:
            return body

    parts: list[str] = []

    def collect(node) -> None:
        if isinstance(node, list):
            for item in node:
                collect(item)
            return
        if not isinstance(node, dict):
            return
        node_type = node.get("type")
        if node_type == "text":
            text = node.get("text")
            if isinstance(text, str):
                parts.append(text)
            return
        if node_type == "mention":
            attrs = node.get("attrs") or {}
            text = attrs.get("text") if isinstance(attrs, dict) else None
            if isinstance(text, str):
                parts.append(text)
            return
        content = node.get("content")
        if node_type == "codeBlock":
            attrs = node.get("attrs") or {}
            language = attrs.get("language") if isinstance(attrs, dict) else None
            if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", language):
                language = ""
            parts.append(f"```{language}\n")
            if isinstance(content, list):
                collect(content)
            parts.append("\n```\n")
            return
        if isinstance(content, list):
            collect(content)
        elif isinstance(node.get("text"), str):
            parts.append(node["text"])
        if node_type in {"paragraph", "heading", "listItem", "blockquote", "codeBlock"}:
            parts.append("\n")

    collect(body)
    flattened = "".join(parts).strip()
    return flattened if flattened else json.dumps(body, sort_keys=True)


def is_own_comment(comment: dict) -> bool:
    """Recognize coordinator comments after Jira normalizes Markdown to ADF."""
    body = comment_body(comment)
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return False
    heading = lines[0].lstrip("#").strip().casefold()
    if heading.startswith(COORDINATOR_ROLE_PREFIX.casefold()):
        heading = heading[len(COORDINATOR_ROLE_PREFIX):].strip()
    prefix = OWN_COMMENT_PREFIX.casefold()
    if heading == prefix or heading.startswith(prefix + " -"):
        return True
    return any(line.startswith("Coordinator action:") for line in lines[-2:])


def planner_focus(comment: dict) -> str | None:
    """Only a first-line PLANNER route can request a technical plan."""
    lines = comment_body(comment).strip().splitlines()
    if not lines:
        return None
    match = PLANNER_COMMAND.fullmatch(lines[0].strip())
    if match:
        return "\n".join([match[1] or "", *lines[1:]]).strip()
    return None


def planner_receipt(comment: dict) -> str | None:
    lines = comment_body(comment).strip().splitlines()
    if not lines or not lines[0].lstrip("#").strip().casefold().startswith("planner: planner update -"):
        return None
    marker = re.fullmatch(rf"{re.escape(PLANNER_RECEIPT)} ([1-9][0-9]*)", lines[-1].strip())
    return marker[1] if marker else None


def planner_coordination(comment: dict) -> dict | None:
    """Read the planner's machine-readable Jira label assignments."""
    body = comment_body(comment)
    if not planner_receipt(comment) or not body.splitlines()[0].strip().casefold().startswith(PLANNER_HEADING.casefold()):
        return None
    match = re.search(r"```planner-coordination\s*(.*?)\s*```", body, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    try:
        proposal = json.loads(match[1])
    except json.JSONDecodeError:
        return None
    return proposal if isinstance(proposal, dict) else None


def is_coordination_feedback(comment: dict) -> bool:
    lines = comment_body(comment).strip().splitlines()
    routed_to_coordinator = bool(lines and lines[0].strip().casefold().startswith(COORDINATOR_ROLE_PREFIX.casefold()))
    return routed_to_coordinator and not is_own_comment(comment) and planner_focus(comment) is None and planner_receipt(comment) is None


def planner_request_path(control_key: str, comment_id: str) -> Path:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*", control_key) or not COMMENT_ID.fullmatch(comment_id):
        raise ValueError("Invalid control ticket key or planner comment ID")
    return PLANNER_REQUEST_DIR / f"{control_key}-{comment_id}.json"


def pending_planner_request(control: dict, control_label: str) -> dict | None:
    parent = parent_key(control)
    if not parent or control_label not in labels(control):
        return None
    items = comments(control)
    handled = {planner_receipt(item) for item in items}
    for item in items:
        comment_id = str(item.get("id") or "")
        if not COMMENT_ID.fullmatch(comment_id) or comment_id in handled or planner_focus(item) is None:
            continue
        control_key = key_of(control)
        path = planner_request_path(control_key, comment_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "control_label": control_label,
            "parent_key": parent,
            "docs_base": os.getenv("BOT_PLANNER_DOCS_BASE_URL", "").strip(),
            "docs_glob": os.getenv("BOT_PLANNER_DOCS_GLOB", "").strip() or "*.md",
        }), encoding="utf-8")
        return {"control_key": control_key, "comment_id": comment_id, "parent_key": parent}
    return None


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


def open_pr_snapshot(children: list[dict], implementation_label: str, project_repos: dict) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Read open GitHub PRs for handed-off tickets so PR-only changes wake the coordinator."""
    tickets_by_repo: dict[str, set[str]] = defaultdict(set)
    for child in children:
        child_labels = labels(child)
        if implementation_label not in child_labels:
            continue
        for label in child_labels:
            if label.startswith("repo:"):
                tickets_by_repo[label[5:]].add(key_of(child))

    snapshot: dict[str, list[dict]] = {key: [] for keys in tickets_by_repo.values() for key in keys}
    errors: dict[str, str] = {}
    for repo_name, ticket_keys in sorted(tickets_by_repo.items()):
        config = project_repos.get(repo_name)
        url = ""
        if isinstance(config, dict):
            url = config.get("upstream") or config.get("url") or ""
        match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", str(url or ""))
        if not match:
            # GitLab repos have no GitHub review queue.
            continue
        upstream = f"{match.group(1)}/{match.group(2)}"
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--repo", upstream, "--state", "open", "--limit", "1000", "--json", "number,title,labels,url"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip()[:300] or "gh pr list failed")
            prs = json.loads(result.stdout)
            if not isinstance(prs, list):
                raise RuntimeError("gh pr list returned an invalid list")
        except Exception as error:
            errors[repo_name] = str(error)
            continue
        for pr in prs:
            if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
                continue
            title = str(pr.get("title") or "")
            names = sorted(
                item["name"] for item in (pr.get("labels") or [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            )
            for ticket_key in ticket_keys:
                marker = re.compile(rf"(?<![A-Za-z0-9])\[{re.escape(ticket_key)}\](?![A-Za-z0-9])", re.IGNORECASE)
                if marker.search(title):
                    snapshot[ticket_key].append({
                        "repo": upstream,
                        "number": pr["number"],
                        "title": title,
                        "url": pr.get("url") or f"https://github.com/{upstream}/pull/{pr['number']}",
                        "labels": names,
                    })
    for prs in snapshot.values():
        prs.sort(key=lambda item: (item["repo"], item["number"]))
    return snapshot, errors


def summarize_child(issue: dict) -> dict:
    blockers, _ = linked_issues(issue)
    issue_labels = labels(issue)
    assignee = value(issue, "assignee")
    return {
        "key": key_of(issue),
        "summary": value(issue, "summary") or "",
        "status": status_name(issue),
        "phase_labels": sorted(label for label in issue_labels if PHASE_RE.fullmatch(label)),
        "repo_labels": sorted(label for label in issue_labels if label.startswith("repo:")),
        "implementation_labeled": False,  # filled by build_snapshot
        "blockers": sorted(blockers),
        "assignee": is_assigned(assignee),
        "description": str(value(issue, "description") or "")[:2000],
    }


def ticket_plan_snapshot(
    children: list[dict],
    team_field_id: str,
    implementation_label: str,
) -> dict[str, dict]:
    """Capture same-Team ticket fields that can change the approved plan."""
    snapshot = {}
    for issue in sorted(children, key=key_of):
        blockers, related = linked_issues(issue)
        issue_labels = labels(issue)
        priority = value(issue, "priority") or {}
        issue_type = value(issue, "issuetype") or value(issue, "issue_type") or {}
        snapshot[key_of(issue)] = {
            "summary": str(value(issue, "summary") or "").strip(),
            "description": str(value(issue, "description") or ""),
            "priority": str(priority.get("name") or priority.get("id") or "")
            if isinstance(priority, dict) else str(priority),
            "issue_type": str(issue_type.get("name") or "")
            if isinstance(issue_type, dict) else str(issue_type),
            "team_id": team_id(issue, team_field_id),
            "phase_labels": sorted(label for label in issue_labels if PHASE_RE.fullmatch(label)),
            "repo_labels": sorted(label for label in issue_labels if label.startswith("repo:")),
            "planning_labels": sorted(
                label for label in issue_labels
                if not PHASE_RE.fullmatch(label)
                and not label.startswith("repo:")
                and not label.startswith("dev-bot-coordinator:")
                and label != implementation_label
            ),
            "blockers": sorted(blockers),
            "related": sorted(related),
        }
    return snapshot


def ticket_plan_changes(
    previous: object,
    current: dict[str, dict],
    implementation_label: str,
) -> dict:
    """Compare membership and plan inputs, ignoring ordinary execution progress."""
    if not isinstance(previous, dict):
        return {
            "baseline_missing": True,
            "added": [],
            "removed": [],
            "changed": [],
        }
    old_keys, new_keys = set(previous), set(current)
    added = [{"key": key, "ticket": current[key]} for key in sorted(new_keys - old_keys)]
    removed = [{"key": key, "ticket": previous[key]} for key in sorted(old_keys - new_keys)]
    changed = []
    plan_fields = {
        "summary",
        "description",
        "priority",
        "issue_type",
        "team_id",
        "phase_labels",
        "repo_labels",
        "planning_labels",
        "blockers",
        "related",
    }
    for key in sorted(old_keys & new_keys):
        old_ticket = previous[key]
        new_ticket = current[key]
        if not isinstance(old_ticket, dict):
            changed.append({"key": key, "fields": {"ticket": {"before": old_ticket, "after": new_ticket}}})
            continue
        fields_changed = {
            field: {"before": old_ticket.get(field), "after": new_ticket.get(field)}
            for field in sorted(plan_fields)
            if old_ticket.get(field) != new_ticket.get(field)
        }
        # Older task snapshots included bot labels in planning_labels. Ignore
        # those during the one-time snapshot format transition.
        old_planning_labels = old_ticket.get("planning_labels")
        new_planning_labels = new_ticket.get("planning_labels")
        if isinstance(old_planning_labels, list) and isinstance(new_planning_labels, list):
            old_user_labels = sorted(
                label for label in old_planning_labels
                if label != implementation_label
            )
            new_user_labels = sorted(
                label for label in new_planning_labels
                if label != implementation_label
            )
            if old_user_labels == new_user_labels:
                fields_changed.pop("planning_labels", None)
        if fields_changed:
            changed.append({"key": key, "fields": fields_changed})
    return {
        "baseline_missing": False,
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def ticket_plan_digest(snapshot: dict[str, dict]) -> str:
    """Bind a published plan approval to the exact ticket state it described."""
    raw = json.dumps(snapshot, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def planner_ticket_snapshot(children: list[dict], team_field_id: str) -> dict[str, dict]:
    """Hash only ticket scope inputs; planner-assigned phase/repo labels are outputs."""
    snapshot = {}
    for issue in sorted(children, key=key_of):
        blockers, related = linked_issues(issue)
        priority = value(issue, "priority") or {}
        issue_type = value(issue, "issuetype") or value(issue, "issue_type") or {}
        snapshot[key_of(issue)] = {
            "summary": str(value(issue, "summary") or "").strip(),
            "description": str(value(issue, "description") or ""),
            "priority": str(priority.get("name") or priority.get("id") or "") if isinstance(priority, dict) else str(priority),
            "issue_type": str(issue_type.get("name") or "") if isinstance(issue_type, dict) else str(issue_type),
            "team_id": team_id(issue, team_field_id),
            "blockers": sorted(blockers),
            "related": sorted(related),
        }
    return snapshot


def current_planner_plan(
    control: dict, children: list[dict], team_field_id: str, repository_names: set[str],
) -> dict:
    """Return the newest planner assignment block and whether it covers current tickets."""
    expected_tickets = {key_of(item) for item in children}
    current_hash = ticket_plan_digest(planner_ticket_snapshot(children, team_field_id))
    for comment in reversed(comments(control)):
        if not planner_receipt(comment):
            continue
        proposal = planner_coordination(comment)
        if proposal is None:
            return {
                "comment_id": str(comment.get("id") or ""),
                "assignments": {},
                "current": False,
                "input_snapshot_hash": None,
                "current_snapshot_hash": current_hash,
            }
        assignments = proposal.get("ticket_assignments")
        valid = (
            isinstance(assignments, dict)
            and set(assignments) == expected_tickets
            and proposal.get("input_snapshot_hash") == current_hash
        )
        if valid:
            for assignment in assignments.values():
                if (not isinstance(assignment, dict)
                        or not PHASE_RE.fullmatch(str(assignment.get("phase") or ""))
                        or assignment.get("repository") not in repository_names):
                    valid = False
                    break
        return {
            "comment_id": str(comment.get("id") or ""),
            "assignments": assignments if isinstance(assignments, dict) else {},
            "current": bool(valid),
            "input_snapshot_hash": proposal.get("input_snapshot_hash"),
            "current_snapshot_hash": current_hash,
        }
    return {
        "comment_id": None,
        "assignments": {},
        "current": False,
        "input_snapshot_hash": None,
        "current_snapshot_hash": current_hash,
    }


def evaluate(control: dict, children: list[dict], repo_names: set[str], implementation_label: str) -> dict:
    """Mechanical plan checks. The agent still judges requirement quality."""
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

    tickets = [summarize_child(issue) for issue in children]
    for ticket, issue in zip(tickets, children):
        ticket["implementation_labeled"] = implementation_label in labels(issue)
    by_key = {ticket["key"]: ticket for ticket in tickets}
    phases: dict[str, list[dict]] = defaultdict(list)
    for ticket in tickets:
        phase_labels = ticket["phase_labels"]
        if not phase_labels:
            gaps.append(f"{ticket['key']} has no phase label")
        if len(phase_labels) > 1:
            gaps.append(f"{ticket['key']} has multiple phase labels")
        if len(phase_labels) == 1:
            phases[phase_labels[0]].append(ticket)

    if not phases.get("phase:01"):
        gaps.append("No same-Team implementation ticket has phase:01")
    blockers_by_key = {ticket["key"]: set(ticket["blockers"]) for ticket in tickets}
    if cyclic_dependencies(blockers_by_key):
        gaps.append("Implementation ticket dependency links form a cycle")

    incomplete_phases = sorted(
        phase for phase, phase_tickets in phases.items() if any(ticket["status"].lower() not in COMPLETE_STATUSES for ticket in phase_tickets)
    )
    current_phase = incomplete_phases[0] if incomplete_phases else None
    ready: list[dict] = []
    waiting: list[str] = []
    blocked_tickets: list[dict] = []
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
            blockers: list[str] = []
            if child_repos and (child_repos - allowed_repos or child_repos - repo_names):
                blockers.append("repo label is outside the control ticket's allowlist")
            if ticket["status"].lower() not in START_STATUSES:
                blockers.append(f"status {ticket['status']} is not not-started")
            if ticket["assignee"]:
                blockers.append("ticket is not unassigned")
            if blockers:
                blocked_tickets.append({"key": ticket["key"], "reasons": blockers})
                continue
            ready.append({"key": ticket["key"], "repo_candidates": sorted(child_repos or allowed_repos)})

    return {
        "state": state_labels[0] if len(state_labels) == 1 else None,
        "allowed_repos": sorted(allowed_repos),
        "current_phase": current_phase,
        "gaps": gaps,
        "ready": ready if not gaps else [],
        "waiting": waiting,
        "blocked_tickets": blocked_tickets,
        "tickets": tickets,
    }


def fingerprint(parent: dict, control: dict, children: list[dict], team_field_id: str, pull_requests: dict | None = None) -> str:
    """Ignore planner traffic, coordinator reports, and coordinator state labels."""
    human_comments = [
        {"id": str(comment.get("id") or ""), "body": comment_body(comment)}
        for comment in comments(control)
        if is_coordination_feedback(comment)
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
            "latest_planner_receipt": next(
                (planner_receipt(comment) for comment in reversed(comments(control)) if planner_receipt(comment)),
                None,
            ),
        },
        "children": [
            {
                "key": key_of(child), "summary": value(child, "summary"), "description": value(child, "description"),
                "updated": value(child, "updated"), "status": status_name(child), "team": team_id(child, team_field_id),
                "priority": value(child, "priority"),
                "issue_type": value(child, "issuetype") or value(child, "issue_type"),
                "labels": sorted(labels(child)), "links": value(child, "issuelinks") or [],
                "assignee": is_assigned(value(child, "assignee")),
            }
            for child in sorted(children, key=key_of)
        ],
        "open_pull_requests": pull_requests or {},
    }
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def main() -> None:
    try:
        project = fabric_setting("JIRA_PROJECT")
        team_field_id = fabric_setting("TEAM_FIELD_ID")
    except ValueError as exc:
        # A missing sandbox delivery is a blocker, not a fallback case.
        output_result("error", str(exc))
        return
    project = project or "RHCLOUD"
    control_label = os.environ.get("BOT_LABEL", "")
    implementation_label = os.environ.get("BOT_IMPLEMENTATION_LABEL", "")
    required = {
        "BOT_LABEL": control_label,
        "BOT_TEAM_FIELD_ID/FABRIC_TEAM_FIELD_ID": team_field_id,
        "BOT_IMPLEMENTATION_LABEL": implementation_label,
    }
    missing = [name for name, item in required.items() if not item]
    if missing:
        output_result("error", f"Missing configuration: {', '.join(missing)}")
        return
    if not all(JQL_VALUE_RE.fullmatch(item) for item in (project, control_label, implementation_label)):
        output_result("error", "Invalid Jira project or label configuration")
        return
    if not re.fullmatch(r"customfield_\d+", team_field_id):
        output_result("error", "BOT_TEAM_FIELD_ID must be a customfield_ number")
        return

    try:
        candidates = search_all(f'project = "{project}" AND labels = "{control_label}" ORDER BY key ASC', "key,labels,parent")
        controls = []
        for candidate in candidates:
            control = get_issue(key_of(candidate))
            planner = pending_planner_request(control, control_label)
            if planner:
                output_result("start", json.dumps({"planner": planner}))
                return
            controls.append(control)
        tasks = load_tasks()
        repo_names = set(load_project_repos())
        actionable = []
        for control in controls:
            control_key = key_of(control)
            control_team_id = team_id(control, team_field_id)
            parent = parent_key(control)
            if not parent:
                task = tasks.get(control_key) or {}
                digest = fingerprint({}, control, [], team_field_id)
                if (task.get("metadata") or {}).get("snapshot_hash") != digest:
                    actionable.append({
                        "control_key": control_key,
                        "team_id": control_team_id,
                        "team_field_id": team_field_id,
                        "snapshot_hash": digest,
                        "error": "Control ticket has no parent issue",
                    })
                continue
            parent_issue = get_issue(parent)
            siblings = search_all(f'parent = "{parent}" ORDER BY key ASC')
            children = implementation_children(siblings, control, team_field_id)
            planner_plan = current_planner_plan(control, children, team_field_id, repo_names)
            other_controls = [issue for issue in children if control_label in labels(issue)]
            assessment = evaluate(control, children, repo_names, implementation_label)
            if not control_team_id:
                assessment["gaps"].append("Control ticket has no Jira Team ID")
                assessment["ready"] = []
            if other_controls:
                assessment["gaps"].append("Parent has multiple same-Team control tickets")
                assessment["ready"] = []
            pull_requests, github_pr_errors = open_pr_snapshot(children, implementation_label, load_project_repos())
            digest = fingerprint(parent_issue, control, children, team_field_id, {
                "tickets": pull_requests,
                "errors": github_pr_errors,
            })
            task = tasks.get(control_key) or {}
            metadata = task.get("metadata") or {}
            old_digest = metadata.get("snapshot_hash")
            plan_snapshot = ticket_plan_snapshot(children, team_field_id, implementation_label)
            plan_snapshot_hash = ticket_plan_digest(plan_snapshot)
            ticket_changes = ticket_plan_changes(
                metadata.get("plan_snapshot"), plan_snapshot, implementation_label
            )
            plan_approval_pending = bool(metadata.get("pending_plan_approval"))
            approved_plan_snapshot_hash = metadata.get("approved_plan_snapshot_hash")
            legacy_snapshot_migrated = False
            previous_plan_snapshot = metadata.get("plan_snapshot")
            has_ticket_delta = any(
                ticket_changes[key] for key in ("added", "removed", "changed")
            )
            # Existing tasks hashed runtime fields too. If that legacy hash
            # still matches the saved snapshot and its plan inputs are intact,
            # migrate it without making users reapprove an unchanged plan.
            if (
                not plan_approval_pending
                and isinstance(previous_plan_snapshot, dict)
                and approved_plan_snapshot_hash == ticket_plan_digest(previous_plan_snapshot)
                and not has_ticket_delta
            ):
                approved_plan_snapshot_hash = plan_snapshot_hash
                legacy_snapshot_migrated = True
            approved_snapshot_changed = (
                isinstance(approved_plan_snapshot_hash, str)
                and approved_plan_snapshot_hash != plan_snapshot_hash
            )
            if not old_digest or old_digest != digest or not assessment["state"] or github_pr_errors:
                actionable.append({
                    "control_key": control_key,
                    "parent_key": parent,
                    "team_id": control_team_id,
                    "team_field_id": team_field_id,
                    "planner_plan": planner_plan,
                    "planner_plan_current": planner_plan["current"],
                    "snapshot_hash": digest,
                    "plan_snapshot": plan_snapshot,
                    "plan_snapshot_hash": plan_snapshot_hash,
                    "approved_plan_snapshot_hash": approved_plan_snapshot_hash,
                    "legacy_plan_snapshot_migrated": legacy_snapshot_migrated,
                    "ticket_changes": ticket_changes,
                    "approved_plan_snapshot_changed": approved_snapshot_changed,
                    "plan_change_required": (
                        has_ticket_delta
                        or approved_snapshot_changed
                        or plan_approval_pending
                    ),
                    "plan_approval_pending": plan_approval_pending,
                    "first_run": not bool(old_digest),
                    "new_comments": [
                        {"id": str(comment.get("id") or ""), "body": comment_body(comment), "created": comment.get("created")}
                        for comment in comments(control)
                        if is_coordination_feedback(comment)
                        and str(comment.get("id") or "") not in set((task.get("metadata") or {}).get("processed_comment_ids") or [])
                    ],
                    "open_pull_requests": pull_requests,
                    "github_pr_errors": github_pr_errors,
                    **assessment,
                })
        if actionable:
            output_result("start", json.dumps({"projects": actionable, "implementation_label": implementation_label}, default=str))
        else:
            output_result("skip", f"No changed {project} control tickets")
    except Exception as error:
        output_result("error", str(error))
    finally:
        jira_cleanup()


if __name__ == "__main__":
    main()
