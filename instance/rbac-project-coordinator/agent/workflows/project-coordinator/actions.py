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
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path.cwd() / ".claude" / "skills"))
sys.path.insert(0, str(Path.cwd() / "presets" / "shared" / "preflight"))

from jira_mcp import jira_call, jira_cleanup  # noqa: E402
from project_coordinator import (  # noqa: E402
    COORD_STATES,
    JQL_VALUE_RE,
    OWN_COMMENT_PREFIX,
    PHASE_RE,
    START_STATUSES,
    comments,
    comment_body,
    cyclic_dependencies,
    evaluate,
    get_issue,
    is_own_comment,
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
LABEL_RE = re.compile(r"^[A-Za-z0-9:_-]+$")
DEPENDENCY_REQUEST_RE = re.compile(
    r"^\s*/coord\s+dependency\s+([A-Z][A-Z0-9]+-\d+)\s+blocks\s+([A-Z][A-Z0-9]+-\d+)\s*$",
    re.IGNORECASE,
)
SETUP_PROPOSAL_RE = re.compile(r"```(?:[A-Za-z0-9_-]+)?\s*(.*?)\s*```", re.DOTALL)
SETUP_APPROVAL_RE = re.compile(r"\b(?:approve|approved)\b", re.IGNORECASE)


def require_config() -> tuple[str, str, str]:
    control_label = os.environ.get("BOT_LABEL", "")
    team_field_id = os.environ.get("BOT_TEAM_FIELD_ID", "")
    implementation_label = os.environ.get("BOT_IMPLEMENTATION_LABEL", "")
    if not all((control_label, team_field_id, implementation_label)):
        raise ValueError("BOT_LABEL, BOT_TEAM_FIELD_ID, and BOT_IMPLEMENTATION_LABEL are required")
    if not re.fullmatch(r"customfield_\d+", team_field_id):
        raise ValueError("BOT_TEAM_FIELD_ID must be a customfield_ number")
    if not all(JQL_VALUE_RE.fullmatch(item) for item in (control_label, implementation_label)):
        raise ValueError("Invalid label configuration")
    return control_label, team_field_id, implementation_label


def checked_control(
    control_key: str,
    config: tuple[str, str, str],
    *,
    require_parent: bool = True,
    require_team: bool = True,
) -> dict:
    if not ISSUE_KEY_RE.fullmatch(control_key):
        raise ValueError("Invalid control issue key")
    control = get_issue(control_key)
    control_label, team_field_id, _ = config
    if control_label not in labels(control):
        raise ValueError("Control marker is missing")
    actual_team = team_id(control, team_field_id)
    if require_team and not actual_team:
        raise ValueError("Control ticket has no Jira Team ID")
    if require_parent and not parent_key(control):
        raise ValueError("Control ticket has no parent")
    return control


def checked_child(control: dict, ticket_key: str, team_field_id: str) -> dict:
    if not ISSUE_KEY_RE.fullmatch(ticket_key):
        raise ValueError("Invalid child issue key")
    child = get_issue(ticket_key)
    if parent_key(child) != parent_key(control):
        raise ValueError("Ticket is not a direct child of the control ticket's parent")
    control_team_id = team_id(control, team_field_id)
    if not control_team_id:
        raise ValueError("Control ticket has no Jira Team ID")
    if team_id(child, team_field_id) != control_team_id:
        raise ValueError("Ticket Team ID differs from control Team ID")
    if ticket_key == key_of(control):
        raise ValueError("Control ticket cannot be an implementation ticket")
    return child


def call_write(tool: str, arguments: dict) -> dict:
    result = jira_call(tool, arguments)
    if not isinstance(result, dict) or result.get("isError") or result.get("success") is False:
        raise RuntimeError(f"{tool} failed: {result}")
    return result


def review_context() -> dict:
    """Load the review bot's shared label settings from this config checkout."""
    config_path = Path.cwd() / "instance" / "rbac-config-review" / "agent" / "review-context.yaml"
    if not config_path.is_file():
        raise ValueError(f"Review context is unavailable: {config_path}")
    try:
        data = yaml.safe_load(config_path.read_text())
    except yaml.YAMLError as error:
        raise ValueError(f"Review context YAML is invalid: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("Review context must be a YAML mapping")
    review_label = data.get("review_label")
    if not isinstance(review_label, str) or not LABEL_RE.fullmatch(review_label):
        raise ValueError("Review context review_label must be a valid GitHub label")
    repositories = data.get("repositories")
    if not isinstance(repositories, dict):
        raise ValueError("Review context repositories must be a mapping")
    return data


def repository_handoff_labels(repo_names: list[str], implementation_label: str) -> list[str]:
    """Return configured Jira labels for the selected repositories."""
    repositories = review_context()["repositories"]
    configured: list[str] = []
    for repo_name in repo_names:
        repo_config = repositories.get(repo_name)
        repo_labels = repo_config.get("dev-bot-labels") if isinstance(repo_config, dict) else None
        if not isinstance(repo_labels, list) or not repo_labels:
            raise ValueError(f"No dev-bot-labels configured for repository {repo_name}")
        if any(not isinstance(label, str) or not LABEL_RE.fullmatch(label) for label in repo_labels):
            raise ValueError(f"Invalid dev-bot-labels configured for repository {repo_name}")
        if f"repo:{repo_name}" not in repo_labels:
            raise ValueError(f"Repository {repo_name} dev-bot-labels must include repo:{repo_name}")
        if implementation_label not in repo_labels:
            raise ValueError(f"Repository {repo_name} dev-bot-labels must include {implementation_label}")
        configured.extend(repo_labels)
    return sorted(set(configured))


def github_repo(repo_config: dict) -> str:
    """Resolve the upstream GitHub repository from project-repos.json."""
    url = repo_config.get("upstream") or repo_config.get("url")
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", str(url or ""))
    if not match:
        raise ValueError("Selected repository does not have a valid upstream GitHub URL")
    return f"{match.group(1)}/{match.group(2)}"


def run_gh(arguments: list[str]) -> str:
    result = subprocess.run(["gh", *arguments], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip()[:500] or f"gh {' '.join(arguments)} failed")
    return result.stdout


def set_labels(issue_key: str, new_labels: list[str]) -> None:
    call_write("jira_update_issue", {"issue_key": issue_key, "fields": json.dumps({"labels": sorted(set(new_labels))})})
    actual = set(labels(get_issue(issue_key)))
    if actual != set(new_labels):
        raise RuntimeError(f"Label update not verified for {issue_key}")


def set_state(request: dict, config: tuple[str, str, str]) -> dict:
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


def fresh_assessment(control: dict, config: tuple[str, str, str]) -> tuple[list[dict], dict]:
    _, team_field_id, implementation_label = config
    control_team_id = team_id(control, team_field_id)
    if not control_team_id:
        raise ValueError("Control ticket has no Jira Team ID")
    parent = parent_key(control)
    siblings = search_all(f'parent = "{parent}" ORDER BY key ASC')
    children = [
        issue
        for issue in siblings
        if key_of(issue) != key_of(control) and team_id(issue, team_field_id) == control_team_id
    ]
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


def handoff(request: dict, config: tuple[str, str, str]) -> dict:
    control = checked_control(request["control_key"], config)
    _, team_field_id, implementation_label = config
    children, assessment = fresh_assessment(control, config)
    if assessment["gaps"]:
        raise ValueError("Plan gate failed: " + "; ".join(assessment["gaps"]))
    ticket_key = request["ticket_key"]
    ready = {item["key"]: item for item in assessment["ready"]}
    if ticket_key not in ready:
        raise ValueError("Ticket is not an unblocked runnable ticket")
    child = checked_child(control, ticket_key, team_field_id)
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
    handoff_labels = repository_handoff_labels(selected_repos, implementation_label)
    updated = labels(child) + handoff_labels
    set_labels(ticket_key, updated)
    return {
        "ticket_key": ticket_key,
        "released_repos": sorted(set(selected_repos)),
        "released_labels": handoff_labels,
    }


def label_review_prs(request: dict, config: tuple[str, str, str]) -> dict:
    """Add the review queue label to open PRs whose title names this Jira ticket."""
    control = checked_control(request["control_key"], config)
    _, team_field_id, implementation_label = config
    ticket_key = request.get("ticket_key")
    child = checked_child(control, ticket_key, team_field_id)
    child_labels = labels(child)
    if implementation_label not in child_labels:
        raise ValueError("Ticket has not been released to the implementation bot")

    allowed_repos = {label[5:] for label in labels(control) if label.startswith("repo:")}
    selected_repos = {label[5:] for label in child_labels if label.startswith("repo:")}
    if not selected_repos or selected_repos - allowed_repos:
        raise ValueError("Ticket repo labels must be present on the control ticket allowlist")

    context = review_context()
    review_label = context["review_label"]
    project_repos = load_project_repos()
    labeled: list[dict] = []
    already_labeled: list[dict] = []
    for repo_name in sorted(selected_repos):
        repo_config = project_repos.get(repo_name)
        if not isinstance(repo_config, dict):
            raise ValueError(f"Repository {repo_name} is missing from project-repos.json")
        upstream = github_repo(repo_config)
        try:
            prs = json.loads(run_gh([
                "pr", "list", "--repo", upstream, "--state", "open", "--limit", "1000",
                "--json", "number,title,labels",
            ]))
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeError(f"Could not read open pull requests from {upstream}") from error
        if not isinstance(prs, list):
            raise RuntimeError(f"gh pr list returned an invalid list for {upstream}")
        title_marker = re.compile(rf"(?<![A-Za-z0-9])\[{re.escape(ticket_key)}\](?![A-Za-z0-9])", re.IGNORECASE)
        for pr in prs:
            if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
                continue
            if not title_marker.search(str(pr.get("title") or "")):
                continue
            current = pr.get("labels") or []
            current_names = {
                item.get("name") for item in current
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            entry = {"repo": upstream, "number": pr["number"], "url": f"https://github.com/{upstream}/pull/{pr['number']}"}
            if review_label in current_names:
                already_labeled.append(entry)
                continue
            run_gh([
                "pr", "edit", str(pr["number"]), "--repo", upstream,
                "--add-label", review_label,
            ])
            verified = json.loads(run_gh([
                "pr", "view", str(pr["number"]), "--repo", upstream, "--json", "labels",
            ]))
            verified_names = {
                item.get("name") for item in verified.get("labels", [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            } if isinstance(verified, dict) else set()
            if review_label not in verified_names:
                raise RuntimeError(f"Review label update was not verified for {upstream}#{pr['number']}")
            labeled.append(entry)
    return {
        "ticket_key": ticket_key,
        "review_label": review_label,
        "labeled": labeled,
        "already_labeled": already_labeled,
    }


def link_dependency(request: dict, config: tuple[str, str, str]) -> dict:
    control = checked_control(request["control_key"], config)
    _, team_field_id, _ = config
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

    blocker = checked_child(control, blocker_key, team_field_id)
    blocked = checked_child(control, blocked_key, team_field_id)
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
    if is_own_comment(item) or not re.search(command_pattern, body, re.IGNORECASE):
        raise ValueError("Comment is not an explicit user request")
    return item


def setup_proposal(control: dict, approval_comment_id: str) -> dict:
    """Read the latest coordinator setup proposal before the explicit approval."""
    items = comments(control)
    approval_indexes = [
        index for index, item in enumerate(items)
        if str(item.get("id") or "") == approval_comment_id
    ]
    if len(approval_indexes) != 1:
        raise ValueError("Setup approval comment not found or ambiguous")
    approval_index = approval_indexes[0]
    approved_comment = request_comment(
        control,
        approval_comment_id,
        SETUP_APPROVAL_RE.pattern,
    )
    if str(approved_comment.get("id") or "") != approval_comment_id:
        raise ValueError("Setup approval comment changed during validation")
    if any(
        not is_own_comment(item)
        for item in items[approval_index + 1:]
    ):
        raise ValueError("Setup approval is stale; review the latest user feedback and approve again")
    for proposal_index in range(approval_index - 1, -1, -1):
        item = items[proposal_index]
        body = comment_body(item)
        if not is_own_comment(item):
            continue
        match = SETUP_PROPOSAL_RE.search(body)
        if not match:
            continue
        try:
            proposal = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            raise ValueError("Coordinator setup proposal is invalid JSON") from error
        if not isinstance(proposal, dict):
            raise ValueError("Coordinator setup proposal must be an object")
        intervening_user_comments = [
            entry for entry in items[proposal_index + 1:approval_index]
            if not is_own_comment(entry)
        ]
        if intervening_user_comments:
            raise ValueError("Setup proposal is stale; refresh it after the latest user feedback")
        return proposal
    raise ValueError("No coordinator setup proposal exists before this approval")


def approve_setup(request: dict, config: tuple[str, str, str]) -> dict:
    """Apply the latest proposed repo, phase, and root-link setup after approval."""
    control = checked_control(request["control_key"], config)
    _, team_field_id, implementation_label = config
    comment_id = str(request.get("request_comment_id") or "")
    if not comment_id:
        raise ValueError("request_comment_id is required")
    proposal = setup_proposal(control, comment_id)

    repo_name = proposal.get("repository")
    repo_names = set(load_project_repos())
    if not isinstance(repo_name, str) or repo_name not in repo_names:
        raise ValueError("Setup proposal repository is not configured")
    repository_handoff_labels([repo_name], implementation_label)

    assignments = proposal.get("phase_assignments")
    roots = proposal.get("root_tickets")
    if not isinstance(assignments, dict) or not isinstance(roots, list):
        raise ValueError("Setup proposal must include phase_assignments and root_tickets")
    phase_by_ticket: dict[str, str] = {}
    for phase, ticket_keys in assignments.items():
        if not isinstance(phase, str) or not PHASE_RE.fullmatch(phase) or not isinstance(ticket_keys, list):
            raise ValueError("Setup proposal has invalid phase assignments")
        for ticket_key in ticket_keys:
            if not isinstance(ticket_key, str) or not ISSUE_KEY_RE.fullmatch(ticket_key):
                raise ValueError("Setup proposal has an invalid implementation ticket key")
            if ticket_key in phase_by_ticket:
                raise ValueError(f"{ticket_key} appears in more than one proposed phase")
            phase_by_ticket[ticket_key] = phase
    if not phase_by_ticket or "phase:01" not in assignments:
        raise ValueError("Setup proposal must assign at least one phase:01 ticket")
    if any(not isinstance(ticket_key, str) or not ISSUE_KEY_RE.fullmatch(ticket_key) for ticket_key in roots):
        raise ValueError("Setup proposal has an invalid root ticket key")
    root_keys = set(roots)
    if len(root_keys) != len(roots):
        raise ValueError("Setup proposal has duplicate root ticket keys")

    parent = parent_key(control)
    siblings = search_all(f'parent = "{parent}" ORDER BY key ASC')
    control_team = team_id(control, team_field_id)
    children = [
        item for item in siblings
        if key_of(item) != key_of(control) and team_id(item, team_field_id) == control_team
    ]
    control_label = config[0]
    if any(control_label in labels(item) for item in children):
        raise ValueError("Parent has multiple same-Team control tickets")
    child_keys = {key_of(item) for item in children}
    if set(phase_by_ticket) != child_keys:
        missing = child_keys - set(phase_by_ticket)
        unknown = set(phase_by_ticket) - child_keys
        raise ValueError(
            "Setup proposal must assign every same-Team implementation ticket"
            + (f"; missing {', '.join(sorted(missing))}" if missing else "")
            + (f"; out of scope {', '.join(sorted(unknown))}" if unknown else "")
        )
    children_by_key = {key_of(item): item for item in children}
    blockers_by_key = {
        key: linked_issues(item)[0]
        for key, item in children_by_key.items()
    }
    expected_roots = {
        key for key, blockers in blockers_by_key.items()
        if not blockers.intersection(child_keys)
    }
    if root_keys != expected_roots:
        raise ValueError("Setup proposal roots do not match the current same-Team dependency graph")

    # Re-read every ticket and validate before performing any Jira writes.
    fresh_children = {key: checked_child(control, key, team_field_id) for key in sorted(child_keys)}
    current_control_labels = labels(control)
    if any(PHASE_RE.fullmatch(label) for label in current_control_labels):
        raise ValueError("Phase labels belong on implementation tickets, not the control ticket")
    current_states = set(current_control_labels) & COORD_STATES
    if "dev-bot-coordinator:active" in current_states or "dev-bot-coordinator:complete" in current_states:
        raise ValueError("Setup approval cannot change an active or complete project plan")

    phase_updates: dict[str, list[str]] = {}
    for ticket_key, phase in phase_by_ticket.items():
        child = fresh_children[ticket_key]
        existing = labels(child)
        existing_phases = {label for label in existing if PHASE_RE.fullmatch(label)}
        if existing_phases - {phase}:
            status = value(child, "status") or {}
            status = str(status.get("name", "") if isinstance(status, dict) else status).lower()
            if implementation_label in existing or status not in START_STATUSES:
                raise ValueError(f"Cannot rephase handed-off or started ticket {ticket_key}")
        updated = [label for label in existing if not PHASE_RE.fullmatch(label)] + [phase]
        phase_updates[ticket_key] = updated

    missing_root_links = [
        key for key in sorted(root_keys)
        if key_of(control) not in linked_issues(fresh_children[key])[1]
    ]
    control_repo_label = f"repo:{repo_name}"
    if control_repo_label not in current_control_labels:
        set_labels(key_of(control), current_control_labels + [control_repo_label])
    updated_phases: list[str] = []
    for ticket_key, updated in sorted(phase_updates.items()):
        if set(updated) != set(labels(fresh_children[ticket_key])):
            set_labels(ticket_key, updated)
            updated_phases.append(ticket_key)
    linked_roots: list[str] = []
    for ticket_key in missing_root_links:
        call_write(
            "jira_create_issue_link",
            {
                "link_type": "Relates",
                "inward_issue_key": ticket_key,
                "outward_issue_key": key_of(control),
            },
        )
        if key_of(control) not in linked_issues(get_issue(ticket_key))[1]:
            raise RuntimeError(f"Jira did not confirm relates-to link for {ticket_key}")
        linked_roots.append(ticket_key)
    return {
        "control_key": key_of(control),
        "repository": repo_name,
        "control_repo_label_added": control_repo_label not in current_control_labels,
        "phase_labels_updated": updated_phases,
        "root_links_added": linked_roots,
        "approval_comment_id": comment_id,
    }


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


def create_ticket(request: dict, config: tuple[str, str, str]) -> dict:
    control = checked_control(request["control_key"], config)
    _, team_field_id, implementation_label = config
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
        is_own_comment(item) and proposal_id in comment_body(item)
        for item in comments(control)
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
    child = checked_child(control, ticket_key, team_field_id)
    if implementation_label in labels(child):
        raise RuntimeError("Created ticket unexpectedly has implementation bot label")
    return {"ticket_key": ticket_key, "created": True, "request_comment_id": comment_id}


def comment_control(request: dict, config: tuple[str, str, str]) -> dict:
    control = checked_control(request["control_key"], config, require_parent=False, require_team=False)
    action_id = str(request.get("action_id") or "")
    body = str(request.get("body") or "").strip()
    next_steps = request.get("next_steps")
    if not action_id or not body:
        raise ValueError("action_id and body are required")
    if not isinstance(next_steps, list) or not 1 <= len(next_steps) <= 5:
        raise ValueError("next_steps must contain between one and five suggestions")
    if any(
        not isinstance(step, str)
        or not step.strip()
        or len(step.strip()) > 280
        or "\n" in step
        or "\r" in step
        for step in next_steps
    ):
        raise ValueError("Each next step must be a non-empty single-line suggestion of at most 280 characters")
    if re.search(r"(?im)^#{1,6}\s+possible next steps\s*$", body):
        raise ValueError("Pass next_steps separately; the action adds the footer")
    marker = f"Coordinator action: {action_id}"
    if any(marker in comment_body(item) for item in comments(control)):
        return {"control_key": key_of(control), "commented": False, "action_id": action_id}
    footer = "### Possible next steps\n" + "\n".join(f"- {step.strip()}" for step in next_steps)
    comment = f"{OWN_COMMENT_PREFIX}\n\n{body}\n\n{footer}\n\n{marker}"
    call_write("jira_add_comment", {"issue_key": key_of(control), "comment": comment})
    return {"control_key": key_of(control), "commented": True, "action_id": action_id}


def dispatch(request: dict) -> dict:
    config = require_config()
    action = request.get("action")
    if action == "set-state":
        return set_state(request, config)
    if action == "approve-setup":
        return approve_setup(request, config)
    if action == "handoff":
        return handoff(request, config)
    if action == "label-review-prs":
        return label_review_prs(request, config)
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
