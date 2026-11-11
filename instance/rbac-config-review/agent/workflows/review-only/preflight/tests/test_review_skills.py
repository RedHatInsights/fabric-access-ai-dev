"""Repository-selected dimensions and per-run recipe skills."""

import base64
import json
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKFLOW_DIR))

import build_context as context  # noqa: E402
import review_skills as skills  # noqa: E402


SHA = "a" * 40


def manifest(tmp_path):
    impact = tmp_path / "impact.json"
    impact.write_text(json.dumps({"changed_files": ["rbac/management/mcp_views.py"]}))
    return {
        "pr": {"number": 42, "repo_key": "insights-rbac", "repo": "project-kessel/insights-rbac",
               "title": "RHCLOUD-42 fix", "body": "Adds permission checks",
               "head_sha": "b" * 40, "base_sha": "c" * 40,
               "checkout_path": "/tmp/pinned-checkout", "diff_path": "/tmp/pinned.diff"},
        "impact_path": str(impact),
        "related": [{"repo": "project-kessel/rbac-config", "sha": "d" * 40,
                     "path": "/tmp/pinned-related"}],
        "docs": [{"url": "https://github.com/org/docs/blob/abc/docs.md", "path": "/tmp/pinned-doc.md"}],
    }


def mock_gh(monkeypatch):
    recipe = {
        "title": "KISS & YAGNI Review",
        "parameters": [{"key": "cloud_id", "default": "example"}],
        "instructions": "Review {{ range }} for {{ ticket }} using {{ cloud_id }}",
        "prompt": "Review PR {{ pr }}",
        "extensions": [{"type": "builtin", "name": "developer"}],
    }
    calls = []

    def fake_gh(endpoint):
        calls.append(endpoint)
        if "/commits/" in endpoint:
            return {"sha": SHA}
        return {"encoding": "base64", "content": base64.b64encode(yaml.safe_dump(recipe).encode()).decode()}

    monkeypatch.setattr(skills, "gh_json", fake_gh)
    return calls


def test_default_dimensions_exclude_the_opt_in_recipe():
    catalog = yaml.safe_load((skills.SKILL_DIR / "dimensions.yaml").read_text())
    entries = {item["name"]: item for item in catalog["dimensions"]}
    default_preference = catalog["config"]["model_routing"]["default"]
    assert default_preference == "terra"
    assert skills.model_route(entries["correctness"], catalog) == {
        "model_preference": "terra", "review_agent": "review-terra",
    }
    assert skills.model_route(entries["code-quality"], catalog) == {
        "model_preference": "luna", "review_agent": "review-luna",
    }
    assert skills.model_route(entries["goose-kiss-review"], catalog) == {
        "model_preference": "terra", "review_agent": "review-terra",
    }
    assert all(item["default"] is True for name, item in entries.items() if name != "goose-kiss-review")
    assert entries["goose-kiss-review"]["default"] is False
    assert entries["goose-kiss-review"]["source"]["path"] == (
        "https://github.com/chmulder-rh/goose-config/blob/master/recipes/kiss-review.yaml"
    )
    selected = skills.select(catalog, ["rbac/management/mcp_views.py"], "high", None, [])
    names = {item["name"] for item in selected}
    assert "goose-kiss-review" not in names
    assert {"correctness", "documentation", "observability"} <= names


def test_high_review_skips_code_only_passes_for_documentation_changes():
    catalog = yaml.safe_load((skills.SKILL_DIR / "dimensions.yaml").read_text())
    selected = skills.select(catalog, ["docs/MCP-operator-guide.md"], "high", None, [])
    names = {item["name"] for item in selected}
    assert names == {"architecture", "documentation", "observability", "security",
                     "api-contract", "cross-repo-dependencies"}
    assert {item["name"] for item in skills.select(catalog, ["README.mdx", "docs/setup.rst"],
                                                    "high", None, [])} == names
    mixed = skills.select(catalog, ["docs/MCP-operator-guide.md", "rbac/management/mcp_views.py"],
                          "high", None, [])
    assert {"correctness", "performance", "integration", "learning-extraction"} <= {
        item["name"] for item in mixed
    }
    focused = skills.select(catalog, ["docs/MCP-operator-guide.md"], "high", ["correctness"], [])
    assert [item["name"] for item in focused] == ["correctness"]


