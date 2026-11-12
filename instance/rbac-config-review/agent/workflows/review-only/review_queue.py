"""Find one labeled GitHub PR requiring a review or feedback check."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from common import INSTANCE_ID, MEMORY_URL, http_get, load_project_repos, output_result
from review_config import ReviewConfigError, github_repo_url, load_config
from review_ledger import (
    EmptyPendingReview,
    LedgerError,
    LedgerPayloadMissing,
    decode_body,
    public_ledger,
    restore_from_review,
    status_summary,
    write_cycle_ledger,
)
from rereview_requests import (
    VERIFY_COMMAND,
    fetch_comments,
    requests as explicit_command_requests,
    requests as rereview_requests,
)

TASK_PREFIX = "review-only:github:"
ACTIVE = {"in_progress", "pr_open", "pr_changes"}
RESUMABLE = {"in_progress", "pr_changes"}
PR_FIELDS = "number,title,url,headRefOid,baseRefOid,headRefName,isDraft,author,updatedAt"
PR_STATE_FIELDS = "state,mergedAt,closedAt,headRefOid,baseRefOid,updatedAt,url,title,headRefName"
REVIEWABLE_BOT_AUTHORS = {"platex-rehor-bot"}
APPROVAL_WAITING_LABEL = "dev-bot-review-waiting-for-approval"


def task_key(repo: str, number: int) -> str:
    return f"{TASK_PREFIX}{repo}#{number}"


def load_tasks() -> list[dict]:
    """Load all task states so archived keys suppress duplicate task creation."""
    tasks: list[dict] = []
    offset = 0
    while True:
        params = {"limit": 100, "offset": offset}
        if INSTANCE_ID:
            params["instance_id"] = INSTANCE_ID
        data = http_get(f"{MEMORY_URL}/api/tasks?{urlencode(params)}")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise RuntimeError("Memory task list unavailable")
        items = data["items"]
        tasks.extend(
            item for item in items
            if item.get("source_type") == "github"
            and item.get("external_key", "").startswith(TASK_PREFIX)
        )
        offset += len(items)
        if not items or offset >= data.get("total", offset):
            return tasks


def list_prs(repo: str, label: str) -> list[dict]:
    result = subprocess.run(
        [
            "gh", "pr", "list", "--repo", repo, "--label", label, "--state", "open",
            "--limit", "1000", "--json", PR_FIELDS,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"gh pr list {repo}: {result.stderr.strip()[:300]}")
    data = json.loads(result.stdout)
    if not isinstance(data, list):
        raise RuntimeError(f"gh pr list {repo} returned no list")
    return data


def view_pr_state(repo: str, number: int) -> dict:
    result = subprocess.run(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json", PR_STATE_FIELDS],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"gh pr view {repo}#{number}: {result.stderr.strip()[:300]}")
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or data.get("state") not in {"OPEN", "CLOSED", "MERGED"}:
        raise RuntimeError(f"gh pr view {repo}#{number} returned an invalid state")
    return data


def _metadata(task: dict) -> dict:
    meta = task.get("metadata") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    return meta if isinstance(meta, dict) else {}


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _ledger_recovery_id(chosen: dict, meta: dict) -> str:
    key = "|".join(str(value or "") for value in (
        chosen["repo"], chosen["number"], meta.get("review_id"),
        meta.get("last_reviewed_sha"), meta.get("last_reviewed_base_sha"),
        chosen["head_sha"], chosen["base_sha"],
    ))
    return "LR-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


_ORG_MEMBER_CACHE: dict[tuple[str, str], bool] = {}


def _is_org_member(org: str, login: str) -> bool:
    """Check private or public organization membership using the bot's GH auth."""
    key = (org.casefold(), login.casefold())
    if key in _ORG_MEMBER_CACHE:
        return _ORG_MEMBER_CACHE[key]
    endpoint = f"orgs/{quote(org, safe='')}/members/{quote(login, safe='')}"
    result = subprocess.run(
        ["gh", "api", "--include", endpoint],
        capture_output=True, text=True, timeout=15, check=False,
    )
    response = result.stdout + "\n" + result.stderr
    status = re.search(r"(?m)^HTTP/\S+\s+(\d{3})\b", response)
    if status and status.group(1) == "204" and result.returncode == 0:
        _ORG_MEMBER_CACHE[key] = True
        return True
    if status and status.group(1) == "404":
        _ORG_MEMBER_CACHE[key] = False
        return False
    reason = response.strip().splitlines()[-1:] or ["GitHub returned no HTTP status"]
    raise RuntimeError(f"Could not verify @{login}'s membership in {org}: {reason[0][:200]}")


