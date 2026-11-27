"""Focused offline checks for the planner request handoff and publishing."""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

PLANNER_DIR = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[6]
ISOLATION_ROOTS = (REPO / "instance", REPO / "dev-bot" / "presets")
TEAM_FIELD = "customfield_12345"
CONTROL_LABEL = "dev-bot-coordinator"
LEGACY_TEAM_FIELD = "customfield_99999"


@pytest.fixture
def planner(monkeypatch):
    """Import the planner workflow against a network-free Jira client.

    The stub replaces jira_mcp only for the import and is restored afterwards.
    Workflow modules loaded during the test (directly or lazily) are removed
    from sys.modules on teardown, so no module object carrying the stub
    callables leaks into other test modules.
    """
    stub = types.ModuleType("jira_mcp")
    stub.jira_call = lambda *_args, **_kwargs: None
    stub.jira_cleanup = lambda: None
    monkeypatch.setitem(sys.modules, "jira_mcp", stub)
    monkeypatch.syspath_prepend(str(REPO / "dev-bot" / "presets" / "shared" / "preflight"))
    monkeypatch.syspath_prepend(str(PLANNER_DIR.parent / "project-coordinator"))
    monkeypatch.syspath_prepend(str(PLANNER_DIR))
    loaded_before = set(sys.modules)
    try:
        yield importlib.import_module("planner")
    finally:
        for name, module in list(sys.modules.items()):
            source = getattr(module, "__file__", None)
            if name in loaded_before or not isinstance(source, str):
                continue
            if any(Path(source).is_relative_to(root) for root in ISOLATION_ROOTS):
                sys.modules.pop(name, None)


@pytest.fixture
def coordinator(planner):
    return importlib.import_module("project_coordinator")


def make_issue(key: str, *, parent: str | None = None, labels=None, team: str | None = "42",
               description: str = "Clear acceptance criteria") -> dict:
    data = {
        "key": key,
        "fields": {
            "summary": key,
            "description": description,
            "status": {"name": "New"},
            "labels": list(labels or []),
            "issuelinks": [],
            "assignee": None,
            "priority": {"name": "P2"},
            "issuetype": {"name": "Task"},
        },
    }
    if team is not None:
        data["fields"][TEAM_FIELD] = {"id": team}
    if parent:
        data["fields"]["parent"] = {"key": parent}
    return data


def settings_path(tmp_path):
    return tmp_path / "RHCLOUD-101-504.json"


def legacy_settings():
    return {"control_label": CONTROL_LABEL, "parent_key": "RHCLOUD-100", "docs_base": "", "docs_glob": "*.md"}


@pytest.fixture
def jira(planner, coordinator, monkeypatch, tmp_path):
    control = make_issue("RHCLOUD-101", parent="RHCLOUD-100", labels=[CONTROL_LABEL])
    control["fields"]["comment"] = {"comments": [
        {"id": "503", "body": "COORDINATOR: earlier status update"},
        {"id": "504", "body": "PLANNER: /coord plan Focus on rollout"},
    ]}
    parent = make_issue("RHCLOUD-100", labels=["repo:rbac-config"], team=None, description="Parent project brief")
    child = make_issue("RHCLOUD-102", parent="RHCLOUD-100", labels=["phase:01"])
    store = {item["key"]: item for item in (control, parent, child)}

    def get_issue(key, *, comments=False, fields="*all"):
        return store[key]

    monkeypatch.setattr(planner, "get_issue", get_issue)
    monkeypatch.setattr(planner, "search_all", lambda jql, *_args, **_kwargs: list(store.values()))
    monkeypatch.setattr(coordinator, "PLANNER_REQUEST_DIR", tmp_path)
    monkeypatch.delenv("REHOR_INPUT_SOURCE", raising=False)
    monkeypatch.setenv("FABRIC_TEAM_FIELD_ID", TEAM_FIELD)
    monkeypatch.setenv("BOT_LABEL", CONTROL_LABEL)
    monkeypatch.setenv("BOT_TEAM_FIELD_ID", TEAM_FIELD)
    monkeypatch.delenv("BOT_PLANNER_DOCS_BASE_URL", raising=False)
    monkeypatch.delenv("BOT_PLANNER_DOCS_GLOB", raising=False)
    return store, tmp_path


