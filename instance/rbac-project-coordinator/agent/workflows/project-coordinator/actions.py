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
    evaluate,
    fabric_setting,
    get_issue,
    implementation_children,
    is_coordination_feedback,
    is_own_comment,
    key_of,
    labels,
    load_project_repos,
    parent_key,
    current_planner_plan,
    search_all,
    is_complete,
    team_id,
    ticket_plan_digest,
    ticket_plan_snapshot,
    value,
)

STATES = COORD_STATES
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
LABEL_RE = re.compile(r"^[A-Za-z0-9:_-]+$")
SETUP_PROPOSAL_RE = re.compile(r"```(?:[A-Za-z0-9_-]+)?\s*(.*?)\s*```", re.DOTALL)
SETUP_APPROVAL_RE = re.compile(r"approv(?:e|ed)", re.IGNORECASE)
CREATE_SELECTOR_RE = re.compile(
    r"COORDINATOR:\s*/coord\s+create\s+(plan|phase:\d{2}|P-\d+(?:,P-\d+)*)",
    re.IGNORECASE,
)
TICKET_PROPOSALS_RE = re.compile(
    r"```coordinator-ticket-proposals\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL,
)
COMMAND_HELP_FOOTER = """---
**Actions:**
- `COORDINATOR: approve` — approve the latest coordinator label proposal, then apply repository and phase labels and hand off eligible tickets. Added/removed tickets or changed plan inputs require a refreshed planner plan and new approval.
- `PLANNER: /coord plan` — draft or refresh the technical plan from project documentation and selected repository context. Add optional focus after the command.
- `COORDINATOR: /coord status` — request a fresh project status.
- `COORDINATOR: /coord create plan` — create all proposed tickets for work without existing Jira keys.
- `COORDINATOR: /coord create phase:01` — create proposed tickets for one phase.
- `COORDINATOR: /coord create P-1,P-3` — create selected proposed tickets.
- Use ticket-creation requests only after the coordinator publishes a proposal list.
- `COORDINATOR: /coord complete project` — confirm project completion after all planned tickets are complete.

Start every control-ticket request with `PLANNER:` or `COORDINATOR:` on its first line."""


def require_config() -> tuple[str, str, str]:
    control_label = os.environ.get("BOT_LABEL", "")
    team_field_id = fabric_setting("TEAM_FIELD_ID")
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


def set_state(
    request: dict,
    config: tuple[str, str, str],
    *,
    approved_plan: bool = False,
) -> dict:
    state = request.get("state")
    if state not in STATES:
        raise ValueError("Unknown coordinator state")
    _, team_field_id, _ = config
    strict = state in {
        "dev-bot-coordinator:active",
        "dev-bot-coordinator:complete",
    }
    control = checked_control(request["control_key"], config, require_parent=strict, require_team=strict)
    if (
        "dev-bot-coordinator:needs-plan" in labels(control)
        and state != "dev-bot-coordinator:needs-plan"
        and not approved_plan
    ):
        raise ValueError("A needs-plan project requires a fresh user-approved plan before changing state")
    if state == "dev-bot-coordinator:active":
        children, assessment = fresh_assessment(control, config)
        if assessment["gaps"]:
            raise ValueError("Plan gate failed: " + "; ".join(assessment["gaps"]))
        planner_plan = current_planner_plan(control, children, team_field_id, set(load_project_repos()))
        if not planner_plan["current"]:
            raise ValueError("A current PLANNER plan is required before activating coordination")
    if state == "dev-bot-coordinator:complete":
        comment_id = str(request.get("request_comment_id") or "")
        if not comment_id:
            raise ValueError("Completion requires an explicit control comment ID")
        request_comment(control, comment_id, r"^\s*COORDINATOR:\s*/coord\s+complete\s+project\s*$")
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
    children = implementation_children(siblings, control, team_field_id)
    other_controls = [issue for issue in children if os.environ["BOT_LABEL"] in labels(issue)]
    assessment = evaluate(
        control,
        children,
        set(load_project_repos()),
        implementation_label,
    )
    if other_controls:
        assessment["gaps"].append("Parent has multiple same-Team control tickets")
        assessment["ready"] = []
    return children, assessment


