#!/usr/bin/env python3
"""Validated Jira writes for the project coordinator.

Usage: python3 actions.py --request-file /tmp/coordinator-request.json
Each operation re-reads Jira. The preflight snapshot is advisory, never write
authorization. Outputs one JSON object; failures exit nonzero.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / ".claude" / "skills"))
sys.path.insert(0, str(Path.cwd() / "presets" / "shared" / "preflight"))

from jira_mcp import jira_call, jira_cleanup  # noqa: E402
from project_coordinator import (  # noqa: E402
    COORD_STATES,
    JQL_VALUE_RE,
    OWN_COMMENT_PREFIX,
    PHASE_RE,
    comments,
    comment_body,
    cyclic_dependencies,
    evaluate,
    get_issue,
    key_of,
    labels,
    load_project_repos,
    parent_key,
    search_all,
    is_complete,
    linked_issues,
    team_id,
    value,
)

STATES = COORD_STATES
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
DEPENDENCY_REQUEST_RE = re.compile(
    r"^\s*/coord\s+dependency\s+([A-Z][A-Z0-9]+-\d+)\s+blocks\s+([A-Z][A-Z0-9]+-\d+)\s*$",
    re.IGNORECASE,
)


def require_config() -> tuple[str, str, str, str]:
    control_label = os.environ.get("BOT_LABEL", "")
    team_field_id = os.environ.get("BOT_TEAM_FIELD_ID", "")
    expected_team = os.environ.get("BOT_TEAM_ID", "")
    implementation_label = os.environ.get("BOT_IMPLEMENTATION_LABEL", "")
    if not all((control_label, team_field_id, expected_team, implementation_label)):
        raise ValueError("BOT_LABEL, BOT_TEAM_FIELD_ID, BOT_TEAM_ID, and BOT_IMPLEMENTATION_LABEL are required")
    if not re.fullmatch(r"customfield_\d+", team_field_id):
        raise ValueError("BOT_TEAM_FIELD_ID must be a customfield_ number")
    if not all(JQL_VALUE_RE.fullmatch(item) for item in (control_label, expected_team, implementation_label)):
        raise ValueError("Invalid label or Team ID configuration")
    return control_label, team_field_id, expected_team, implementation_label


def checked_control(
    control_key: str,
    config: tuple[str, str, str, str],
    *,
    require_parent: bool = True,
    require_team: bool = True,
) -> dict:
    if not ISSUE_KEY_RE.fullmatch(control_key):
        raise ValueError("Invalid control issue key")
    control = get_issue(control_key)
    control_label, team_field_id, expected_team, _ = config
    if control_label not in labels(control):
        raise ValueError("Control marker is missing")
    actual_team = team_id(control, team_field_id)
    if actual_team != expected_team and (require_team or actual_team):
        raise ValueError("Control Team ID differs from configured Team ID")
    if require_parent and not parent_key(control):
        raise ValueError("Control ticket has no parent")
    return control


def checked_child(control: dict, ticket_key: str, team_field_id: str, expected_team: str) -> dict:
    if not ISSUE_KEY_RE.fullmatch(ticket_key):
        raise ValueError("Invalid child issue key")
    child = get_issue(ticket_key)
    if parent_key(child) != parent_key(control):
        raise ValueError("Ticket is not a direct child of the control ticket's parent")
    if team_id(child, team_field_id) != expected_team:
        raise ValueError("Ticket Team ID differs from control Team ID")
    if ticket_key == key_of(control):
        raise ValueError("Control ticket cannot be an implementation ticket")
    return child


def call_write(tool: str, arguments: dict) -> dict:
    result = jira_call(tool, arguments)
    if not isinstance(result, dict) or result.get("isError") or result.get("success") is False:
        raise RuntimeError(f"{tool} failed: {result}")
    return result


def set_labels(issue_key: str, new_labels: list[str]) -> None:
    call_write("jira_update_issue", {"issue_key": issue_key, "fields": json.dumps({"labels": sorted(set(new_labels))})})
    actual = set(labels(get_issue(issue_key)))
    if actual != set(new_labels):
        raise RuntimeError(f"Label update not verified for {issue_key}")


def set_state(request: dict, config: tuple[str, str, str, str]) -> dict:
    state = request.get("state")
    if state not in STATES:
        raise ValueError("Unknown coordinator state")
    strict = state in {
        "dev-bot-coordinator:active",
        "dev-bot-coordinator:complete",
    }
    control = checked_control(request["control_key"], config, require_parent=strict, require_team=strict)
    if state == "dev-bot-coordinator:active":
        _, assessment = fresh_assessment(control, config)
        if assessment["gaps"]:
            raise ValueError("Plan gate failed: " + "; ".join(assessment["gaps"]))
    if state == "dev-bot-coordinator:complete":
        comment_id = str(request.get("request_comment_id") or "")
        if not comment_id:
            raise ValueError("Completion requires an explicit control comment ID")
        request_comment(control, comment_id, r"^\s*/coord\s+complete\s+project\s*$")
        children, assessment = fresh_assessment(control, config)
        if assessment["gaps"] or any(not is_complete(child) for child in children):
            raise ValueError("In-scope implementation tickets are not all complete")
    current = labels(control)
    updated = [label for label in current if label not in STATES]
    updated.append(state)
    if set(updated) != set(current):
        set_labels(key_of(control), updated)
    return {"control_key": key_of(control), "state": state}


def fresh_assessment(control: dict, config: tuple[str, str, str, str]) -> tuple[list[dict], dict]:
    _, team_field_id, expected_team, implementation_label = config
    parent = parent_key(control)
    siblings = search_all(f'parent = "{parent}" ORDER BY key ASC')
    children = [issue for issue in siblings if key_of(issue) != key_of(control) and team_id(issue, team_field_id) == expected_team]
    other_controls = [issue for issue in children if os.environ["BOT_LABEL"] in labels(issue)]
    assessment = evaluate(
        control,
        children,
        set(load_project_repos()),
        implementation_label,
        os.environ.get("BOT_INCLUDE_BACKLOG", "").lower() in {"1", "true", "yes"},
    )
    if other_controls:
        assessment["gaps"].append("Parent has multiple same-Team control tickets")
        assessment["ready"] = []
    return children, assessment


def handoff(request: dict, config: tuple[str, str, str, str]) -> dict:
    control = checked_control(request["control_key"], config)
    _, team_field_id, expected_team, implementation_label = config
    children, assessment = fresh_assessment(control, config)
    if assessment["gaps"]:
        raise ValueError("Plan gate failed: " + "; ".join(assessment["gaps"]))
    ticket_key = request["ticket_key"]
    ready = {item["key"]: item for item in assessment["ready"]}
    if ticket_key not in ready:
        raise ValueError("Ticket is not an unblocked runnable ticket")
    child = checked_child(control, ticket_key, team_field_id, expected_team)
    if not any(PHASE_RE.fullmatch(label) for label in labels(child)):
        raise ValueError("Implementation ticket has no phase label")
    selected_repos = request.get("repos") or []
    if isinstance(selected_repos, str):
        selected_repos = [selected_repos]
    allowed = set(assessment["allowed_repos"])
    if not selected_repos or set(selected_repos) - allowed or set(selected_repos) - set(load_project_repos()):
        raise ValueError("Selected repos must be configured and allowed on the control ticket")
    existing_repos = {label[5:] for label in labels(child) if label.startswith("repo:")}
    if existing_repos and existing_repos != set(selected_repos):
        raise ValueError("Existing ticket repo labels differ from selected repos")
    updated = labels(child) + [f"repo:{name}" for name in selected_repos] + [implementation_label]
    set_labels(ticket_key, updated)
    return {"ticket_key": ticket_key, "released_repos": sorted(set(selected_repos)), "implementation_label": implementation_label}


def link_dependency(request: dict, config: tuple[str, str, str, str]) -> dict:
    control = checked_control(request["control_key"], config)
    _, team_field_id, expected_team, _ = config
    comment_id = str(request.get("request_comment_id") or "")
    if not comment_id:
        raise ValueError("request_comment_id is required")
    command = request_comment(control, comment_id, DEPENDENCY_REQUEST_RE.pattern)
    match = DEPENDENCY_REQUEST_RE.fullmatch(comment_body(command))
    if not match:
        raise ValueError("Dependency request must be '/coord dependency KEY blocks KEY'")

    blocker_key, blocked_key = (key.upper() for key in match.groups())
    requested_blocker = str(request.get("blocking_ticket_key") or "").upper()
    requested_blocked = str(request.get("blocked_ticket_key") or "").upper()
    if (requested_blocker, requested_blocked) != (blocker_key, blocked_key):
        raise ValueError("Dependency action does not match the user's comment")
    if blocker_key == blocked_key:
        raise ValueError("A ticket cannot block itself")

    blocker = checked_child(control, blocker_key, team_field_id, expected_team)
    blocked = checked_child(control, blocked_key, team_field_id, expected_team)
    control_label = config[0]
    if control_label in labels(blocker) or control_label in labels(blocked):
        raise ValueError("Dependency endpoints must be implementation tickets")

    children, _ = fresh_assessment(control, config)
    children_by_key = {key_of(child): child for child in children}
    if blocker_key not in children_by_key or blocked_key not in children_by_key:
        raise ValueError("Dependency endpoints must be same-Team project tickets")
    blockers_by_key = {
        key: linked_issues(child)[0]
        for key, child in children_by_key.items()
    }
    if blocker_key in blockers_by_key[blocked_key]:
        return {
            "blocking_ticket_key": blocker_key,
            "blocked_ticket_key": blocked_key,
            "created": False,
        }

    blockers_by_key[blocked_key].add(blocker_key)
    if cyclic_dependencies(blockers_by_key):
        raise ValueError("Dependency would create a cycle")

    # For Jira's Blocks type, the blocked issue is inward and its blocker is outward.
    call_write(
        "jira_create_issue_link",
        {
            "link_type": "Blocks",
            "inward_issue_key": blocked_key,
            "outward_issue_key": blocker_key,
        },
    )
    verified_blockers, _ = linked_issues(get_issue(blocked_key))
    if blocker_key not in verified_blockers:
        raise RuntimeError(f"Jira did not confirm {blocker_key} blocks {blocked_key}")
    return {
        "blocking_ticket_key": blocker_key,
        "blocked_ticket_key": blocked_key,
        "created": True,
    }


def request_comment(control: dict, comment_id: str, command_pattern: str) -> dict:
    matches = [item for item in comments(control) if str(item.get("id") or "") == comment_id]
    if len(matches) != 1:
        raise ValueError("Request comment not found or ambiguous")
    item = matches[0]
    body = comment_body(item)
    if body.startswith(OWN_COMMENT_PREFIX) or not re.search(command_pattern, body, re.IGNORECASE):
        raise ValueError("Comment is not an explicit user request")
    return item


def creation_fields(control: dict, team_field_id: str, extra_labels: list[str]) -> tuple[str, dict]:
    parent = get_issue(parent_key(control))
    issue_type = value(parent, "issuetype") or value(parent, "issue_type") or {}
    if isinstance(issue_type, dict):
        type_name = str(issue_type.get("name") or "").lower()
    else:
        type_name = str(issue_type).lower()
    if type_name == "epic":
        child_type, hierarchy = "Story", {"epicKey": key_of(parent)}
    elif type_name in {"story", "task", "bug"}:
        child_type, hierarchy = "Sub-task", {"parent": key_of(parent)}
    else:
        raise ValueError("Unsupported parent issue type for child creation")
    team_value = team_id(control, team_field_id)
    if not team_value:
        raise ValueError("Control ticket has no Team field value")
    if child_type == "Sub-task":
        if team_id(parent, team_field_id) != team_value:
            raise ValueError("Sub-task inherits Team from parent, which differs from control Team")
        return child_type, {**hierarchy, "labels": extra_labels}
    return child_type, {**hierarchy, team_field_id: team_value, "labels": extra_labels}


def create_ticket(request: dict, config: tuple[str, str, str, str]) -> dict:
    control = checked_control(request["control_key"], config)
    _, team_field_id, expected_team, implementation_label = config
    comment_id = str(request.get("request_comment_id") or "")
    if not comment_id:
        raise ValueError("request_comment_id is required")
    command = request_comment(control, comment_id, r"^\s*/coord\s+create\s+P-\d+\b")
    summary = str(request.get("summary") or "").strip()
    description = str(request.get("description") or "").strip()
    if not summary or not description:
        raise ValueError("Summary and description are required")
    proposal_id = str(request.get("proposal_id") or "")
    if not re.fullmatch(r"P-\d+", proposal_id) or not re.search(rf"^\s*/coord\s+create\s+{proposal_id}\b", comment_body(command), re.IGNORECASE):
        raise ValueError("Creation request must name an existing proposal ID")
    if not any(
        item_body.startswith(OWN_COMMENT_PREFIX) and proposal_id in item_body
        for item_body in (comment_body(item) for item in comments(control))
    ):
        raise ValueError("Proposal ID was not found in a coordinator comment")
    phase = str(request.get("phase") or "")
    if not re.fullmatch(r"\d{2}", phase):
        raise ValueError("Implementation ticket phase must be two digits")
    issue_labels = [f"phase:{phase}"]
    marker = f"Coordinator request: {key_of(control)}#{comment_id}"
    siblings = search_all(f'parent = "{parent_key(control)}" ORDER BY key ASC')
    existing = [issue for issue in siblings if marker in str(value(issue, "description") or "")]
    if existing:
        return {"ticket_key": key_of(existing[0]), "created": False, "request_comment_id": comment_id}
    child_type, additional = creation_fields(control, team_field_id, issue_labels)
    if implementation_label in additional["labels"]:
        raise ValueError("Proposed ticket cannot have the implementation bot label")
    project_key = key_of(control).split("-", 1)[0]
    result = call_write(
        "jira_create_issue",
        {
            "project_key": project_key,
            "summary": summary,
            "issue_type": child_type,
            "description": f"{description}\n\n{marker}",
            "additional_fields": json.dumps(additional),
        },
    )
    ticket_key = result.get("key") or key_of(result.get("issue"))
    if not ticket_key:
        raise RuntimeError("Jira create returned no issue key; inspect parent before retry")
    child = checked_child(control, ticket_key, team_field_id, expected_team)
    if implementation_label in labels(child):
        raise RuntimeError("Created ticket unexpectedly has implementation bot label")
    return {"ticket_key": ticket_key, "created": True, "request_comment_id": comment_id}


def comment_control(request: dict, config: tuple[str, str, str, str]) -> dict:
    control = checked_control(request["control_key"], config, require_parent=False, require_team=False)
    action_id = str(request.get("action_id") or "")
    body = str(request.get("body") or "").strip()
    if not action_id or not body:
        raise ValueError("action_id and body are required")
    marker = f"Coordinator action: {action_id}"
    if any(marker in comment_body(item) for item in comments(control)):
        return {"control_key": key_of(control), "commented": False, "action_id": action_id}
    call_write("jira_add_comment", {"issue_key": key_of(control), "comment": f"{OWN_COMMENT_PREFIX}\n\n{body}\n\n{marker}"})
    return {"control_key": key_of(control), "commented": True, "action_id": action_id}


def dispatch(request: dict) -> dict:
    config = require_config()
    action = request.get("action")
    if action == "set-state":
        return set_state(request, config)
    if action == "handoff":
        return handoff(request, config)
    if action == "link-dependency":
        return link_dependency(request, config)
    if action == "create-ticket":
        return create_ticket(request, config)
    if action == "comment":
        return comment_control(request, config)
    raise ValueError("Unknown coordinator action")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] != "--request-file":
        raise ValueError("Pass --request-file PATH")
    request = json.loads(Path(sys.argv[2]).read_text())
    if not isinstance(request, dict):
        raise ValueError("Request must be a JSON object")
    print(json.dumps(dispatch(request)))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        raise SystemExit(1) from error
    finally:
        jira_cleanup()