def test_sandbox_handoff_builds_request_in_memory(planner, jira, monkeypatch):
    store, tmp_path = jira
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    request = planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")
    assert request["control_key"] == "RHCLOUD-101"
    assert request["comment_id"] == "504"
    assert request["parent"]["key"] == "RHCLOUD-100"
    assert request["focus"] == "Focus on rollout"
    assert request["docs_base"] == ""
    assert request["docs_glob"] == "*.md"
    assert [item["key"] for item in request["implementation_tickets"]] == ["RHCLOUD-102"]
    assert request["ticket_snapshot_hash"]
    assert "rbac-config" in request["available_repositories"]
    # The sandbox handoff is file-free.
    assert not settings_path(tmp_path).exists()
    # A retried build reproduces the same request.
    retry = planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")
    assert retry["request_id"] == request["request_id"]


def test_sandbox_build_requires_parent_even_with_stale_file(planner, jira, monkeypatch):
    store, tmp_path = jira
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    settings_path(tmp_path).write_text(json.dumps({**legacy_settings(), "parent_key": "RHCLOUD-999"}))
    with pytest.raises(RuntimeError, match="parent_key"):
        planner.current_request("RHCLOUD-101", "504")
    # A stale request file never overrides the explicit sandbox handoff.
    request = planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")
    assert request["parent"]["key"] == "RHCLOUD-100"


def test_sandbox_validates_control_and_comment_identifiers(planner, jira, monkeypatch):
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    with pytest.raises(ValueError, match="Invalid control ticket key or planner comment ID"):
        planner.current_request("rhcloud-101", "504", "RHCLOUD-100")
    with pytest.raises(ValueError, match="Invalid control ticket key or planner comment ID"):
        planner.current_request("RHCLOUD-101;DROP", "504", "RHCLOUD-100")
    with pytest.raises(ValueError, match="Invalid control ticket key or planner comment ID"):
        planner.current_request("RHCLOUD-101", "50a", "RHCLOUD-100")
    with pytest.raises(ValueError, match="Invalid control ticket key or planner comment ID"):
        planner.current_request("RHCLOUD-101", "0", "RHCLOUD-100")
    with pytest.raises(ValueError, match="Invalid control ticket key or planner comment ID"):
        planner.current_request("RHCLOUD-101", "../504", "RHCLOUD-100")


def test_handoff_rejects_changed_parent_label_command_and_receipt(planner, coordinator, jira, monkeypatch):
    store, tmp_path = jira
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    control = store["RHCLOUD-101"]
    planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")

    control["fields"]["parent"]["key"] = "RHCLOUD-999"
    with pytest.raises(RuntimeError, match="parent changed after preflight"):
        planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")
    control["fields"]["parent"]["key"] = "RHCLOUD-100"

    control["fields"]["labels"] = []
    with pytest.raises(RuntimeError, match="parent changed after preflight"):
        planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")
    control["fields"]["labels"] = [CONTROL_LABEL]

    control["fields"]["comment"]["comments"][1]["body"] = "PLANNER: /coord status"
    with pytest.raises(RuntimeError, match="removed or changed"):
        planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")
    control["fields"]["comment"]["comments"][1]["body"] = "PLANNER: /coord plan Focus on rollout"

    control["fields"]["comment"]["comments"].append({"id": "505", "body": (
        f"{coordinator.PLANNER_HEADING} for RHCLOUD-100\n\n"
        f"{coordinator.PLANNER_RECEIPT} 504"
    )})
    with pytest.raises(RuntimeError, match="already has a published plan"):
        planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")

    with pytest.raises(ValueError, match="Invalid parent issue key"):
        planner.current_request("RHCLOUD-101", "504", "rhcloud-100")


def test_handoff_settings_come_from_configured_env_only(planner, jira, monkeypatch):
    store, tmp_path = jira
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    monkeypatch.setenv("BOT_PLANNER_DOCS_BASE_URL", "https://gitlab.example.com/team/docs/")
    monkeypatch.setenv("BOT_PLANNER_DOCS_GLOB", "docs/*.md")
    request = planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")
    assert request["docs_base"] == "https://gitlab.example.com/team/docs/"
    assert request["docs_glob"] == "docs/*.md"

    monkeypatch.delenv("BOT_LABEL", raising=False)
    with pytest.raises(RuntimeError, match="BOT_LABEL is required"):
        planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")


