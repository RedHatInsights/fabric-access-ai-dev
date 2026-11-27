"""Focused project-gate and Jira-write checks for the coordinator instance."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[2]
REPO = Path(__file__).resolve().parents[7]
sys.path.insert(0, str(WORKFLOW))
sys.path.insert(0, str(REPO / "dev-bot" / "presets" / "shared" / "preflight"))
jira_stub = types.ModuleType("jira_mcp")
jira_stub.jira_call = lambda *_args, **_kwargs: None
jira_stub.jira_cleanup = lambda: None
sys.modules.setdefault("jira_mcp", jira_stub)

import actions  # noqa: E402
import project_coordinator as coordinator  # noqa: E402

TEAM_FIELD = "customfield_12345"
TEAM_ID = "42"
CONFIG = ("dev-bot-coordinator", TEAM_FIELD, "hcc-ai-platform-accessmanagement")


def issue(key: str, *, parent: str | None = "RHCLOUD-100", team: str = TEAM_ID, labels=None, status="New", links=None, summary=None, description="Clear acceptance criteria") -> dict:
    data = {
        "key": key,
        "fields": {
            "summary": summary or key,
            "description": description,
            "status": {"name": status},
            "labels": list(labels or []),
            TEAM_FIELD: {"id": team},
            "issuelinks": list(links or []),
            "assignee": None,
            "customfield_10020": [{"id": 1, "state": "active"}],
        },
    }
    if parent:
        data["fields"]["parent"] = {"key": parent}
    return data


def relates(key: str) -> dict:
    return {"type": {"name": "Relates", "inward": "relates to", "outward": "relates to"}, "outwardIssue": {"key": key}}


def blocked_by(key: str) -> dict:
    return {"type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"}, "inwardIssue": {"key": key}}


def add_planner_comment(control: dict, children: list[dict], assignments: dict, comment_id="499") -> str:
    snapshot_hash = coordinator.ticket_plan_digest(coordinator.planner_ticket_snapshot(children, TEAM_FIELD))
    body = (
        f"{coordinator.PLANNER_HEADING} for RHCLOUD-100\n\n"
        "```planner-coordination\n"
        + json.dumps({"input_snapshot_hash": snapshot_hash, "ticket_assignments": assignments})
        + f"\n```\n\n{coordinator.PLANNER_RECEIPT} 498"
    )
    control["fields"]["comment"]["comments"].append({"id": comment_id, "body": body})
    return snapshot_hash


def test_control_ticket_planner_command_uses_coordinator_preflight(monkeypatch, tmp_path, capsys):
    control = issue("RHCLOUD-101", labels=[CONFIG[0]])
    control["fields"]["assignee"] = {"displayName": "Assigned Coordinator"}
    parent = issue("RHCLOUD-100", parent=None, description="Parent project brief")
    before = coordinator.fingerprint(parent, control, [], TEAM_FIELD)
    command = {"id": "504", "body": "PLANNER: /coord plan Describe the approved rollout"}
    control["comments"] = [command]
    assert coordinator.fingerprint(parent, control, [], TEAM_FIELD) == before

    monkeypatch.setenv("BOT_LABEL", CONFIG[0])
    monkeypatch.setenv("BOT_TEAM_FIELD_ID", TEAM_FIELD)
    monkeypatch.setenv("BOT_IMPLEMENTATION_LABEL", CONFIG[2])
    monkeypatch.setenv("BOT_PLANNER_DOCS_BASE_URL", "https://gitlab.example.com/team/docs/")
    monkeypatch.setattr(coordinator, "PLANNER_REQUEST_DIR", tmp_path)
    monkeypatch.setattr(coordinator, "search_all", lambda *_args: [control])
    monkeypatch.setattr(coordinator, "get_issue", lambda key: {"RHCLOUD-101": control}[key])
    monkeypatch.setattr(coordinator, "load_tasks", lambda: pytest.fail("Planner must not use coordinator tasks"))
    coordinator.main()
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "start"
    assert json.loads(output["content"]) == {
        "planner": {"control_key": "RHCLOUD-101", "comment_id": "504", "parent_key": "RHCLOUD-100"},
    }
    settings = json.loads((tmp_path / "RHCLOUD-101-504.json").read_text())
    assert settings["docs_base"] == "https://gitlab.example.com/team/docs/"
    with pytest.raises(ValueError, match="first-line COORDINATOR"):
        actions.request_comment(control, "504", actions.SETUP_APPROVAL_RE.pattern)

    control["comments"].append({"id": "505", "body": (
        f"{coordinator.PLANNER_HEADING} for RHCLOUD-100\n\n"
        f"{coordinator.PLANNER_RECEIPT} 504"
    )})
    assert coordinator.pending_planner_request(control, CONFIG[0]) is None
    assert coordinator.fingerprint(parent, control, [], TEAM_FIELD) != before
    assert coordinator.planner_focus({"body": "/coord plan"}) is None
    assert coordinator.planner_focus({"body": "PLANNER: /coord plan"}) == ""


def test_parallel_first_tickets_and_release_pending_unblock_next_phase():
    control = issue("RHCLOUD-101", labels=["dev-bot-coordinator", "repo:rbac-config"])
    first = issue("RHCLOUD-102", labels=["phase:01"], links=[relates("RHCLOUD-101")])
    parallel = issue("RHCLOUD-103", labels=["phase:01"], links=[relates("RHCLOUD-101")])
    later = issue("RHCLOUD-104", labels=["phase:02"], links=[blocked_by("RHCLOUD-102"), blocked_by("RHCLOUD-103")])

    result = coordinator.evaluate(control, [first, parallel, later], {"rbac-config"}, CONFIG[2])
    assert result["gaps"] == []
    assert result["current_phase"] == "phase:01"
    assert {item["key"] for item in result["ready"]} == {"RHCLOUD-102", "RHCLOUD-103"}

    first["fields"]["status"]["name"] = "Release Pending"
    parallel["fields"]["status"]["name"] = "Release Pending"
    result = coordinator.evaluate(control, [first, parallel, later], {"rbac-config"}, CONFIG[2])
    assert result["current_phase"] == "phase:02"
    assert result["ready"] == [{"key": "RHCLOUD-104", "repo_candidates": ["rbac-config"]}]


def test_control_phase_stops_handoff_without_a_relation_link():
    control = issue("RHCLOUD-101", labels=["dev-bot-coordinator", "repo:rbac-config", "phase:01"])
    first = issue("RHCLOUD-102", labels=["phase:01"])
    result = coordinator.evaluate(control, [first], {"rbac-config"}, CONFIG[2])
    assert any("control ticket" in gap.lower() for gap in result["gaps"])
    assert not any("must relate" in gap for gap in result["gaps"])
    assert result["ready"] == []


def test_unphased_child_is_a_plan_gap_without_relation_links():
    control = issue("RHCLOUD-101", labels=["dev-bot-coordinator", "repo:rbac-config"])
    first = issue("RHCLOUD-102", labels=["phase:01"], links=[relates("RHCLOUD-101")])
    unphased = issue("RHCLOUD-103")
    later_root = issue("RHCLOUD-104", labels=["phase:02"])
    result = coordinator.evaluate(control, [first, unphased, later_root], {"rbac-config"}, CONFIG[2])
    assert any("RHCLOUD-103 has no phase label" in gap for gap in result["gaps"])
    assert not any("must relate" in gap for gap in result["gaps"])
    assert result["ready"] == []


def test_team_parser_reads_nested_list_and_json_field_shapes():
    assert coordinator.team_id({"fields": {TEAM_FIELD: {"teamId": "42"}}}, TEAM_FIELD) == "42"
    assert coordinator.team_id({"fields": {TEAM_FIELD: {"value": {"id": "42"}}}}, TEAM_FIELD) == "42"
    assert coordinator.team_id({"fields": {TEAM_FIELD: [{"id": "42"}]}}, TEAM_FIELD) == "42"
    assert coordinator.team_id({"fields": {TEAM_FIELD: '{"id":"42","name":"Access"}'}}, TEAM_FIELD) == "42"
    assert coordinator.team_id({"fields": {TEAM_FIELD: [{"id": "42"}, {"id": "43"}]}}, TEAM_FIELD) == ""


def test_search_walks_cloud_cursor_and_server_offset(monkeypatch):
    pages = [
        {"issues": [{"key": "RHCLOUD-1"}], "next_page_token": "next", "total": -1},
        {"issues": [{"key": "RHCLOUD-2"}], "total": -1},
    ]
    calls = []

    def jira_call(_name, args):
        calls.append(args)
        return pages.pop(0)

    monkeypatch.setattr(coordinator, "jira_call", jira_call)
    assert [item["key"] for item in coordinator.search_all("project = RHCLOUD")] == ["RHCLOUD-1", "RHCLOUD-2"]
    assert calls[1]["page_token"] == "next"

    pages = [
        {"issues": [{"key": "RHCLOUD-1"}], "total": 2},
        {"issues": [{"key": "RHCLOUD-2"}], "total": 2},
    ]
    calls.clear()
    assert len(coordinator.search_all("project = RHCLOUD")) == 2
    assert calls[1]["start_at"] == 1


@pytest.fixture
def jira_store(monkeypatch):
    control = issue(
        "RHCLOUD-101",
        labels=["dev-bot-coordinator", "repo:rbac-config", "dev-bot-coordinator:active"],
    )
    first = issue("RHCLOUD-102", labels=["phase:01"], links=[relates("RHCLOUD-101")])
    parent = issue("RHCLOUD-100", parent=None, summary="Parent")
    parent["fields"]["issuetype"] = {"name": "Epic"}
    control["fields"]["comment"] = {"comments": [{"id": "500", "body": "COORDINATOR: /coord create P-1"}]}
    add_planner_comment(control, [first], {
        "RHCLOUD-102": {"phase": "phase:01", "repository": "rbac-config"},
    })
    store = {item["key"]: item for item in (control, first, parent)}
    writes = []

    def get_issue(key):
        return store[key]

    def search_all(jql, _fields="*all"):
        return [item for key, item in store.items() if key not in {"RHCLOUD-100"}]

    def jira_call(name, args):
        writes.append((name, args))
        if name == "jira_update_issue":
            store[args["issue_key"]]["fields"]["labels"] = json.loads(args["fields"])["labels"]
            return {"success": True}
        if name == "jira_create_issue_link":
            inward = args["inward_issue_key"]
            outward = args["outward_issue_key"]
            store[inward]["fields"].setdefault("issuelinks", []).append(relates(outward))
            return {"success": True}
        if name == "jira_create_issue":
            created = issue("RHCLOUD-105", labels=json.loads(args["additional_fields"])["labels"], summary=args["summary"], description=args["description"])
            store["RHCLOUD-105"] = created
            return {"key": "RHCLOUD-105"}
        return {"success": True}

    monkeypatch.setattr(actions, "get_issue", get_issue)
    monkeypatch.setattr(actions, "search_all", search_all)
    monkeypatch.setattr(actions, "load_project_repos", lambda: {"rbac-config": {}})
    monkeypatch.setattr(actions, "jira_call", jira_call)
    monkeypatch.setenv("BOT_LABEL", CONFIG[0])
    return store, writes


def test_handoff_rechecks_team(jira_store):
    store, writes = jira_store
    result = actions.handoff({"control_key": "RHCLOUD-101", "ticket_key": "RHCLOUD-102", "repos": ["rbac-config"]}, CONFIG)
    assert result["ticket_key"] == "RHCLOUD-102"
    assert CONFIG[2] in store["RHCLOUD-102"]["fields"]["labels"]
    assert "repo:rbac-config" in store["RHCLOUD-102"]["fields"]["labels"]
    assert any(name == "jira_update_issue" for name, _ in writes)

    store["RHCLOUD-102"]["fields"]["labels"] = ["phase:01"]
    store["RHCLOUD-102"]["fields"][TEAM_FIELD] = {"id": "another-team"}
    write_count = len(writes)
    with pytest.raises(ValueError, match="Plan gate failed"):
        actions.handoff({"control_key": "RHCLOUD-101", "ticket_key": "RHCLOUD-102", "repos": ["rbac-config"]}, CONFIG)
    assert len(writes) == write_count


def test_handoff_recognizes_unassigned_placeholder_without_sprint(jira_store):
    store, writes = jira_store
    child = store["RHCLOUD-102"]
    request = {"control_key": "RHCLOUD-101", "ticket_key": "RHCLOUD-102", "repos": ["rbac-config"]}
    child["fields"]["assignee"] = {"name": "someone"}
    with pytest.raises(ValueError, match="not unassigned"):
        actions.handoff(request, CONFIG)
    child["fields"]["assignee"] = {"displayName": "Unassigned"}
    child["fields"]["customfield_10020"] = None
    result = actions.handoff(request, CONFIG)
    assert result["ticket_key"] == "RHCLOUD-102"
    assert child["fields"]["assignee"] == {"displayName": "Unassigned"}
    assert any(name == "jira_update_issue" for name, _ in writes)


def test_approved_setup_labels_and_starts_implementation_without_issue_links(jira_store, monkeypatch):
    store, writes = jira_store
    control = store["RHCLOUD-101"]
    control["fields"]["labels"] = ["dev-bot-coordinator", "dev-bot-coordinator:needs-plan"]
    child = store["RHCLOUD-102"]
    child["fields"]["labels"] = []
    child["fields"]["issuelinks"] = []
    later = issue("RHCLOUD-103", labels=[], links=[blocked_by("RHCLOUD-102")])
    store["RHCLOUD-103"] = later
    control["fields"]["comment"]["comments"] = []
    planner_hash = add_planner_comment(control, [child, later], {
        "RHCLOUD-102": {"phase": "phase:01", "repository": "insights-rbac"},
        "RHCLOUD-103": {"phase": "phase:02", "repository": "insights-rbac"},
    }, comment_id="699")
    proposal = {
        "phase_assignments": {"phase:01": ["RHCLOUD-102"], "phase:02": ["RHCLOUD-103"]},
        "ticket_repositories": {"RHCLOUD-102": "insights-rbac", "RHCLOUD-103": "insights-rbac"},
        "planner_comment_id": "699",
        "planner_input_snapshot_hash": planner_hash,
        "ticket_snapshot_hash": coordinator.ticket_plan_digest(
            coordinator.ticket_plan_snapshot([child, later], TEAM_FIELD, CONFIG[2])
        ),
    }
    control["fields"]["comment"]["comments"] = [
        {
            "id": "700",
            "body": "COORDINATOR: Coordinator update - Initial plan #1\n\nReview this setup.\n\n```coordinator-setup\n"
            + json.dumps(proposal)
            + "\n```",
        },
        {"id": "701", "body": "COORDINATOR: I approve this proposal after review."},
    ]
    control["fields"]["comment"]["comments"].insert(0, {
        "id": "699",
        "body": (
            f"{coordinator.PLANNER_HEADING} for RHCLOUD-100\n\n"
            "```planner-coordination\n"
            + json.dumps({
                "input_snapshot_hash": planner_hash,
                "ticket_assignments": {
                    "RHCLOUD-102": {"phase": "phase:01", "repository": "insights-rbac"},
                    "RHCLOUD-103": {"phase": "phase:02", "repository": "insights-rbac"},
                },
            })
            + "\n```\n\nPlanner request comment: 698"
        ),
    })
    monkeypatch.setattr(actions, "review_context", lambda: {
        "review_label": "dev-bot-review",
        "repositories": {"insights-rbac": {"dev-bot-labels": ["repo:insights-rbac", CONFIG[2]]}},
    })
    monkeypatch.setattr(actions, "load_project_repos", lambda: {"insights-rbac": {}})

    result = actions.approve_setup(
        {"control_key": "RHCLOUD-101", "request_comment_id": "701"},
        CONFIG,
    )
    assert result["repositories"] == ["insights-rbac"]
    assert result["phase_labels_updated"] == ["RHCLOUD-102", "RHCLOUD-103"]
    assert result["handed_off"] == ["RHCLOUD-102"]
    assert result["handoff_failures"] == []
    assert result["next_action"] == "report_handoff_results"
    assert "root_links_added" not in result
    assert "repo:insights-rbac" in control["fields"]["labels"]
    assert "dev-bot-coordinator:active" in control["fields"]["labels"]
    assert set(child["fields"]["labels"]) == {"phase:01", "repo:insights-rbac", CONFIG[2]}
    assert later["fields"]["labels"] == ["phase:02"]
    assert not any(name == "jira_create_issue_link" for name, _ in writes)


def test_setup_approval_requires_latest_user_feedback_to_be_in_proposal(jira_store):
    store, writes = jira_store
    control = store["RHCLOUD-101"]
    proposal = {
        "repository": "rbac-config",
        "phase_assignments": {"phase:01": ["RHCLOUD-102"]},
    }
    control["fields"]["comment"]["comments"] = [
        {"id": "710", "body": "COORDINATOR: Coordinator update - Initial plan #1\n```coordinator-setup\n" + json.dumps(proposal) + "\n```"},
        {"id": "711", "body": "COORDINATOR: Please use insights-rbac instead."},
        {"id": "712", "body": "COORDINATOR: Approved"},
    ]
    with pytest.raises(ValueError, match="stale"):
        actions.approve_setup({"control_key": "RHCLOUD-101", "request_comment_id": "712"}, CONFIG)
    assert writes == []


def test_review_label_is_added_to_matching_open_prs_only(jira_store, monkeypatch):
    store, _ = jira_store
    child = store["RHCLOUD-102"]
    child["fields"]["labels"].extend(["repo:rbac-config", CONFIG[2]])
    monkeypatch.setattr(actions, "review_context", lambda: {"review_label": "dev-bot-review", "repositories": {}})
    monkeypatch.setattr(actions, "load_project_repos", lambda: {
        "rbac-config": {"upstream": "https://github.com/project-kessel/rbac-config.git"},
    })
    gh_calls = []

    def run_gh(args):
        gh_calls.append(args)
        if args[:3] == ["pr", "list", "--repo"]:
            return json.dumps([
                {"number": 7, "title": "[RHCLOUD-102] Add permission check", "labels": []},
                {"number": 8, "title": "[RHCLOUD-1020] Similar key", "labels": []},
                {"number": 9, "title": "[RHCLOUD-102] Docs", "labels": [{"name": "dev-bot-review"}]},
            ])
        if args[:2] == ["pr", "view"]:
            return json.dumps({"labels": [{"name": "dev-bot-review"}]})
        return ""

    monkeypatch.setattr(actions, "run_gh", run_gh)
    result = actions.label_review_prs({"control_key": "RHCLOUD-101", "ticket_key": "RHCLOUD-102"}, CONFIG)
    assert result["review_label"] == "dev-bot-review"
    assert [item["number"] for item in result["labeled"]] == [7]
    assert [item["number"] for item in result["already_labeled"]] == [9]
    assert any(call[:2] == ["pr", "edit"] for call in gh_calls)


def test_open_pr_snapshot_detects_pr_only_changes(jira_store, monkeypatch):
    store, _ = jira_store
    child = store["RHCLOUD-102"]
    child["fields"]["labels"].extend(["repo:rbac-config", CONFIG[2]])
    monkeypatch.setattr(coordinator.subprocess, "run", lambda *_args, **_kwargs: types.SimpleNamespace(
        returncode=0,
        stdout=json.dumps([
            {"number": 12, "title": "[RHCLOUD-102] Implement access check", "labels": [{"name": "dev-bot-review"}], "url": "https://github.com/project-kessel/rbac-config/pull/12"},
            {"number": 13, "title": "[RHCLOUD-1020] Similar key", "labels": [], "url": "https://github.com/project-kessel/rbac-config/pull/13"},
        ]),
        stderr="",
    ))
    repos = {"rbac-config": {"upstream": "https://github.com/project-kessel/rbac-config.git"}}
    snapshot, errors = coordinator.open_pr_snapshot([child], CONFIG[2], repos)
    assert errors == {}
    assert [pr["number"] for pr in snapshot["RHCLOUD-102"]] == [12]
    before = coordinator.fingerprint(store["RHCLOUD-100"], store["RHCLOUD-101"], [child], TEAM_FIELD)
    after = coordinator.fingerprint(store["RHCLOUD-100"], store["RHCLOUD-101"], [child], TEAM_FIELD, snapshot)
    assert before != after

def test_subtask_inherits_parent_team_and_refuses_mismatch(jira_store):
    store, _ = jira_store
    parent = store["RHCLOUD-100"]
    parent["fields"]["issuetype"] = {"name": "Story"}
    child_type, additional = actions.creation_fields(store["RHCLOUD-101"], TEAM_FIELD, ["phase:01"])
    assert child_type == "Sub-task"
    assert additional == {"parent": "RHCLOUD-100", "labels": ["phase:01"]}
    parent["fields"][TEAM_FIELD] = {"id": "another-team"}
    with pytest.raises(ValueError, match="inherits Team from parent"):
        actions.creation_fields(store["RHCLOUD-101"], TEAM_FIELD, ["phase:01"])


def test_normal_ticket_creation_requires_explicit_named_proposal(jira_store):
    store, writes = jira_store
    control = store["RHCLOUD-101"]
    control["fields"]["comment"]["comments"] = [
        {"id": "501", "body": "COORDINATOR: Coordinator update - Status update #1\n\nProposal P-1: add a routing test"},
        {"id": "502", "body": "COORDINATOR: Please do not create P-1 yet"},
    ]
    request = {
        "control_key": "RHCLOUD-101", "request_comment_id": "502", "proposal_id": "P-1",
        "phase": "01", "summary": "Add routing test", "description": "Verify route selection",
    }
    with pytest.raises(ValueError, match="explicit user request"):
        actions.create_ticket(request, CONFIG)
    assert not any(name == "jira_create_issue" for name, _ in writes)

    control["fields"]["comment"]["comments"][-1]["body"] = "COORDINATOR: /coord create P-1"
    created = actions.create_ticket(request, CONFIG)
    assert created["ticket_key"] == "RHCLOUD-105"
    assert store["RHCLOUD-105"]["fields"]["labels"] == ["phase:01"]


def test_preflight_bootstrap_then_skips_unchanged_and_wakes_on_completion(monkeypatch):
    control = issue("RHCLOUD-101", labels=["dev-bot-coordinator", "repo:rbac-config"])
    first = issue("RHCLOUD-102", labels=["phase:01"], links=[relates("RHCLOUD-101")])
    parent = issue("RHCLOUD-100", parent=None)
    outputs = []
    tasks = {}

    monkeypatch.setattr(coordinator, "search_all", lambda jql, _fields="*all": [control] if "labels =" in jql else [control, first])
    monkeypatch.setattr(coordinator, "get_issue", lambda key: {"RHCLOUD-101": control, "RHCLOUD-100": parent}[key])
    monkeypatch.setattr(coordinator, "load_tasks", lambda: tasks)
    monkeypatch.setattr(coordinator, "load_project_repos", lambda: {"rbac-config": {}})
    monkeypatch.setattr(coordinator, "output_result", lambda status, content: outputs.append((status, content)))
    monkeypatch.setattr(coordinator, "jira_cleanup", lambda: None)
    monkeypatch.setenv("BOT_JIRA_PROJECT", "RHCLOUD")
    monkeypatch.setenv("BOT_LABEL", CONFIG[0])
    monkeypatch.setenv("BOT_TEAM_FIELD_ID", TEAM_FIELD)
    monkeypatch.setenv("BOT_IMPLEMENTATION_LABEL", CONFIG[2])

    coordinator.main()
    assert outputs[-1][0] == "start"
    project = json.loads(outputs[-1][1])["projects"][0]
    assert project["first_run"] is True
    tasks["RHCLOUD-101"] = {"external_key": "RHCLOUD-101", "metadata": {"snapshot_hash": project["snapshot_hash"]}}
    control["fields"]["labels"].append("dev-bot-coordinator:active")
    coordinator.main()
    assert outputs[-1][0] == "skip"

    first["fields"]["status"]["name"] = "Release Pending"
    coordinator.main()
    assert outputs[-1][0] == "start"


def test_state_label_does_not_wake_preflight():
    control = issue("RHCLOUD-101", labels=["dev-bot-coordinator", "dev-bot-coordinator:needs-plan"])
    parent = issue("RHCLOUD-100", parent=None)
    original = coordinator.fingerprint(parent, control, [], TEAM_FIELD)
    control["fields"]["labels"][-1] = "dev-bot-coordinator:blocked"
    assert coordinator.fingerprint(parent, control, [], TEAM_FIELD) == original


def test_truncated_control_comments_fail_closed():
    control = issue("RHCLOUD-101", labels=["dev-bot-coordinator"])
    control["fields"]["comment"] = {"total": 101, "comments": [{"id": "1", "body": "COORDINATOR: /coord status"}]}
    with pytest.raises(RuntimeError, match="only 1 of 101 comments"):
        coordinator.comments(control)


def test_coordinator_comment_appends_dynamic_next_steps_footer(jira_store):
    _, writes = jira_store
    result = actions.comment_control(
        {
            "control_key": "RHCLOUD-101",
            "action_id": "status-1",
            "body": "Phase 1 has two runnable tickets.",
            "next_steps": [
                "Confirm the insights-rbac repo choice.",
                "Add the missing assignee before handoff.",
            ],
        },
        CONFIG,
    )
    comment = next(args["comment"] for name, args in writes if name == "jira_add_comment")
    assert result["commented"] is True
    assert "### Possible next steps" in comment
    assert "- Confirm the insights-rbac repo choice." in comment
    assert "- Add the missing assignee before handoff." in comment


def test_coordinator_comment_requires_footer_suggestions(jira_store):
    _, writes = jira_store
    with pytest.raises(ValueError, match="next_steps"):
        actions.comment_control(
            {"control_key": "RHCLOUD-101", "action_id": "status-2", "body": "Status update."},
            CONFIG,
        )
    assert not any(name == "jira_add_comment" for name, _ in writes)


def test_fabric_setting_prefers_sandbox_name(monkeypatch):
    monkeypatch.setenv("FABRIC_TEAM_FIELD_ID", TEAM_FIELD)
    monkeypatch.setenv("BOT_TEAM_FIELD_ID", "customfield_999999")
    assert coordinator.fabric_setting("TEAM_FIELD_ID") == TEAM_FIELD


def test_fabric_setting_falls_back_to_legacy_bot_name(monkeypatch):
    monkeypatch.delenv("REHOR_INPUT_SOURCE", raising=False)
    monkeypatch.delenv("FABRIC_TEAM_FIELD_ID", raising=False)
    monkeypatch.setenv("BOT_TEAM_FIELD_ID", TEAM_FIELD)
    assert coordinator.fabric_setting("TEAM_FIELD_ID") == TEAM_FIELD


def test_fabric_setting_empty_when_neither_name_is_set(monkeypatch):
    monkeypatch.delenv("FABRIC_TEAM_FIELD_ID", raising=False)
    monkeypatch.delenv("BOT_TEAM_FIELD_ID", raising=False)
    assert coordinator.fabric_setting("TEAM_FIELD_ID") == ""


def test_fabric_setting_jira_project_prefers_sandbox_name(monkeypatch):
    monkeypatch.setenv("FABRIC_JIRA_PROJECT", "SYNTH")
    monkeypatch.setenv("BOT_JIRA_PROJECT", "LEGACY")
    assert coordinator.fabric_setting("JIRA_PROJECT") == "SYNTH"
    monkeypatch.delenv("FABRIC_JIRA_PROJECT", raising=False)
    assert coordinator.fabric_setting("JIRA_PROJECT") == "LEGACY"


def test_require_config_reads_fabric_team_field_id(monkeypatch):
    monkeypatch.setenv("BOT_LABEL", "dev-bot-coordinator")
    monkeypatch.setenv("BOT_IMPLEMENTATION_LABEL", "hcc-ai-platform-accessmanagement")
    monkeypatch.setenv("FABRIC_TEAM_FIELD_ID", TEAM_FIELD)
    monkeypatch.delenv("BOT_TEAM_FIELD_ID", raising=False)
    assert actions.require_config() == CONFIG


def test_require_config_keeps_legacy_bot_team_field_id(monkeypatch):
    monkeypatch.setenv("BOT_LABEL", "dev-bot-coordinator")
    monkeypatch.setenv("BOT_IMPLEMENTATION_LABEL", "hcc-ai-platform-accessmanagement")
    monkeypatch.delenv("FABRIC_TEAM_FIELD_ID", raising=False)
    monkeypatch.setenv("BOT_TEAM_FIELD_ID", TEAM_FIELD)
    assert actions.require_config() == CONFIG


def test_require_config_rejects_missing_team_field(monkeypatch):
    monkeypatch.setenv("BOT_LABEL", "dev-bot-coordinator")
    monkeypatch.setenv("BOT_IMPLEMENTATION_LABEL", "hcc-ai-platform-accessmanagement")
    monkeypatch.delenv("FABRIC_TEAM_FIELD_ID", raising=False)
    monkeypatch.delenv("BOT_TEAM_FIELD_ID", raising=False)
    with pytest.raises(ValueError, match="required"):
        actions.require_config()


def test_fabric_setting_sandbox_requires_fabric_name(monkeypatch):
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    monkeypatch.delenv("FABRIC_TEAM_FIELD_ID", raising=False)
    monkeypatch.setenv("BOT_TEAM_FIELD_ID", TEAM_FIELD)
    with pytest.raises(ValueError, match="FABRIC_TEAM_FIELD_ID"):
        coordinator.fabric_setting("TEAM_FIELD_ID")


def test_fabric_setting_sandbox_uses_fabric_value(monkeypatch):
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    monkeypatch.setenv("FABRIC_TEAM_FIELD_ID", TEAM_FIELD)
    monkeypatch.setenv("BOT_TEAM_FIELD_ID", "customfield_999999")
    assert coordinator.fabric_setting("TEAM_FIELD_ID") == TEAM_FIELD


def test_require_config_blocks_sandbox_without_fabric_team_field(monkeypatch):
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    monkeypatch.setenv("BOT_LABEL", "dev-bot-coordinator")
    monkeypatch.setenv("BOT_IMPLEMENTATION_LABEL", "hcc-ai-platform-accessmanagement")
    monkeypatch.delenv("FABRIC_TEAM_FIELD_ID", raising=False)
    monkeypatch.setenv("BOT_TEAM_FIELD_ID", TEAM_FIELD)
    with pytest.raises(ValueError, match="FABRIC_TEAM_FIELD_ID"):
        actions.require_config()


def test_require_config_accepts_sandbox_fabric_team_field(monkeypatch):
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    monkeypatch.setenv("BOT_LABEL", "dev-bot-coordinator")
    monkeypatch.setenv("BOT_IMPLEMENTATION_LABEL", "hcc-ai-platform-accessmanagement")
    monkeypatch.setenv("FABRIC_TEAM_FIELD_ID", TEAM_FIELD)
    monkeypatch.delenv("BOT_TEAM_FIELD_ID", raising=False)
    assert actions.require_config() == CONFIG


def test_main_reports_sandbox_missing_fabric_settings(monkeypatch):
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    monkeypatch.setenv("BOT_LABEL", "dev-bot-coordinator")
    monkeypatch.setenv("BOT_IMPLEMENTATION_LABEL", "hcc-ai-platform-accessmanagement")
    monkeypatch.setenv("BOT_JIRA_PROJECT", "RHCLOUD")
    monkeypatch.setenv("BOT_TEAM_FIELD_ID", TEAM_FIELD)
    monkeypatch.delenv("FABRIC_JIRA_PROJECT", raising=False)
    monkeypatch.delenv("FABRIC_TEAM_FIELD_ID", raising=False)
    results: list[tuple[str, str]] = []
    monkeypatch.setattr(coordinator, "output_result", lambda kind, message: results.append((kind, message)))
    coordinator.main()
    assert results and results[0][0] == "error"
    assert "FABRIC_" in results[0][1]
