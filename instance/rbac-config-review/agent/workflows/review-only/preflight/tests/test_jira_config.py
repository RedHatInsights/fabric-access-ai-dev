"""Jira creation settings: FABRIC_ sandbox names, legacy BOT_ fallback, and
Team-field indirection. All values are synthetic; no real setting is read."""

import sys
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKFLOW_DIR))

from jira_config import resolve  # noqa: E402
from review_config import ReviewConfigError, load_config  # noqa: E402


RBAC_TARGET = "project-kessel/insights-rbac"
CHROME_TARGET = "RedHatInsights/insights-chrome"

FABRIC_ENV = {
    "FABRIC_TEAM_FIELD_ID": "customfield_424242",
    "FABRIC_JIRA_PROJECT_INSIGHTS_RBAC": "SYNTH",
    "FABRIC_JIRA_TEAM_ID_INSIGHTS_RBAC": "team-1",
    "FABRIC_JIRA_TEAM_NAME_INSIGHTS_RBAC": "Synthetic Access",
    "FABRIC_JIRA_LABELS_INSIGHTS_RBAC": '["synthetic-label"]',
}

LEGACY_ENV = {
    "BOT_TEAM_FIELD_ID": "customfield_424242",
    "BOT_JIRA_PROJECT_INSIGHTS_RBAC": "SYNTH",
    "BOT_JIRA_TEAM_ID_INSIGHTS_RBAC": "team-1",
    "BOT_JIRA_TEAM_NAME_INSIGHTS_RBAC": "Synthetic Access",
    "BOT_JIRA_LABELS_INSIGHTS_RBAC": '["synthetic-label"]',
}

CHROME_ENV = {
    "FABRIC_TEAM_FIELD_ID": "customfield_424242",
    "FABRIC_JIRA_PROJECT_INSIGHTS_CHROME": "SYNTH",
    "FABRIC_JIRA_TEAM_ID_INSIGHTS_CHROME": "team-2",
    "FABRIC_JIRA_TEAM_NAME_INSIGHTS_CHROME": "Synthetic Framework",
    "FABRIC_JIRA_COMPONENT_ID_INSIGHTS_CHROME": "12345",
    "FABRIC_JIRA_LABELS_INSIGHTS_CHROME": "[]",
}


def test_config_uses_fabric_references():
    config = load_config()
    repositories = config["repositories"]
    for repo_key in ("insights-rbac", "insights-chrome"):
        jira = repositories[repo_key]["jira"]
        assert jira["team_field_env"] == "${FABRIC_TEAM_FIELD_ID}"
        profiles = jira["profiles"]
        for profile in profiles.values():
            for field in ("project_env", "team_id_env", "team_name_env", "labels_env"):
                assert profile[field].startswith("${FABRIC_")
    assert "${FABRIC_JIRA_COMPONENT_ID_INSIGHTS_CHROME}" in {
        profile.get("component_id_env")
        for profile in repositories["insights-chrome"]["jira"]["profiles"].values()
    }


def test_resolves_sandbox_fabric_names():
    resolved = resolve("insights-rbac", RBAC_TARGET, environ=dict(FABRIC_ENV))
    assert resolved["project"] == "SYNTH"
    assert resolved["team_id"] == "team-1"
    assert resolved["team_name"] == "Synthetic Access"
    assert resolved["labels"] == ["synthetic-label"]
    assert resolved["team_field"] == "customfield_424242"
    assert "component_id" not in resolved


def test_resolves_chrome_component_from_fabric_names():
    resolved = resolve("insights-chrome", CHROME_TARGET, environ=dict(CHROME_ENV))
    assert resolved["component_id"] == "12345"
    assert resolved["team_field"] == "customfield_424242"


def test_legacy_bot_names_still_resolve():
    resolved = resolve("insights-rbac", RBAC_TARGET, environ=dict(LEGACY_ENV))
    assert resolved["project"] == "SYNTH"
    assert resolved["team_id"] == "team-1"
    assert resolved["team_name"] == "Synthetic Access"
    assert resolved["labels"] == ["synthetic-label"]
    assert resolved["team_field"] == "customfield_424242"


def test_fabric_names_win_over_legacy_bot_names():
    env = dict(LEGACY_ENV)
    env.update(FABRIC_ENV)
    env["FABRIC_JIRA_PROJECT_INSIGHTS_RBAC"] = "FABRICWINS"
    resolved = resolve("insights-rbac", RBAC_TARGET, environ=env)
    assert resolved["project"] == "FABRICWINS"


def test_missing_everywhere_fails_closed():
    with pytest.raises(ReviewConfigError):
        resolve("insights-rbac", RBAC_TARGET, environ={})


def test_missing_team_name_fails_closed():
    env = dict(FABRIC_ENV)
    del env["FABRIC_JIRA_TEAM_NAME_INSIGHTS_RBAC"]
    with pytest.raises(ReviewConfigError):
        resolve("insights-rbac", RBAC_TARGET, environ=env)


