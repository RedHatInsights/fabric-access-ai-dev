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
CONFIG = ("dev-bot-control", TEAM_FIELD, TEAM_ID, "hcc-ai-platform-accessmanagement")


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
            "customfield_10020": [{"id": 1}],
        },
    }
    if parent:
        data["fields"]["parent"] = {"key": parent}
    return data


def relates(key: str) -> dict:
    return {"type": {"name": "Relates", "inward": "relates to", "outward": "relates to"}, "outwardIssue": {"key": key}}


def blocked_by(key: str) -> dict:
    return {"type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"}, "inwardIssue": {"key": key}}


def test_parallel_first_tickets_and_release_pending_unblock_next_phase():
    control = issue("RHCLOUD-101", labels=["dev-bot-control", "repo:rbac-config"])
    first = issue("RHCLOUD-102", labels=["phase:01"], links=[relates("RHCLOUD-101")])
    parallel = issue("RHCLOUD-103", labels=["phase:01"], links=[relates("RHCLOUD-101")])
    later = issue("RHCLOUD-104", labels=["phase:02"], links=[blocked_by("RHCLOUD-102"), blocked_by("RHCLOUD-103")])

    result = coordinator.evaluate(control, [first, parallel, later], {"rbac-config"}, CONFIG[3], False)
    assert result["gaps"] == []
    assert result["current_phase"] == "phase:01"
    assert {item["key"] for item in result["ready"]} == {"RHCLOUD-102", "RHCLOUD-103"}

    first["fields"]["status"]["name"] = "Release Pending"
    parallel["fields"]["status"]["name"] = "Release Pending"
    result = coordinator.evaluate(control, [first, parallel, later], {"rbac-config"}, CONFIG[3], False)
    assert result["current_phase"] == "phase:02"
    assert result["ready"] == [{"key": "RHCLOUD-104", "repo_candidates": ["rbac-config"]}]


def test_control_phase_and_missing_first_link_stop_handoff():
    control = issue("RHCLOUD-101", labels=["dev-bot-control", "repo:rbac-config", "phase:01"])
    first = issue("RHCLOUD-102", labels=["phase:01"])
    result = coordinator.evaluate(control, [first], {"rbac-config"}, CONFIG[3], False)
    assert any("control ticket" in gap.lower() for gap in result["gaps"])
    assert any("must relate" in gap for gap in result["gaps"])
    assert result["ready"] == []


def test_unphased_child_and_later_root_are_plan_gaps():
    control = issue("RHCLOUD-101", labels=["dev-bot-control", "repo:rbac-config"])
    first = issue("RHCLOUD-102", labels=["phase:01"], links=[relates("RHCLOUD-101")])
    unphased = issue("RHCLOUD-103")
    later_root = issue("RHCLOUD-104", labels=["phase:02"])
    result = coordinator.evaluate(control, [first, unphased, later_root], {"rbac-config"}, CONFIG[3], False)
    assert any("RHCLOUD-103 has no phase label" in gap for gap in result["gaps"])
    assert any("RHCLOUD-104 must relate" in gap for gap in result["gaps"])
    assert result["ready"] == []


def test_poc_ticket_cannot_receive_phase_or_implementation_label():
    control = issue("RHCLOUD-101", labels=["dev-bot-control", "repo:rbac-config"])
    first = issue("RHCLOUD-102", labels=["phase:01"], links=[relates("RHCLOUD-101")])
    poc = issue("RHCLOUD-103", labels=["phase:01", CONFIG[3]], summary="[POC 1] Try routing")
    result = coordinator.evaluate(control, [first, poc], {"rbac-config"}, CONFIG[3], False)
    assert any("POC ticket" in gap for gap in result["gaps"])
    assert result["ready"] == []


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
    control = issue("RHCLOUD-101", labels=["dev-bot-control", "repo:rbac-config", "coord:active"])
    first = issue("RHCLOUD-102", labels=["phase:01"], links=[relates("RHCLOUD-101")])
    parent = issue("RHCLOUD-100", parent=None, summary="Parent")
    parent["fields"]["issuetype"] = {"name": "Epic"}
    control["fields"]["comment"] = {"comments": [{"id": "500", "body": "/coord poc 01: Try routing"}]}
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
    monkeypatch.setenv("BOT_INCLUDE_BACKLOG", "false")
    return store, writes