def handoff(
    request: dict,
    config: tuple[str, str, str],
    *,
    approved_plan: bool = False,
) -> dict:
    control = checked_control(request["control_key"], config)
    _, team_field_id, implementation_label = config
    control_states = set(labels(control)) & STATES
    if "dev-bot-coordinator:needs-plan" in control_states and not approved_plan:
        raise ValueError("Plan changes are waiting for a new user approval before handoff")
    children, assessment = fresh_assessment(control, config)
    if assessment["gaps"]:
        raise ValueError("Plan gate failed: " + "; ".join(assessment["gaps"]))
    planner_plan = current_planner_plan(control, children, team_field_id, set(load_project_repos()))
    if not planner_plan["current"]:
        raise ValueError("A current PLANNER plan is required before implementation handoff")
    if not approved_plan:
        expected_snapshot_hash = request.get("approved_plan_snapshot_hash")
        actual_snapshot_hash = ticket_plan_digest(
            ticket_plan_snapshot(children, team_field_id, implementation_label)
        )
        if expected_snapshot_hash is None and "dev-bot-coordinator:active" not in control_states:
            pass
        elif expected_snapshot_hash != actual_snapshot_hash:
            raise ValueError("Plan inputs changed after approval; refresh the PLANNER plan and request approval")
    ticket_key = request["ticket_key"]
    ready = {item["key"]: item for item in assessment["ready"]}
    if ticket_key not in ready:
        ticket = next(
            (item for item in assessment["tickets"] if item["key"] == ticket_key),
            None,
        )
        blocked = next(
            (item for item in assessment["blocked_tickets"] if item["key"] == ticket_key),
            None,
        )
        if blocked:
            raise ValueError(f"{ticket_key} is blocked: {'; '.join(blocked['reasons'])}")
        if ticket_key in assessment["waiting"]:
            dependencies = ticket.get("blockers", []) if ticket else []
            detail = ", ".join(dependencies) or "an incomplete or external dependency"
            raise ValueError(f"{ticket_key} is waiting on blocking dependency: {detail}")
        if ticket and ticket["implementation_labeled"]:
            raise ValueError(f"{ticket_key} is already released to the implementation bot")
        raise ValueError(f"{ticket_key} is not a runnable ticket in the lowest incomplete phase")
    child = checked_child(control, ticket_key, team_field_id)
    planned_assignment = planner_plan["assignments"].get(ticket_key)
    if not planned_assignment:
        raise ValueError(f"{ticket_key} is missing from the current PLANNER assignment map")
    if planned_assignment["phase"] not in labels(child):
        raise ValueError("Implementation ticket phase label differs from the current PLANNER plan")
    selected_repos = request.get("repos") or []
    if isinstance(selected_repos, str):
        selected_repos = [selected_repos]
    allowed = set(assessment["allowed_repos"])
    if not selected_repos or set(selected_repos) - allowed or set(selected_repos) - set(load_project_repos()):
        raise ValueError("Selected repos must be configured and allowed on the control ticket")
    if set(selected_repos) != {planned_assignment["repository"]}:
        raise ValueError("Selected repositories differ from the current PLANNER assignment")
    existing_repos = {label[5:] for label in labels(child) if label.startswith("repo:")}
    if existing_repos and existing_repos != set(selected_repos):
        raise ValueError("Existing ticket repo labels differ from selected repos")
    if not approved_plan and expected_snapshot_hash is not None:
        current_children, _ = fresh_assessment(control, config)
        current_snapshot_hash = ticket_plan_digest(
            ticket_plan_snapshot(current_children, team_field_id, implementation_label)
        )
        if current_snapshot_hash != expected_snapshot_hash:
            raise ValueError("Plan inputs changed during handoff validation; refresh the PLANNER plan and request approval")
        child = next(item for item in current_children if key_of(item) == ticket_key)
    handoff_labels = repository_handoff_labels(selected_repos, implementation_label)
    updated = labels(child) + handoff_labels
    set_labels(ticket_key, updated)
    refreshed_control = get_issue(key_of(control))
    refreshed_children, _ = fresh_assessment(refreshed_control, config)
    return {
        "ticket_key": ticket_key,
        "released_repos": sorted(set(selected_repos)),
        "released_labels": handoff_labels,
        "plan_snapshot": ticket_plan_snapshot(
            refreshed_children, team_field_id, implementation_label
        ),
        "approved_plan_snapshot_hash": ticket_plan_digest(
            ticket_plan_snapshot(refreshed_children, team_field_id, implementation_label)
        ),
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


def request_comment(
    control: dict,
    comment_id: str,
    command_pattern: str,
    *,
    requirement: str = "the required authorization",
) -> dict:
    matches = [item for item in comments(control) if str(item.get("id") or "") == comment_id]
    if len(matches) != 1:
        raise ValueError("Request comment not found or ambiguous")
    item = matches[0]
    body = comment_body(item)
    if is_own_comment(item):
        raise ValueError("Coordinator-authored comments cannot authorize actions")
    if not is_coordination_feedback(item):
        raise ValueError("Coordinator actions require a first-line COORDINATOR: comment")
    if not re.search(command_pattern, body, re.IGNORECASE):
        raise ValueError(f"User comment does not contain {requirement}")
    return item


def setup_proposal(
    control: dict,
    approval_comment_id: str,
    inline_proposal: object | None = None,
) -> dict:
    """Read the latest setup proposal or accept the freshly checked inline plan."""
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
        requirement="approval wording (`approve` or `approved`)",
    )
    if str(approved_comment.get("id") or "") != approval_comment_id:
        raise ValueError("Setup approval comment changed during validation")
    if any(
        is_coordination_feedback(item)
        for item in items[approval_index + 1:]
    ):
        raise ValueError("A newer user comment exists after this setup request; process the latest comment")
    if inline_proposal is not None:
        if not isinstance(inline_proposal, dict):
            raise ValueError("Inline setup proposal must be an object")
        return inline_proposal
    for proposal_index in range(approval_index - 1, -1, -1):
        item = items[proposal_index]
        body = comment_body(item)
        if not is_own_comment(item):
            continue
        match = SETUP_PROPOSAL_RE.search(body)
        if not match:
            continue
        if any(
            is_coordination_feedback(comment)
            for comment in items[proposal_index + 1:approval_index]
        ):
            raise ValueError("Setup proposal is stale; refresh it after the latest user feedback")
        try:
            proposal = json.loads(match.group(1))
        except json.JSONDecodeError as error:
            raise ValueError("Coordinator setup proposal is invalid JSON") from error
        if not isinstance(proposal, dict):
            raise ValueError("Coordinator setup proposal must be an object")
        return proposal
    raise ValueError("No coordinator setup proposal exists before this approval")


