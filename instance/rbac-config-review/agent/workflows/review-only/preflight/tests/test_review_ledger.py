"""The persisted findings ledger: carry-forward status, re-verification, and the update counter."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

WORKFLOW_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WORKFLOW_DIR))

import impact_analysis as impact  # noqa: E402
import review_evidence as evidence  # noqa: E402


PATCH = """diff --git a/api/permissions.py b/api/permissions.py
--- a/api/permissions.py
+++ b/api/permissions.py
@@ -1,2 +1,2 @@
-PERMISSION = "inventory.read_hosts"
+PERMISSION = "inventory.read_systems"
 context = True
"""


def snapshot(tmp_path):
    target = tmp_path / "target"
    related = tmp_path / "related"
    (target / "api").mkdir(parents=True)
    related.mkdir()
    (target / "api" / "permissions.py").write_text('PERMISSION = "inventory.read_systems"\ncontext = True\n')
    (related / "client.py").write_text('permission = "inventory.read_hosts"\n')
    manifest = {
        "pr": {
            "repo": "org/rbac", "number": 7, "head_sha": "a" * 40,
            "base_sha": "b" * 40, "checkout_path": str(target),
            "diff_path": str(tmp_path / "pr.diff"),
        },
        "related": [{
            "id": "hbi", "repo": "org/hbi", "sha": "c" * 40,
            "path": str(related), "matches": ['HEAD:client.py:1:permission = "inventory.read_hosts"'],
        }],
        "docs": [],
    }
    (tmp_path / "pr.diff").write_text(PATCH)
    return manifest


def finding(ident="F1"):
    return {
        "id": ident, "category": "cross_repo", "severity": "blocking",
        "location": {"repo": "org/rbac", "file": "api/permissions.py", "line": 1},
        "claim": "Old permission disappears", "trigger": "HBI sends inventory.read_hosts",
        "impact": "The RBAC check rejects HBI requests", "suggested_fix": "Retain an alias",
        "scenario": ["HBI sends inventory.read_hosts", "RBAC checks the renamed permission"],
        "evidence": {"explanation": "HBI still sends the old permission [R1]", "refs": [{
            "id": "R1", "repo": "org/hbi", "sha": "c" * 40, "file": "client.py", "line": 1,
        }]},
    }


def accept_one(manifest, report, ident="F1"):
    checked = evidence.validate_findings({"findings": [finding(ident)]}, manifest, report)
    return evidence.confirmed(checked, {"verdicts": [{
        "finding_id": ident, "status": "confirmed",
        "refutation_check": "Checked for a fallback alias", "reason": "No fallback exists",
    }]})


def test_standard_review_states_the_originating_dimension(tmp_path):
    manifest = snapshot(tmp_path)
    manifest["review_plan"] = {"passes": [{"name": "cross-repo-dependencies"}]}
    report = impact.make_impact_report(manifest, PATCH)
    with pytest.raises(evidence.EvidenceError, match="source_dimensions"):
        evidence.validate_findings({"findings": [finding()]}, manifest, report)
    checked = evidence.validate_findings({"findings": [
        {**finding(), "source_dimensions": ["cross-repo-dependencies"]}
    ]}, manifest, report)
    text = evidence.render_review(manifest, checked)
    comments = evidence.render_inline_comments(checked)
    assert "cross-repo-dependencies" in text
    assert "**Dimension:** cross-repo-dependencies" in comments[0]["body"]


def test_first_cycle_ledger_has_no_carried_entries(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    accepted = accept_one(manifest, report)
    ledger = evidence.apply_ledger_updates([], accepted, [], manifest["pr"]["head_sha"])
    assert len(ledger) == 1
    assert ledger[0]["status"] == "open"
    body = evidence.render_ledger(manifest, ledger, accepted, update_count=1)
    assert body.startswith("## Review 2\n")
    assert "not recorded in prior review" in body
    assert "| F1 | blocking | not recorded in prior review | open | [Open thread](review-comment:F1) |" in body
    assert "Old permission disappears" not in body
    assert "fabric-access-review:" + "a" * 40 + ":" + "b" * 40 in body


def test_verification_status_records_time_head_and_each_request(tmp_path):
    manifest = snapshot(tmp_path)
    verified_at = datetime(2026, 11, 12, 13, 3, 55, tzinfo=timezone.utc)
    line = evidence.verification_status_line(manifest, [101, 102], verified_at)
    body = evidence.render_ledger(manifest, [], [], update_count=1,
                                  summary="This PR updates permissions.", last_verification=line)
    assert body.startswith("## Review 2\n\nThis PR updates permissions.")
    assert "**Last verification:** 2026-11-12 13:03:55 UTC" in body
    assert f"/commit/{'a' * 40}" in body
    assert "#issuecomment-101" in body
    assert "#issuecomment-102" in body
    with pytest.raises(evidence.EvidenceError, match="unique positive"):
        evidence.verification_status_line(manifest, [101, 101], verified_at)


def test_same_head_rereview_increments_heading_and_retains_finding(tmp_path, monkeypatch, capsys):
    manifest = snapshot(tmp_path)
    impact_path = tmp_path / "impact.json"
    impact_path.write_text(json.dumps(impact.make_impact_report(manifest, PATCH)))
    manifest["impact_path"] = str(impact_path)

    def write(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        return str(path)

    manifest_file = write("manifest.json", manifest)
    findings_file = write("findings.json", {"findings": [finding()]})
    verdicts_file = write("verdicts.json", {"verdicts": [{
        "finding_id": "F1", "status": "confirmed", "refutation_check": "Checked for fallback",
        "reason": "No fallback exists",
    }]})
    first_body, first_ledger = tmp_path / "first-publication.json", tmp_path / "first-ledger.json"
    common = ["review_evidence.py", "--manifest", manifest_file, "--findings", findings_file,
              "--verdicts", verdicts_file]
    monkeypatch.setattr(sys, "argv", common + ["--publication-output", str(first_body), "--ledger-output", str(first_ledger)])
    evidence.main()
    assert json.loads(capsys.readouterr().out)["update_count"] == 0
    assert json.loads(first_body.read_text())["body"].startswith("## Review 1\n")

    findings_file = write("new-findings.json", {"findings": []})
    verdicts_file = write("new-verdicts.json", {"verdicts": []})
    verifications = write("verifications.json", {"verifications": [{
        "finding_id": "F1", "status": "still_open", "explanation": "The alias is still missing",
    }]})
    recheck_verdicts = write("recheck-verdicts.json", {"verdicts": []})
    second_body, second_ledger = tmp_path / "second-publication.json", tmp_path / "second-ledger.json"
    monkeypatch.setattr(sys, "argv", ["review_evidence.py", "--manifest", manifest_file,
                                      "--findings", findings_file, "--verdicts", verdicts_file,
                                      "--publication-output", str(second_body), "--ledger", str(first_ledger),
                                      "--ledger-output", str(second_ledger), "--verifications", verifications,
                                      "--verification-verdicts", recheck_verdicts, "--update-count", "0"])
    evidence.main()
    assert json.loads(capsys.readouterr().out)["update_count"] == 1
    second_text = json.loads(second_body.read_text())["body"]
    assert second_text.startswith("## Review 2\n")
    assert "| F1 | blocking | not recorded in prior review | open | [Open thread](review-comment:F1) |" in second_text
    assert json.loads(second_ledger.read_text())[0]["id"] == "F1"


def test_custom_format_uses_confirmed_findings_and_preserves_skill_layout(tmp_path, monkeypatch, capsys):
    manifest = snapshot(tmp_path)
    impact_path = tmp_path / "impact.json"
    impact_path.write_text(json.dumps(impact.make_impact_report(manifest, PATCH)))
    guide = tmp_path / "SKILL.md"
    guide.write_text("Use the KISS Review heading and identify simplifications.")
    manifest.update({"impact_path": str(impact_path), "format": "custom", "format_skills": ["goose-kiss-review"],
                     "review_plan": {"passes": [{"name": "goose-kiss-review", "guide_path": str(guide)},
                                                  {"name": "correctness", "guide_path": str(guide)}]}})
    report = impact.make_impact_report(manifest, PATCH)
    with pytest.raises(evidence.EvidenceError, match="source_dimensions"):
        evidence.validate_findings({"findings": [finding()]}, manifest, report)
    with pytest.raises(evidence.EvidenceError, match="source_dimensions"):
        evidence.validate_findings({"findings": [{**finding(), "source_dimensions": ["unknown"]}]}, manifest, report)

    def write(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        return str(path)

    manifest_file = write("manifest.json", manifest)
    findings_file = write("findings.json", {"findings": [{**finding("F1"), "source_dimensions": ["goose-kiss-review"]},
                                                        {**finding("F2"), "source_dimensions": ["goose-kiss-review"]}]})
    verdicts_file = write("verdicts.json", {"verdicts": [
        {"finding_id": "F1", "status": "confirmed", "refutation_check": "Checked fallback", "reason": "Missing"},
        {"finding_id": "F2", "status": "refuted", "refutation_check": "Checked caller", "reason": "Safe"},
    ]})
    body, ledger, inline, prepared = (tmp_path / name for name in ("publication.json", "ledger.json",
                                                                    "inline.json", "prepared.json"))
    results = write("skill-results.json", {"results": [{"dimension": "goose-kiss-review", "verdict": "pass",
                      "severity": "info", "findings": [], "summary": "No simplifications found."}]})
    common = ["review_evidence.py", "--manifest", manifest_file, "--findings", findings_file,
              "--verdicts", verdicts_file, "--publication-output", str(body), "--ledger-output", str(ledger),
              "--inline-comments-output", str(inline), "--skill-results", results]
    monkeypatch.setattr(sys, "argv", common[:-2] + ["--prepare-custom", str(prepared)])
    with pytest.raises(SystemExit):
        evidence.main()
    assert "requires --skill-results" in capsys.readouterr().err
    assert not prepared.exists()
    monkeypatch.setattr(sys, "argv", common + ["--prepare-custom", str(prepared)])
    evidence.main()
    capsys.readouterr()
    accepted = json.loads(prepared.read_text())
    assert [item["id"] for item in accepted["findings"]] == ["F1"]
    assert accepted["format_skills"] == [{"name": "goose-kiss-review", "guide_path": str(guide)}]
    assert accepted["skill_results"][0]["verdict"] == "pass"
    assert not body.exists()

    custom_comment = ("🟠 [F1] Old permission disappears\n\n**Severity:** blocking\n\n"
        "**Dimension:** goose-kiss-review\n\n**Location:** `api/permissions.py:1`\n\n"
        "**Trigger:** HBI sends inventory.read_hosts\n\n**Scenario:**\n"
        "1. HBI sends inventory.read_hosts\n2. RBAC checks the renamed permission\n\n"
        "**Impact:** The RBAC check rejects HBI requests\n\n**Evidence:** HBI still sends the old permission [R1]\n\n"
        "**Suggested fix:** Retain an alias")
    custom = {"inline_comments": [{"finding_id": "F1", "skill": "goose-kiss-review", "text": custom_comment}]}
    missing_dimension = {"inline_comments": [{"finding_id": "F1", "skill": "goose-kiss-review",
        "text": custom_comment.replace("**Dimension:** goose-kiss-review", "Dimension: goose-kiss-review")}]}
    with pytest.raises(evidence.EvidenceError, match="state its dimension"):
        evidence.render_custom(manifest, accepted["ledger"], accepted["findings"], 0, missing_dimension)
    formatted = write("custom.json", custom)
    rejected_verdict = write("rejected-format.json", {"status": "refuted", "refutation_check": "Checked claims",
                                                       "reason": "Introduces an unsupported statement"})
    monkeypatch.setattr(sys, "argv", common + ["--custom-format", formatted, "--custom-verdict", rejected_verdict])
    with pytest.raises(SystemExit):
        evidence.main()
    capsys.readouterr()
    assert not body.exists()
    verdict = write("custom-verdict.json", {"status": "confirmed", "refutation_check": "Checked against findings",
        "reason": "The wording matches the confirmed finding", "skills": [{"name": "goose-kiss-review",
        "status": "confirmed", "refutation_check": "Compared the pinned guide and recorded KISS pass",
        "reason": "The inline layout follows the skill guide"}]})
    monkeypatch.setattr(sys, "argv", common + ["--custom-format", formatted, "--custom-verdict", verdict])
    evidence.main()
    capsys.readouterr()
    rendered = json.loads(body.read_text())["body"]
    assert rendered.startswith("## Review 1")
    assert "| F1 | blocking | goose-kiss-review | open | [Open thread](review-comment:F1) |" in rendered
    assert "**Location:**" not in rendered
    posted = json.loads(inline.read_text())
    assert len(posted) == 1

    _, posted = evidence.render_custom(manifest, accepted["ledger"], accepted["findings"], 0, custom)
    assert posted[0]["line"] == 1
    assert posted[0]["body"].startswith("🟠 [F1] Old permission disappears")
    assert "dev-review-bot:fabric-access-finding:F1" in posted[0]["body"]
    custom["inline_comments"][0]["text"] = "🟠 [F1] Keep the alias."
    with pytest.raises(evidence.EvidenceError, match="state its dimension"):
        evidence.render_custom(manifest, accepted["ledger"], accepted["findings"], 0, custom)

    custom["inline_comments"][0]["text"] = custom_comment
    custom["inline_comments"].append({"finding_id": "F2", "skill": "goose-kiss-review", "text": "[F2] Refuted"})
    with pytest.raises(evidence.EvidenceError, match="identify each selected-skill finding"):
        evidence.render_custom(manifest, accepted["ledger"], accepted["findings"], 0, custom)


def test_custom_rereview_keeps_ledger_ids_and_skill_heading(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    ledger = evidence.apply_ledger_updates([], accept_one(manifest, report), [], manifest["pr"]["head_sha"])
    manifest["format_skills"] = ["goose-kiss-review"]
    custom = {"inline_comments": []}
    body, comments = evidence.render_custom(manifest, ledger, [], 1, custom)
    assert body.startswith("## Review 2")
    assert "| F1 | blocking | not recorded in prior review | open | [Open thread](review-comment:F1) |" in body
    assert comments == []


def test_custom_format_requires_each_skill_result_and_independent_format_check(tmp_path):
    guide = tmp_path / "skill.md"
    guide.write_text("Use this skill's headings and verdict.")
    manifest = {"format_skills": ["first", "second"], "review_plan": {"passes": [
        {"name": "first", "guide_path": str(guide)}, {"name": "second", "guide_path": str(guide)}]}}
    result = lambda name: {"dimension": name, "verdict": "pass", "severity": "info",
                           "findings": [], "summary": "No findings."}
    with pytest.raises(evidence.EvidenceError, match="completed result"):
        evidence.custom_skill_results(manifest, {"results": [result("first")]})
    with pytest.raises(evidence.EvidenceError, match="match the formatting skills"):
        evidence.custom_skill_results(manifest, {"results": [result("first"), result("other")]})
    guides, results = evidence.custom_skill_results(manifest, {"results": [result("second"), result("first")]})
    assert [item["name"] for item in guides] == ["first", "second"]
    assert [item["dimension"] for item in results] == ["first", "second"]
    verdict = {"status": "confirmed", "refutation_check": "Checked", "reason": "Matches", "skills": [
        {"name": "first", "status": "confirmed", "refutation_check": "Checked first guide", "reason": "Matches"}]}
    with pytest.raises(evidence.EvidenceError, match="independently confirmed"):
        evidence.validate_format_verdict(verdict, guides)
    verdict["skills"].append({"name": "second", "status": "refuted", "refutation_check": "Checked second guide",
                              "reason": "Wrong heading"})
    with pytest.raises(evidence.EvidenceError, match="independently confirmed"):
        evidence.validate_format_verdict(verdict, guides)
    verdict["skills"][1]["status"] = "confirmed"
    evidence.validate_format_verdict(verdict, guides)


def test_custom_summary_remains_compact_for_carried_findings(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    ledger = evidence.apply_ledger_updates([], accept_one(manifest, report), [], manifest["pr"]["head_sha"])
    ledger[0]["source_dimensions"] = ["correctness"]
    manifest["format_skills"] = ["first", "second"]
    custom = {"inline_comments": []}
    body, comments = evidence.render_custom(manifest, ledger, [], 0, custom)
    assert body.startswith("## Review 1")
    assert "| F1 | blocking | correctness | open | [Open thread](review-comment:F1) |" in body
    assert comments == []


def test_addressed_verification_with_confirmed_verdict_closes_entry(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    ledger = evidence.apply_ledger_updates([], accept_one(manifest, report), [], manifest["pr"]["head_sha"])

    fix_ref = {"repo": "org/rbac", "sha": "a" * 40, "file": "api/permissions.py", "line": 1}
    verifications = evidence.validate_verifications(
        {"verifications": [{"finding_id": "F1", "status": "addressed",
                             "explanation": "Alias added", "refs": [fix_ref]}]},
        ledger, manifest,
    )
    resolved = evidence.confirm_verifications(verifications, {"verdicts": [{
        "finding_id": "F1", "status": "confirmed",
        "refutation_check": "Checked the alias covers the old permission name", "reason": "Alias is present",
    }]})
    updated = evidence.apply_ledger_updates(ledger, [], resolved, "d" * 40)
    assert updated[0]["status"] == "addressed"
    assert updated[0]["status_evidence"]["explanation"] == "Alias added"

    body = evidence.render_ledger(manifest, updated, [], update_count=2)
    assert body.startswith("## Review 3\n")
    assert "| F1 | blocking | not recorded in prior review | addressed | [Open thread](review-comment:F1) |" in body
    assert "Alias added" not in body


def test_addressed_verification_requires_pinned_ref(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    ledger = evidence.apply_ledger_updates([], accept_one(manifest, report), [], manifest["pr"]["head_sha"])
    with pytest.raises(evidence.EvidenceError, match="pinned ref"):
        evidence.validate_verifications(
            {"verifications": [{"finding_id": "F1", "status": "addressed", "explanation": "Alias added"}]},
            ledger, manifest,
        )


def test_refuted_addressed_claim_stays_open(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    ledger = evidence.apply_ledger_updates([], accept_one(manifest, report), [], manifest["pr"]["head_sha"])

    fix_ref = {"repo": "org/rbac", "sha": "a" * 40, "file": "api/permissions.py", "line": 1}
    verifications = evidence.validate_verifications(
        {"verifications": [{"finding_id": "F1", "status": "addressed",
                             "explanation": "Alias added", "refs": [fix_ref]}]},
        ledger, manifest,
    )
    resolved = evidence.confirm_verifications(verifications, {"verdicts": [{
        "finding_id": "F1", "status": "refuted",
        "refutation_check": "Checked the alias", "reason": "The alias only covers a different permission",
    }]})
    updated = evidence.apply_ledger_updates(ledger, [], resolved, "d" * 40)
    assert updated[0]["status"] == "open"
    assert "not confirmed" in updated[0]["status_note"]

    body = evidence.render_ledger(manifest, updated, [], update_count=2)
    assert "| F1 | blocking | not recorded in prior review | open | [Open thread](review-comment:F1) |" in body


def test_still_open_verification_needs_no_verdict(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    ledger = evidence.apply_ledger_updates([], accept_one(manifest, report), [], manifest["pr"]["head_sha"])

    verifications = evidence.validate_verifications(
        {"verifications": [{"finding_id": "F1", "status": "still_open",
                             "explanation": "No change detected in this region"}]},
        ledger, manifest,
    )
    resolved = evidence.confirm_verifications(verifications, {"verdicts": []})
    updated = evidence.apply_ledger_updates(ledger, [], resolved, "d" * 40)
    assert updated[0]["status"] == "open"
    assert updated[0]["status_note"] == "No change detected in this region"


def test_verification_must_cover_every_open_entry(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    ledger = evidence.apply_ledger_updates([], accept_one(manifest, report), [], manifest["pr"]["head_sha"])
    with pytest.raises(evidence.EvidenceError, match="Every open finding needs"):
        evidence.validate_verifications({"verifications": []}, ledger, manifest)


def test_verification_rejects_unknown_finding_id(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    ledger = evidence.apply_ledger_updates([], accept_one(manifest, report), [], manifest["pr"]["head_sha"])
    with pytest.raises(evidence.EvidenceError, match="is not open"):
        evidence.validate_verifications(
            {"verifications": [{"finding_id": "F99", "status": "still_open", "explanation": "n/a"}]},
            ledger, manifest,
        )


def test_addressed_entries_are_not_re_verified(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    ledger = evidence.apply_ledger_updates([], accept_one(manifest, report), [], manifest["pr"]["head_sha"])
    ledger[0]["status"] = "addressed"
    ledger[0]["status_evidence"] = {"explanation": "Fixed already", "refs": []}
    verifications = evidence.validate_verifications({"verifications": []}, ledger, manifest)
    assert verifications == []
