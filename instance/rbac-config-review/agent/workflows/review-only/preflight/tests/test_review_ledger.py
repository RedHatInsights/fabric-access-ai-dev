"""The persisted findings ledger: carry-forward status, re-verification, and the update counter."""

import sys
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
        "id": ident, "category": "cross_repo",
        "location": {"repo": "org/rbac", "file": "api/permissions.py", "line": 1},
        "claim": "Old permission disappears", "trigger": "HBI sends inventory.read_hosts",
        "impact": "The RBAC check rejects HBI requests", "suggested_fix": "Retain an alias",
        "evidence": {"explanation": "HBI still sends the old permission", "refs": [{
            "repo": "org/hbi", "sha": "c" * 40, "file": "client.py", "line": 1,
        }]},
    }


def accept_one(manifest, report, ident="F1"):
    checked = evidence.validate_findings({"findings": [finding(ident)]}, manifest, report)
    return evidence.confirmed(checked, {"verdicts": [{
        "finding_id": ident, "status": "confirmed",
        "refutation_check": "Checked for a fallback alias", "reason": "No fallback exists",
    }]})


def test_first_cycle_ledger_has_no_carried_entries(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    accepted = accept_one(manifest, report)
    ledger = evidence.apply_ledger_updates([], accepted, [], manifest["pr"]["head_sha"])
    assert len(ledger) == 1
    assert ledger[0]["status"] == "open"
    body = evidence.render_ledger(manifest, ledger, accepted, update_count=1)
    assert "update #1" in body
    assert "None yet." in body
    assert "## New Findings" in body
    assert "Old permission disappears" in body
    assert "fabric-access-review:" + "a" * 40 + ":" + "b" * 40 in body


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
    assert "update #2" in body
    assert "✅ Addressed" in body
    assert "Alias added" in body


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
    assert "⚠️ Still open" in body


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
