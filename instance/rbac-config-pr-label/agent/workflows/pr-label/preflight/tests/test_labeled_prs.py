"""Tests for PR-label preflight — find open PRs labeled for the bot."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = Path(__file__).resolve()
while not (REPO_ROOT / "dev-bot").is_dir() and REPO_ROOT != REPO_ROOT.parent:
    REPO_ROOT = REPO_ROOT.parent
SHARED_DIR = REPO_ROOT / "dev-bot" / "presets" / "shared" / "preflight"

sys.path.insert(0, str(WORKFLOW_DIR))
sys.path.insert(0, str(SHARED_DIR))

from labeled_prs import (  # noqa: E402
    TASK_KEY_PREFIX,
    github_repos,
    is_tracked,
    main,
    task_key,
)


def test_task_key_format():
    assert task_key("project-kessel/insights-rbac", 42) == "pr-label:project-kessel/insights-rbac#42"


def test_is_tracked_matches_exact_key():
    tasks = [{"external_key": "pr-label:org/repo#7", "status": "pr_open"}]
    assert is_tracked("pr-label:org/repo#7", tasks) is True


def test_is_tracked_ignores_other_keys():
    tasks = [{"external_key": "pr-label:org/repo#8", "status": "pr_open"}]
    assert is_tracked("pr-label:org/repo#7", tasks) is False


def test_github_repos_skips_gitlab():
    repos = {
        "insights-rbac": {
            "url": "https://github.com/bot/insights-rbac.git",
            "upstream": "https://github.com/project-kessel/insights-rbac.git",
        },
        "app-interface": {
            "url": "https://gitlab.cee.redhat.com/bot/app-interface.git",
            "upstream": "https://gitlab.cee.redhat.com/service/app-interface.git",
            "host": "gitlab",
        },
    }

    def fake_upstream(name):
        if name == "insights-rbac":
            return "project-kessel/insights-rbac", "github"
        return "service/app-interface", "gitlab"

    with patch("labeled_prs.upstream_repo", side_effect=fake_upstream):
        result = github_repos(repos)

    assert result == [("insights-rbac", "project-kessel/insights-rbac")]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr("labeled_prs.save_state", lambda x: None)
    monkeypatch.setattr("labeled_prs.BOT_LABEL", "dev-bot")


def _run_main(monkeypatch, *, tasks=None, capacity=(0, 10), repos=None, labeled=None):
    monkeypatch.setattr("labeled_prs.get_tasks", lambda: tasks or [])
    monkeypatch.setattr("labeled_prs.get_capacity", lambda: capacity)
    monkeypatch.setattr("labeled_prs.load_project_repos", lambda: repos if repos is not None else {})
    monkeypatch.setattr(
        "labeled_prs.upstream_repo",
        lambda name: ("project-kessel/insights-rbac", "github") if name == "insights-rbac" else ("", "github"),
    )
    monkeypatch.setattr("labeled_prs.gh_pr_list", lambda *_a, **_k: labeled if labeled is not None else [])


def test_main_skip_no_repos(env, monkeypatch, capsys):
    _run_main(monkeypatch, repos={})
    main()
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "skip"
    assert "No repos" in out["content"]


def test_main_skip_at_capacity(env, monkeypatch, capsys):
    _run_main(
        monkeypatch,
        capacity=(10, 10),
        repos={"insights-rbac": {"upstream": "https://github.com/project-kessel/insights-rbac.git"}},
    )
    main()
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "skip"
    assert "At capacity" in out["content"]


def test_main_skip_no_labeled_prs(env, monkeypatch, capsys):
    _run_main(
        monkeypatch,
        repos={"insights-rbac": {"upstream": "https://github.com/project-kessel/insights-rbac.git"}},
        labeled=[],
    )
    main()
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "skip"
    assert "No open PRs" in out["content"]


def test_main_skip_all_already_tracked(env, monkeypatch, capsys):
    key = task_key("project-kessel/insights-rbac", 12)
    _run_main(
        monkeypatch,
        tasks=[{"external_key": key, "status": "pr_open"}],
        capacity=(1, 10),
        repos={"insights-rbac": {"upstream": "https://github.com/project-kessel/insights-rbac.git"}},
        labeled=[
            {
                "number": 12,
                "title": "Fix thing",
                "url": "https://github.com/project-kessel/insights-rbac/pull/12",
                "headRefName": "fix/thing",
                "author": {"login": "alice"},
                "isCrossRepository": False,
                "maintainerCanModify": True,
            }
        ],
    )
    main()
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "skip"
    assert "already tracked" in out["content"]


def test_main_start_with_new_labeled_pr(env, monkeypatch, capsys):
    _run_main(
        monkeypatch,
        repos={"insights-rbac": {"upstream": "https://github.com/project-kessel/insights-rbac.git"}},
        labeled=[
            {
                "number": 99,
                "title": "Add feature",
                "url": "https://github.com/project-kessel/insights-rbac/pull/99",
                "headRefName": "alice/feature",
                "author": {"login": "alice"},
                "isCrossRepository": True,
                "maintainerCanModify": False,
            }
        ],
    )
    main()
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "start"
    payload = json.loads(out["content"])
    assert payload["label"] == "dev-bot"
    assert payload["prs"][0]["task_key"] == "pr-label:project-kessel/insights-rbac#99"
    assert payload["prs"][0]["can_push"] is False
    assert payload["prs"][0]["is_fork"] is True


def test_task_key_prefix_constant():
    assert TASK_KEY_PREFIX == "pr-label:"
