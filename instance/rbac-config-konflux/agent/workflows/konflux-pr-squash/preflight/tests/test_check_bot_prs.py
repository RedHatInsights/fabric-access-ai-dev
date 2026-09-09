"""Regression tests for Konflux consolidation preflight candidate selection."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = Path(__file__).resolve()
while not (REPO_ROOT / "dev-bot").is_dir() and REPO_ROOT.parent != REPO_ROOT:
    REPO_ROOT = REPO_ROOT.parent
SHARED_DIR = REPO_ROOT / "dev-bot" / "presets" / "shared" / "preflight"
sys.path.insert(0, str(WORKFLOW_DIR))
sys.path.insert(0, str(SHARED_DIR))

MODULE_PATH = WORKFLOW_DIR / "preflight" / "02-check-bot-prs.py"
spec = importlib.util.spec_from_file_location("check_bot_prs", MODULE_PATH)
check_bot_prs = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = check_bot_prs
spec.loader.exec_module(check_bot_prs)


@pytest.fixture
def cycle_39829():
    fixture = Path(__file__).parent / "fixtures" / "cycle-39829.json"
    return json.loads(fixture.read_text())


def test_cross_tier_prs_are_not_consolidatable(cycle_39829):
    for repo in cycle_39829["repos"]:
        assert check_bot_prs._consolidatable_groups(repo["prs"]) == []


def test_same_tier_prs_are_consolidatable():
    prs = [
        {"title": "chore(deps): update dependency alpha from 1.2.3 to 1.2.4"},
        {"title": "chore(deps): update dependency beta from 2.0.0 to 2.0.1"},
    ]

    groups = check_bot_prs._consolidatable_groups(prs)

    assert groups == [{"ecosystem": "python", "tier": "patch", "prs": prs}]


def test_single_version_titles_use_body_for_tier():
    """Renovate-style titles ("Update dependency X to vY") only state the
    target version, so title-only classification stalls at "unknown" (see
    cycle-39829). The PR body's changelog table states old -> new, which is
    enough to classify these as the same tier and consolidate them.
    """
    prs = [
        {
            "title": "Update dependency sentry-sdk to v2.69.1",
            "body": "| datasource | package | change |\n|---|---|---|\n| pypi | sentry-sdk | `2.69.0` -> `2.69.1` |",
        },
        {
            "title": "Update dependency djangorestframework to v3.18.1",
            "body": "| datasource | package | change |\n|---|---|---|\n| pypi | djangorestframework | `3.18.0` -> `3.18.1` |",
        },
    ]

    groups = check_bot_prs._consolidatable_groups(prs)

    assert groups == [{"ecosystem": "python", "tier": "patch", "prs": prs}]


def test_single_version_title_without_body_data_stays_unknown():
    prs = [
        {"title": "Update dependency uuid-utils to v1"},
        {"title": "Update dependency mcp to v2.2.0"},
    ]

    assert check_bot_prs._consolidatable_groups(prs) == []


def test_cycle_39829_emits_skip(cycle_39829, monkeypatch, capsys):
    repo_by_name = {repo["repo"]: repo for repo in cycle_39829["repos"]}
    repos = {name: {"url": data["bot_url"], "upstream": data["repo"]} for name, data in repo_by_name.items()}

    monkeypatch.setattr(check_bot_prs, "get_tasks", lambda: [])
    monkeypatch.setattr(check_bot_prs, "get_capacity", lambda: (0, 10))
    monkeypatch.setattr(check_bot_prs, "load_project_repos", lambda: repos)
    monkeypatch.setattr(check_bot_prs, "upstream_repo", lambda name: (repos[name]["upstream"], "github"))
    monkeypatch.setattr(check_bot_prs, "has_open_consolidation_pr", lambda repo: False)
    monkeypatch.setattr(check_bot_prs, "find_bot_prs", lambda repo, author: repo_by_name[repo]["prs"])

    check_bot_prs.main()
    output = json.loads(capsys.readouterr().out.strip())

    assert output["status"] == cycle_39829["expected"]["status"]
    assert "same ecosystem+tier" in output["content"]
