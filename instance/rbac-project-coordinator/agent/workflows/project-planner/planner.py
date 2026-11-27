#!/usr/bin/env python3
"""Build and publish the plan requested on a coordinator control ticket."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / ".claude" / "skills"))
sys.path.insert(0, str(Path.cwd() / "presets" / "shared" / "preflight"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "project-coordinator"))
from actions import COMMAND_HELP_FOOTER  # noqa: E402
from jira_mcp import jira_call, jira_cleanup  # noqa: E402
from project_coordinator import (  # noqa: E402
    PLANNER_RECEIPT,
    comments,
    fabric_setting,
    key_of,
    labels,
    parent_key,
    planner_focus,
    planner_receipt,
    planner_request_path,
    implementation_children,
    linked_issues,
    planner_ticket_snapshot,
    search_all,
    status_name,
    ticket_plan_digest,
    value,
)

COMMENT_LIMIT = 100
REVIEW_AGENT = Path(__file__).resolve().parents[4] / "rbac-config-review" / "agent"
PARENT_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*")


def in_sandbox() -> bool:
    """The private sandbox marks itself so subprocess helpers can tell."""
    return os.environ.get("REHOR_INPUT_SOURCE", "").strip() == "sandbox"


def request_settings(control_key: str, comment_id: str, preflight_parent: str | None) -> dict:
    """Read the preflight handoff settings for one planner request.

    Legacy preflights write data/planner-requests/<control>-<comment>.json
    before the agent starts; outside the sandbox that file stays the only
    source and the extra parent argument is rejected. The dispatcher
    preflight cannot write into the sandbox, so a sandbox build carries the
    preflight's parent_key and derives the remaining settings in memory from
    configured environment names only; current_request then revalidates
    request eligibility against live Jira.
    """
    # Validates the control key and comment ID with the shared contract; the
    # sandbox path uses the result only for that check and never touches the
    # file itself.
    path = planner_request_path(control_key, comment_id)
    if in_sandbox():
        if preflight_parent is None:
            raise RuntimeError(
                f"{control_key}: sandbox planner builds must pass the preflight parent_key "
                "as the third build argument"
            )
        if not PARENT_KEY_RE.fullmatch(preflight_parent):
            raise ValueError("Invalid parent issue key for the planner handoff")
        control_label = os.environ.get("BOT_LABEL", "").strip()
        if not control_label:
            raise RuntimeError("BOT_LABEL is required to build planner request settings")
        return {
            "control_label": control_label,
            "parent_key": preflight_parent,
            "docs_base": os.getenv("BOT_PLANNER_DOCS_BASE_URL", "").strip(),
            "docs_glob": os.getenv("BOT_PLANNER_DOCS_GLOB", "").strip() or "*.md",
        }
    if preflight_parent is not None:
        raise RuntimeError("The parent_key build argument is only valid in the sandbox")
    return json.loads(path.read_text(encoding="utf-8"))


def plain_text(value: object) -> str:
    """Read Jira Server text and Jira Cloud's Atlassian Document Format."""
    if isinstance(value, str):
        if value.lstrip().startswith("{"):
            try:
                parsed = json.loads(value)
            except ValueError:
                return value
            if isinstance(parsed, dict) and parsed.get("type") == "doc":
                return plain_text(parsed)
        return value
    if not isinstance(value, dict):
        return ""
    kind = value.get("type")
    if kind == "text":
        return value.get("text") or ""
    if kind == "hardBreak":
        return "\n"
    if kind in {"inlineCard", "blockCard"}:
        attrs = value.get("attrs")
        url = attrs.get("url") if isinstance(attrs, dict) else None
        return url if isinstance(url, str) else ""
    inner = "".join(plain_text(node) for node in value.get("content") or [])
    return inner + "\n" if kind in {"paragraph", "heading", "codeBlock", "listItem"} else inner


def call(tool: str, args: dict) -> dict:
    result = jira_call(tool, args, timeout=15)
    if not isinstance(result, dict) or result.get("isError") or result.get("success") is False:
        raise RuntimeError(f"{tool} failed for {args['issue_key']}; see the Jira MCP log")
    return result


