"""Impact candidates and the evidence gate for published review findings."""

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


def finding():
    return {
        "id": "F1", "category": "cross_repo",
        "location": {"repo": "org/rbac", "file": "api/permissions.py", "line": 1},
        "claim": "Old permission disappears", "trigger": "HBI sends inventory.read_hosts",
        "impact": "The RBAC check rejects HBI requests", "suggested_fix": "Retain an alias",
        "evidence": {"explanation": "HBI still sends the old permission", "refs": [{
            "repo": "org/hbi", "sha": "c" * 40, "file": "client.py", "line": 1,
        }]},
    }


def test_impact_reports_changed_identifier_and_concrete_usage(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    assert report["changed_files"] == ["api/permissions.py"]
    assert {item["identifier"] for item in report["changed_identifiers"]} == {
        "inventory.read_hosts", "inventory.read_systems",
    }
    assert report["consumer_usages"][0]["identifier"] == "inventory.read_hosts"
    assert report["consumer_usages"][0]["status"] == "candidate"


def test_evidence_gate_accepts_pinned_related_code_and_confirmed_verdict(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    checked = evidence.validate_findings({"findings": [finding()]}, manifest, report)
    accepted = evidence.confirmed(checked, {"verdicts": [{
        "finding_id": "F1", "status": "confirmed",
        "refutation_check": "Checked for a fallback alias in the RBAC permission path",
        "reason": "No fallback exists at the pinned revision",
    }]})
    body = evidence.render_review(manifest, accepted)
    assert body.startswith("## Review #1\n")
    assert "`/review-bot re-review`" in body
    assert "org/hbi/blob/" + "c" * 40 + "/client.py#L1" in body
    assert "dev-review-bot:fabric-access-review:" + "a" * 40 + ":" + "b" * 40 in body


def test_evidence_gate_rejects_missing_consumer_or_unpinned_source(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    proposed = finding()
    proposed["evidence"]["refs"] = [{
        "repo": "org/rbac", "sha": "a" * 40, "file": "api/permissions.py", "line": 1,
    }]
    with pytest.raises(evidence.EvidenceError, match="related-repository"):
        evidence.validate_findings({"findings": [proposed]}, manifest, report)
    proposed["evidence"]["refs"][0]["sha"] = "d" * 40
    with pytest.raises(evidence.EvidenceError, match="not pinned"):
        evidence.validate_findings({"findings": [proposed]}, manifest, report)


def test_uncertain_verdict_is_not_published(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    checked = evidence.validate_findings({"findings": [finding()]}, manifest, report)
    accepted = evidence.confirmed(checked, {"verdicts": [{
        "finding_id": "F1", "status": "uncertain",
        "refutation_check": "Could not inspect runtime fallback configuration",
        "reason": "The current checkout does not establish the runtime value",
    }]})
    assert accepted == []
    assert "Old permission disappears" not in evidence.render_review(manifest, accepted)


def test_missing_verifier_decision_blocks_review(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    checked = evidence.validate_findings({"findings": [finding()]}, manifest, report)
    with pytest.raises(evidence.EvidenceError, match="exactly one verdict"):
        evidence.confirmed(checked, {"verdicts": []})


def test_evidence_gate_rejects_unrelated_location_and_over_limit(tmp_path):
    manifest = snapshot(tmp_path)
    report = impact.make_impact_report(manifest, PATCH)
    proposed = finding()
    proposed["location"]["line"] = 2
    with pytest.raises(evidence.EvidenceError, match="changed PR line"):
        evidence.validate_findings({"findings": [proposed]}, manifest, report)
    with pytest.raises(evidence.EvidenceError, match="At most seven"):
        evidence.validate_findings({"findings": [finding()] * 8}, manifest, report)


def test_removed_file_is_reported_and_can_be_cited_from_pinned_diff(tmp_path):
    manifest = snapshot(tmp_path)
    patch = """diff --git a/api/old.py b/api/old.py
--- a/api/old.py
+++ /dev/null
@@ -1 +0,0 @@
-PERMISSION = "inventory.read_hosts"
"""
    Path(manifest["pr"]["diff_path"]).write_text(patch)
    report = impact.make_impact_report(manifest, patch)
    assert report["changed_files"] == ["api/old.py"]
    removed = evidence.validate_ref({
        "repo": "org/rbac", "sha": "b" * 40, "file": "api/old.py", "line": 1,
    }, manifest)
    assert removed["text"] == 'PERMISSION = "inventory.read_hosts"'
