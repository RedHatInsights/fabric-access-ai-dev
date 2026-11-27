"""Trusted Jira creation helper: flow, fail-closed paths, and malicious-echo
guards. All Jira interactions are fakes shaped like the deployed
mcp-atlassian server's simplified output (custom fields as
{"value": <processed>, "name": <FIELD DISPLAY NAME>}, create results wrapped
as {"message", "issue"}, field options as a top-level list); no Vault
access, no live Jira, and every setting value is synthetic."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKFLOW_DIR))

import jira_create  # noqa: E402

RBAC_TARGET = "project-kessel/insights-rbac"
CHROME_TARGET = "RedHatInsights/insights-chrome"
TEAM_FIELD = "customfield_424242"
BROWSE_URL = "https://jira.example.com/browse/SYNTH-123"
REST_URL = "https://jira.example.com/rest/api/2/issue/12345"

FABRIC_ENV = {
    "FABRIC_TEAM_FIELD_ID": TEAM_FIELD,
    "FABRIC_JIRA_PROJECT_INSIGHTS_RBAC": "SYNTH",
    "FABRIC_JIRA_TEAM_ID_INSIGHTS_RBAC": "team-1",
    "FABRIC_JIRA_TEAM_NAME_INSIGHTS_RBAC": "Synthetic Access",
    "FABRIC_JIRA_LABELS_INSIGHTS_RBAC": '["synthetic-label"]',
}

CHROME_ENV = {
    "FABRIC_TEAM_FIELD_ID": TEAM_FIELD,
    "FABRIC_JIRA_PROJECT_INSIGHTS_CHROME": "SYNTH",
    "FABRIC_JIRA_TEAM_ID_INSIGHTS_CHROME": "team-2",
    "FABRIC_JIRA_TEAM_NAME_INSIGHTS_CHROME": "Synthetic Framework",
    "FABRIC_JIRA_COMPONENT_ID_INSIGHTS_CHROME": "12345",
    "FABRIC_JIRA_LABELS_INSIGHTS_CHROME": "[]",
}

# Canary values a malicious or misbehaving shared client might try to echo
# through stderr, exception text, or malformed results.
CANARIES = ("LEAK-SECRET-PROJECT", "LEAK-SECRET-TEAM-ID", "Leaky Secret Team")


class FakeJira:
    """Stand-in for the shared jira_mcp client tuple (call, cleanup)."""

    def __init__(self, responses=None, stderr_text=None, exc=None):
        self.responses = responses or {}
        self.stderr_text = stderr_text or []
        self.exc = exc
        self.calls = []
        self.cleaned_up = False

    def __call__(self, tool, arguments, timeout=30):
        self.calls.append((tool, arguments))
        # Write directly to fd 2, like the real client's stderr, so the
        # helper's fd-level capture is what stands between it and the test.
        for line in self.stderr_text:
            os.write(2, (line + "\n").encode("utf-8"))
        if self.exc is not None:
            raise self.exc
        handler = self.responses.get(tool)
        return handler(self, tool, arguments) if callable(handler) else handler

    def cleanup(self):
        self.cleaned_up = True

    def tools(self):
        return [tool for tool, _ in self.calls]

    def calls_for(self, tool):
        return [arguments for name, arguments in self.calls if name == tool]


def set_env(monkeypatch, env):
    monkeypatch.setattr(os, "environ", dict(env))


def marker_for(repo_key="insights-rbac", pr=42, finding_id="F1",
               target=RBAC_TARGET):
    return jira_create.deterministic_marker(repo_key, pr, finding_id, target)


def write_request(tmp_path, **overrides):
    marker = marker_for(
        repo_key=overrides.pop("repo_key", "insights-rbac"),
        pr=overrides.pop("pr", 42),
        finding_id=overrides.pop("finding_id", "F1"),
        target=overrides.pop("target", RBAC_TARGET),
    )
    payload = {
        "authorization": "cross_repo_followup",
        "requester": "synthetic-member",
        "pr": 42,
        "finding_id": "F1",
        "marker_token": marker,
        "summary": "Synthetic follow-up summary",
        "description": f"Synthetic description containing {marker}.",
        "target_repository": RBAC_TARGET,
    }
    payload.update(overrides)
    path = tmp_path / "request.json"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


def happy_responses():
    """Deployed-server shapes: options as a list, create wrapped, custom
    fields simplified to {"value", "name"} with the id discarded."""
    return {
        "jira_search": {"issues": [], "total": 0},
        "jira_get_field_options": [
            {"id": "team-1", "value": "Synthetic Access"}
        ],
        "jira_create_issue": {
            "message": "Issue created successfully",
            "issue": {
                "key": "SYNTH-123",
                "url": REST_URL,
                "browse_url": BROWSE_URL,
            },
        },
        "jira_get_issue": {
            "key": "SYNTH-123",
            "browse_url": BROWSE_URL,
            TEAM_FIELD: {"value": "Synthetic Access", "name": "Team"},
        },
    }


def run_helper(monkeypatch, tmp_path, env=FABRIC_ENV, client=None, **overrides):
    set_env(monkeypatch, env)
    request = write_request(tmp_path, **overrides)
    return jira_create.run(
        "insights-rbac", RBAC_TARGET, request, client=client
    )


def test_creates_issue_with_resolved_settings(monkeypatch, tmp_path):
    fake = FakeJira(happy_responses())
    result = run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert result == {
        "status": "created",
        "created": True,
        "issue_key": "SYNTH-123",
        "url": BROWSE_URL,
        # The simplified read-back discards the option id, so the recheck
        # verifies at name level (Jira validated the id on the create write).
        "team_verified": "name",
    }
    create_calls = fake.calls_for("jira_create_issue")
    assert len(create_calls) == 1
    args = create_calls[0]
    assert args["project_key"] == "SYNTH"
    assert args["summary"] == "Synthetic follow-up summary"
    assert marker_for() in args["description"]
    assert args["issue_type"] == "Task"
    additional = json.loads(args["additional_fields"])
    assert additional == {
        TEAM_FIELD: {"id": "team-1"},
        "labels": ["synthetic-label"],
    }
    assert fake.cleaned_up


def test_field_options_receive_project_and_issue_type(monkeypatch, tmp_path):
    fake = FakeJira(happy_responses())
    run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    option_calls = fake.calls_for("jira_get_field_options")
    assert len(option_calls) == 1
    # Required on Server/DC, ignored on Cloud: always passed.
    assert option_calls[0] == {
        "field_id": TEAM_FIELD,
        "project_key": "SYNTH",
        "issue_type": "Task",
    }


def test_url_uses_browse_url_not_rest_endpoint(monkeypatch, tmp_path):
    fake = FakeJira(happy_responses())
    result = run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert result["url"] == BROWSE_URL
    assert REST_URL not in json.dumps(result)


def test_creates_chrome_issue_with_component(monkeypatch, tmp_path):
    responses = happy_responses()
    responses["jira_get_field_options"] = [
        {"id": "team-2", "value": "Synthetic Framework"}
    ]
    responses["jira_get_project_components"] = [
        {"id": "12345", "name": "Framework"}
    ]
    responses["jira_get_issue"] = {
        "key": "SYNTH-77",
        # No browse_url from the server: the result must carry url null
        # instead of deriving one from the REST endpoint.
        "url": "https://jira.example.com/rest/api/2/issue/777",
        TEAM_FIELD: {"value": {"id": "team-2", "title": "Synthetic Framework"},
                     "name": "Team"},
    }
    responses["jira_create_issue"] = {
        "message": "Issue created successfully",
        "issue": {"key": "SYNTH-77", "url": REST_URL},
    }
    fake = FakeJira(responses)
    set_env(monkeypatch, CHROME_ENV)
    marker = marker_for(repo_key="insights-chrome", target=CHROME_TARGET)
    request = write_request(
        tmp_path, repo_key="insights-chrome", target=CHROME_TARGET,
        marker_token=marker, target_repository=CHROME_TARGET,
        description=f"Chrome description containing {marker}.",
    )
    result = jira_create.run("insights-chrome", CHROME_TARGET, request,
                             client=(fake, fake.cleanup))
    assert result["issue_key"] == "SYNTH-77"
    assert result["url"] is None
    # A preserved complex (gh.team-style) value keeps the id: id-level check.
    assert result["team_verified"] == "id"
    additional = json.loads(fake.calls_for("jira_create_issue")[0]
                            ["additional_fields"])
    assert additional == {
        TEAM_FIELD: {"id": "team-2"},
        "labels": [],
        "components": [{"id": "12345"}],
    }


def test_simplified_wrapper_field_name_is_not_the_team(monkeypatch, tmp_path):
    # The wrapper's "name" is the FIELD display name ("Team"), not the team;
    # the team name sits in "value" with the option id discarded. The recheck
    # must verify through "value" and must not mismatch on "Team".
    responses = happy_responses()
    responses["jira_get_issue"] = {
        "key": "SYNTH-123",
        TEAM_FIELD: {"value": "Synthetic Access", "name": "Team"},
    }
    fake = FakeJira(responses)
    result = run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert result["team_verified"] == "name"


def test_field_display_name_only_object_fails_closed(monkeypatch, tmp_path):
    # {"name": "Team"} alone cannot be told apart from a team reference name;
    # it must not be matched against the configured team name.
    responses = happy_responses()
    responses["jira_get_issue"] = {
        "key": "SYNTH-123",
        TEAM_FIELD: {"name": "Team"},
    }
    fake = FakeJira(responses)
    with pytest.raises(jira_create.HelperError) as excinfo:
        run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert excinfo.value.status == "post_create_unverified"
    assert excinfo.value.issue_key == "SYNTH-123"


def test_raw_select_shape_on_issue_verifies_by_id(monkeypatch, tmp_path):
    # Legacy/raw shapes keep the option id directly on the value.
    responses = happy_responses()
    responses["jira_get_issue"] = {
        "key": "SYNTH-123",
        "fields": {TEAM_FIELD: {"id": "team-1", "value": "Synthetic Access"}},
    }
    fake = FakeJira(responses)
    result = run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert result["team_verified"] == "id"


def test_existing_marker_returns_existing_issue(monkeypatch, tmp_path):
    marker = marker_for()
    responses = happy_responses()
    responses["jira_search"] = {
        "issues": [
            {"key": "SYNTH-9", "description": f"older issue {marker}",
             "browse_url": "https://jira.example.com/browse/SYNTH-9"},
            {"key": "SYNTH-10", "description": "unrelated"},
        ]
    }
    fake = FakeJira(responses)
    result = run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert result == {
        "status": "exists",
        "created": False,
        "issue_key": "SYNTH-9",
        "url": "https://jira.example.com/browse/SYNTH-9",
    }
    assert "jira_create_issue" not in fake.tools()


def test_marker_phrase_false_positive_does_not_deduplicate(monkeypatch, tmp_path):
    # Token-order false positive: the description shares the marker's tokens
    # but not the exact substring, so creation must proceed.
    responses = happy_responses()
    responses["jira_search"] = {
        "issues": [
            {"key": "SYNTH-11",
             "description": "review fabric access 42 rbac insights F1 project-kessel reversed"},
        ]
    }
    fake = FakeJira(responses)
    result = run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert result["status"] == "created"
    assert "jira_create_issue" in fake.tools()


def test_team_option_unconfirmed_fails_closed_before_create(monkeypatch, tmp_path):
    responses = happy_responses()
    responses["jira_get_field_options"] = [{"id": "other-team", "value": "Other"}]
    responses["jira_search"] = {"issues": [], "total": 0}
    fake = FakeJira(responses)
    with pytest.raises(jira_create.HelperError) as excinfo:
        run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert excinfo.value.status == "team_option_unconfirmed"
    assert "jira_create_issue" not in fake.tools()


def test_team_name_mismatch_on_options_fails_closed(monkeypatch, tmp_path):
    responses = happy_responses()
    responses["jira_get_field_options"] = [
        {"id": "team-1", "value": "Different Team"}
    ]
    fake = FakeJira(responses)
    with pytest.raises(jira_create.HelperError) as excinfo:
        run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert excinfo.value.status == "team_option_unconfirmed"


def test_reference_issue_fallback_confirms_gh_team_field(monkeypatch, tmp_path):
    # gh.team-style field: options cannot be enumerated (empty list), so the
    # helper confirms through the equality JQL plus a read of the reference
    # issue, whose preserved value carries the id.
    responses = happy_responses()
    responses["jira_get_field_options"] = []

    def search(fake, tool, arguments):
        if "cf[" in arguments["jql"]:
            return {"issues": [
                {"key": "SYNTH-50",
                 TEAM_FIELD: {"value": {"id": "team-1"},
                              "name": "Team"}},
            ]}
        return {"issues": [], "total": 0}

    responses["jira_search"] = search
    fake = FakeJira(responses)
    result = run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert result["status"] == "created"


def test_reference_issue_fallback_name_level_confirmation(monkeypatch, tmp_path):
    # Same fallback, but the reference issue's simplified value discarded the
    # id: the JQL equality confirmed the id server-side, and the name-level
    # read-back corroborates it.
    responses = happy_responses()
    responses["jira_get_field_options"] = None

    def search(fake, tool, arguments):
        if "cf[" in arguments["jql"]:
            return {"issues": [
                {"key": "SYNTH-50",
                 TEAM_FIELD: {"value": "Synthetic Access", "name": "Team"}},
            ]}
        return {"issues": [], "total": 0}

    responses["jira_search"] = search
    fake = FakeJira(responses)
    result = run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert result["status"] == "created"


def test_reference_issue_fallback_zero_results_fails_closed(monkeypatch, tmp_path):
    responses = happy_responses()
    responses["jira_get_field_options"] = []
    fake = FakeJira(responses)
    with pytest.raises(jira_create.HelperError) as excinfo:
        run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert excinfo.value.status == "team_option_unconfirmed"
    assert "jira_create_issue" not in fake.tools()


def test_component_missing_fails_closed_before_create(monkeypatch, tmp_path):
    responses = happy_responses()
    responses["jira_get_field_options"] = [
        {"id": "team-2", "value": "Synthetic Framework"}
    ]
    responses["jira_get_project_components"] = [{"id": "999", "name": "Other"}]
    fake = FakeJira(responses)
    set_env(monkeypatch, CHROME_ENV)
    marker = marker_for(repo_key="insights-chrome", target=CHROME_TARGET)
    request = write_request(
        tmp_path, repo_key="insights-chrome", target=CHROME_TARGET,
        marker_token=marker, target_repository=CHROME_TARGET,
        description=f"Chrome description containing {marker}.",
    )
    with pytest.raises(jira_create.HelperError) as excinfo:
        jira_create.run("insights-chrome", CHROME_TARGET, request,
                        client=(fake, fake.cleanup))
    assert excinfo.value.status == "component_missing"
    assert "jira_create_issue" not in fake.tools()


def test_team_mismatch_after_create_reports_key_without_delete(monkeypatch, tmp_path):
    responses = happy_responses()
    responses["jira_get_issue"] = {
        "key": "SYNTH-123",
        TEAM_FIELD: {"value": "Different Team", "name": "Team"},
    }
    fake = FakeJira(responses)
    with pytest.raises(jira_create.HelperError) as excinfo:
        run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert excinfo.value.status == "team_mismatch"
    # The failure carries the created issue key so a retry cannot lose it.
    assert excinfo.value.issue_key == "SYNTH-123"
    assert not any("delete" in tool for tool in fake.tools())


def test_unreadable_post_create_team_fails_closed_with_key(monkeypatch, tmp_path):
    responses = happy_responses()
    responses["jira_get_issue"] = {
        "key": "SYNTH-123",
        TEAM_FIELD: {"value": {"weird": "shape"}, "name": "Team"},
    }
    fake = FakeJira(responses)
    with pytest.raises(jira_create.HelperError) as excinfo:
        run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert excinfo.value.status == "post_create_unverified"
    assert excinfo.value.issue_key == "SYNTH-123"
    assert not any("delete" in tool for tool in fake.tools())


def test_retry_after_post_create_failure_finds_marker_not_duplicate(
        monkeypatch, tmp_path):
    # A created-but-unverified issue must not be duplicated on retry: the
    # marker search finds the issue the first run created.
    marker = marker_for()
    responses = happy_responses()
    responses["jira_get_issue"] = {
        "key": "SYNTH-123",
        TEAM_FIELD: {"weird": "shape"},
    }
    first = FakeJira(responses)
    with pytest.raises(jira_create.HelperError):
        run_helper(monkeypatch, tmp_path, client=(first, first.cleanup))
    assert "jira_create_issue" in first.tools()

    retry_responses = happy_responses()
    retry_responses["jira_search"] = {
        "issues": [
            {"key": "SYNTH-123", "description": f"created issue {marker}"},
        ]
    }
    retry = FakeJira(retry_responses)
    result = run_helper(monkeypatch, tmp_path, client=(retry, retry.cleanup))
    assert result == {
        "status": "exists",
        "created": False,
        "issue_key": "SYNTH-123",
        "url": None,
    }
    assert "jira_create_issue" not in retry.tools()


def test_create_without_key_fails_closed(monkeypatch, tmp_path):
    responses = happy_responses()
    responses["jira_create_issue"] = {
        "message": "Issue created successfully",
        "issue": {"id": "12345"},
    }
    fake = FakeJira(responses)
    with pytest.raises(jira_create.HelperError) as excinfo:
        run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert excinfo.value.status == "create_no_key"


def test_missing_settings_fail_closed_without_client(monkeypatch, tmp_path):
    set_env(monkeypatch, {})
    request = write_request(tmp_path)
    with pytest.raises(jira_create.HelperError) as excinfo:
        jira_create.run("insights-rbac", RBAC_TARGET, request, client=None)
    assert excinfo.value.status == "config_missing"


def test_unconfigured_profile_fails_closed(monkeypatch, tmp_path):
    set_env(monkeypatch, FABRIC_ENV)
    request = write_request(
        tmp_path, target="project-kessel/rbac-config",
        target_repository="project-kessel/rbac-config",
        marker_token=marker_for(target="project-kessel/rbac-config"),
        description=(
            "Synthetic description containing "
            f"{marker_for(target='project-kessel/rbac-config')}."
        ),
    )
    with pytest.raises(jira_create.HelperError) as excinfo:
        jira_create.run("insights-rbac", "project-kessel/rbac-config", request)
    assert excinfo.value.status == "profile_not_configured"


def test_sandbox_requires_fabric_names(monkeypatch, tmp_path):
    env = {
        "BOT_TEAM_FIELD_ID": TEAM_FIELD,
        "BOT_JIRA_PROJECT_INSIGHTS_RBAC": "SYNTH",
        "BOT_JIRA_TEAM_ID_INSIGHTS_RBAC": "team-1",
        "BOT_JIRA_TEAM_NAME_INSIGHTS_RBAC": "Synthetic Access",
        "BOT_JIRA_LABELS_INSIGHTS_RBAC": "[]",
        "REHOR_INPUT_SOURCE": "sandbox",
    }
    set_env(monkeypatch, env)
    request = write_request(tmp_path)
    with pytest.raises(jira_create.HelperError) as excinfo:
        jira_create.run("insights-rbac", RBAC_TARGET, request)
    assert excinfo.value.status == "config_missing"


@pytest.mark.parametrize(
    "overrides",
    [
        {"marker_token": "fabric-access-review-wrong"},
        {"description": "no marker here"},
        {"summary": ""},
        {"pr": 0},
        {"finding_id": "bad id!"},
        {"authorization": "ad-hoc"},
        {"requester": ""},
        {"target_repository": "other/repo"},
    ],
)
def test_invalid_public_inputs_rejected(monkeypatch, tmp_path, overrides):
    set_env(monkeypatch, FABRIC_ENV)
    request = write_request(tmp_path, **overrides)
    with pytest.raises(jira_create.HelperError) as excinfo:
        jira_create.run("insights-rbac", RBAC_TARGET, request)
    assert excinfo.value.status == "invalid_request"


def test_unknown_request_key_rejected(monkeypatch, tmp_path):
    set_env(monkeypatch, FABRIC_ENV)
    request = write_request(tmp_path)
    payload = json.loads(request.read_text())
    payload["extra"] = "field"
    request.write_text(json.dumps(payload))
    with pytest.raises(jira_create.HelperError) as excinfo:
        jira_create.run("insights-rbac", RBAC_TARGET, request)
    assert excinfo.value.status == "invalid_request"


def test_oversized_request_rejected(monkeypatch, tmp_path):
    set_env(monkeypatch, FABRIC_ENV)
    request = write_request(
        tmp_path, description="x" * (jira_create.MAX_REQUEST_BYTES + 1)
    )
    with pytest.raises(jira_create.HelperError) as excinfo:
        jira_create.run("insights-rbac", RBAC_TARGET, request)
    assert excinfo.value.status == "invalid_request"


def test_world_readable_request_rejected(monkeypatch, tmp_path):
    set_env(monkeypatch, FABRIC_ENV)
    request = write_request(tmp_path)
    os.chmod(request, 0o644)
    with pytest.raises(jira_create.HelperError) as excinfo:
        jira_create.run("insights-rbac", RBAC_TARGET, request)
    assert excinfo.value.status == "invalid_request"


def test_symlinked_request_rejected(monkeypatch, tmp_path):
    # O_NOFOLLOW: a request file swapped to a symlink is not opened.
    set_env(monkeypatch, FABRIC_ENV)
    request = write_request(tmp_path)
    link = tmp_path / "request-link.json"
    link.symlink_to(request)
    with pytest.raises(jira_create.HelperError) as excinfo:
        jira_create.run("insights-rbac", RBAC_TARGET, link)
    assert excinfo.value.status == "invalid_request"


def test_malicious_stderr_canaries_never_echoed(monkeypatch, tmp_path):
    fake = FakeJira(
        responses=None,
        stderr_text=[
            f"  ERR MCP jira_search: {canary} exploded" for canary in CANARIES
        ],
    )
    set_env(monkeypatch, FABRIC_ENV)
    request = write_request(tmp_path)
    with pytest.raises(jira_create.HelperError) as excinfo:
        jira_create.run("insights-rbac", RBAC_TARGET, request,
                        client=(fake, fake.cleanup))
    assert excinfo.value.status == "mcp_tool_error"


def test_malicious_stderr_canaries_never_echoed_through_cli(
        monkeypatch, tmp_path, capsys):
    fake = FakeJira(
        responses=None,
        stderr_text=[
            f"  ERR MCP jira_search: {canary} exploded" for canary in CANARIES
        ],
    )
    set_env(monkeypatch, FABRIC_ENV)
    request = write_request(tmp_path)
    monkeypatch.setattr(jira_create, "resolve_client",
                        lambda: (fake, fake.cleanup))
    monkeypatch.setattr(sys, "argv", [
        "jira_create.py", "--repo-key", "insights-rbac",
        "--target-repository", RBAC_TARGET, "--request", str(request),
    ])
    with pytest.raises(SystemExit) as excinfo:
        jira_create.main()
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result == {"status": "mcp_tool_error"}
    for canary in CANARIES:
        assert canary not in captured.out
        assert canary not in captured.err


def test_post_create_failure_cli_output_carries_issue_key(
        monkeypatch, tmp_path, capsys):
    responses = happy_responses()
    responses["jira_get_issue"] = {
        "key": "SYNTH-123",
        TEAM_FIELD: {"value": "Different Team", "name": "Team"},
    }
    fake = FakeJira(responses)
    set_env(monkeypatch, FABRIC_ENV)
    request = write_request(tmp_path)
    monkeypatch.setattr(jira_create, "resolve_client",
                        lambda: (fake, fake.cleanup))
    monkeypatch.setattr(sys, "argv", [
        "jira_create.py", "--repo-key", "insights-rbac",
        "--target-repository", RBAC_TARGET, "--request", str(request),
    ])
    with pytest.raises(SystemExit) as excinfo:
        jira_create.main()
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "status": "team_mismatch", "issue_key": "SYNTH-123",
    }
    for canary in CANARIES:
        assert canary not in captured.out
        assert canary not in captured.err


def test_malicious_exception_text_never_echoed(monkeypatch, tmp_path, capsys):
    fake = FakeJira(exc=RuntimeError(
        f"boom {CANARIES[0]} {CANARIES[1]} {CANARIES[2]}"
    ))
    set_env(monkeypatch, FABRIC_ENV)
    request = write_request(tmp_path)
    monkeypatch.setattr(jira_create, "resolve_client",
                        lambda: (fake, fake.cleanup))
    monkeypatch.setattr(sys, "argv", [
        "jira_create.py", "--repo-key", "insights-rbac",
        "--target-repository", RBAC_TARGET, "--request", str(request),
    ])
    with pytest.raises(SystemExit) as excinfo:
        jira_create.main()
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "internal_error"}
    for canary in CANARIES:
        assert canary not in captured.out
        assert canary not in captured.err


def test_malicious_result_payload_never_echoed(monkeypatch, tmp_path, capsys):
    responses = happy_responses()
    responses["jira_create_issue"] = {
        "message": f"Issue created successfully {CANARIES[0]}",
        "echo": f"{CANARIES[1]} {CANARIES[2]}",
        "issue": {
            "key": "SYNTH-123",
            "url": REST_URL,
            "browse_url": BROWSE_URL,
            "echo": CANARIES[2],
        },
    }
    responses["jira_get_issue"] = {
        "key": "SYNTH-123",
        "browse_url": BROWSE_URL,
        TEAM_FIELD: {"value": "Synthetic Access", "name": "Team"},
        "echo": CANARIES[0],
    }
    fake = FakeJira(responses)
    set_env(monkeypatch, FABRIC_ENV)
    request = write_request(tmp_path)
    monkeypatch.setattr(jira_create, "resolve_client",
                        lambda: (fake, fake.cleanup))
    monkeypatch.setattr(sys, "argv", [
        "jira_create.py", "--repo-key", "insights-rbac",
        "--target-repository", RBAC_TARGET, "--request", str(request),
    ])
    jira_create.main()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "created"
    assert result["issue_key"] == "SYNTH-123"
    assert result["url"] == BROWSE_URL
    for canary in CANARIES:
        assert canary not in captured.out
        assert canary not in captured.err


def test_timeout_stderr_classified_sanitized(monkeypatch, tmp_path):
    fake = FakeJira(responses=None, stderr_text=["  ERR MCP jira_search: timed out"])
    with pytest.raises(jira_create.HelperError) as excinfo:
        run_helper(monkeypatch, tmp_path, client=(fake, fake.cleanup))
    assert excinfo.value.status == "mcp_timeout"


def test_unset_mcp_url_classified_unavailable():
    assert jira_create.classify_stderr("WARN: JIRA_MCP_URL not set") == \
        "mcp_unavailable"


def test_jira_config_cli_no_longer_prints_settings(monkeypatch):
    # The resolver stays callable, but running it as a script must not print
    # resolved settings (gate against reintroducing the transcript leak).
    script = WORKFLOW_DIR / "jira_config.py"
    env = {**os.environ, **FABRIC_ENV}
    for name in list(env):
        if name.startswith("BOT_") or name.startswith("FABRIC_"):
            if name not in FABRIC_ENV:
                del env[name]
    completed = subprocess.run(
        [sys.executable, str(script),
         "--repo-key", "insights-rbac", "--target-repository", RBAC_TARGET],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