def get_issue(key: str, *, comments: bool = False, fields: str = "*all") -> dict:
    issue = call("jira_get_issue", {
        "issue_key": key, "fields": fields, "comment_limit": COMMENT_LIMIT if comments else 0,
    })
    if key_of(issue) != key:
        raise RuntimeError(f"Jira response did not contain issue {key}")
    return issue


def issue_context(issue: dict) -> dict:
    return {
        "key": key_of(issue),
        "summary": plain_text(value(issue, "summary")),
        "description": plain_text(value(issue, "description")),
        "repo_labels": sorted({
            label for label in labels(issue)
            if isinstance(label, str) and label.startswith("repo:")
        }),
        "labels": sorted(label for label in labels(issue) if isinstance(label, str)),
        "status": status_name(issue),
        "blockers": sorted(linked_issues(issue)[0]),
        "related": sorted(linked_issues(issue)[1]),
    }


def load_jira_document(key: str) -> dict:
    issue = get_issue(key, fields="summary,description")
    return {**issue_context(issue), "url": issue.get("url")}


def current_request(control_key: str, comment_id: str, preflight_parent: str | None = None) -> dict:
    settings = request_settings(control_key, comment_id, preflight_parent)
    control = get_issue(control_key, comments=True)
    parent = parent_key(control)
    if settings["control_label"] not in labels(control) or not parent or settings["parent_key"] != parent:
        raise RuntimeError(f"{control_key}: the control label or parent changed after preflight")
    items = comments(control)
    matched = [item for item in items if str(item.get("id") or "") == comment_id]
    focus = planner_focus(matched[0]) if len(matched) == 1 else None
    if focus is None:
        raise RuntimeError(f"{control_key}: planner command comment was removed or changed")
    if any(planner_receipt(item) == comment_id for item in items):
        raise RuntimeError(f"{control_key}: that planner command already has a published plan")
    parent_issue = get_issue(parent, fields="summary,description,labels,priority,issuetype,issuelinks")
    parent_context = issue_context(parent_issue)
    control_context = issue_context(control)
    if in_sandbox():
        team_field_id = fabric_setting("TEAM_FIELD_ID")
        if not re.fullmatch(r"customfield_\d+", team_field_id):
            raise ValueError("FABRIC_TEAM_FIELD_ID must be a customfield_ number")
    else:
        # Legacy parity: always the BOT_ name, even when a FABRIC_ alias is
        # present; a malformed value keeps the historical silent skip.
        team_field_id = os.getenv("BOT_TEAM_FIELD_ID", "")
    implementation_tickets = []
    ticket_snapshot_hash = ""
    if re.fullmatch(r"customfield_\d+", team_field_id):
        siblings = search_all(f'parent = "{parent}" ORDER BY key ASC')
        children = implementation_children(siblings, control, team_field_id)
        implementation_tickets = [issue_context(item) for item in children]
        ticket_snapshot_hash = ticket_plan_digest(planner_ticket_snapshot(children, team_field_id))
    repo_catalog = json.loads((REVIEW_AGENT / "project-repos.json").read_text(encoding="utf-8"))
    data = {
        "control_key": control_key,
        "comment_id": comment_id,
        "control": control_context,
        "parent": parent_context,
        "implementation_tickets": implementation_tickets,
        "ticket_snapshot_hash": ticket_snapshot_hash,
        "available_repositories": sorted(repo_catalog),
        "focus": focus,
        "docs_base": settings["docs_base"],
        "docs_glob": settings["docs_glob"],
        "warnings": [],
    }
    request_id = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return {**data, "request_id": request_id}


