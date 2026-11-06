#!/usr/bin/env python3
"""Validate review evidence and verifier decisions, then render a review body."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote

from impact_analysis import changed_lines


class EvidenceError(ValueError):
    pass


def required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{field} must be non-empty text")
    return value.strip()


def source_root(manifest: dict, repo: str, sha: str) -> Path:
    pr = manifest["pr"]
    if repo == pr["repo"] and sha == pr["head_sha"]:
        return Path(pr["checkout_path"])
    for related in manifest["related"]:
        if related.get("repo") == repo and related.get("sha") == sha and related.get("path"):
            return Path(related["path"])
    raise EvidenceError(f"Source is not pinned in this snapshot: {repo}@{sha}")


def validate_ref(ref: object, manifest: dict) -> dict:
    if not isinstance(ref, dict):
        raise EvidenceError("Evidence reference must be an object")
    repo = required_text(ref.get("repo"), "ref.repo")
    sha = required_text(ref.get("sha"), "ref.sha")
    file = required_text(ref.get("file"), "ref.file")
    line = ref.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise EvidenceError("ref.line must be a positive integer")
    pr = manifest["pr"]
    if repo == pr["repo"] and sha == pr["base_sha"]:
        removed = next((item for item in changed_lines(Path(pr["diff_path"]).read_text())
                        if item["side"] == "removed" and item["file"] == file and item["line"] == line), None)
        if removed is None:
            raise EvidenceError(f"Removed evidence line is not in the PR diff: {file}:{line}")
        return {"repo": repo, "sha": sha, "file": file, "line": line, "text": removed["text"]}
    root = source_root(manifest, repo, sha).resolve()
    path = (root / file).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise EvidenceError(f"Evidence file is outside the pinned checkout: {repo}:{file}")
    try:
        selected = path.read_text().splitlines()[line - 1]
    except (UnicodeError, IndexError) as exc:
        raise EvidenceError(f"Evidence line unavailable: {repo}:{file}:{line}") from exc
    return {"repo": repo, "sha": sha, "file": file, "line": line, "text": selected}


def validate_findings(data: object, manifest: dict, impact: dict) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        raise EvidenceError("Expected findings array")
    findings = data["findings"]
    if len(findings) > 7:
        raise EvidenceError("At most seven findings may be proposed")
    ids: set[str] = set()
    validated: list[dict] = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise EvidenceError("Finding must be an object")
        ident = required_text(finding.get("id"), "finding.id")
        if not re.fullmatch(r"F[1-9]\d*", ident):
            raise EvidenceError("finding.id must use the F1, F2, ... format")
        if ident in ids:
            raise EvidenceError(f"Duplicate finding id: {ident}")
        ids.add(ident)
        category = required_text(finding.get("category"), "finding.category")
        if category not in {"cross_repo", "contract", "deploy", "invariant", "local"}:
            raise EvidenceError(f"Unsupported finding category: {category}")
        for field in ("claim", "trigger", "impact", "suggested_fix"):
            required_text(finding.get(field), f"finding.{field}")
        location = finding.get("location")
        if not isinstance(location, dict):
            raise EvidenceError("finding.location must be an object")
        if location.get("repo") != manifest["pr"]["repo"]:
            raise EvidenceError("Finding location must be in the PR repository")
        if location.get("file") not in impact["changed_files"]:
            raise EvidenceError("Finding location must be a changed file")
        side = location.get("side", "added")
        if side not in {"added", "removed"}:
            raise EvidenceError("finding.location.side must be added or removed")
        if not any(
            item["file"] == location["file"] and item["line"] == location.get("line")
            and item["side"] == side
            for item in changed_lines(Path(manifest["pr"]["diff_path"]).read_text())
        ):
            raise EvidenceError("Finding location must be a changed PR line")
        location_sha = manifest["pr"]["base_sha"] if side == "removed" else manifest["pr"]["head_sha"]
        location_ref = validate_ref({**location, "sha": location_sha}, manifest)
        location_ref["side"] = side
        evidence = finding.get("evidence")
        if not isinstance(evidence, dict) or not isinstance(evidence.get("refs"), list):
            raise EvidenceError("finding.evidence.refs must be an array")
        required_text(evidence.get("explanation"), "finding.evidence.explanation")
        refs = [validate_ref(ref, manifest) for ref in evidence["refs"]]
        if not refs:
            raise EvidenceError("Every finding needs a pinned code reference")
        if category == "cross_repo" and not any(ref["repo"] != manifest["pr"]["repo"] for ref in refs):
            raise EvidenceError("Cross-repository finding needs a related-repository code reference")
        validated.append({**finding, "location": location_ref, "refs": refs})
    return validated


def confirmed(findings: list[dict], data: object) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        raise EvidenceError("Expected verifier verdicts array")
    verdicts = data["verdicts"]
    by_id = {}
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            raise EvidenceError("Verdict must be an object")
        ident = required_text(verdict.get("finding_id"), "verdict.finding_id")
        status = verdict.get("status")
        if ident in by_id or status not in {"confirmed", "refuted", "uncertain"}:
            raise EvidenceError(f"Duplicate or invalid verdict: {ident}")
        required_text(verdict.get("refutation_check"), "verdict.refutation_check")
        required_text(verdict.get("reason"), "verdict.reason")
        by_id[ident] = verdict
    if set(by_id) != {finding["id"] for finding in findings}:
        raise EvidenceError("Verifier must return exactly one verdict per finding")
    return [finding for finding in findings if by_id[finding["id"]]["status"] == "confirmed"]


def render_inline_comments(findings: list[dict]) -> list[dict]:
    """Render validated findings as line-anchored GitHub review comments."""
    comments = []
    for finding in findings:
        location = finding["location"]
        side = "RIGHT" if location["side"] == "added" else "LEFT"
        body = "\n\n".join([
            f"**[{finding['id']}]** {finding['claim']}",
            f"**Location:** `{location['file']}:{location['line']}`",
            f"**Trigger:** {finding['trigger']}",
            f"**Impact:** {finding['impact']}",
            f"**Evidence:** {finding['evidence']['explanation']}",
            f"**Suggested fix:** {finding['suggested_fix']}",
            f"<!-- fabric-access-finding:{finding['id']} -->",
        ])
        comments.append({
            "id": finding["id"],
            "path": location["file"],
            "line": location["line"],
            "side": side,
            "body": body,
        })
    return comments


def render_review(manifest: dict, findings: list[dict]) -> str:
    pr = manifest["pr"]
    lines = ["## Review", ""]
    if not findings:
        lines.append("No confirmed actionable findings from the checked scope.")
    for finding in findings:
        loc = finding["location"]
        lines.extend([
            f"### [{finding['id']}] {finding['claim']}", "",
            f"**Location:** `{loc['file']}:{loc['line']}`", "",
            f"**Trigger:** {finding['trigger']}", "",
            f"**Impact:** {finding['impact']}", "",
            f"**Evidence:** {finding['evidence']['explanation']}", "",
        ])
        for ref in finding["refs"]:
            if ref["repo"] == pr["repo"] and ref["sha"] == pr["base_sha"]:
                url = f"https://github.com/{pr['repo']}/compare/{pr['base_sha']}...{pr['head_sha']}"
                label = f"{ref['repo']}:{ref['file']}:{ref['line']} (removed in PR diff)"
            else:
                url = f"https://github.com/{ref['repo']}/blob/{ref['sha']}/{quote(ref['file'], safe='/')}#L{ref['line']}"
                label = f"{ref['repo']}:{ref['file']}:{ref['line']}"
            lines.append(
                f"- [{label}]({url})"
            )
        lines.extend(["", f"**Suggested fix:** {finding['suggested_fix']}", ""])
    lines.append(f"<!-- fabric-access-review:{pr['head_sha']}:{pr['base_sha']} -->")
    return "\n".join(lines) + "\n"


def open_entries(ledger: list[dict]) -> list[dict]:
    return [entry for entry in ledger if entry.get("status") == "open"]


def validate_verifications(data: object, ledger: list[dict], manifest: dict) -> list[dict]:
    """Validate re-checks of currently-open ledger entries against the new revision."""
    if not isinstance(data, dict) or not isinstance(data.get("verifications"), list):
        raise EvidenceError("Expected verifications array")
    expected_ids = {entry["id"] for entry in open_entries(ledger)}
    verifications = data["verifications"]
    by_id: dict[str, dict] = {}
    for item in verifications:
        if not isinstance(item, dict):
            raise EvidenceError("Verification must be an object")
        ident = required_text(item.get("finding_id"), "verification.finding_id")
        if ident not in expected_ids:
            raise EvidenceError(f"Verification targets a finding that is not open: {ident}")
        if ident in by_id:
            raise EvidenceError(f"Duplicate verification: {ident}")
        status = item.get("status")
        if status not in {"addressed", "still_open", "uncertain"}:
            raise EvidenceError(f"Unsupported verification status: {status}")
        explanation = required_text(item.get("explanation"), "verification.explanation")
        refs: list[dict] = []
        if status == "addressed":
            raw_refs = item.get("refs")
            if not isinstance(raw_refs, list) or not raw_refs:
                raise EvidenceError(f"Addressed verification needs at least one pinned ref: {ident}")
            refs = [validate_ref(ref, manifest) for ref in raw_refs]
        by_id[ident] = {"finding_id": ident, "status": status, "explanation": explanation, "refs": refs}
    if set(by_id) != expected_ids:
        raise EvidenceError("Every open finding needs exactly one verification")
    return list(by_id.values())


def confirm_verifications(verifications: list[dict], data: object) -> list[dict]:
    """Require review-verifier confirmation only for 'addressed' claims; pass the rest through."""
    addressed = {v["finding_id"]: v for v in verifications if v["status"] == "addressed"}
    passthrough = [v for v in verifications if v["status"] != "addressed"]
    if not addressed:
        if isinstance(data, dict) and data.get("verdicts"):
            raise EvidenceError("No addressed verifications were proposed; verdicts must be empty")
        return passthrough
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), list):
        raise EvidenceError("Expected verifier verdicts array")
    verdicts = data["verdicts"]
    by_id: dict[str, dict] = {}
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            raise EvidenceError("Verdict must be an object")
        ident = required_text(verdict.get("finding_id"), "verdict.finding_id")
        status = verdict.get("status")
        if ident in by_id or status not in {"confirmed", "refuted", "uncertain"}:
            raise EvidenceError(f"Duplicate or invalid verdict: {ident}")
        required_text(verdict.get("refutation_check"), "verdict.refutation_check")
        required_text(verdict.get("reason"), "verdict.reason")
        by_id[ident] = verdict
    if set(by_id) != set(addressed):
        raise EvidenceError("Verifier must return exactly one verdict per addressed verification")
    resolved: list[dict] = list(passthrough)
    for ident, verification in addressed.items():
        if by_id[ident]["status"] == "confirmed":
            resolved.append(verification)
        else:
            resolved.append({**verification, "status": "still_open",
                              "explanation": f"Claimed fix not confirmed: {by_id[ident]['reason']}", "refs": []})
    return resolved


def apply_ledger_updates(ledger: list[dict], new_findings: list[dict],
                          resolved_verifications: list[dict], head_sha: str) -> list[dict]:
    """Merge this cycle's re-checks and new findings into the persisted ledger."""
    by_id = {v["finding_id"]: v for v in resolved_verifications}
    ledger_ids = [entry["id"] for entry in ledger]
    if len(ledger_ids) != len(set(ledger_ids)):
        raise EvidenceError("Prior ledger contains duplicate finding IDs")
    prior_ids = set(ledger_ids)
    reused_ids = sorted(prior_ids.intersection(finding["id"] for finding in new_findings))
    if reused_ids:
        raise EvidenceError(f"New findings reuse prior ledger IDs: {', '.join(reused_ids)}")
    updated: list[dict] = []
    for entry in ledger:
        if entry.get("status") != "open" or entry["id"] not in by_id:
            updated.append(entry)
            continue
        verification = by_id[entry["id"]]
        merged = {**entry, "last_checked_sha": head_sha}
        if verification["status"] == "addressed":
            merged["status"] = "addressed"
            merged["status_evidence"] = {"explanation": verification["explanation"], "refs": verification["refs"]}
        else:
            merged["status_note"] = verification["explanation"]
        updated.append(merged)
    for finding in new_findings:
        updated.append({
            **finding, "status": "open", "first_flagged_sha": head_sha,
            "last_checked_sha": head_sha, "status_evidence": None, "status_note": None,
        })
    return updated


