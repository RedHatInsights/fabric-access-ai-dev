"""Queue behavior: one PR per cycle, SHA deduplication, and feedback checks."""

import json
import sys
from pathlib import Path

WORKFLOW_DIR = Path(__file__).resolve().parents[2]
ROOT = Path(__file__).resolve()
while not (ROOT / "dev-bot" / "presets" / "shared" / "preflight").is_dir() and ROOT != ROOT.parent:
    ROOT = ROOT.parent
sys.path.insert(0, str(WORKFLOW_DIR))
sys.path.insert(0, str(ROOT / "dev-bot" / "presets" / "shared" / "preflight"))

import review_queue as queue  # noqa: E402
from review_config import github_markdown_url, github_repo_url, load_config  # noqa: E402


REPOS = {
    "insights-rbac": {"upstream": "https://github.com/project-kessel/insights-rbac.git"},
    "app-interface": {"upstream": "https://gitlab.cee.redhat.com/service/app-interface.git"},
}
PR = {
    "number": 42,
    "title": "Fix a permission",
    "url": "https://github.com/project-kessel/insights-rbac/pull/42",
    "headRefOid": "a" * 40,
    "baseRefOid": "b" * 40,
    "headRefName": "fix/permission",
    "isDraft": False,
    "author": {"login": "alice"},
    "updatedAt": "2026-11-05T10:00:00Z",
}


def test_config_accepts_direct_markdown_links_and_known_related_repos():
    config = load_config()
    assert config["review_label"] == "dev-bot-review"
    related = config["repositories"]["insights-rbac"]["related"]
    assert {item["id"] for item in related} >= {"rbac-config", "hbi", "kessel-inventory-api"}
    assert len(config["repositories"]["insights-rbac"]["docs"]) >= 3
    assert github_repo_url(REPOS["insights-rbac"]["upstream"]) == "project-kessel/insights-rbac"


def test_direct_file_url_rejects_repository_root_and_non_markdown():
    import pytest

    with pytest.raises(ValueError):
        github_markdown_url("https://github.com/org/docs")
    with pytest.raises(ValueError):
        github_markdown_url("https://github.com/org/docs/blob/main/notes.txt")
    with pytest.raises(ValueError):
        github_markdown_url("https://evil.example/org/docs/blob/main/notes.md")
    with pytest.raises(ValueError):
        github_markdown_url("https://github.com/org/docs/blob/main/%2e%2e%2fsecret.md")


def test_candidate_state_transitions():
    fresh = queue.candidate(PR, "insights-rbac", "project-kessel/insights-rbac", None)
    assert fresh["kind"] == "review"
    assert fresh["task_key"] == "review-only:github:project-kessel/insights-rbac#42"
    task = {
        "status": "done",
        "metadata": {
            "last_reviewed_sha": PR["headRefOid"],
            "last_reviewed_base_sha": PR["baseRefOid"],
            "last_seen_updated_at": PR["updatedAt"],
        },
    }
    assert queue.candidate(PR, "insights-rbac", "project-kessel/insights-rbac", task) is None
    changed = {**PR, "headRefOid": "c" * 40}
    assert queue.candidate(changed, "insights-rbac", "project-kessel/insights-rbac", task)["kind"] == "review"
    rebased = {**PR, "baseRefOid": "d" * 40}
    assert queue.candidate(rebased, "insights-rbac", "project-kessel/insights-rbac", task)["kind"] == "review"
    feedback = {**PR, "updatedAt": "2026-11-05T11:00:00Z"}
    assert queue.candidate(feedback, "insights-rbac", "project-kessel/insights-rbac", task)["kind"] == "feedback_check"
    active = {**task, "status": "in_progress"}
    assert queue.candidate(PR, "insights-rbac", "project-kessel/insights-rbac", active)["kind"] == "resume"
    paused = {**task, "status": "paused", "metadata": {
        "paused_sha": PR["headRefOid"], "paused_base_sha": PR["baseRefOid"],
    }}
    assert queue.candidate(PR, "insights-rbac", "project-kessel/insights-rbac", paused) is None


def test_queue_skips_draft_and_own_pr(monkeypatch):
    assert queue.candidate({**PR, "isDraft": True}, "insights-rbac", "project-kessel/insights-rbac", None) is None
    monkeypatch.setenv("GH_USER_NAME", "alice")
    assert queue.candidate(PR, "insights-rbac", "project-kessel/insights-rbac", None) is None


def test_main_fails_closed_when_memory_unavailable(monkeypatch):
    results = []
    monkeypatch.setattr(queue, "load_config", lambda: {"review_label": "bot-review"})
    monkeypatch.setattr(queue, "load_project_repos", lambda: REPOS)
    monkeypatch.setattr(queue, "load_tasks", lambda: (_ for _ in ()).throw(RuntimeError("Memory unavailable")))
    monkeypatch.setattr(queue, "output_result", lambda status, content: results.append((status, content)))
    queue.main()
    assert results == [("error", "Memory unavailable")]


def test_task_list_paginates_before_deduplicating(monkeypatch):
    calls = []

    def fake_get(url):
        calls.append(url)
        if "offset=0" in url:
            return {"items": [{"external_key": "review-only:github:org/repo#1", "source_type": "github"}], "total": 2}
        return {"items": [{"external_key": "review-only:github:org/repo#2", "source_type": "github"}], "total": 2}

    monkeypatch.setattr(queue, "http_get", fake_get)
    assert len(queue.load_tasks()) == 2
    assert len(calls) == 2


def test_main_selects_one_github_pr_and_ignores_gitlab(monkeypatch):
    results = []
    calls = []
    monkeypatch.setattr(queue, "load_config", lambda: {"review_label": "bot-review"})
    monkeypatch.setattr(queue, "load_project_repos", lambda: REPOS)
    monkeypatch.setattr(queue, "load_tasks", lambda: [])
    monkeypatch.setattr(queue, "list_prs", lambda repo, label: calls.append((repo, label)) or [PR])
    monkeypatch.setattr(queue, "output_result", lambda status, content: results.append((status, content)))
    queue.main()
    assert calls == [("project-kessel/insights-rbac", "bot-review")]
    assert results[0][0] == "start"
    assert json.loads(results[0][1])["work"]["number"] == 42


def test_main_respects_capacity_for_rereview(monkeypatch):
    results = []
    done = {
        "external_key": "review-only:github:project-kessel/insights-rbac#42",
        "status": "done",
        "metadata": {"last_reviewed_sha": "b" * 40},
    }
    active = [
        {"external_key": f"review-only:github:org/repo#{number}", "status": "in_progress"}
        for number in range(10)
    ]
    monkeypatch.setattr(queue, "load_config", lambda: {"review_label": "bot-review"})
    monkeypatch.setattr(queue, "load_project_repos", lambda: REPOS)
    monkeypatch.setattr(queue, "load_tasks", lambda: active + [done])
    monkeypatch.setattr(queue, "list_prs", lambda *_args: [PR])
    monkeypatch.setattr(queue, "output_result", lambda status, content: results.append((status, content)))
    queue.main()
    assert results[0][0] == "skip"
    assert "capacity" in results[0][1]