def test_team_field_alias_and_fail_closed(planner, jira, monkeypatch):
    store, tmp_path = jira
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    monkeypatch.setenv("FABRIC_TEAM_FIELD_ID", TEAM_FIELD)
    monkeypatch.setenv("BOT_TEAM_FIELD_ID", LEGACY_TEAM_FIELD)
    # The legacy alias points at a field no ticket carries; only the
    # FABRIC_ delivery can select the same-Team implementation tickets.
    request = planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")
    assert [item["key"] for item in request["implementation_tickets"]] == ["RHCLOUD-102"]

    monkeypatch.setenv("FABRIC_TEAM_FIELD_ID", "not-a-customfield")
    with pytest.raises(ValueError, match="customfield_"):
        planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")

    monkeypatch.delenv("FABRIC_TEAM_FIELD_ID", raising=False)
    with pytest.raises(ValueError, match="FABRIC_TEAM_FIELD_ID"):
        planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")

    # Legacy keeps the historical silent skip on a malformed field value.
    monkeypatch.delenv("REHOR_INPUT_SOURCE", raising=False)
    settings_path(tmp_path).write_text(json.dumps(legacy_settings()))
    monkeypatch.setenv("BOT_TEAM_FIELD_ID", "not-a-customfield")
    request = planner.current_request("RHCLOUD-101", "504")
    assert request["implementation_tickets"] == []
    assert request["ticket_snapshot_hash"] == ""


def test_legacy_team_field_reads_bot_alias_directly(planner, jira, monkeypatch):
    store, tmp_path = jira
    settings_path(tmp_path).write_text(json.dumps(legacy_settings()))
    # Legacy ignores a FABRIC_ delivery entirely and reads BOT_ directly:
    # FABRIC points at a field no ticket carries, BOT at the real one.
    monkeypatch.setenv("FABRIC_TEAM_FIELD_ID", LEGACY_TEAM_FIELD)
    request = planner.current_request("RHCLOUD-101", "504")
    assert [item["key"] for item in request["implementation_tickets"]] == ["RHCLOUD-102"]


def test_build_to_publish_continuity(planner, coordinator, jira, monkeypatch, tmp_path):
    store, _ = jira
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    builder = importlib.import_module("build_planner_context")
    monkeypatch.setattr(builder, "update_related_repo", lambda url, destination: "deadbeef")
    monkeypatch.setattr(builder, "fetch_doc", lambda url: ("Related documentation text.", "https://example.com/doc"))

    def load_jira_document(key):
        raise AssertionError("no Jira document should be loaded for this request")

    request = planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")
    built = builder.build_context(
        request, root=tmp_path / "planner-context", load_jira_document=load_jira_document,
    )
    assert built["status"] == "context_ready"
    manifest_path = Path(built["manifest"])
    manifest = json.loads(manifest_path.read_text())
    assert manifest["parent"]["key"] == "RHCLOUD-100"
    assert manifest["request_id"] == request["request_id"]

    plan = (
        "# Technical plan\n\nDo the work.\n\n```planner-coordination\n"
        + json.dumps({
            "input_snapshot_hash": manifest["ticket_snapshot_hash"],
            "ticket_assignments": {"RHCLOUD-102": {"phase": "phase:01", "repository": "rbac-config"}},
        })
        + "\n```\n"
    )
    manifest_path.with_name("plan.md").write_text(plan)

    posted = []
    monkeypatch.setattr(planner, "call", lambda tool, args: posted.append(args) or {"id": "900"})
    result = planner.publish(manifest_path)
    assert result["status"] == "published"
    assert posted[0]["issue_key"] == "RHCLOUD-101"
    assert "Technical plan for RHCLOUD-100" in posted[0]["comment"]
    assert f"{coordinator.PLANNER_RECEIPT} 504" in posted[0]["comment"]

    # A tampered manifest parent is revalidated against the live control parent.
    manifest["parent"]["key"] = "RHCLOUD-102"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="parent changed after preflight"):
        planner.publish(manifest_path)
    manifest["parent"]["key"] = "RHCLOUD-100"
    manifest_path.write_text(json.dumps(manifest))

    # Environment drift since the build changes the recomputed fingerprint.
    monkeypatch.setenv("BOT_PLANNER_DOCS_GLOB", "docs/*.md")
    with pytest.raises(RuntimeError, match="planner inputs changed"):
        planner.publish(manifest_path)
    monkeypatch.delenv("BOT_PLANNER_DOCS_GLOB", raising=False)

    # A tampered request fingerprint is rejected.
    manifest["request_id"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="planner inputs changed"):
        planner.publish(manifest_path)
    manifest["request_id"] = request["request_id"]

    # So is a live Jira change after the plan was drafted.
    store["RHCLOUD-103"] = make_issue("RHCLOUD-103", parent="RHCLOUD-100", labels=["phase:01"])
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="planner inputs changed"):
        planner.publish(manifest_path)
    del store["RHCLOUD-103"]

    # And a changed live parent.
    store["RHCLOUD-101"]["fields"]["parent"]["key"] = "RHCLOUD-999"
    with pytest.raises(RuntimeError, match="parent changed after preflight"):
        planner.publish(manifest_path)
    store["RHCLOUD-101"]["fields"]["parent"]["key"] = "RHCLOUD-100"

    # The sandbox publish requires the manifest parent key at all.
    manifest.pop("parent")
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest parent key"):
        planner.publish(manifest_path)