def publish(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan = ""
    if manifest.get("context_available"):
        plan = manifest_path.with_name("plan.md").read_text(encoding="utf-8").strip()
        if not plan or len(plan) > 24_000:
            raise ValueError("plan.md must contain between 1 and 24,000 characters")
        assignments = re.search(r"```planner-coordination\s*(.*?)\s*```", plan, re.I | re.S)
        expected_tickets = {item["key"] for item in manifest.get("implementation_tickets", [])}
        if not assignments:
            raise ValueError("plan.md must include a planner-coordination JSON block")
        try:
            structured = json.loads(assignments[1])
        except json.JSONDecodeError as error:
            raise ValueError("planner-coordination block must contain valid JSON") from error
        ticket_assignments = structured.get("ticket_assignments") if isinstance(structured, dict) else None
        if not isinstance(ticket_assignments, dict) or set(ticket_assignments) != expected_tickets:
            raise ValueError("planner-coordination must assign every current same-Team implementation ticket exactly once")
        if structured.get("input_snapshot_hash") != manifest.get("ticket_snapshot_hash"):
            raise ValueError("planner-coordination input_snapshot_hash must match the supplied ticket snapshot")
        available = set(manifest.get("available_repositories", []))
        for key, assignment in ticket_assignments.items():
            if (not isinstance(assignment, dict)
                    or not re.fullmatch(r"phase:\d{2}", str(assignment.get("phase") or ""))
                    or assignment.get("repository") not in available):
                raise ValueError(f"planner-coordination has an invalid phase or repository for {key}")
    control_key, comment_id = manifest["control_key"], manifest["comment_id"]
    if in_sandbox():
        # The sandbox handoff is file-free: rebuild the request from the
        # manifest's parent key and the same trusted environment. The
        # recomputed fingerprint below then catches any drift since the build.
        manifest_parent = manifest.get("parent")
        expected_parent = manifest_parent.get("key") if isinstance(manifest_parent, dict) else None
        if not isinstance(expected_parent, str) or not expected_parent.strip():
            raise ValueError(f"{control_key}: the manifest parent key is required to revalidate the request")
        request = current_request(control_key, comment_id, expected_parent)
    else:
        request = current_request(control_key, comment_id)
    if manifest["request_id"] != request["request_id"]:
        raise RuntimeError(f"{control_key}: the planner inputs changed after the plan was drafted")
    warnings = manifest["warnings"]
    parent = request["parent"]["key"]
    if not manifest.get("context_available"):
        control = request.get("control") or {}
        has_declarations = bool(
            re.search(r"(?im)^\s*project documentation\s*:", control.get("description") or "")
        )
        if not control.get("repo_labels") and not has_declarations:
            missing = "No repo: labels are set on the parent and no Project documentation is declared on the control ticket."
        else:
            missing = "No readable project documentation or repository context was available; see the context gaps below."
        body = f"PLANNER: Planner update - Context required for {parent}\n\n{missing}\n"
        body += "The planner did not create a technical plan. Add project documentation to the control ticket or resolve the listed context gaps, then post `PLANNER: /coord plan` again."
    else:
        body = f"PLANNER: Planner update - Technical plan for {parent}\n\n{plan}\n"
    if warnings:
        body += "**Context gaps**\n\n" + "".join(f"- {warning}\n" for warning in warnings) + "\n"
    if manifest.get("context_available"):
        body += (
            "### Possible next steps\n"
            "- Ask the coordinator to publish ticket proposals for plan work without Jira tickets.\n"
            "- After the proposal list is published, use `COORDINATOR: /coord create plan` for all proposals without Jira keys, "
            "`COORDINATOR: /coord create phase:01` for one phase, or "
            "`COORDINATOR: /coord create P-1,P-3` for selected items.\n"
            "- After ticket creation, request `PLANNER: /coord plan` again to assign the new Jira keys.\n\n"
        )
    body += f"\n{COMMAND_HELP_FOOTER}\n\n{PLANNER_RECEIPT} {comment_id}"
    comment = call("jira_add_comment", {"issue_key": control_key, "comment": body})
    return {"status": "published", "control_key": control_key, "comment_id": comment.get("id"), "warnings": warnings}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    build = commands.add_parser("build")
    build.add_argument("control_key")
    build.add_argument("comment_id")
    build.add_argument(
        "parent_key", nargs="?",
        help="preflight parent_key; required in the sandbox and rejected outside it",
    )
    post = commands.add_parser("publish")
    post.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "build":
            from build_planner_context import build_context

            result = build_context(
                current_request(args.control_key, args.comment_id, args.parent_key),
                load_jira_document=load_jira_document,
            )
        else:
            result = publish(args.manifest)
        print(json.dumps(result))
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        parser.exit(1, f"Planner: {exc}\n")
    finally:
        jira_cleanup()


if __name__ == "__main__":
    main()