def _request_ledger_recovery(chosen: dict, meta: dict) -> bool:
    """Require an organization member's approval before discarding review history."""
    recovery_id = _ledger_recovery_id(chosen, meta)
    marker = f"<!-- dev-review-bot:ledger-recovery:{recovery_id} -->"
    comments = fetch_comments(chosen["repo"], chosen["number"])
    bot_login = _bot_login()
    requests = []
    for comment in comments:
        user = comment.get("user") or {}
        body = comment.get("body")
        login = user.get("login") if isinstance(user, dict) else None
        comment_id = comment.get("id")
        if (isinstance(login, str) and login.casefold() == bot_login.casefold()
                and isinstance(comment_id, int) and not isinstance(comment_id, bool) and comment_id > 0
                and isinstance(body, str) and marker in body):
            created = _parse_timestamp(comment.get("created_at"))
            if created is not None:
                requests.append((created, comment))

    if not requests:
        body = (
            f"**Review history needs recovery ({recovery_id}).**\n\n"
            "I could not find a usable ledger in the status review, saved publication, or local ledger. "
            "A fresh review will restart the finding history and may assign new finding IDs. "
            "Should a fresh re-review run? Any member of the repository's GitHub organization can "
            f"include `Review bot: yes {recovery_id}` to approve, or "
            f"`Review bot: no {recovery_id}` to decline, anywhere in their comment.\n\n{marker}"
        )
        result = subprocess.run(
            ["gh", "api", f"repos/{chosen['repo']}/issues/{chosen['number']}/comments",
             "--method", "POST", "--field", f"body={body}"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Cannot ask about ledger recovery: {result.stderr.strip()[:300]}")
        chosen["ledger_recovery_state"] = "asked"
        return False

    request_at, request_comment = min(
        requests, key=lambda item: (item[0], item[1]["id"])
    )
    request_id = request_comment["id"]
    org = chosen["repo"].split("/", 1)[0]
    response_pattern = re.compile(
        rf"Review bot:\s*(yes|no)\s+{re.escape(recovery_id)}(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    )
    answers = []
    for comment in comments:
        user = comment.get("user") or {}
        created = _parse_timestamp(comment.get("created_at"))
        comment_id = comment.get("id")
        body = comment.get("body")
        if (not isinstance(user, dict) or user.get("type") != "User"
                or not isinstance(user.get("login"), str)
                or created is None
                or not isinstance(comment_id, int) or isinstance(comment_id, bool)
                or (created, comment_id) <= (request_at, request_id)
                or not isinstance(body, str)):
            continue
        decisions = {match.group(1).casefold() for match in response_pattern.finditer(body)}
        if len(decisions) != 1:
            continue
        try:
            member = _is_org_member(org, user["login"])
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            chosen["ledger_recovery_state"] = "verification_unavailable"
            return False
        if member:
            answers.append((created, comment_id, decisions.pop() == "yes", user["login"]))
    if not answers:
        chosen["ledger_recovery_state"] = "waiting"
        return False
    _answer_at, response_id, approved, responder = max(answers, key=lambda item: (item[0], item[1]))
    chosen["ledger_recovery_responder"] = responder
    chosen["ledger_recovery_response_comment_id"] = response_id
    chosen["ledger_recovery_state"] = "approved" if approved else "declined"
    return approved


def restore_prior_ledger(chosen: dict, task: dict | None) -> bool:
    """Restore the status-review ledger, then saved copies, before any model run."""
    if chosen.get("kind") not in {"review", "resume", "feedback_check", "verify"}:
        return True
    if task is None:
        if chosen.get("kind") == "verify":
            chosen["ledger_recovery_state"] = "unavailable"
            return False
        chosen["update_count"] = 0
        return True
    meta = _metadata(task)
    had_review = bool(meta.get("review_id") or meta.get("last_reviewed_sha")
                      or meta.get("update_count", 0) or task.get("status") in {"pr_open", "pr_changes"})
    if not had_review:
        if chosen.get("kind") == "verify":
            chosen["ledger_recovery_state"] = "unavailable"
            return False
        chosen["update_count"] = 0
        return True

    payload = None
    empty_pending: EmptyPendingReview | None = None
    review_id = meta.get("review_id")
    if isinstance(review_id, int) and not isinstance(review_id, bool) and review_id > 0:
        try:
            payload = restore_from_review(
                chosen["repo"], chosen["number"], review_id, _bot_login(), chosen["head_sha"]
            )
        except EmptyPendingReview as exc:
            empty_pending = exc
        except LedgerPayloadMissing:
            # Upgrade path for existing reviews: use an old local ledger once,
            # then the next published status comment carries the durable copy.
            payload = None
    elif review_id is not None:
        raise LedgerError("Tracked review ID is invalid")

    # The published status review is authoritative. An interrupted publication
    # is the next recoverable copy, followed by the legacy local ledger.
    ledger_available = payload is not None
    if payload is None:
        pending = meta.get("pending_publication")
        pending_body = pending.get("publication_body") if isinstance(pending, dict) else None
        if isinstance(pending_body, str):
            try:
                pending_ledger = decode_body(pending_body, chosen["repo"], chosen["number"])
            except LedgerPayloadMissing:
                pending_ledger = None
            if (pending_ledger is not None
                    and pending_ledger["head_sha"] == chosen["head_sha"]
                    and pending_ledger["base_sha"] == chosen["base_sha"]):
                pending_ledger["status_summary"] = status_summary(pending_body)
                payload = pending_ledger

    if payload is None:
        legacy_path = meta.get("ledger_path")
        if isinstance(legacy_path, str) and Path(legacy_path).is_file():
            try:
                ledger = public_ledger(json.loads(Path(legacy_path).read_text()))
            except (OSError, json.JSONDecodeError) as exc:
                raise LedgerError(f"Could not read the legacy ledger: {exc}") from exc
            update_count = meta.get("update_count", 0)
            if not isinstance(update_count, int) or isinstance(update_count, bool) or update_count < 0:
                raise LedgerError("Legacy review update_count is invalid")
            ledger_available = True
        else:
            if chosen.get("kind") == "verify":
                chosen["ledger_recovery_state"] = "unavailable"
                return False
            if not _request_ledger_recovery(chosen, meta):
                return False
            chosen["update_count"] = 0
            chosen["ledger_recovery_approved"] = True
            chosen["kind"] = "review"
            chosen["feedback_comment_ids"] = [
                ident for ident in (chosen.get("feedback_comment_ids") or [])
                if ident != f"issue:{chosen['ledger_recovery_response_comment_id']}"
            ]
            if empty_pending is not None and not empty_pending.reusable:
                chosen["ignore_review_id"] = True
            return True
    else:
        ledger = payload["findings"]
        update_count = payload["update_count"]
        ledger_available = True
        if isinstance(payload.get("status_summary"), str):
            chosen["pr_summary"] = payload["status_summary"]

    if chosen.get("kind") == "verify" and not ledger_available:
        chosen["ledger_recovery_state"] = "unavailable"
        return False

    chosen["ledger_path"] = write_cycle_ledger(
        chosen["repo_key"], chosen["number"], chosen["head_sha"], chosen["base_sha"], ledger
    )
    chosen["update_count"] = update_count
    if empty_pending is not None and not empty_pending.reusable:
        chosen["ignore_review_id"] = True
    return True


def _bot_login() -> str:
    configured = os.environ.get("GH_USER_NAME", "").strip()
    if configured:
        return configured
    result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Cannot identify the GitHub bot account")
    return result.stdout.strip()


def _api_items(endpoint: str) -> list[dict]:
    result = subprocess.run(
        ["gh", "api", endpoint, "--paginate", "--slurp"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Cannot inspect PR feedback: {result.stderr.strip()[:300]}")
    pages = json.loads(result.stdout)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("GitHub returned invalid PR feedback pages")
    return [item for page in pages for item in page if isinstance(item, dict)]


def _latest_review_label_event(repo: str, number: int, label: str) -> dict | None:
    """Return the newest GitHub event that added the configured review label."""
    events = _api_items(f"repos/{repo}/issues/{number}/events?per_page=100")
    matches = []
    for event in events:
        event_label = event.get("label") or {}
        event_id = event.get("id")
        created = _parse_timestamp(event.get("created_at"))
        if (event.get("event") == "labeled" and isinstance(event_label, dict)
                and isinstance(event_label.get("name"), str)
                and event_label["name"].casefold() == label.casefold()
                and isinstance(event_id, int) and not isinstance(event_id, bool) and event_id > 0
                and created is not None):
            matches.append((created, event_id, event))
    if not matches:
        return None
    _created, _event_id, event = max(matches, key=lambda item: (item[0], item[1]))
    return {"id": event["id"], "created_at": event["created_at"]}


def _is_new_review_label_event(event: dict | None, task: dict | None) -> bool:
    if event is None:
        return False
    meta = _metadata(task or {})
    event_id = event["id"]
    saved_id = meta.get("review_label_event_id")
    if isinstance(saved_id, int) and not isinstance(saved_id, bool):
        return event_id > saved_id
    if saved_id is not None:
        return False
    if task is None:
        return True
    if meta.get("last_reviewed_sha"):
        reviewed_at = _parse_timestamp(meta.get("last_reviewed_at"))
        added_at = _parse_timestamp(event.get("created_at"))
        return reviewed_at is not None and added_at is not None and added_at > reviewed_at
    return True


def _apply_review_label_event(item: dict, event: dict | None, task: dict | None) -> None:
    meta = _metadata(task or {})
    if event is not None:
        item["review_label_event_id"] = event["id"]
    elif isinstance(meta.get("review_label_event_id"), int):
        item["review_label_event_id"] = meta["review_label_event_id"]
    if _is_new_review_label_event(event, task) and item.get("kind") != "resume":
        item["kind"] = "review"
        item["priority"] = 1
        item["review_authorization"] = "label_added"
    elif item.get("kind") == "review":
        item["review_authorization"] = "approval_required"


def _review_approval_id(chosen: dict, task: dict | None) -> str:
    meta = _metadata(task or {})
    command_ids = chosen.get("rereview_comment_ids") or []
    trigger = "rereview:" + ",".join(str(item) for item in sorted(command_ids)) if command_ids else "revision"
    key = "|".join(str(value or "") for value in (
        chosen["repo"], chosen["number"], meta.get("last_reviewed_sha"),
        meta.get("last_reviewed_base_sha"), chosen["head_sha"], chosen["base_sha"], trigger,
    ))
    return "RA-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _api_status(result: subprocess.CompletedProcess) -> int | None:
    response = result.stdout + "\n" + result.stderr
    match = re.search(r"(?m)^HTTP/\S+\s+(\d{3})\b", response)
    return int(match.group(1)) if match else None


def _set_approval_waiting_label(repo: str, number: int, present: bool) -> None:
    """Keep the approval-waiting label in sync with the approval state."""
    label_path = f"repos/{repo}/labels/{quote(APPROVAL_WAITING_LABEL, safe='')}"
    issue_label_path = f"repos/{repo}/issues/{number}/labels"
    if present:
        lookup = subprocess.run(
            ["gh", "api", "--include", label_path],
            capture_output=True, text=True, timeout=15, check=False,
        )
        status = _api_status(lookup)
        if lookup.returncode and status == 404:
            create = subprocess.run(
                ["gh", "api", f"repos/{repo}/labels", "--method", "POST",
                 "--field", f"name={APPROVAL_WAITING_LABEL}",
                 "--field", "color=ededed",
                 "--field", "description=Review is waiting for organization-member approval."],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if create.returncode:
                # Another cycle may have created it after our lookup.
                lookup = subprocess.run(
                    ["gh", "api", "--include", label_path],
                    capture_output=True, text=True, timeout=15, check=False,
                )
                if lookup.returncode or _api_status(lookup) != 200:
                    raise RuntimeError(
                        f"Cannot create approval label for {repo}: {create.stderr.strip()[:300]}"
                    )
        elif lookup.returncode or status != 200:
            raise RuntimeError(
                f"Cannot check approval label for {repo}: {lookup.stderr.strip()[:300]}"
            )
        result = subprocess.run(
            ["gh", "api", issue_label_path, "--method", "POST",
             "--field", f"labels[]={APPROVAL_WAITING_LABEL}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"Cannot add approval label to {repo}#{number}: {result.stderr.strip()[:300]}"
            )
        return

    result = subprocess.run(
        ["gh", "api", "--include",
         f"{issue_label_path}/{quote(APPROVAL_WAITING_LABEL, safe='')}",
         "--method", "DELETE"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode and _api_status(result) != 404:
        raise RuntimeError(
            f"Cannot remove approval label from {repo}#{number}: {result.stderr.strip()[:300]}"
        )


def _request_review_approval(chosen: dict, task: dict | None) -> dict:
    """Ask for an organization member's approval, scoped to this revision."""
    approval_id = _review_approval_id(chosen, task)
    marker = f"<!-- dev-review-bot:review-approval:{approval_id} -->"
    comments = fetch_comments(chosen["repo"], chosen["number"])
    bot_login = _bot_login()
    requests = []
    for comment in comments:
        user = comment.get("user") or {}
        if (isinstance(user, dict) and isinstance(user.get("login"), str)
                and user["login"].casefold() == bot_login.casefold()
                and isinstance(comment.get("body"), str) and marker in comment["body"]):
            created = _parse_timestamp(comment.get("created_at"))
            ident = comment.get("id")
            if (created is not None and isinstance(ident, int)
                    and not isinstance(ident, bool) and ident > 0):
                requests.append((created, ident, comment))
    if not requests:
        if chosen.get("rereview_comment_ids"):
            reason = "A re-review was requested without a new review-label event."
        elif task and _metadata(task).get("last_reviewed_sha"):
            reason = "The PR head or base changed since the last review."
        else:
            reason = "The configured review label has no new label-add event."
        body = (
            f"### Review approval required\n\n"
            f"**Request:** `{approval_id}`\n\n"
            f"**Reason:** {reason}\n\n"
            "**Target revision**\n"
            f"- **Head:** `{chosen['head_sha'][:12]}`\n"
            f"- **Base:** `{chosen['base_sha'][:12]}`\n\n"
            "No review will run until an organization member approves this exact revision.\n\n"
            "**Reply with one option:**\n"
            f"- `Review bot: yes {approval_id}` — approve and start the review.\n"
            f"- `Review bot: no {approval_id}` — decline the review.\n\n{marker}"
        )
        _set_approval_waiting_label(chosen["repo"], chosen["number"], True)
        result = subprocess.run(
            ["gh", "api", f"repos/{chosen['repo']}/issues/{chosen['number']}/comments",
             "--method", "POST", "--field", f"body={body}"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"Cannot ask for review approval: {result.stderr.strip()[:300]}")
        return {"state": "waiting", "approval_id": approval_id}

    request_at, request_id, _request_comment = min(requests, key=lambda item: (item[0], item[1]))
    org = chosen["repo"].split("/", 1)[0]
    response_pattern = re.compile(
        rf"Review bot:\s*(yes|no)\s+{re.escape(approval_id)}(?![A-Za-z0-9_-])",
        re.IGNORECASE,
    )
    answers = []
    for comment in comments:
        user = comment.get("user") or {}
        created = _parse_timestamp(comment.get("created_at"))
        ident = comment.get("id")
        body = comment.get("body")
        if (not isinstance(user, dict) or user.get("type") != "User"
                or not isinstance(user.get("login"), str) or created is None
                or not isinstance(ident, int) or isinstance(ident, bool)
                or (created, ident) <= (request_at, request_id)
                or not isinstance(body, str)):
            continue
        decisions = {match.group(1).casefold() for match in response_pattern.finditer(body)}
        if len(decisions) != 1:
            continue
        login = user["login"]
        try:
            member = _is_org_member(org, login)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return {"state": "verification_unavailable", "approval_id": approval_id}
        if member:
            answers.append((created, ident, decisions.pop() == "yes", login))
    if not answers:
        _set_approval_waiting_label(chosen["repo"], chosen["number"], True)
        return {"state": "waiting", "approval_id": approval_id}
    _at, response_id, approved, responder = max(answers, key=lambda item: (item[0], item[1]))
    _set_approval_waiting_label(chosen["repo"], chosen["number"], False)
    return {
        "state": "approved" if approved else "declined",
        "approval_id": approval_id,
        "response_comment_id": response_id,
        "responder": responder,
    }


def _external_feedback_key(item: dict, since: str, through: str,
                           key_prefix: str, seen_ids: set[str], bot_login: str) -> str | None:
    if key_prefix == "issue" and isinstance(item.get("body"), str):
        # This command is a review-authorization request, not feedback to send
        # through the model. It is handled separately after org membership is
        # verified.
        if re.fullmatch(r"/review-bot\s+(?:re-review|verify)",
                        item["body"].strip(), re.IGNORECASE):
            return None
        # A negative ledger-recovery answer closes that request without making
        # every later preflight treat the same control comment as new feedback.
        if re.fullmatch(r"Review bot: no LR-[0-9a-f]{12}", item["body"].strip(), re.IGNORECASE):
            return None
    user = item.get("user") or item.get("author") or {}
    login = user.get("login") if isinstance(user, dict) else None
    if (not isinstance(login, str) or not login.strip()
            or user.get("type") == "Bot"
            or login.casefold() == bot_login.casefold()):
        return None
    timestamp = (item.get("updated_at") or item.get("created_at")
                 or item.get("submitted_at"))
    ident = item.get("id")
    if (not isinstance(timestamp, str) or not since <= timestamp <= through
            or not isinstance(ident, int) or isinstance(ident, bool) or ident <= 0):
        return None
    key = f"{key_prefix}:{ident}"
    return None if key in seen_ids else key


def external_feedback_ids(repo: str, number: int, since: str, through: str,
                          seen_ids: set[str]) -> list[str]:
    """Return same-window human feedback IDs, excluding bot-only PR updates."""
    found: list[str] = []
    bot_login = _bot_login()
    sources = [
        ("issue", fetch_comments(repo, number, since)),
        ("review-comment", _api_items(f"repos/{repo}/pulls/{number}/comments?per_page=100")),
        ("review", _api_items(f"repos/{repo}/pulls/{number}/reviews?per_page=100")),
    ]
    for prefix, items in sources:
        for item in items:
            key = _external_feedback_key(item, since, through, prefix, seen_ids, bot_login)
            if key:
                found.append(key)
    return found


def _eligible_rereview_ids(chosen: dict, task: dict | None, since: str) -> list[int]:
    if task is None:
        return []
    meta = _metadata(task)
    seen = meta.get("seen_rereview_comment_ids") or []
    if not isinstance(seen, list) or any(type(ident) is not int or ident <= 0 for ident in seen):
        raise ValueError("Invalid seen_rereview_comment_ids")
    comments = fetch_comments(chosen["repo"], chosen["number"], since)
    candidates = rereview_requests(
        comments, since, chosen["updated_at"], set(seen)
    )
    org = chosen["repo"].split("/", 1)[0]
    eligible = []
    for request in candidates:
        if _is_org_member(org, request["login"]):
            eligible.append(request["id"])
    return eligible


def _eligible_verify_ids(chosen: dict, task: dict | None, since: str) -> list[int]:
    """Find exact verification commands from organization members on reviewed PRs."""
    if task is None or task.get("status") != "pr_open":
        return []
    meta = _metadata(task)
    review_id = meta.get("review_id")
    if (not isinstance(review_id, int) or isinstance(review_id, bool) or review_id < 1
            or not (meta.get("last_reviewed_sha") or meta.get("update_count"))):
        return []
    seen = meta.get("seen_verify_comment_ids") or []
    if not isinstance(seen, list) or any(type(ident) is not int or ident <= 0 for ident in seen):
        raise ValueError("Invalid seen_verify_comment_ids")
    comments = fetch_comments(chosen["repo"], chosen["number"], since)
    candidates = explicit_command_requests(
        comments, since, chosen["updated_at"], set(seen), VERIFY_COMMAND
    )
    org = chosen["repo"].split("/", 1)[0]
    return [request["id"] for request in candidates
            if _is_org_member(org, request["login"])]


def _task_pr(task: dict, repos: dict) -> tuple[str, str, int] | None:
    key = task.get("external_key", "")
    if not key.startswith(TASK_PREFIX):
        return None
    target, separator, number = key[len(TASK_PREFIX):].rpartition("#")
    if not separator or not number.isdigit():
        return None
    for repo_key, entry in repos.items():
        try:
            repo = github_repo_url(entry["upstream"])
        except (KeyError, ReviewConfigError):
            continue
        if repo == target:
            return repo_key, repo, int(number)
    return None


def lifecycle_work(task: dict, repo_key: str, repo: str, number: int, pr: dict) -> dict | None:
    status = task.get("status")
    if status == "paused":
        return None
    meta = _metadata(task)
    common = {
        "priority": 0,
        "task_key": task["external_key"],
        "task_exists": True,
        "task_active": status in ACTIVE,
        "repo_key": repo_key,
        "repo": repo,
        "number": number,
        "title": pr.get("title") or task.get("title", ""),
        "url": pr.get("url") or f"https://github.com/{repo}/pull/{number}",
        "head_sha": pr.get("headRefOid") or meta.get("last_reviewed_sha"),
        "base_sha": pr.get("baseRefOid") or meta.get("last_reviewed_base_sha"),
        "updated_at": pr.get("updatedAt", ""),
    }
    if pr.get("state") == "MERGED" or pr.get("mergedAt"):
        if status == "done" and meta.get("merged_at"):
            return None
        return {**common, "kind": "merge_complete", "merged_at": pr.get("mergedAt")}
    if pr.get("state") == "CLOSED":
        return {**common, "kind": "closed_unmerged"}
    if pr.get("state") == "OPEN":
        if status == "done" and not meta.get("merged_at"):
            return {**common, "kind": "reconcile_open", "priority": 3}
    return None


def candidate(pr: dict, repo_key: str, repo: str, task: dict | None,
              pending_rereview: bool = False, force_review: bool = False) -> dict | None:
    if not isinstance(pr.get("number"), int):
        return None
    author = (pr.get("author") or {}).get("login", "")
    author_login = author.casefold() if isinstance(author, str) else ""
    bot_login = os.environ.get("GH_USER_NAME", "").strip().casefold()
    if author_login and author_login == bot_login and author_login not in REVIEWABLE_BOT_AUTHORS:
        return None
    sha = pr.get("headRefOid")
    base_sha = pr.get("baseRefOid")
    if not isinstance(sha, str) or not sha or not isinstance(base_sha, str) or not base_sha:
        return None
    meta = _metadata(task or {})
    status = (task or {}).get("status")
    if status == "paused":
        return None
    if pr.get("isDraft"):
        if status == "done" and not meta.get("merged_at"):
            kind, priority = "reconcile_open", 3
        else:
            return None
    elif status in RESUMABLE:
        active_head = meta.get("active_head_sha")
        active_base = meta.get("active_base_sha")
        if active_head == sha and active_base == base_sha:
            kind, priority = "resume", 0
        else:
            # A task without a matching active revision cannot safely resume
            # against a different or unknown snapshot. It needs fresh review
            # authorization, unless the label was just added.
            kind, priority = "review", 1
    elif force_review:
        kind, priority = "review", 1
    elif meta.get("last_reviewed_sha") != sha or meta.get("last_reviewed_base_sha") != base_sha:
        if (meta.get("last_verified_sha") == sha
                and meta.get("last_verified_base_sha") == base_sha):
            kind, priority = "feedback_check", 2
        else:
            kind, priority = "review", 1
    elif pr.get("updatedAt", "") > meta.get("last_seen_updated_at", "") or pending_rereview:
        kind, priority = "feedback_check", 2
    elif status == "done" and not meta.get("merged_at"):
        kind, priority = "reconcile_open", 3
    else:
        return None
    return {
        "kind": kind,
        "priority": priority,
        "task_key": task_key(repo, pr["number"]),
        "task_exists": task is not None,
        "task_active": status in ACTIVE,
        "repo_key": repo_key,
        "repo": repo,
        "number": pr["number"],
        "title": pr.get("title", ""),
        "url": pr.get("url", ""),
        "head_sha": sha,
        "base_sha": base_sha,
        "head_branch": pr.get("headRefName", ""),
        "pr_author": author,
        "updated_at": pr.get("updatedAt", ""),
    }


def pick_work(repos: dict, config: dict, tasks: list[dict],
              excluded_task_keys: set[str] | None = None) -> tuple[dict | None, list[str]]:
    excluded_task_keys = excluded_task_keys or set()
    by_key = {task["external_key"]: task for task in tasks}
    found: list[dict] = []
    errors: list[str] = []
    boundary_probes: list[tuple[dict, dict, str, str]] = []
    open_pr_keys: set[str] = set()
    failed_repos: set[str] = set()
    for repo_key, entry in repos.items():
        try:
            repo = github_repo_url(entry["upstream"])
        except (KeyError, ReviewConfigError):
            continue  # The review-only instance handles GitHub repositories.
        try:
            prs = list_prs(repo, config["review_label"])
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            errors.append(f"{repo}: {exc}")
            failed_repos.add(repo)
            continue
        for pr in prs:
            key = task_key(repo, pr.get("number"))
            open_pr_keys.add(key)
            if key in excluded_task_keys:
                continue
            task = by_key.get(key)
            if task and task.get("status") in {"archived", "paused"}:
                # Keep archived and paused tasks out of all review, feedback,
                # and lifecycle paths until an operator changes their status.
                continue
            item = candidate(pr, repo_key, repo, task)
            if item is None and task and task.get("status") in {"done", "pr_open"}:
                # updatedAt has second precision: a new command can share the
                # last-seen timestamp and otherwise never enter feedback_check.
                probe = candidate(pr, repo_key, repo, task, pending_rereview=True)
                meta = _metadata(task)
                since, through = meta.get("last_seen_updated_at"), pr.get("updatedAt")
                if probe and probe["kind"] == "feedback_check" and since and through and since <= through:
                    boundary_probes.append((probe, meta, since, through))
            if item and item["kind"] != "resume":
                try:
                    label_event = _latest_review_label_event(repo, pr["number"], config["review_label"])
                except (RuntimeError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
                    errors.append(f"{repo}#{pr['number']}: cannot verify review-label event: {exc}")
                    # Fail closed when GitHub cannot confirm that the label
                    # was just added. Changed revisions still require approval.
                    if item["kind"] == "review":
                        item["review_authorization"] = "approval_required"
                else:
                    _apply_review_label_event(item, label_event, task)
            if item:
                meta = _metadata(task or {})
                seen_feedback = meta.get("seen_feedback_comment_ids") or []
                if (not isinstance(seen_feedback, list)
                        or any(not isinstance(ident, str) for ident in seen_feedback)):
                    errors.append(f"{repo}#{pr['number']}: Invalid seen_feedback_comment_ids")
                    continue
                since = meta.get("last_seen_updated_at") or meta.get("last_reviewed_at") or "1970-01-01T00:00:00Z"
                try:
                    command_ids = _eligible_rereview_ids(item, task, since)
                    if command_ids:
                        item["rereview_comment_ids"] = command_ids
                        item["kind"] = "review"
                        item["priority"] = 1
                        item["review_authorization"] = "approval_required"
                    verify_ids = []
                    if (not command_ids
                            and item.get("review_authorization") != "label_added"
                            and item["kind"] != "resume"):
                        verify_ids = _eligible_verify_ids(item, task, since)
                    if verify_ids:
                        item["verify_comment_ids"] = verify_ids
                        item["kind"] = "verify"
                        item["priority"] = 1
                        item.pop("review_authorization", None)
                    feedback_ids = external_feedback_ids(
                        repo, pr["number"], since, item["updated_at"], set(seen_feedback)
                    ) if task and item["kind"] in {"review", "feedback_check"} else []
                    if item["kind"] != "feedback_check" or feedback_ids:
                        item["feedback_comment_ids"] = feedback_ids
                        found.append(item)
                except (RuntimeError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
                    if item["kind"] == "feedback_check":
                        errors.append(f"{repo}#{pr['number']}: {exc}")
                        continue
                    # An unverified comment must not authorize or trigger a
                    # full review; SHA changes remain gated by approval.
                    if item["kind"] == "review":
                        item["review_authorization"] = item.get("review_authorization", "approval_required")
                    item["feedback_comment_ids"] = []
                    found.append(item)
    for task in tasks:
        status = task.get("status")
        meta = _metadata(task)
        if status not in {"pr_open", "in_progress", "pr_changes"} and not (
            status == "done" and not meta.get("merged_at")
        ):
            continue
        task_ref = _task_pr(task, repos)
        if task_ref is None:
            continue
        repo_key, repo, number = task_ref
        if task["external_key"] in excluded_task_keys:
            continue
        if repo in failed_repos:
            continue
        if task_key(repo, number) in open_pr_keys:
            continue
        try:
            state = view_pr_state(repo, number)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            errors.append(f"{repo}#{number}: {exc}")
            continue
        item = lifecycle_work(task, repo_key, repo, number, state)
        if item:
            found.append(item)
    # Only probe timestamp-boundary comments when no higher-priority work is
    # ready. These requests are independently authorized by the runner.
    if not found:
        boundary_probes.sort(key=lambda entry: (entry[0]["updated_at"], entry[0]["repo"], entry[0]["number"]))
        for probe, meta, since, through in boundary_probes:
            if probe["task_key"] in excluded_task_keys:
                continue
            try:
                label_event = _latest_review_label_event(
                    probe["repo"], probe["number"], config["review_label"]
                )
                if _is_new_review_label_event(label_event, by_key.get(probe["task_key"])):
                    _apply_review_label_event(probe, label_event, by_key.get(probe["task_key"]))
                    found.append(probe)
                    break
                seen_feedback = meta.get("seen_feedback_comment_ids") or []
                if (not isinstance(seen_feedback, list)
                        or any(not isinstance(ident, str) for ident in seen_feedback)):
                    raise ValueError("Invalid seen_feedback_comment_ids")
                feedback_ids = external_feedback_ids(
                    probe["repo"], probe["number"], since, through, set(seen_feedback)
                )
                command_ids = _eligible_rereview_ids(probe, by_key.get(probe["task_key"]), since)
                verify_ids = []
                if not command_ids:
                    verify_ids = _eligible_verify_ids(probe, by_key.get(probe["task_key"]), since)
                if command_ids:
                    probe["rereview_comment_ids"] = command_ids
                    probe["kind"] = "review"
                    probe["priority"] = 1
                    probe["review_authorization"] = "approval_required"
                elif verify_ids:
                    probe["verify_comment_ids"] = verify_ids
                    probe["kind"] = "verify"
                    probe["priority"] = 1
                if command_ids or feedback_ids:
                    probe["feedback_comment_ids"] = feedback_ids
                    found.append(probe)
                    break
                if verify_ids:
                    found.append(probe)
                    break
            except (RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
                errors.append(f"{probe['repo']}#{probe['number']}: {exc}")
    found.sort(key=lambda item: (item["priority"], item["updated_at"], item["repo"], item["number"]))
    return (found[0] if found else None), errors


def main() -> None:
    try:
        config = load_config()
        repos = load_project_repos()
        if not repos:
            output_result("error", "No repositories in project-repos.json")
            return
        tasks = load_tasks()
        excluded_task_keys: set[str] = set()
        recovery_waits: list[str] = []
        approval_waits: list[str] = []
        verification_waits: list[str] = []
        while True:
            chosen, errors = pick_work(repos, config, tasks, excluded_task_keys)
            if chosen is None:
                status = "error" if errors else "skip"
                detail = "; ".join(errors) if errors else "No labeled PR needs review or feedback handling"
                if recovery_waits:
                    detail = ("Waiting for organization-member ledger-recovery decisions on "
                              + ", ".join(recovery_waits) + "; " + detail)
                if approval_waits:
                    detail = ("Waiting for organization-member review approvals on "
                              + ", ".join(approval_waits) + "; " + detail)
                if verification_waits:
                    detail = ("Verification commands need a recoverable prior ledger on "
                              + ", ".join(verification_waits) + "; " + detail)
                output_result(status, detail)
                return
            active_count = sum(
                task.get("status") in ACTIVE
                and task.get("external_key") not in excluded_task_keys
                for task in tasks
            )
            if chosen["kind"] == "review" and not chosen["task_active"] and active_count >= 10:
                output_result("skip", f"Review instance at task capacity ({active_count}/10)")
                return
            task = next((item for item in tasks if item.get("external_key") == chosen["task_key"]), None)
            if (chosen["kind"] == "review"
                    and chosen.get("review_authorization") != "label_added"):
                try:
                    approval = _request_review_approval(chosen, task)
                except (RuntimeError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
                    output_result("error", f"{chosen['repo']}#{chosen['number']}: {exc}")
                    return
                if approval["state"] != "approved":
                    excluded_task_keys.add(chosen["task_key"])
                    approval_waits.append(f"{chosen['repo']}#{chosen['number']} ({approval['state']})")
                    continue
                chosen["review_authorization"] = "organization_member_approved"
                chosen["review_approval_id"] = approval["approval_id"]
                chosen["review_approval_response_comment_id"] = approval["response_comment_id"]
                chosen["review_approval_responder"] = approval["responder"]
            try:
                proceed = restore_prior_ledger(chosen, task)
            except (LedgerError, OSError, subprocess.TimeoutExpired) as exc:
                output_result("error", f"{chosen['repo']}#{chosen['number']}: {exc}")
                return
            if not proceed:
                state = chosen.get("ledger_recovery_state", "waiting")
                if state in {"asked", "waiting", "declined", "verification_unavailable", "unavailable"}:
                    # A consent wait gates only this PR; keep scanning the queue.
                    excluded_task_keys.add(chosen["task_key"])
                    if state == "unavailable":
                        verification_waits.append(f"{chosen['repo']}#{chosen['number']}")
                    else:
                        recovery_waits.append(f"{chosen['repo']}#{chosen['number']}")
                    continue
                output_result("skip", f"{chosen['repo']}#{chosen['number']}: ledger recovery state {state}")
                return
            chosen.pop("priority")
            output_result("start", json.dumps({"work": chosen, "discovery_errors": errors}))
            return
    except (ReviewConfigError, RuntimeError, ValueError) as exc:
        print(f"Review queue error: {exc}", file=sys.stderr)
        output_result("error", str(exc))


if __name__ == "__main__":
    main()