def published_plan_revision(control: dict, approval_comment_id: str) -> dict | None:
    """Return the latest published revision when a needs-plan state is pending approval."""
    if "dev-bot-coordinator:needs-plan" not in labels(control):
        return None
    items = comments(control)
    approval_indexes = [
        index for index, item in enumerate(items)
        if str(item.get("id") or "") == approval_comment_id
    ]
    if len(approval_indexes) != 1:
        raise ValueError("Setup approval comment not found or ambiguous")
    approval_index = approval_indexes[0]
    revision = None
    for index, item in enumerate(items[:approval_index]):
        if not is_own_comment(item):
            continue
        lines = [line.strip() for line in comment_body(item).splitlines() if line.strip()]
        heading = lines[0].lstrip("#").strip() if lines else ""
        if heading.casefold().startswith("coordinator:"):
            heading = heading[len("COORDINATOR:"):].strip()
        if re.fullmatch(rf"{re.escape(OWN_COMMENT_PREFIX)} - Plan revision #\d+", heading, re.IGNORECASE):
            revision = item
    if revision is None:
        return None
    match = SETUP_PROPOSAL_RE.search(comment_body(revision))
    if not match:
        raise ValueError("Plan revision has no published coordinator setup proposal")
    try:
        revision_proposal = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ValueError("Published plan revision proposal is invalid JSON") from error
    if not isinstance(revision_proposal, dict):
        raise ValueError("Published plan revision proposal must be an object")
    approved_proposal = setup_proposal(control, approval_comment_id)
    if approved_proposal != revision_proposal:
        raise ValueError("Approval does not match the latest published plan revision")
    return revision_proposal