def test_legacy_build_to_publish_file_contract(planner, coordinator, jira, monkeypatch, tmp_path):
    store, _ = jira
    # No repo labels and no declarations: the real build reports missing context.
    store["RHCLOUD-100"]["fields"]["labels"] = []
    settings_path(tmp_path).write_text(json.dumps({
        **legacy_settings(), "docs_base": "https://gitlab.example.com/team/docs/",
    }))
    builder = importlib.import_module("build_planner_context")

    request = planner.current_request("RHCLOUD-101", "504")
    assert request["docs_base"] == "https://gitlab.example.com/team/docs/"
    built = builder.build_context(
        request, root=tmp_path / "planner-context", load_jira_document=planner.load_jira_document,
    )
    assert built["status"] == "context_missing"
    manifest_path = Path(built["manifest"])

    posted = []
    monkeypatch.setattr(planner, "call", lambda tool, args: posted.append(args) or {"id": "901"})
    result = planner.publish(manifest_path)
    assert result["status"] == "published"
    assert "Context required for RHCLOUD-100" in posted[0]["comment"]
    assert f"{coordinator.PLANNER_RECEIPT} 504" in posted[0]["comment"]


def test_legacy_file_contract_unchanged_and_cannot_be_bypassed(planner, jira):
    store, tmp_path = jira
    settings = {**legacy_settings(), "docs_base": "https://gitlab.example.com/team/docs/", "docs_glob": "docs/*.md"}
    settings_path(tmp_path).write_text(json.dumps(settings))
    request = planner.current_request("RHCLOUD-101", "504")
    assert request["docs_base"] == "https://gitlab.example.com/team/docs/"
    assert request["docs_glob"] == "docs/*.md"

    # The extra parent argument cannot bypass the legacy file contract.
    with pytest.raises(RuntimeError, match="only valid in the sandbox"):
        planner.current_request("RHCLOUD-101", "504", "RHCLOUD-100")

    # A missing preflight file still fails closed on the read.
    with pytest.raises(FileNotFoundError):
        planner.current_request("RHCLOUD-101", "507")


def test_cli_build_receives_exact_preflight_payload(planner, jira, monkeypatch, capsys):
    store, tmp_path = jira
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    payload = {"planner": {"control_key": "RHCLOUD-101", "comment_id": "504", "parent_key": "RHCLOUD-100"}}
    captured = []
    monkeypatch.setattr(
        planner, "current_request",
        lambda control_key, comment_id, preflight_parent=None: captured.append(
            (control_key, comment_id, preflight_parent)
        ) or {"control_key": control_key, "comment_id": comment_id},
    )
    stub = types.ModuleType("build_planner_context")
    stub.build_context = lambda request, load_jira_document=None: {
        "status": "context_ready", "manifest": "m.json", "warnings": [],
    }
    monkeypatch.setitem(sys.modules, "build_planner_context", stub)

    entry = payload["planner"]
    monkeypatch.setattr(sys, "argv", [
        "planner.py", "build", entry["control_key"], entry["comment_id"], entry["parent_key"],
    ])
    planner.main()
    assert captured == [(entry["control_key"], entry["comment_id"], entry["parent_key"])]
    assert json.loads(capsys.readouterr().out)["status"] == "context_ready"

    # The legacy two-argument invocation still passes no parent key.
    monkeypatch.delenv("REHOR_INPUT_SOURCE", raising=False)
    monkeypatch.setattr(sys, "argv", ["planner.py", "build", "RHCLOUD-101", "504"])
    planner.main()
    assert captured[-1] == ("RHCLOUD-101", "504", None)


def test_cli_build_rejects_wrong_argument_shapes(planner, jira, monkeypatch, capsys):
    store, tmp_path = jira
    monkeypatch.setenv("REHOR_INPUT_SOURCE", "sandbox")
    monkeypatch.setattr(sys, "argv", ["planner.py", "build", "RHCLOUD-101", "504"])
    with pytest.raises(SystemExit):
        planner.main()
    assert "parent_key" in capsys.readouterr().err

    monkeypatch.delenv("REHOR_INPUT_SOURCE", raising=False)
    monkeypatch.setattr(sys, "argv", ["planner.py", "build", "RHCLOUD-101", "504", "RHCLOUD-100"])
    with pytest.raises(SystemExit):
        planner.main()
    assert "only valid in the sandbox" in capsys.readouterr().err
