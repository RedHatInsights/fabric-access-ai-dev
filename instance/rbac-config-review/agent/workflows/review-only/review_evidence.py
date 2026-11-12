#!/usr/bin/env python3
"""Validate review evidence and verifier decisions, then render a review body."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from impact_analysis import changed_lines
from public_comment_safety import ensure_public_content


class EvidenceError(ValueError):
    pass


_SEVERITIES = {"blocking", "suggestion", "nit", "question"}
_REFERENCE_ID = re.compile(r"R[1-9]\d*")


def required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{field} must be non-empty text")
    return value.strip()


def contains_in_order(text: str, values: list[str]) -> bool:
    cursor = 0
    for value in values:
        cursor = text.find(value, cursor)
        if cursor < 0:
            return False
        cursor += len(value)
    return True


def dimension_label(finding: dict) -> str:
    """Show recorded pass provenance without inventing it for older ledger entries."""
    names = finding.get("source_dimensions") or []
    return ", ".join(names) if names else "not recorded in prior review"


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


def validate_scenario(value: object, field: str) -> list[str]:
    if (not isinstance(value, list) or not 2 <= len(value) <= 8
            or any(not isinstance(step, str) or not step.strip() for step in value)):
        raise EvidenceError(f"{field} must contain 2 to 8 concrete sequential steps")
    return [step.strip() for step in value]


def validate_evidence(value: object, manifest: dict, field: str) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("refs"), list):
        raise EvidenceError(f"{field} must contain an explanation and references")
    explanation = required_text(value.get("explanation"), f"{field}.explanation")
    refs = []
    for ref in value["refs"]:
        if not isinstance(ref, dict) or not isinstance(ref.get("id"), str) or not _REFERENCE_ID.fullmatch(ref["id"]):
            raise EvidenceError(f"Each {field} reference needs an id such as R1")
        validated_ref = validate_ref(ref, manifest)
        refs.append({**validated_ref, "id": ref["id"],
                     "url": _ref_url(manifest["pr"], validated_ref)})
    ref_ids = {ref["id"] for ref in refs}
    cited_ids = set(re.findall(r"\[(R[1-9]\d*)\]", explanation))
    if not refs or len(ref_ids) != len(refs) or cited_ids != ref_ids:
        raise EvidenceError(f"{field}.explanation must cite each unique reference ID in its text")
    return {**value, "explanation": explanation, "refs": refs}


def context_question_id(question: str) -> str:
    normalized = re.sub(r"\s+", " ", question.strip()).casefold()
    return "Q-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def validate_context_questions(data: object, manifest: dict, update_count: int = 0) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        raise EvidenceError("Expected context questions array")
    if len(data["questions"]) > 5:
        raise EvidenceError("At most five context questions may be asked in one review")
    questions = []
    ids: set[str] = set()
    for item in data["questions"]:
        if not isinstance(item, dict):
            raise EvidenceError("Context question must be an object")
        question = required_text(item.get("question"), "question.question")
        if question.count("?") != 1 or not question.endswith("?"):
            raise EvidenceError("Each context question must contain exactly one direct question")
        revision = manifest.get("pr", {})
        scope = str(revision.get("head_sha", "")) + ":" + str(revision.get("base_sha", "")) + ":" + str(update_count)
        ident = context_question_id(scope + " " + question)
        if ident in ids:
            raise EvidenceError("Context questions must be unique")
        ids.add(ident)
        questions.append({
            "id": ident,
            "question": question,
            "why_needed": required_text(item.get("why_needed"), "question.why_needed"),
            "impact": required_text(item.get("impact"), "question.impact"),
            "scenario": validate_scenario(item.get("scenario"), "question.scenario"),
            "evidence": validate_evidence(item.get("evidence"), manifest, "question.evidence"),
        })
    return questions


def render_context_question_comments(questions: list[dict]) -> list[dict]:
    comments = []
    for question in questions:
        ident = question["id"]
        body = "\n\n".join([
            f"**Question {ident}:** {question['question']}",
            "**Severity:** question",
            "**Dimension:** missing-context-detector",
            f"**Why I need this context:** {question['why_needed']}",
            "**Scenario:**\n" + "\n".join(
                f"{index}. {step}" for index, step in enumerate(question["scenario"], 1)
            ),
            f"**Impact:** {question['impact']}",
            f"**Evidence:** {link_evidence(question['evidence']['explanation'], question['evidence']['refs'])}",
            f"Please reply with `Answer {ident}: ...`. This is a context request; no defect is assumed.",
            f"<!-- dev-review-bot:missing-context:{ident} -->",
        ])
        comments.append({"id": ident, "body": body})
    return comments


def validate_context_responses(data: object, manifest: dict) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get("responses"), list):
        raise EvidenceError("Expected context responses array")
    responses = []
    answer_keys: set[tuple[str, int]] = set()
    for item in data["responses"]:
        if not isinstance(item, dict):
            raise EvidenceError("Context response must be an object")
        question_id = required_text(item.get("question_id"), "response.question_id")
        answer_id = item.get("answer_id")
        if (not re.fullmatch(r"Q-[0-9a-f]{12}", question_id)
                or not isinstance(answer_id, int) or isinstance(answer_id, bool) or answer_id <= 0):
            raise EvidenceError("Context response needs a question ID and positive answer comment ID")
        key = (question_id, answer_id)
        if key in answer_keys:
            raise EvidenceError("Duplicate context response")
        answer_keys.add(key)
        decision = item.get("decision")
        if decision not in {"action_required", "no_change", "needs_more_context"}:
            raise EvidenceError("Unsupported context answer decision")
        severity = item.get("severity")
        if severity not in _SEVERITIES:
            raise EvidenceError("Context response severity must be blocking, suggestion, nit, or question")
        instruction = item.get("instruction")
        if decision == "action_required":
            instruction = required_text(instruction, "response.instruction")
            if severity not in {"blocking", "suggestion", "nit"}:
                raise EvidenceError("An action-required response needs blocking, suggestion, or nit severity")
        elif decision == "no_change":
            instruction = "No code change is indicated by this answer."
            if severity != "question":
                raise EvidenceError("A no-change response must use question severity")
        else:
            instruction = required_text(item.get("follow_up_question"), "response.follow_up_question")
            if instruction.count("?") != 1 or not instruction.endswith("?") or severity != "question":
                raise EvidenceError("A follow-up response must contain one question and question severity")
        responses.append({
            "question_id": question_id,
            "answer_id": answer_id,
            "decision": decision,
            "severity": severity,
            "summary": required_text(item.get("summary"), "response.summary"),
            "why_or_how": required_text(item.get("why_or_how"), "response.why_or_how"),
            "impact": required_text(item.get("impact"), "response.impact"),
            "example": (required_text(item["example"], "response.example")
                        if item.get("example") is not None else None),
            "instruction": instruction,
            "scenario": validate_scenario(item.get("scenario"), "response.scenario"),
            "evidence": validate_evidence(item.get("evidence"), manifest, "response.evidence"),
        })
    return responses


def render_context_response_comments(responses: list[dict]) -> list[dict]:
    comments = []
    for response in responses:
        question_id, answer_id = response["question_id"], response["answer_id"]
        parts = [
            f"**Answer ({question_id}):** {response['summary']}",
            f"**Why or how:** {response['why_or_how']}",
            f"**Reasoning:** {link_evidence(response['evidence']['explanation'], response['evidence']['refs'])}",
            f"**Impact:** {response['impact']}",
        ]
        if response["example"]:
            parts.append(f"**Example:** {response['example']}")
        if response["decision"] == "needs_more_context":
            follow_up_id = context_question_id(question_id + " " + response["instruction"])
            parts.extend([
                "**Implication:** The answer does not resolve the review question; more context is needed.",
                f"**Severity:** {response['severity']}",
                "**Dimension:** missing-context-detector",
                "**Scenario:**\n" + "\n".join(
                    f"{index}. {step}" for index, step in enumerate(response["scenario"], 1)
                ),
            ])
        else:
            parts.extend([
                f"**Implication:** {response['instruction']}",
                f"**Severity:** {response['severity']}",
                "**Dimension:** missing-context-detector",
                "**Scenario:**\n" + "\n".join(
                    f"{index}. {step}" for index, step in enumerate(response["scenario"], 1)
                ),
            ])
        parts.append(f"<!-- dev-review-bot:context-response:{question_id}:{answer_id} -->")
        body = "\n\n".join(parts)
        comments.append({"id": f"{question_id}:{answer_id}", "body": body})
        if response["decision"] == "needs_more_context":
            comments.append({
                "id": follow_up_id,
                "body": "\n\n".join([
                    f"**Question {follow_up_id}:** {response['instruction']}",
                    "**Severity:** question",
                    "**Dimension:** missing-context-detector",
                    f"**Impact:** {response['impact']}",
                    "**Scenario:**\n" + "\n".join(
                        f"{index}. {step}" for index, step in enumerate(response["scenario"], 1)
                    ),
                    f"**Evidence:** {link_evidence(response['evidence']['explanation'], response['evidence']['refs'])}",
                    f"Please reply with `Answer {follow_up_id}: ...`. This is a context request; no defect is assumed.",
                    f"<!-- dev-review-bot:missing-context:{follow_up_id} -->",
                ]),
            })
    return comments


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
        severity = required_text(finding.get("severity"), "finding.severity")
        if severity not in _SEVERITIES - {"question"}:
            raise EvidenceError("Finding severity must be blocking, suggestion, or nit")
        if manifest.get("review_plan") or manifest.get("format") == "custom":
            dimensions = finding.get("source_dimensions")
            planned = {item["name"] for item in manifest.get("review_plan", {}).get("passes", [])}
            if (not isinstance(dimensions, list) or not dimensions
                    or any(not isinstance(name, str) or name not in planned for name in dimensions)
                    or len(set(dimensions)) != len(dimensions)):
                raise EvidenceError("Findings need unique source_dimensions from the review plan")
        for field in ("claim", "trigger", "impact", "suggested_fix"):
            required_text(finding.get(field), f"finding.{field}")
        scenario = validate_scenario(finding.get("scenario"), "finding.scenario")
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
        evidence = validate_evidence(finding.get("evidence"), manifest, "finding.evidence")
        refs = evidence["refs"]
        if category == "cross_repo" and not any(ref["repo"] != manifest["pr"]["repo"] for ref in refs):
            raise EvidenceError("Cross-repository finding needs a related-repository code reference")
        validated.append({**finding, "severity": severity, "scenario": [step.strip() for step in scenario],
                          "location": location_ref, "refs": refs,
                          "evidence": evidence})
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
        body_parts = [
            f"**[{finding['id']}]** {finding['claim']}",
            f"**Severity:** {finding['severity']}",
            f"**Dimension:** {dimension_label(finding)}",
            f"**Location:** `{location['file']}:{location['line']}`",
            f"**Trigger:** {finding['trigger']}",
            "**Scenario:**\n" + "\n".join(
                f"{index}. {step}" for index, step in enumerate(finding["scenario"], 1)
            ),
            f"**Impact:** {finding['impact']}",
            f"**Evidence:** {link_evidence(finding['evidence']['explanation'], finding['refs'])}",
        ]
        body_parts.extend([
            f"**Suggested fix:** {finding['suggested_fix']}",
            f"<!-- dev-review-bot:fabric-access-finding:{finding['id']} -->",
        ])
        body = "\n\n".join(body_parts)
        comments.append({
            "id": finding["id"],
            "path": location["file"],
            "line": location["line"],
            "side": side,
            "body": body,
        })
    return comments


def validate_pr_summary(data: object, manifest: dict) -> str:
    """Return a concise, single-line description of what the PR changes."""
    if not isinstance(data, dict):
        raise EvidenceError("Expected findings object")
    summary = data.get("pr_summary")
    if summary is None:
        summary = manifest.get("pr", {}).get("title")
        if not isinstance(summary, str) or not summary.strip():
            summary = "This PR updates the reviewed code."
        summary = " ".join(summary.split())
        if len(summary) > 280:
            summary = summary[:277].rsplit(" ", 1)[0].rstrip() + "..."
        return summary
    if not isinstance(summary, str) or not summary.strip():
        raise EvidenceError("pr_summary must be a non-empty sentence")
    summary = " ".join(summary.split())
    if len(summary) > 280:
        raise EvidenceError("pr_summary must not exceed 280 characters")
    if "<!--" in summary or "-->" in summary:
        raise EvidenceError("pr_summary must not contain HTML comments")
    return summary


def table_cell(value: object) -> str:
    """Keep generated finding text on one valid Markdown table row."""
    text = " ".join(str(value).split())
    return text.replace("|", "&#124;") or "—"


def findings_table(findings: list[dict]) -> list[str]:
    lines = [
        "| ID | Severity | Dimension | Status | Inline comment |",
        "|---|---|---|---|---|",
    ]
    if not findings:
        lines.append("| — | — | — | No confirmed findings. | — |")
        return lines
    for item in findings:
        ident = item["id"]
        lines.append(
            f"| {ident} | {table_cell(item.get('severity') or 'not recorded')} "
            f"| {table_cell(dimension_label(item))} "
            f"| {table_cell(item.get('status', 'open'))} | [Open thread](review-comment:{ident}) |"
        )
    return lines


def verification_status_line(manifest: dict, command_ids: list[int], verified_at: datetime) -> str:
    """Describe the latest completed verification and link every request it handled."""
    if (not command_ids or any(type(ident) is not int or ident < 1 for ident in command_ids)
            or len(command_ids) != len(set(command_ids))):
        raise EvidenceError("Verification needs unique positive command IDs")
    if verified_at.tzinfo is None:
        raise EvidenceError("Verification time needs a timezone")
    pr = manifest["pr"]
    repo, number, head = pr["repo"], pr["number"], pr["head_sha"]
    timestamp = verified_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    requests = ", ".join(
        f"[request #{ident}](https://github.com/{repo}/pull/{number}#issuecomment-{ident})"
        for ident in command_ids
    )
    return (f"**Last verification:** {timestamp} for "
            f"[head `{head[:12]}`](https://github.com/{repo}/commit/{head}); "
            f"requested by {requests}.")


def render_status_review(manifest: dict, findings: list[dict], review_number: int,
                         summary: str | None = None,
                         last_verification: str | None = None) -> str:
    """Render the shared status-review template for standard and custom reviews."""
    pr = manifest["pr"]
    summary = summary or validate_pr_summary({}, manifest)
    lines = [
        f"## Review {review_number}",
        "",
        summary,
        "",
        f"### {compact_status_line(findings)}",
        "",
        *findings_table(findings),
        "",
        *([last_verification, ""] if last_verification else []),
        "---",
        "",
        "- **Ask the bot:** Comment `/review-bot <question or request>` for an answer based on the pinned PR and review context.",
        "- **Re-review:** Add a new top-level PR comment containing `/review-bot re-review`.",
        "- **Verify findings:** Add a new top-level PR comment containing `/review-bot verify`; the bot checks existing findings only.",
        "- **Jira:** Ask for a ticket for out-of-scope work; confirmed cross-repo findings with one configured owner are tracked automatically.",
        "",
        f"<!-- dev-review-bot:fabric-access-review:{pr['head_sha']}:{pr['base_sha']} -->",
    ]
    return "\n".join(lines) + "\n"


def render_review(manifest: dict, findings: list[dict], summary: str | None = None) -> str:
    return render_status_review(manifest, findings, 1, summary)


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


def _ref_url(pr: dict, ref: dict) -> str:
    if ref["repo"] == pr["repo"] and ref["sha"] == pr["base_sha"]:
        return f"https://github.com/{pr['repo']}/compare/{pr['base_sha']}...{pr['head_sha']}"
    return f"https://github.com/{ref['repo']}/blob/{ref['sha']}/{quote(ref['file'], safe='/')}#L{ref['line']}"


def _ref_link(pr: dict, ref: dict) -> str:
    label = f"{ref['repo']}:{ref['file']}:{ref['line']}"
    if ref["repo"] == pr["repo"] and ref["sha"] == pr["base_sha"]:
        label += " (removed in PR diff)"
    return f"[{label}]({_ref_url(pr, ref)})"


def link_evidence(explanation: str, refs: list[dict]) -> str:
    """Replace in-text evidence IDs with links to the cited pinned lines."""
    by_id = {ref["id"]: f"[{ref['repo']}:{ref['file']}:{ref['line']}]({ref['url']})" for ref in refs}
    for ref in refs:
        token = f"[{ref['id']}]"
        explanation = explanation.replace(token, by_id[ref["id"]])
    return explanation


def compact_status_line(findings: list[dict]) -> str:
    """Summarize finding counts for the Findings heading."""
    open_count = sum(item.get("status", "open") == "open" for item in findings)
    addressed_count = sum(item.get("status") == "addressed" for item in findings)
    return (f"Findings ({len(findings)} total; {open_count} open, "
            f"{addressed_count} addressed)")


def render_ledger(manifest: dict, ledger: list[dict], new_findings: list[dict],
                  update_count: int, summary: str | None = None,
                  last_verification: str | None = None) -> str:
    """Render the updated status review with the carried finding ledger."""
    return render_status_review(manifest, ledger, update_count + 1, summary,
                                last_verification)


def custom_skill_results(manifest: dict, data: object) -> tuple[list[dict], list[dict]]:
    """Require a recorded, completed pass and pinned guide for every formatting skill."""
    names = manifest.get("format_skills")
    passes = manifest.get("review_plan", {}).get("passes", [])
    if (not isinstance(names, list) or not names or any(not isinstance(n, str) or not n for n in names)
            or len(set(names)) != len(names)):
        raise EvidenceError("Custom format needs unique formatting skills")
    guides = {item.get("name"): item.get("guide_path") for item in passes if isinstance(item, dict)}
    if any(not isinstance(guides.get(name), str) or not Path(guides[name]).is_file() for name in names):
        raise EvidenceError("Every formatting skill needs its selected guide")
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise EvidenceError("Custom format needs recorded skill results")
    by_name = {}
    for result in data["results"]:
        if not isinstance(result, dict):
            raise EvidenceError("Skill result must be an object")
        name = result.get("dimension")
        if not isinstance(name, str) or name not in names or name in by_name:
            raise EvidenceError("Skill results must match the formatting skills")
        if (result.get("verdict") not in ("pass", "warn", "fail")
                or result.get("severity") not in ("critical", "high", "medium", "low", "info")
                or not isinstance(result.get("findings"), list)):
            raise EvidenceError(f"Invalid skill result: {name}")
        required_text(result.get("summary"), "skill result.summary")
        by_name[name] = result
    if set(by_name) != set(names):
        raise EvidenceError("Every formatting skill needs a completed result")
    return ([{"name": name, "guide_path": guides[name]} for name in names],
            [by_name[name] for name in names])


def validate_format_verdict(data: object, format_skills: list[dict]) -> None:
    if not isinstance(data, dict) or data.get("status") != "confirmed":
        raise EvidenceError("Custom formatting must be independently confirmed")
    required_text(data.get("refutation_check"), "custom verdict.refutation_check")
    required_text(data.get("reason"), "custom verdict.reason")
    verdicts = data.get("skills")
    if not isinstance(verdicts, list):
        raise EvidenceError("Custom formatting needs a verdict for each skill")
    names = {item["name"] for item in format_skills}
    seen = set()
    for verdict in verdicts:
        if not isinstance(verdict, dict):
            raise EvidenceError("Skill formatting verdict must be an object")
        name = verdict.get("name")
        if not isinstance(name, str) or name not in names or name in seen or verdict.get("status") != "confirmed":
            raise EvidenceError("Every formatting skill must be independently confirmed")
        required_text(verdict.get("refutation_check"), "skill verdict.refutation_check")
        required_text(verdict.get("reason"), "skill verdict.reason")
        seen.add(name)
    if seen != names:
        raise EvidenceError("Every formatting skill must be independently confirmed")


def render_custom(manifest: dict, ledger: list[dict], new_findings: list[dict],
                  update_count: int, data: object,
                  summary: str | None = None) -> tuple[str, list[dict]]:
    """Validate skill-formatted inline comments and use the shared status template."""
    if not isinstance(data, dict):
        raise EvidenceError("Custom format must be an object")
    entries = {entry["id"]: entry for entry in ledger}
    added = {finding["id"]: finding for finding in new_findings}
    format_skills = set(manifest.get("format_skills", []))
    custom_expected = {
        ident for ident, finding in added.items()
        if format_skills.intersection(finding.get("source_dimensions", []))
    }
    inline = data.get("inline_comments")
    if not isinstance(inline, list):
        raise EvidenceError("Custom format needs an inline_comments array")
    comments = []
    inline_ids: set[str] = set()
    for item in inline:
        if not isinstance(item, dict):
            raise EvidenceError("Custom inline comment must be an object")
        ident = item.get("finding_id")
        skill = item.get("skill")
        text = required_text(item.get("text"), "custom inline text")
        if not isinstance(ident, str) or ident not in custom_expected or ident in inline_ids or f"[{ident}]" not in text:
            raise EvidenceError("Custom inline comments must identify each selected-skill finding once")
        if (not isinstance(skill, str) or skill not in format_skills
                or skill not in added[ident].get("source_dimensions", [])):
            raise EvidenceError("Custom inline comment must name one selected source skill for the finding")
        if f"**Dimension:** {dimension_label(added[ident])}" not in text:
            raise EvidenceError("Custom inline comment must state its dimension")
        finding = added[ident]
        labels = ("Severity", "Dimension", "Location", "Trigger", "Scenario", "Impact", "Evidence", "Suggested fix")
        if any(f"**{label}:**" not in text for label in labels):
            raise EvidenceError("Custom inline comment must include all bold finding labels and a real scenario")
        if f"**Severity:** {finding['severity']}" not in text:
            raise EvidenceError("Custom inline comment must use the validated severity")
        if not contains_in_order(text, finding["scenario"]):
            raise EvidenceError("Custom inline comment must include every scenario step in order")
        if any(text.count(f"[{ref['id']}]") != 1 for ref in finding["refs"]):
            raise EvidenceError("Custom inline comment must cite each evidence ID once beside its supporting claim")
        if all("id" in ref and "url" in ref for ref in finding["refs"]):
            text = link_evidence(text, finding["refs"])
        if ("fabric-access-" in text or "<!-- dev-review-bot:" in text
                or not set(re.findall(r"\bF[1-9]\d*\b", text)) <= entries.keys()):
            raise EvidenceError("Custom inline comment contains a marker or unconfirmed finding")
        inline_ids.add(ident)
        location = added[ident]["location"]
        comments.append({
            "id": ident, "path": location["file"], "line": location["line"],
            "side": "RIGHT" if location["side"] == "added" else "LEFT",
            "format": "custom", "skill": skill,
            "body": text + f"\n\n<!-- dev-review-bot:fabric-access-finding:{ident} -->",
        })
    if inline_ids != custom_expected:
        raise EvidenceError("Custom format needs one custom inline comment per finding from a selected skill")
    custom_by_id = {comment["id"]: comment for comment in comments}
    for finding in new_findings:
        if finding["id"] not in custom_expected:
            custom_by_id[finding["id"]] = render_inline_comments([finding])[0]
    comments = [custom_by_id[finding["id"]] for finding in new_findings]
    body = render_status_review(manifest, ledger, update_count + 1, summary)
    return body, comments


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "publication-output"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--findings", type=Path, help="Proposed finding JSON for a full review")
    parser.add_argument("--verdicts", type=Path, help="Verifier verdicts for proposed findings")
    parser.add_argument("--ledger", type=Path, help="Path to the previous cycle's ledger.json, if any")
    parser.add_argument("--verifications", type=Path, help="Proposed re-checks of open ledger entries")
    parser.add_argument("--verification-verdicts", type=Path, help="review-verifier's verdicts for 'addressed' re-checks")
    parser.add_argument("--update-count", type=int,
                        help="Current review count; full reviews increment it, verification-only runs preserve it")
    parser.add_argument("--verification-only", action="store_true",
                        help="Update only prior finding statuses without incrementing the review number")
    parser.add_argument("--verification-command-id", action="append", type=int, default=[],
                        help="Top-level PR comment ID requesting verification; repeat for each request")
    parser.add_argument("--pr-summary", help="Existing one-line PR summary to preserve during verification")
    parser.add_argument("--ledger-output", type=Path, help="Where to write the updated ledger.json")
    parser.add_argument("--inline-comments-output", type=Path,
                        help="Where to write inline comments rendered from confirmed findings")
    parser.add_argument("--context-questions", type=Path,
                        help="Questions returned by missing-context-detector")
    parser.add_argument("--context-question-comments-output", type=Path,
                        help="Where to write validated one-question-per-comment payloads")
    parser.add_argument("--context-responses", type=Path,
                        help="Evaluations of author answers returned by missing-context-detector")
    parser.add_argument("--context-response-comments-output", type=Path,
                        help="Where to write validated feedback comments for author answers")
    parser.add_argument("--prepare-custom", type=Path,
                        help="Write confirmed findings and ledger for skill-based formatting")
    parser.add_argument("--custom-format", type=Path,
                        help="Skill-formatted inline comments")
    parser.add_argument("--custom-verdict", type=Path,
                        help="Independent verification of the custom formatting")
    parser.add_argument("--skill-results", type=Path,
                        help="Recorded completed passes for the selected formatting skills")
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text())
        impact = json.loads(Path(manifest["impact_path"]).read_text())
        if not args.verification_only and (args.findings is None or args.verdicts is None):
            parser.error("--findings and --verdicts are required unless --verification-only is used")
        if bool(args.context_questions) != bool(args.context_question_comments_output):
            raise EvidenceError("--context-questions and --context-question-comments-output must be used together")
        if bool(args.context_responses) != bool(args.context_response_comments_output):
            raise EvidenceError("--context-responses and --context-response-comments-output must be used together")
        if args.context_questions:
            questions = validate_context_questions(json.loads(args.context_questions.read_text()), manifest, args.update_count or 0)
            question_comments = render_context_question_comments(questions)
            for comment in question_comments:
                ensure_public_content(comment["body"], "Context question")
            args.context_question_comments_output.write_text(json.dumps(question_comments, indent=2))
        if args.context_responses:
            responses = validate_context_responses(json.loads(args.context_responses.read_text()), manifest)
            response_comments = render_context_response_comments(responses)
            for comment in response_comments:
                ensure_public_content(comment["body"], "Context response")
            args.context_response_comments_output.write_text(json.dumps(response_comments, indent=2))
        if args.verification_only:
            if (args.ledger is None or args.verifications is None or args.verification_verdicts is None
                    or args.ledger_output is None or args.update_count is None):
                raise EvidenceError("--verification-only requires --ledger, --verifications, "
                                    "--verification-verdicts, --ledger-output, and --update-count")
            last_verification = verification_status_line(
                manifest, args.verification_command_id, datetime.now(timezone.utc)
            )
            summary = validate_pr_summary(
                {"pr_summary": args.pr_summary} if args.pr_summary else {}, manifest
            )
            findings = []
            accepted = []
            ledger = json.loads(args.ledger.read_text())
            verifications = validate_verifications(
                json.loads(args.verifications.read_text()), ledger, manifest
            )
            resolved = confirm_verifications(
                verifications, json.loads(args.verification_verdicts.read_text())
            )
            new_ledger = apply_ledger_updates(ledger, [], resolved, manifest["pr"]["head_sha"])
            update_count = args.update_count
        else:
            if args.verification_command_id:
                raise EvidenceError("--verification-command-id requires --verification-only")
            findings_data = json.loads(args.findings.read_text())
            summary = validate_pr_summary(findings_data, manifest)
            findings = validate_findings(findings_data, manifest, impact)
            accepted = confirmed(findings, json.loads(args.verdicts.read_text()))
            if args.ledger is None:
                new_ledger = apply_ledger_updates([], accepted, [], manifest["pr"]["head_sha"])
                update_count = 0
            else:
                if not (args.verifications and args.verification_verdicts and args.ledger_output and args.update_count is not None):
                    raise EvidenceError("--ledger requires --verifications, --verification-verdicts, --update-count, --ledger-output")
                ledger = json.loads(args.ledger.read_text()) if args.ledger.exists() else []
                verifications = validate_verifications(json.loads(args.verifications.read_text()), ledger, manifest)
                resolved = confirm_verifications(verifications, json.loads(args.verification_verdicts.read_text()))
                new_ledger = apply_ledger_updates(ledger, accepted, resolved, manifest["pr"]["head_sha"])
                update_count = args.update_count + 1
        format_name = "standard" if args.verification_only else manifest.get("format", "standard")
        if format_name == "custom":
            if not args.skill_results:
                raise EvidenceError("Custom format requires --skill-results")
            format_skills, skill_results = custom_skill_results(manifest, json.loads(args.skill_results.read_text()))
            if args.prepare_custom and not args.custom_format and not args.custom_verdict:
                args.prepare_custom.write_text(json.dumps({
                    "pr_summary": summary, "findings": accepted,
                    "ledger": new_ledger, "update_count": update_count,
                    "review_number": update_count + 1, "format_skills": format_skills,
                    "skill_results": skill_results,
                }, indent=2))
                print(json.dumps({"custom_input_file": str(args.prepare_custom),
                                  "confirmed": len(accepted), "update_count": update_count}))
                return
            if not args.custom_format or not args.custom_verdict or args.prepare_custom:
                raise EvidenceError("Custom format requires --custom-format and --custom-verdict, or --prepare-custom first")
            format_verdict = json.loads(args.custom_verdict.read_text())
            validate_format_verdict(format_verdict, format_skills)
            body, comments = render_custom(manifest, new_ledger, accepted, update_count,
                                           json.loads(args.custom_format.read_text()), summary)
        elif format_name == "standard":
            if args.custom_format or args.custom_verdict or args.prepare_custom or args.skill_results:
                raise EvidenceError("Custom formatting options require format: custom")
            if args.verification_only:
                body = render_ledger(manifest, new_ledger, [], update_count, summary,
                                     last_verification)
                comments = []
            else:
                body = render_review(manifest, accepted, summary) if args.ledger is None else render_ledger(
                    manifest, new_ledger, accepted, update_count, summary
                )
                comments = render_inline_comments(accepted)
        else:
            raise EvidenceError("Unknown review format")
        ensure_public_content(body, "Status review")
        for comment in comments:
            ensure_public_content(comment["body"], f"Finding {comment['id']}")
        args.publication_output.write_text(json.dumps({"body": body}, indent=2))
        if args.ledger_output:
            args.ledger_output.write_text(json.dumps(new_ledger, indent=2))
        if args.inline_comments_output:
            args.inline_comments_output.write_text(json.dumps(comments, indent=2))
        print(json.dumps({
            "confirmed": len(accepted), "proposed": len(findings),
            "ledger_size": len(new_ledger), "update_count": update_count,
            "publication_file": str(args.publication_output),
            "ledger_file": str(args.ledger_output) if args.ledger_output else None,
            "inline_comments_file": str(args.inline_comments_output) if args.inline_comments_output else None,
        }))
    except (EvidenceError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Review evidence invalid: {exc}\n")


if __name__ == "__main__":
    main()