def test_handoff_rechecks_team_and_blocks_poc_mode(jira_store):
    store, writes = jira_store
    result = actions.handoff({"control_key": "RHCLOUD-101", "ticket_key": "RHCLOUD-102", "repos": ["rbac-config"]}, CONFIG)
    assert result["ticket_key"] == "RHCLOUD-102"
    assert CONFIG[3] in store["RHCLOUD-102"]["fields"]["labels"]
    assert any(name == "jira_update_issue" for name, _ in writes)

    store["RHCLOUD-102"]["fields"]["labels"] = ["phase:01"]
    store["RHCLOUD-102"]["fields"][TEAM_FIELD] = {"id": "another-team"}
    write_count = len(writes)
    with pytest.raises(ValueError, match="Plan gate failed"):
        actions.handoff({"control_key": "RHCLOUD-101", "ticket_key": "RHCLOUD-102", "repos": ["rbac-config"]}, CONFIG)
    assert len(writes) == write_count

    store["RHCLOUD-102"]["fields"][TEAM_FIELD] = {"id": TEAM_ID}
    store["RHCLOUD-101"]["fields"]["labels"].append("coord-mode:poc")
    with pytest.raises(ValueError, match="POC mode"):
        actions.handoff({"control_key": "RHCLOUD-101", "ticket_key": "RHCLOUD-102", "repos": ["rbac-config"]}, CONFIG)


def test_poc_request_creates_once_and_never_labels_implementation(jira_store):
    store, writes = jira_store
    request = {"control_key": "RHCLOUD-101", "request_comment_id": "500", "phase": "01", "summary": "Try routing", "description": "Explore routing options"}
    created = actions.create_child(request, CONFIG, poc=True)
    assert created == {"ticket_key": "RHCLOUD-105", "created": True, "request_comment_id": "500"}
    new_labels = store["RHCLOUD-105"]["fields"]["labels"]
    assert new_labels == ["coord-poc"]
    assert CONFIG[3] not in store["RHCLOUD-102"]["fields"]["labels"]
    create_calls = [args for name, args in writes if name == "jira_create_issue"]
    assert json.loads(create_calls[0]["additional_fields"])[TEAM_FIELD] == {"id": TEAM_ID}
    assert json.loads(create_calls[0]["additional_fields"])["epicKey"] == "RHCLOUD-100"

    again = actions.create_child(request, CONFIG, poc=True)
    assert again == {"ticket_key": "RHCLOUD-105", "created": False, "request_comment_id": "500"}
    assert len([name for name, _ in writes if name == "jira_create_issue"]) == 1


def test_normal_ticket_creation_requires_explicit_named_proposal(jira_store):
    store, writes = jira_store
    control = store["RHCLOUD-101"]
    control["fields"]["comment"]["comments"] = [
        {"id": "501", "body": "### Coordinator update\n\nProposal P-1: add a routing test"},
        {"id": "502", "body": "Please do not create P-1 yet"},
    ]
    request = {
        "control_key": "RHCLOUD-101", "request_comment_id": "502", "proposal_id": "P-1",
        "phase": "01", "summary": "Add routing test", "description": "Verify route selection",
    }
    with pytest.raises(ValueError, match="explicit user request"):
        actions.create_child(request, CONFIG, poc=False)
    assert not any(name == "jira_create_issue" for name, _ in writes)

    control["fields"]["comment"]["comments"][-1]["body"] = "/coord create P-1"
    created = actions.create_child(request, CONFIG, poc=False)
    assert created["ticket_key"] == "RHCLOUD-105"
    assert store["RHCLOUD-105"]["fields"]["labels"] == ["phase:01"]


def test_preflight_bootstrap_then_skips_unchanged_and_wakes_on_completion(monkeypatch):
    control = issue("RHCLOUD-101", labels=["dev-bot-control", "repo:rbac-config"])
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
    monkeypatch.setenv("BOT_TEAM_ID", TEAM_ID)
    monkeypatch.setenv("BOT_IMPLEMENTATION_LABEL", CONFIG[3])
    monkeypatch.setenv("BOT_INCLUDE_BACKLOG", "false")

    coordinator.main()
    assert outputs[-1][0] == "start"
    project = json.loads(outputs[-1][1])["projects"][0]
    assert project["first_run"] is True
    tasks["RHCLOUD-101"] = {"external_key": "RHCLOUD-101", "metadata": {"snapshot_hash": project["snapshot_hash"]}}
    control["fields"]["labels"].append("coord:active")
    coordinator.main()
    assert outputs[-1][0] == "skip"

    first["fields"]["status"]["name"] = "Release Pending"
    coordinator.main()
    assert outputs[-1][0] == "start"


def test_mode_label_wakes_preflight_but_state_label_does_not():
    control = issue("RHCLOUD-101", labels=["dev-bot-control", "coord:needs-plan"])
    parent = issue("RHCLOUD-100", parent=None)
    original = coordinator.fingerprint(parent, control, [], TEAM_FIELD)
    control["fields"]["labels"][-1] = "coord:blocked"
    assert coordinator.fingerprint(parent, control, [], TEAM_FIELD) == original
    control["fields"]["labels"].append("coord-mode:poc")
    assert coordinator.fingerprint(parent, control, [], TEAM_FIELD) != original


def test_truncated_control_comments_fail_closed():
    control = issue("RHCLOUD-101", labels=["dev-bot-control"])
    control["fields"]["comment"] = {"total": 101, "comments": [{"id": "1", "body": "/coord poc 01: test"}]}
    with pytest.raises(RuntimeError, match="only 1 of 101 comments"):
        coordinator.comments(control)