def test_team_field_falls_back_to_literal_without_env():
    env = dict(FABRIC_ENV)
    del env["FABRIC_TEAM_FIELD_ID"]
    resolved = resolve("insights-rbac", RBAC_TARGET, environ=env)
    assert resolved["team_field"] == "customfield_10001"


def test_team_field_legacy_bot_alias_fallback():
    env = dict(FABRIC_ENV)
    del env["FABRIC_TEAM_FIELD_ID"]
    env["BOT_TEAM_FIELD_ID"] = "customfield_777777"
    resolved = resolve("insights-rbac", RBAC_TARGET, environ=env)
    assert resolved["team_field"] == "customfield_777777"


def test_invalid_team_field_value_fails_closed():
    env = dict(FABRIC_ENV)
    env["FABRIC_TEAM_FIELD_ID"] = "not-a-field"
    with pytest.raises(ReviewConfigError):
        resolve("insights-rbac", RBAC_TARGET, environ=env)


def test_non_numeric_component_fails_closed():
    env = dict(CHROME_ENV)
    env["FABRIC_JIRA_COMPONENT_ID_INSIGHTS_CHROME"] = "not-a-number"
    with pytest.raises(ReviewConfigError):
        resolve("insights-chrome", CHROME_TARGET, environ=env)


def test_invalid_labels_json_fails_closed():
    env = dict(FABRIC_ENV)
    env["FABRIC_JIRA_LABELS_INSIGHTS_RBAC"] = "{not-json"
    with pytest.raises(ReviewConfigError):
        resolve("insights-rbac", RBAC_TARGET, environ=env)


def test_absent_labels_default_to_empty_list():
    env = {name: value for name, value in FABRIC_ENV.items()
           if name != "FABRIC_JIRA_LABELS_INSIGHTS_RBAC"}
    resolved = resolve("insights-rbac", RBAC_TARGET, environ=env)
    assert resolved["labels"] == []


# Sandbox mode (REHOR_INPUT_SOURCE=sandbox): every configured FABRIC_ name is
# required; legacy BOT_ values, defaults, and public literals never stand in.
SANDBOX_MARKER = {"REHOR_INPUT_SOURCE": "sandbox"}


def test_sandbox_resolves_fabric_names():
    resolved = resolve("insights-rbac", RBAC_TARGET, environ={**FABRIC_ENV, **SANDBOX_MARKER})
    assert resolved["project"] == "SYNTH"
    assert resolved["team_field"] == "customfield_424242"
    assert resolved["labels"] == ["synthetic-label"]


def test_sandbox_rejects_legacy_bot_fallback():
    with pytest.raises(ReviewConfigError, match="FABRIC_JIRA_PROJECT_INSIGHTS_RBAC"):
        resolve("insights-rbac", RBAC_TARGET, environ={**LEGACY_ENV, **SANDBOX_MARKER})


def test_sandbox_rejects_missing_setting_despite_legacy_value():
    env = {**FABRIC_ENV, **LEGACY_ENV, **SANDBOX_MARKER}
    del env["FABRIC_JIRA_TEAM_NAME_INSIGHTS_RBAC"]
    with pytest.raises(ReviewConfigError, match="FABRIC_JIRA_TEAM_NAME_INSIGHTS_RBAC"):
        resolve("insights-rbac", RBAC_TARGET, environ=env)


def test_sandbox_rejects_absent_labels_instead_of_defaulting():
    env = {name: value for name, value in FABRIC_ENV.items()
           if name != "FABRIC_JIRA_LABELS_INSIGHTS_RBAC"}
    env.update(SANDBOX_MARKER)
    env["BOT_JIRA_LABELS_INSIGHTS_RBAC"] = "[]"
    with pytest.raises(ReviewConfigError, match="FABRIC_JIRA_LABELS_INSIGHTS_RBAC"):
        resolve("insights-rbac", RBAC_TARGET, environ=env)


def test_sandbox_rejects_missing_team_field_instead_of_literal():
    env = {name: value for name, value in FABRIC_ENV.items()
           if name != "FABRIC_TEAM_FIELD_ID"}
    env.update(SANDBOX_MARKER)
    env["BOT_TEAM_FIELD_ID"] = "customfield_10001"
    with pytest.raises(ReviewConfigError, match="FABRIC_TEAM_FIELD_ID"):
        resolve("insights-rbac", RBAC_TARGET, environ=env)


def test_sandbox_accepts_explicit_empty_labels_list():
    env = dict(FABRIC_ENV)
    env["FABRIC_JIRA_LABELS_INSIGHTS_RBAC"] = "[]"
    env.update(SANDBOX_MARKER)
    resolved = resolve("insights-rbac", RBAC_TARGET, environ=env)
    assert resolved["labels"] == []
