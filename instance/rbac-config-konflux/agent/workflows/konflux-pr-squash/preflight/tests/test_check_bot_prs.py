"""Regression tests for Konflux consolidation preflight candidate selection."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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


@pytest.fixture
def cycle_non_default_base():
    fixture = Path(__file__).parent / "fixtures" / "cycle-non-default-base.json"
    return json.loads(fixture.read_text())


def test_non_major_prs_in_a_repo_dont_block_the_solo_major_pr(cycle_39829):
    """insights-rbac has a 0.x-target-only PR (app-common-python) alongside an
    unrelated unknown-tier PR (django) — neither is actionable, since the
    0.x-target guess has no source version to confirm it against (see
    test_unconfirmed_0x_target_major_is_excluded) and django never resolves
    past "unknown". entitlements-api-go has a confidently-classified major
    (an explicit Go module path bump, v4 -> v7, stated directly in the title)
    alongside an unrelated patch-tier digest bump. The digest bump must not
    block or get folded into the major PR's group — the major PR forms its
    own solo group and the digest bump is simply dropped.
    """
    insights_rbac, entitlements_api_go = cycle_39829["repos"]

    assert check_bot_prs._consolidatable_groups(insights_rbac["prs"]) == []
    assert check_bot_prs._consolidatable_groups(entitlements_api_go["prs"]) == [
        {"ecosystem": "go", "tier": "major", "prs": [entitlements_api_go["prs"][1]]}
    ]


def test_unconfirmed_0x_target_major_is_excluded():
    """A title that only states a 0.x target version, with no source version
    available from a diff, title, or body, is a guess that the bump is
    breaking (per 0.x semver convention) — not a fact. Since major bumps now
    run solo with no batching threshold to absorb a wrong guess, this
    low-confidence case must not be actionable on its own, unlike a diff- or
    version-pair-confirmed major.
    """
    prs = [{"title": "chore(deps): update dependency app-common-python to v0.3.0"}]

    assert check_bot_prs._tier(prs[0]["title"]) == "major_unconfirmed"
    assert check_bot_prs._consolidatable_groups(prs) == []


def test_patch_only_prs_are_excluded():
    """This workflow only consolidates major-tier bumps — patch bumps are
    never grouped, regardless of how many are open."""
    prs = [
        {"title": "chore(deps): update dependency alpha from 1.2.3 to 1.2.4"},
        {"title": "chore(deps): update dependency beta from 2.0.0 to 2.0.1"},
    ]

    groups = check_bot_prs._consolidatable_groups(prs)

    assert groups == []


def test_single_version_titles_with_patch_body_are_excluded():
    """Renovate-style titles ("Update dependency X to vY") only state the
    target version, so title-only classification stalls at "unknown" (see
    cycle-39829). The PR body's changelog table states old -> new, which is
    enough to classify these as patch tier — which this workflow excludes.
    """
    prs = [
        {
            "title": "Update dependency sentry-sdk to v2.69.1",
            "body": (
                "| datasource | package | change |\n"
                "|---|---|---|\n"
                "| pypi | sentry-sdk | `2.69.0` -> `2.69.1` |"
            ),
        },
        {
            "title": "Update dependency djangorestframework to v3.18.1",
            "body": (
                "| datasource | package | change |\n"
                "|---|---|---|\n"
                "| pypi | djangorestframework | `3.18.0` -> `3.18.1` |"
            ),
        },
    ]

    groups = check_bot_prs._consolidatable_groups(prs)

    assert groups == []


def test_single_version_title_without_body_data_stays_unknown():
    prs = [
        {"title": "Update dependency uuid-utils to v1"},
        {"title": "Update dependency mcp to v2.2.0"},
    ]

    assert check_bot_prs._consolidatable_groups(prs) == []


def test_html_table_body_minor_tier_is_excluded():
    """Renovate/Mintmaker sometimes renders the changelog table as raw HTML
    (`<code>1.2.3</code> -&gt; <code>1.4.0</code>`) instead of markdown
    backticks. The tag text between the version and the arrow used to break
    every _BODY_VERSION_PATTERNS regex, silently stalling these at "unknown"
    tier even though the body clearly states old -> new. These resolve to
    minor tier, which this workflow excludes.
    """
    prs = [
        {
            "title": "Update dependency django to v6.1",
            "body": (
                "<table>\n<tr><th>Package</th><th>Change</th></tr>\n"
                "<tr><td>django</td><td><code>6.0.2</code> -&gt; <code>6.1</code></td></tr>\n</table>"
            ),
        },
        {
            "title": "Update dependency djangorestframework to v3.18.1",
            "body": (
                "<table>\n<tr><th>Package</th><th>Change</th></tr>\n"
                "<tr><td>djangorestframework</td><td><code>3.17.0</code> -&gt; <code>3.18.1</code></td></tr>\n</table>"
            ),
        },
    ]

    groups = check_bot_prs._consolidatable_groups(prs)

    assert groups == []


def test_date_suffixed_stub_package_patch_versions_are_excluded():
    """types-* stub packages (types-pyyaml, types-requests, ...) version as
    <upstream-major>.<minor>.<patch>.<YYYYMMDD>. The old _VERSION_TOKEN only
    captured 3 dotted segments, so `6.0.12.20250801` -> `6.0.12.20260906`
    truncated to `6.0.12` for both sides, compared equal, and the bump
    silently stalled at "unknown" tier even with a clean body match. These
    resolve to patch tier, which this workflow excludes.
    """
    prs = [
        {
            "title": "Update dependency types-pyyaml to v6.0.12.20260906",
            "body": "`6.0.12.20250801` -> `6.0.12.20260906`",
        },
        {
            "title": "Update dependency types-requests to v2.32.0.20260901",
            "body": "`2.32.0.20250101` -> `2.32.0.20260901`",
        },
    ]

    groups = check_bot_prs._consolidatable_groups(prs)

    assert groups == []


def test_cycle_39829_emits_start_with_solo_major_groups(cycle_39829, monkeypatch, capsys):
    """Only entitlements-api-go's confidently-classified major (an explicit
    Go module path bump stated in the title) is actionable — insights-rbac's
    only major-looking PR is the low-confidence 0.x-target-only guess, which
    is excluded (see test_unconfirmed_0x_target_major_is_excluded). Since
    major bumps are handled solo (no 2+ threshold), a single confident major
    is enough to start a run.
    """
    repo_by_name = {repo["repo"]: repo for repo in cycle_39829["repos"]}
    repos = {name: {"url": data["bot_url"], "upstream": data["repo"]} for name, data in repo_by_name.items()}

    monkeypatch.setattr(check_bot_prs, "get_tasks", lambda: [])
    monkeypatch.setattr(check_bot_prs, "get_capacity", lambda: (0, 10))
    monkeypatch.setattr(check_bot_prs, "load_project_repos", lambda: repos)
    monkeypatch.setattr(check_bot_prs, "upstream_repo", lambda name: (repos[name]["upstream"], "github"))
    monkeypatch.setattr(check_bot_prs, "has_open_consolidation_pr", lambda repo: False)
    monkeypatch.setattr(check_bot_prs, "find_bot_prs", lambda repo, author: repo_by_name[repo]["prs"])
    monkeypatch.setattr(
        check_bot_prs,
        "get_default_branch",
        lambda repo: repo_by_name[repo]["default_branch"],
        raising=False,
    )
    # No manifest diff is available for this fixture (no real repo checkout) —
    # fall back to title/body classification only, same as the direct
    # _consolidatable_groups tests above.
    monkeypatch.setattr(check_bot_prs, "_diff_versions", lambda repo_nwo, pr_number, ecosystem: None)

    check_bot_prs.main()
    output = json.loads(capsys.readouterr().out.strip())

    assert output["status"] == cycle_39829["expected"]["status"]
    content = json.loads(output["content"])
    repos_in_output = {repo["repo"] for repo in content["repos"]}
    assert repos_in_output == {"RedHatInsights/entitlements-api-go"}
    groups_by_repo = {repo["repo"]: repo["groups"] for repo in content["repos"]}
    assert groups_by_repo["RedHatInsights/entitlements-api-go"] == [
        {"ecosystem": "go", "tier": "major", "pr_count": 1}
    ]


def test_major_pr_targeting_non_default_branch_does_not_start_cycle(
    cycle_non_default_base, monkeypatch, capsys
):
    """A major dependency PR against a maintenance branch is not eligible
    work for this default-branch consolidation workflow.
    """
    repo = cycle_non_default_base["repo"]
    repos = {repo: {"url": "https://example.invalid/bot/repo.git", "upstream": repo}}
    versions = cycle_non_default_base["diff_versions"]
    gh_calls = []

    def fake_gh(command, **kwargs):
        gh_calls.append(command)
        if command[1:3] == ["pr", "list"]:
            return SimpleNamespace(
                returncode=0, stdout=json.dumps(cycle_non_default_base["prs"])
            )
        if command[1:3] == ["repo", "view"]:
            return SimpleNamespace(
                returncode=0, stdout=cycle_non_default_base["default_branch"]
            )
        raise AssertionError(f"Unexpected gh command: {command}")

    monkeypatch.setattr(check_bot_prs, "get_tasks", lambda: [])
    monkeypatch.setattr(check_bot_prs, "get_capacity", lambda: (0, 10))
    monkeypatch.setattr(check_bot_prs, "load_project_repos", lambda: repos)
    monkeypatch.setattr(check_bot_prs, "upstream_repo", lambda name: (repo, "github"))
    monkeypatch.setattr(check_bot_prs, "has_open_consolidation_pr", lambda repo: False)
    monkeypatch.setattr(check_bot_prs.subprocess, "run", fake_gh)
    monkeypatch.setattr(
        check_bot_prs,
        "_diff_versions",
        lambda repo_nwo, pr_number, ecosystem: tuple(versions[str(pr_number)]),
    )

    check_bot_prs.main()
    output = json.loads(capsys.readouterr().out.strip())

    assert output["status"] == cycle_non_default_base["expected_status"]
    pr_list_command = next(
        command for command in gh_calls if command[1:3] == ["pr", "list"]
    )
    requested_fields = pr_list_command[pr_list_command.index("--json") + 1].split(",")
    assert "baseRefName" in requested_fields
    assert any(command[1:3] == ["repo", "view"] for command in gh_calls)


def test_minor_and_patch_never_combine_since_both_are_excluded():
    """A minor bump and a patch bump used to combine into one ecosystem batch
    when both were below the 2+ threshold alone. Now that only major-tier
    bumps are consolidated, neither tier is ever grouped, combined or not.
    """
    prs = [
        {"title": "chore(deps): update dependency django from 6.0.2 to 6.1.0"},
        {"title": "chore(deps): update dependency psycopg2 from 2.9.12 to 2.9.13"},
    ]

    groups = check_bot_prs._consolidatable_groups(prs)

    assert groups == []


def test_major_bumps_never_combine_with_minor_or_patch():
    """A major bump must never get folded into a minor/patch ecosystem batch
    — it needs its own breaking-change investigation (see CLAUDE.md). It
    forms its own solo group; the minor/patch PRs are excluded entirely.
    """
    prs = [
        {"title": "chore(deps): update dependency alpha from 1.9.0 to 2.0.0"},
        {"title": "chore(deps): update dependency beta from 2.0.0 to 2.0.1"},
        {"title": "chore(deps): update dependency gamma from 3.0.0 to 3.0.1"},
    ]

    groups = check_bot_prs._consolidatable_groups(prs)

    assert groups == [{"ecosystem": "python", "tier": "major", "prs": [prs[0]]}]


def test_major_bumps_never_combine_with_each_other():
    """Two major-tier PRs for the same ecosystem must never be batched into
    one consolidated PR, even though they'd have hit the old 2+ threshold —
    each major bump needs its own isolated breaking-change investigation, so
    each gets its own solo group instead.
    """
    prs = [
        {"title": "chore(deps): update dependency alpha from 1.9.0 to 2.0.0"},
        {"title": "chore(deps): update dependency beta from 2.9.0 to 3.0.0"},
    ]

    groups = check_bot_prs._consolidatable_groups(prs)

    assert groups == [
        {"ecosystem": "python", "tier": "major", "prs": [prs[0]]},
        {"ecosystem": "python", "tier": "major", "prs": [prs[1]]},
    ]


def test_diff_versions_resolve_an_otherwise_unknown_tier(monkeypatch):
    """Titles that only state the target version, with no body table either,
    stay "unknown" from text parsing alone. When a repo checkout is
    available, the PR diff's manifest hunk gives the real old -> new versions
    directly and resolves the tier.
    """
    prs = [
        {"number": 3376, "title": "Update dependency uuid-utils to v1"},
        {"number": 3341, "title": "Update dependency app-common-python to v0.3.0"},
    ]
    fake_versions = {3376: ("0.9.0", "1.0.0"), 3341: ("0.2.5", "0.3.0")}
    monkeypatch.setattr(
        check_bot_prs,
        "_diff_versions",
        lambda repo_nwo, pr_number, ecosystem: fake_versions[pr_number],
    )

    groups = check_bot_prs._consolidatable_groups(prs, "org/repo")

    assert groups == [
        {"ecosystem": "python", "tier": "major", "prs": [prs[0]]},
        {"ecosystem": "python", "tier": "major", "prs": [prs[1]]},
    ]