def approve_setup(request: dict, config: tuple[str, str, str]) -> dict:
    """Apply the approved setup and hand off eligible tickets in its first phase."""
    control = checked_control(request["control_key"], config)
    _, team_field_id, implementation_label = config
    comment_id = str(request.get("request_comment_id") or "")
    if not comment_id:
        raise ValueError("request_comment_id is required")
    revision_proposal = published_plan_revision(control, comment_id)
    if revision_proposal is not None:
        if request.get("proposal") is not None:
            raise ValueError("Plan revision approval must use the published proposal")
        proposal = revision_proposal
    else:
        proposal = setup_proposal(control, comment_id, request.get("proposal"))

    repo_names = set(load_project_repos())
    assignments = proposal.get("phase_assignments")
    if not isinstance(assignments, dict):
        raise ValueError("Setup proposal must include phase_assignments")
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
    parent = parent_key(control)
    siblings = search_all(f'parent = "{parent}" ORDER BY key ASC')
    children = implementation_children(siblings, control, team_field_id)
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
    current_ticket_snapshot_hash = ticket_plan_digest(
        ticket_plan_snapshot(children, team_field_id, implementation_label)
    )
    if proposal.get("ticket_snapshot_hash") != current_ticket_snapshot_hash:
        raise ValueError("Tickets changed after the coordinator label proposal; refresh it before approval")
    planner_plan = current_planner_plan(control, children, team_field_id, repo_names)
    if not planner_plan["current"]:
        raise ValueError("No current PLANNER technical plan covers every same-Team implementation ticket")
    if (proposal.get("planner_comment_id") != planner_plan["comment_id"]
            or proposal.get("planner_input_snapshot_hash") != planner_plan["input_snapshot_hash"]):
        raise ValueError("Setup proposal does not reference the latest current PLANNER plan")
    ticket_repositories = proposal.get("ticket_repositories")
    if ticket_repositories is None:
        default_repository = proposal.get("repository")
        if not isinstance(default_repository, str) or default_repository not in repo_names:
            raise ValueError("Setup proposal must include configured ticket_repositories or a configured repository")
        repo_by_ticket = {key: default_repository for key in child_keys}
    else:
        if not isinstance(ticket_repositories, dict) or set(ticket_repositories) != child_keys:
            raise ValueError("Setup proposal must assign a configured repository to every implementation ticket")
        if any(not isinstance(repo, str) or repo not in repo_names for repo in ticket_repositories.values()):
            raise ValueError("Setup proposal contains an unconfigured ticket repository")
        repo_by_ticket = dict(ticket_repositories)
    expected_phases = {
        key: assignment["phase"] for key, assignment in planner_plan["assignments"].items()
    }
    expected_repositories = {
        key: assignment["repository"] for key, assignment in planner_plan["assignments"].items()
    }
    if phase_by_ticket != expected_phases or repo_by_ticket != expected_repositories:
        raise ValueError("Setup labels must match the latest PLANNER phase and repository assignments")
    repository_handoff_labels(sorted(set(repo_by_ticket.values())), implementation_label)
    if revision_proposal is not None:
        expected_snapshot_hash = revision_proposal.get("ticket_snapshot_hash")
        actual_snapshot = ticket_plan_snapshot(children, team_field_id, implementation_label)
        if expected_snapshot_hash != ticket_plan_digest(actual_snapshot):
            raise ValueError("Tickets changed after the plan revision; refresh the proposal and request approval again")
    children_by_key = {key_of(item): item for item in children}
    # Re-read every ticket and validate before performing any Jira writes.
    fresh_children = {key: checked_child(control, key, team_field_id) for key in sorted(child_keys)}
    if revision_proposal is not None:
        current_children, _ = fresh_assessment(control, config)
        fresh_snapshot = ticket_plan_snapshot(
            current_children, team_field_id, implementation_label
        )
        if revision_proposal.get("ticket_snapshot_hash") != ticket_plan_digest(fresh_snapshot):
            raise ValueError("Tickets changed during approval validation; refresh the Plan revision and request approval again")
        fresh_children = {key_of(item): item for item in current_children}
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
        existing_repositories = {label[5:] for label in existing if label.startswith("repo:")}
        selected_repository = repo_by_ticket[ticket_key]
        if existing_phases - {phase} or existing_repositories - {selected_repository}:
            status = value(child, "status") or {}
            status = str(status.get("name", "") if isinstance(status, dict) else status).lower()
            if implementation_label in existing or status not in START_STATUSES:
                raise ValueError(f"Cannot change phase or repository for handed-off or started ticket {ticket_key}")
        updated = [label for label in existing if not PHASE_RE.fullmatch(label) and not label.startswith("repo:")]
        updated.extend([phase, f"repo:{selected_repository}"])
        phase_updates[ticket_key] = updated

    selected_repositories = sorted(set(repo_by_ticket.values()))
    updated_control_labels = [label for label in current_control_labels if not label.startswith("repo:")]
    updated_control_labels.extend(f"repo:{repo}" for repo in selected_repositories)
    control_repo_labels_added = sorted(
        set(updated_control_labels) - set(current_control_labels)
    )
    if set(updated_control_labels) != set(current_control_labels):
        set_labels(key_of(control), updated_control_labels)
    updated_phases: list[str] = []
    for ticket_key, updated in sorted(phase_updates.items()):
        if set(updated) != set(labels(fresh_children[ticket_key])):
            set_labels(ticket_key, updated)
            updated_phases.append(ticket_key)
    refreshed_control = get_issue(key_of(control))
    assessed_children, assessment = fresh_assessment(refreshed_control, config)
    children_by_key = {key_of(child): child for child in assessed_children}
    handed_off: list[str] = []
    handoff_failures: list[dict[str, str]] = []
    if not assessment["gaps"]:
        for ready_ticket in assessment["ready"]:
            ticket_key = ready_ticket["key"]
            child_repos = {
                label[5:] for label in labels(children_by_key[ticket_key])
                if label.startswith("repo:")
            }
            selected_repos = sorted(child_repos) or [repo_by_ticket[ticket_key]]
            try:
                handoff(
                    {"control_key": key_of(control), "ticket_key": ticket_key, "repos": selected_repos},
                    config,
                    approved_plan=True,
                )
            except (RuntimeError, ValueError) as error:
                handoff_failures.append({"ticket_key": ticket_key, "error": str(error)})
            else:
                handed_off.append(ticket_key)

    already_released = [
        ticket["key"] for ticket in assessment["tickets"]
        if ticket["implementation_labeled"] and ticket["phase_labels"] == [assessment["current_phase"]]
    ] if assessment["current_phase"] else []
    if not assessment["gaps"]:
        set_state(
            {"control_key": key_of(control), "state": "dev-bot-coordinator:active"},
            config,
            approved_plan=True,
        )
    refreshed_control = get_issue(key_of(control))
    final_children, _ = fresh_assessment(refreshed_control, config)
    return {
        "control_key": key_of(control),
        "repositories": selected_repositories,
        "control_repo_labels_added": control_repo_labels_added,
        "phase_labels_updated": updated_phases,
        "approval_comment_id": comment_id,
        "handed_off": handed_off,
        "already_released": already_released,
        "handoff_failures": handoff_failures,
        "handoff_blockers": assessment["gaps"],
        "blocked_tickets": assessment["blocked_tickets"],
        "waiting": assessment["waiting"],
        "plan_snapshot": ticket_plan_snapshot(
            final_children, team_field_id, implementation_label
        ),
        "approved_plan_snapshot_hash": ticket_plan_digest(
            ticket_plan_snapshot(final_children, team_field_id, implementation_label)
        ),
        "plan_approval_pending": False,
        "next_action": "report_handoff_results",
    }