def _ref_link(pr: dict, ref: dict) -> str:
    if ref["repo"] == pr["repo"] and ref["sha"] == pr["base_sha"]:
        url = f"https://github.com/{pr['repo']}/compare/{pr['base_sha']}...{pr['head_sha']}"
        return f"[{ref['repo']}:{ref['file']}:{ref['line']} (removed in PR diff)]({url})"
    url = f"https://github.com/{ref['repo']}/blob/{ref['sha']}/{quote(ref['file'], safe='/')}#L{ref['line']}"
    return f"[{ref['repo']}:{ref['file']}:{ref['line']}]({url})"


def _render_finding_block(pr: dict, finding: dict) -> list[str]:
    loc = finding["location"]
    lines = [
        f"### [{finding['id']}] {finding['claim']}", "",
        f"**Location:** `{loc['file']}:{loc['line']}`", "",
        f"**Trigger:** {finding['trigger']}", "",
        f"**Impact:** {finding['impact']}", "",
        f"**Evidence:** {finding['evidence']['explanation']}", "",
    ]
    lines.extend(f"- {_ref_link(pr, ref)}" for ref in finding["refs"])
    lines.extend(["", f"**Suggested fix:** {finding['suggested_fix']}", ""])
    return lines


def render_ledger(manifest: dict, ledger: list[dict], new_findings: list[dict], update_count: int) -> str:
    """Render the single persistent review body: previously-flagged status, then new findings."""
    pr = manifest["pr"]
    short_sha = pr["head_sha"][:12]
    lines = [f"**Review status** — update #{update_count} • reviewed through `{short_sha}`", ""]
    carried = [entry for entry in ledger if entry["id"] not in {f["id"] for f in new_findings}]
    lines.append("## Previously Flagged")
    lines.append("")
    if not carried:
        lines.extend(["None yet.", ""])
    for entry in carried:
        label = "✅ Addressed" if entry["status"] == "addressed" else "⚠️ Still open"
        lines.append(f"### [{entry['id']}] {entry['claim']} — {label}")
        lines.append("")
        lines.append(f"**Location:** `{entry['location']['file']}:{entry['location']['line']}`")
        lines.append("")
        lines.append(f"**Trigger:** {entry['trigger']}")
        lines.append("")
        lines.append(f"**Impact:** {entry['impact']}")
        lines.append("")
        lines.append(f"**Evidence:** {entry['evidence']['explanation']}")
        lines.extend(f"- {_ref_link(pr, ref)}" for ref in entry["refs"])
        lines.extend(["", f"**Suggested fix:** {entry['suggested_fix']}", ""])
        if entry["status"] == "addressed" and entry.get("status_evidence"):
            lines.append(f"**Status evidence:** {entry['status_evidence']['explanation']}")
            lines.extend(f"- {_ref_link(pr, ref)}" for ref in entry["status_evidence"]["refs"])
        elif entry.get("status_note"):
            lines.append(f"**Status:** {entry['status_note']}")
        else:
            lines.append("**Status:** No change detected in this region since it was last checked.")
        lines.append("")
    lines.append("## New Findings")
    lines.append("")
    if not new_findings:
        lines.extend(["No new confirmed findings in this revision.", ""])
    for finding in new_findings:
        lines.extend(_render_finding_block(pr, finding))
    lines.append(f"<!-- fabric-access-review:{pr['head_sha']}:{pr['base_sha']} -->")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "findings", "verdicts", "output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--ledger", type=Path, help="Path to the previous cycle's ledger.json, if any")
    parser.add_argument("--verifications", type=Path, help="Proposed re-checks of open ledger entries")
    parser.add_argument("--verification-verdicts", type=Path, help="review-verifier's verdicts for 'addressed' re-checks")
    parser.add_argument("--update-count", type=int, help="Update count so far; this run writes update-count + 1")
    parser.add_argument("--ledger-output", type=Path, help="Where to write the updated ledger.json")
    parser.add_argument("--inline-comments-output", type=Path,
                        help="Where to write inline comments rendered from confirmed findings")
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
        impact = json.loads(Path(manifest["impact_path"]).read_text())
        findings = validate_findings(json.loads(args.findings.read_text()), manifest, impact)
        accepted = confirmed(findings, json.loads(args.verdicts.read_text()))
        if args.ledger is None:
            body = render_review(manifest, accepted)
            args.output.write_text(body)
            initial_ledger = apply_ledger_updates(
                [], accepted, [], manifest["pr"]["head_sha"]
            )
            if args.ledger_output:
                args.ledger_output.write_text(json.dumps(initial_ledger, indent=2))
            if args.inline_comments_output:
                args.inline_comments_output.write_text(json.dumps(render_inline_comments(accepted), indent=2))
            print(json.dumps({
                "confirmed": len(accepted), "proposed": len(findings),
                "body_file": str(args.output),
                "ledger_size": len(initial_ledger),
                "ledger_file": str(args.ledger_output) if args.ledger_output else None,
                "update_count": 0,
                "inline_comments_file": str(args.inline_comments_output) if args.inline_comments_output else None,
            }))
            return
        if not (args.verifications and args.verification_verdicts and args.ledger_output and args.update_count is not None):
            raise EvidenceError("--ledger requires --verifications, --verification-verdicts, --update-count, --ledger-output")
        ledger = json.loads(args.ledger.read_text()) if args.ledger.exists() else []
        verifications = validate_verifications(json.loads(args.verifications.read_text()), ledger, manifest)
        resolved = confirm_verifications(verifications, json.loads(args.verification_verdicts.read_text()))
        new_ledger = apply_ledger_updates(ledger, accepted, resolved, manifest["pr"]["head_sha"])
        update_count = args.update_count + 1
        body = render_ledger(manifest, new_ledger, accepted, update_count)
        args.output.write_text(body)
        args.ledger_output.write_text(json.dumps(new_ledger, indent=2))
        if args.inline_comments_output:
            args.inline_comments_output.write_text(json.dumps(render_inline_comments(accepted), indent=2))
        print(json.dumps({
            "confirmed": len(accepted), "proposed": len(findings),
            "ledger_size": len(new_ledger), "update_count": update_count,
            "body_file": str(args.output), "ledger_file": str(args.ledger_output),
            "inline_comments_file": str(args.inline_comments_output) if args.inline_comments_output else None,
        }))
    except (EvidenceError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Review evidence invalid: {exc}\n")


if __name__ == "__main__":
    main()
