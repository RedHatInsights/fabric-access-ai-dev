"""Context builder pins sources and refuses a stale PR head."""

import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKFLOW_DIR))

import build_context as context  # noqa: E402


def test_diff_terms_prefers_changed_contract_identifiers():
    patch = (
        "+++ b/permissions.py\n"
        "+permission = 'inventory.read_hosts'\n"
        "-route = '/api/old-hosts'\n"
        "+route = '/api/hosts'\n"
        " unchanged_permission\n"
    )
    terms = context.diff_terms(patch)
    assert "inventory.read_hosts" in terms
    assert "api/old-hosts" in terms
    assert "unchanged_permission" not in terms


def test_fetch_doc_uses_resolved_sha(monkeypatch):
    sha = "a" * 40
    calls = []

    def fake_gh_json(endpoint):
        calls.append(endpoint)
        if "/commits/" in endpoint:
            return {"sha": sha}
        return {"encoding": "base64", "content": base64.b64encode(b"# Guidance\n").decode()}

    monkeypatch.setattr(context, "gh_json", fake_gh_json)
    text, url = context.fetch_doc("https://github.com/org/docs/blob/main/guides/review.md")
    assert text == "# Guidance\n"
    assert url == f"https://github.com/org/docs/blob/{sha}/guides/review.md"
    assert calls[1].endswith(f"guides/review.md?ref={sha}")


def test_find_matches_uses_git_tracked_related_code(tmp_path):
    repo = tmp_path / "related"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "permissions.py").write_text("permission = 'inventory.read_hosts'\n")
    subprocess.run(["git", "-C", str(repo), "add", "permissions.py"], check=True)
    subprocess.run([
        "git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "-c", "commit.gpgsign=false", "commit", "-qm", "seed",
    ], check=True)
    matches = context.find_matches(repo, ["inventory.read_hosts"])
    assert len(matches) == 1
    assert "permissions.py:1:permission" in matches[0]


def test_build_snapshots_related_code_and_any_number_of_docs(tmp_path, monkeypatch):
    sha = "a" * 40
    repo_file = tmp_path / "project-repos.json"
    repo_file.write_text(json.dumps({
        "insights-rbac": {"upstream": "https://github.com/project-kessel/insights-rbac.git"}
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(context, "load_config", lambda: {
        "repositories": {
            "insights-rbac": {
                "related": [{"id": "rbac-config", "url": "https://github.com/project-kessel/rbac-config",
                             "relationship": "Permission configuration"}],
                "docs": [
                    {"id": "first", "url": "https://github.com/org/docs/blob/main/first.md"},
                    {"id": "second", "url": "https://github.com/org/docs/blob/main/second.md"},
                    {"id": "third", "url": "https://github.com/org/docs/blob/main/third.md"},
                ],
            }
        }
    })

    def fake_run(args, **_kwargs):
        if args[:3] == ["gh", "pr", "view"]:
            return json.dumps({"headRefOid": sha, "baseRefOid": "b" * 40, "title": "Permission fix"})
        if args[:3] == ["gh", "pr", "diff"]:
            return "+permission = 'inventory.read_hosts'\n"
        raise AssertionError(args)

    monkeypatch.setattr(context, "run", fake_run)
    monkeypatch.setattr(context, "update_pr_checkout", lambda *_args: sha)
    monkeypatch.setattr(context, "update_related_repo", lambda *_args: "c" * 40)
    monkeypatch.setattr(context, "find_matches", lambda *_args: ["permissions.py:12:inventory.read_hosts"])
    monkeypatch.setattr(context, "fetch_doc", lambda url: ("# Doc\n", url.replace("/main/", f"/{'d' * 40}/")))
    result = context.build("insights-rbac", 42, sha, "b" * 40, tmp_path / "snapshots")
    assert len(result["docs"]) == 3
    assert result["related"][0]["sha"] == "c" * 40
    assert result["related"][0]["matches"] == ["permissions.py:12:inventory.read_hosts"]
    assert result["pr"]["checkout_path"].endswith("project-kessel__insights-rbac--42")
    assert Path(result["pr"]["diff_path"]).read_text().startswith("+permission")
    assert Path(result["manifest_path"]).is_file()


def test_build_rejects_changed_head_before_fetching_context(tmp_path, monkeypatch):
    (tmp_path / "project-repos.json").write_text(json.dumps({
        "insights-rbac": {"upstream": "https://github.com/project-kessel/insights-rbac.git"}
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(context, "load_config", lambda: {"repositories": {}})
    monkeypatch.setattr(context, "run", lambda *_args, **_kwargs: json.dumps({"headRefOid": "b" * 40}))
    with pytest.raises(context.ContextError, match="PR head or base changed"):
        context.build("insights-rbac", 42, "a" * 40, "c" * 40, tmp_path / "snapshots")
    assert not (tmp_path / "snapshots").exists()