def creation_fields(
    control: dict,
    team_field_id: str,
    extra_labels: list[str],
    *,
    parent_issue: dict | None = None,
) -> tuple[str, dict]:
    parent = parent_issue if parent_issue is not None else get_issue(parent_key(control))
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


def create_selection(control: dict, request_comment_id: str, config: tuple[str, str, str]) -> tuple[list[dict], str, str, bool]:
    """Validate a create selector and resolve it to the latest published ticket proposals."""
    items = comments(control)
    command = request_comment(
        control,
        request_comment_id,
        r"^\s*COORDINATOR:\s*/coord\s+create\b",
        requirement="an explicit user request with a ticket creation selection",
    )
    request_index = next(
        index for index, item in enumerate(items)
        if str(item.get("id") or "") == request_comment_id
    )
    first_line = comment_body(command).strip().splitlines()[0].strip()
    match = CREATE_SELECTOR_RE.fullmatch(first_line)
    if not match:
        raise ValueError("Use `COORDINATOR: /coord create plan`, `phase:NN`, or comma-separated P-IDs")
    selector = match[1].casefold()
    if any(
        is_coordination_feedback(item) or planner_focus(item) is not None
        for item in items[request_index + 1:]
    ):
        raise ValueError("A newer coordinator request exists; process the latest comment")

    source_index = None
    source = None
    payload = None
    for index in range(request_index - 1, -1, -1):
        if not is_own_comment(items[index]):
            continue
        block = TICKET_PROPOSALS_RE.search(comment_body(items[index]))
        if not block:
            continue
        try:
            payload = json.loads(block[1])
        except json.JSONDecodeError as error:
            raise ValueError("Coordinator ticket proposals are invalid JSON") from error
        source_index, source = index, items[index]
        break

    if source is None:
        if selector.startswith("p-"):
            proposal_ids = selector.upper().split(",")
            if len(proposal_ids) != 1 or not re.fullmatch(r"P-\d+", proposal_ids[0]):
                raise ValueError("A proposal list requires a coordinator-ticket-proposals block")
            return [], proposal_ids[0], request_comment_id, True
        raise ValueError("No structured ticket proposal list exists before this request")
    if any(
        is_coordination_feedback(item) or planner_focus(item) is not None
        for item in items[source_index + 1:request_index]
    ):
        raise ValueError("Ticket proposals are stale after intervening user feedback or a planner refresh request")
    if not isinstance(payload, dict) or not isinstance(payload.get("proposals"), list):
        raise ValueError("Coordinator ticket proposal block must contain a proposals list")

    children, _ = fresh_assessment(control, config)
    planner_plan = current_planner_plan(control, children, config[1], set(load_project_repos()))
    if not planner_plan["current"]:
        raise ValueError("A current PLANNER plan is required before creating proposed tickets")
    if (
        str(payload.get("planner_comment_id") or "") != str(planner_plan["comment_id"] or "")
        or payload.get("planner_input_snapshot_hash") != planner_plan["input_snapshot_hash"]
        or payload.get("ticket_snapshot_hash") != planner_plan["current_snapshot_hash"]
    ):
        raise ValueError("Ticket proposals do not match the latest current PLANNER plan")

    proposals = payload["proposals"]
    if len(proposals) > 100:
        raise ValueError("At most 100 proposed tickets can be created from one plan")
    by_id = {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            raise ValueError("Each ticket proposal must be an object")
        proposal_id = str(proposal.get("id") or "").upper()
        if not re.fullmatch(r"P-\d+", proposal_id) or proposal_id in by_id:
            raise ValueError("Ticket proposal IDs must be unique P-<number> values")
        summary_value = proposal.get("summary")
        description_value = proposal.get("description")
        phase_value = proposal.get("phase")
        repository_value = proposal.get("repository")
        if not all(
            isinstance(item, str)
            for item in (summary_value, description_value, phase_value, repository_value)
        ):
            raise ValueError(f"{proposal_id} must use text values for summary, description, phase, and repository")
        summary = summary_value.strip()
        description = description_value.strip()
        phase = phase_value.strip()
        repository = repository_value.strip()
        if not summary or len(summary) > 255 or not description or len(description) > 24000:
            raise ValueError(f"{proposal_id} needs a summary and description within Jira size limits")
        if not PHASE_RE.fullmatch(phase):
            raise ValueError(f"{proposal_id} must use a phase:NN assignment")
        if not repository:
            raise ValueError(f"{proposal_id} must name a repository")
        proposal["id"] = proposal_id
        proposal["summary"] = summary
        proposal["description"] = description
        proposal["phase"] = phase
        proposal["repository"] = repository
        by_id[proposal_id] = proposal

    if selector == "plan":
        selected_ids = list(by_id)
    elif selector.startswith("phase:"):
        selected_ids = [key for key, proposal in by_id.items() if proposal["phase"].casefold() == selector]
    else:
        selected_ids = [item.upper() for item in selector.split(",")]
        if len(selected_ids) != len(set(selected_ids)):
            raise ValueError("A selected proposal ID may appear only once")
        unknown = set(selected_ids) - set(by_id)
        if unknown:
            raise ValueError(f"Unknown proposal IDs: {', '.join(sorted(unknown))}")
    if not selected_ids:
        raise ValueError("The selected plan or phase has no proposed tickets")
    if any(not re.fullmatch(r"P-\d+", item) for item in selected_ids):
        raise ValueError("Selected proposal IDs must use P-<number>")
    if len(selected_ids) > 100:
        raise ValueError("At most 100 proposed tickets can be created from one request")
    selected_proposals = [by_id[item] for item in selected_ids]
    allowed_repositories = {
        label.removeprefix("repo:") for label in labels(control)
        if isinstance(label, str) and label.startswith("repo:")
    }
    for proposal in selected_proposals:
        repository = proposal["repository"]
        if repository not in allowed_repositories or repository not in load_project_repos():
            raise ValueError(f"{proposal['id']} repository {repository} is not allowed on the control ticket")
        repository_handoff_labels([repository], config[2])
    source_comment_id = str(source.get("id") or "")
    if not re.fullmatch(r"[1-9][0-9]*", source_comment_id):
        raise ValueError("Coordinator proposal comment has no valid Jira comment ID")
    return selected_proposals, source_comment_id, request_comment_id, False


def create_from_proposal(
    control: dict,
    proposal: dict,
    source_comment_id: str,
    request_comment_id: str,
    config: tuple[str, str, str],
    *,
    siblings: list[dict],
    parent_issue: dict,
) -> dict:
    _, team_field_id, implementation_label = config
    proposal_id = str(proposal["id"])
    summary = str(proposal["summary"])
    description = str(proposal["description"])
    phase = str(proposal["phase"])
    repository = str(proposal.get("repository") or "")
    issue_labels = [phase]
    if repository:
        allowed_repositories = {
            label.removeprefix("repo:") for label in labels(control)
            if isinstance(label, str) and label.startswith("repo:")
        }
        if repository not in allowed_repositories or repository not in load_project_repos():
            raise ValueError(f"{proposal_id} repository {repository} is not allowed on the control ticket")
        repository_handoff_labels([repository], implementation_label)
        issue_labels.append(f"repo:{repository}")
    marker = (
        f"Coordinator proposal: {key_of(control)}#{source_comment_id} {proposal_id}"
        if source_comment_id.isdigit()
        else f"Coordinator request: {key_of(control)}#{request_comment_id} {proposal_id}"
    )
    existing = [issue for issue in siblings if marker in str(value(issue, "description") or "")]
    if existing:
        issue = checked_child(control, key_of(existing[0]), team_field_id)
        if any(label not in labels(issue) for label in issue_labels):
            raise ValueError(f"Previously created {proposal_id} is missing its phase or repository label")
        return {"proposal_id": proposal_id, "ticket_key": key_of(issue), "created": False}
    child_type, additional = creation_fields(
        control, team_field_id, issue_labels, parent_issue=parent_issue,
    )
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
        raise RuntimeError("Created ticket unexpectedly has the implementation bot label")
    if any(label not in labels(child) for label in issue_labels):
        raise RuntimeError("Created ticket is missing its phase or repository label")
    return {"proposal_id": proposal_id, "ticket_key": ticket_key, "created": True}


def create_tickets(request: dict, config: tuple[str, str, str]) -> dict:
    control = checked_control(request["control_key"], config)
    comment_id = str(request.get("request_comment_id") or "")
    if not comment_id:
        raise ValueError("request_comment_id is required")
    proposals, source_comment_id, request_comment_id, legacy = create_selection(
        control, comment_id, config,
    )
    if legacy:
        proposal_id = source_comment_id
        summary = str(request.get("summary") or "").strip()
        description = str(request.get("description") or "").strip()
        if not any(
            is_own_comment(item) and proposal_id in comment_body(item)
            for item in comments(control)
        ):
            raise ValueError("Proposal ID was not found in a coordinator comment")
        phase = str(request.get("phase") or "")
        if not summary or not description:
            raise ValueError("Summary and description are required")
        if not re.fullmatch(r"\d{2}", phase):
            raise ValueError("Implementation ticket phase must be two digits")
        proposals = [{
            "id": proposal_id,
            "summary": summary,
            "description": description,
            "phase": f"phase:{phase}",
            "repository": str(request.get("repository") or ""),
        }]
    parent_issue = get_issue(parent_key(control))
    creation_fields(control, config[1], [], parent_issue=parent_issue)
    siblings = search_all(f'parent = "{parent_key(control)}" ORDER BY key ASC')
    results = []
    failures = []
    for proposal in proposals:
        try:
            results.append(create_from_proposal(
                control,
                proposal,
                source_comment_id,
                request_comment_id,
                config,
                siblings=siblings,
                parent_issue=parent_issue,
            ))
        except (OSError, RuntimeError, ValueError) as error:
            failures.append({"proposal_id": proposal["id"], "error": str(error)})
    return {
        "control_key": key_of(control),
        "request_comment_id": request_comment_id,
        "tickets": results,
        "failures": failures,
    }


def create_ticket(request: dict, config: tuple[str, str, str]) -> dict:
    """Compatibility wrapper for an explicitly selected single proposal."""
    control = checked_control(request["control_key"], config)
    comment_id = str(request.get("request_comment_id") or "")
    if not comment_id:
        raise ValueError("request_comment_id is required")
    command = request_comment(
        control,
        comment_id,
        r"^\s*COORDINATOR:\s*/coord\s+create\b",
        requirement="an explicit user request with a ticket creation selection",
    )
    first_line = comment_body(command).strip().splitlines()[0].strip()
    match = CREATE_SELECTOR_RE.fullmatch(first_line)
    if not match or not re.fullmatch(r"P-\d+", match[1], re.IGNORECASE):
        raise ValueError("create-ticket accepts one P-ID; use create-tickets for a plan, phase, or batch")
    result = create_tickets(request, config)
    if result["failures"]:
        raise ValueError(result["failures"][0]["error"])
    if len(result["tickets"]) != 1:
        raise ValueError("create-ticket accepts exactly one proposal; use create-tickets for a batch")
    ticket = result["tickets"][0]
    return {**ticket, "request_comment_id": result["request_comment_id"]}


def comment_control(request: dict, config: tuple[str, str, str]) -> dict:
    control = checked_control(request["control_key"], config, require_parent=False, require_team=False)
    action_id = str(request.get("action_id") or "")
    comment_type = str(request.get("comment_type") or "Status update").strip()
    body = str(request.get("body") or "").strip()
    next_steps = request.get("next_steps")
    if not action_id or not body:
        raise ValueError("action_id and body are required")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 /-]{0,39}", comment_type):
        raise ValueError("comment_type must be a short, single-line label")
    body_lines = body.splitlines()
    while body_lines:
        heading = body_lines[0].strip().lstrip("#").strip().casefold()
        if heading.startswith("coordinator:"):
            heading = heading[len("coordinator:"):].strip()
        prefix = OWN_COMMENT_PREFIX.casefold()
        if heading != prefix and not heading.startswith(prefix + " -"):
            break
        body_lines.pop(0)
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
    body = "\n".join(body_lines).strip()
    if not body:
        raise ValueError("body must contain update text after the coordinator heading")
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
    existing_comments = comments(control)
    if any(marker in comment_body(item) for item in existing_comments):
        return {"control_key": key_of(control), "commented": False, "action_id": action_id}
    update_number = sum(1 for item in existing_comments if is_own_comment(item)) + 1
    footer = "### Possible next steps\n" + "\n".join(f"- {step.strip()}" for step in next_steps)
    heading = f"COORDINATOR: {OWN_COMMENT_PREFIX} - {comment_type} #{update_number}"
    comment = f"{heading}\n\n{body}\n\n{footer}\n\n{COMMAND_HELP_FOOTER}\n\n{marker}"
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
    if action == "create-ticket":
        return create_ticket(request, config)
    if action == "create-tickets":
        return create_tickets(request, config)
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