def test_repository_skill_runs_despite_default_and_level_and_receives_pinned_context(tmp_path, monkeypatch):
    calls = mock_gh(monkeypatch)
    source = manifest(tmp_path)
    config = {"repositories": {"insights-rbac": {"skills": ["goose-kiss-review", "db-schema"]}}}
    plan = skills.prepare(source, config, tmp_path / "run", level="light")
    names = [item["name"] for item in plan["passes"]]
    assert names == ["correctness", "security", "db-schema", "goose-kiss-review"]
    assert plan["default_model_preference"] == "terra"
    assert [item["review_agent"] for item in plan["passes"]] == [
        "review-terra", "review-terra", "review-terra", "review-terra",
    ]
    custom = plan["passes"][-1]
    skill = Path(custom["guide_path"])
    assert skill.name == "guide.txt"
    assert "Name: goose-kiss-review" in skill.read_text()
    assert "c" * 40 + "..." + "b" * 40 in skill.read_text()
    assert "RHCLOUD-42" in skill.read_text()
    assert "Review PR 42" in skill.read_text()
    assert custom["source_url"].endswith(f"/blob/{SHA}/recipes/kiss-review.yaml")
    assert all(endpoint.endswith("?ref=" + SHA) for endpoint in calls if "/contents/" in endpoint)
    pinned_context = json.loads(Path(custom["context_path"]).read_text())
    assert pinned_context["related"] == source["related"]
    assert pinned_context["docs"] == source["docs"]
    assert pinned_context["impact_path"] == source["impact_path"]


def test_focus_and_repository_skills_combine_without_duplicates(tmp_path, monkeypatch):
    mock_gh(monkeypatch)
    config = {"repositories": {"insights-rbac": {"skills": ["goose-kiss-review", "correctness"]}}}
    plan = skills.prepare(manifest(tmp_path), config, tmp_path / "run", focus=["goose-kiss-review"])
    assert [item["name"] for item in plan["passes"]] == ["correctness", "goose-kiss-review"]
    other = {"repositories": {"other-repo": {"skills": ["goose-kiss-review"]}}}
    plan = skills.prepare(manifest(tmp_path), other, tmp_path / "other", level="light")
    assert [item["name"] for item in plan["passes"]] == ["correctness", "security"]


def test_context_builder_generates_a_fresh_skill_for_each_review_run(tmp_path, monkeypatch):
    mock_gh(monkeypatch)
    (tmp_path / "project-repos.json").write_text(json.dumps({
        "insights-rbac": {"upstream": "https://github.com/project-kessel/insights-rbac.git"}
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(context, "load_config", lambda: {
        "repositories": {"insights-rbac": {"skills": ["goose-kiss-review"], "format": "custom"}}
    })
    monkeypatch.setattr(context, "update_pr_checkout", lambda *_args: "b" * 40)

    def run(args, **_kwargs):
        if args[:3] == ["gh", "pr", "view"]:
            return json.dumps({"headRefOid": "b" * 40, "baseRefOid": "c" * 40,
                               "title": "RHCLOUD-42 fix", "body": "Adds permission checks"})
        if args[:3] == ["gh", "pr", "diff"]:
            return "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
        raise AssertionError(args)

    monkeypatch.setattr(context, "run", run)
    first = context.build("insights-rbac", 42, "b" * 40, "c" * 40, tmp_path / "snapshots")
    second = context.build("insights-rbac", 42, "b" * 40, "c" * 40, tmp_path / "snapshots")
    first_skill = next(item for item in first["review_plan"]["passes"] if item["name"] == "goose-kiss-review")
    second_skill = next(item for item in second["review_plan"]["passes"] if item["name"] == "goose-kiss-review")
    assert first_skill["guide_path"] != second_skill["guide_path"]
    assert Path(first_skill["guide_path"]).is_file()
    assert first["format"] == "custom"
    assert first["format_skills"] == ["goose-kiss-review"]


def test_unsupported_template_fails_instead_of_silently_misinterpreting_it():
    with pytest.raises(skills.SkillError, match="unsupported template"):
        skills.render_text("Review {{ pr | upper }}", {"pr": "42"})
