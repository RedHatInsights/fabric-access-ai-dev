#!/usr/bin/env python3
"""Trusted Jira issue-creation helper for the review-only workflow.

The review runner delegates creation here after the `jira-ticket-creator`
subagent has planned public issue content. This helper is the only component
that resolves the Vault-backed project, Team, label, and component settings:
they are read in-process through `jira_config.resolve()` and never returned
to the model, GitHub, or a transcript. Every Jira call goes through the
shared `.claude/skills/jira_mcp.py` client (no fork) with file-descriptor
level stderr capture, so raw MCP errors and exception text — which can echo
settings — never reach stdout or stderr. The only output is one sanitized
JSON object on stdout.

Request file (public content only; the runner creates it with mktemp):
  {
    "authorization": "explicit_request" | "cross_repo_followup" (advisory),
    "requester": "<verified GitHub login>",
    "pr": <number>,
    "finding_id": "<finding id>" | null,
    "marker_token": "<deterministic idempotency marker>",
    "summary": "<public issue summary>",
    "description": "<public issue description containing the marker>",
    "target_repository": "<owner/repo>"
  }

Result on stdout:
  {"status": "created", "created": <bool>, "issue_key": <str>,
   "url": <str|null>, "team_verified": "id"|"name"}
  {"status": "exists", "created": false, "issue_key": <str>, "url": <str|null>}
or, on failure, {"status": "<sanitized category>"} with exit code 1 — plus
"issue_key" for post-create failures so a retry cannot silently lose the
created issue. Failure statuses never include settings, JQL, issue data,
or raw errors. A Team mismatch found after creation is reported as
`team_mismatch`; the issue is never deleted automatically. `url` is the
server-provided `browse_url` only, or null when the server does not send
one; the REST endpoint is never returned or converted. `team_verified`
reports the recheck level honestly: "id" when the read-back carried the
option id, "name" when the simplified output discarded the id and only the
team name could be compared (Jira validates option ids at create time, so
the accepted write carried the configured id).

Known limitations, reported rather than hidden: `create_no_key` can occur
after a server-side create whose response could not be read; a retry then
relies on the marker search (and Jira's text index) to avoid a duplicate.
The marker search itself is JQL text search plus an exact-substring guard,
so a just-created issue is only findable once Jira indexes it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

from jira_config import resolve
from review_config import ReviewConfigError

MAX_REQUEST_BYTES = 64 * 1024
MAX_SUMMARY_CHARS = 255
MARKER_PREFIX = "fabric-access-review-"
ISSUE_TYPE = "Task"
REQUEST_KEYS = frozenset({
    "authorization", "requester", "pr", "finding_id",
    "marker_token", "summary", "description", "target_repository",
})
AUTHORIZATION_VALUES = frozenset({"explicit_request", "cross_repo_followup"})
REPO_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
FINDING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")
JQL_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
SANITIZED_STATUSES = frozenset({
    "invalid_request", "config_missing", "profile_not_configured",
    "mcp_unavailable", "mcp_timeout", "mcp_tool_error",
    "team_option_unconfirmed", "component_missing", "create_no_key",
    "team_mismatch", "post_create_unverified", "internal_error",
})


class HelperError(Exception):
    """Failure carrying only a sanitized status category.

    ``issue_key`` is set for post-create failures so the runner can record
    which issue exists and a retry cannot silently lose it; it is public
    issue data, never a setting.
    """

    def __init__(self, status: str, issue_key: str | None = None):
        if status not in SANITIZED_STATUSES:
            status = "internal_error"
            issue_key = None
        super().__init__(status)
        self.status = status
        self.issue_key = issue_key


class StderrCapture:
    """Capture file descriptor 2 for the whole run.

    The shared jira_mcp client prints raw MCP error and exception text to
    stderr, and a compromised or misbehaving client could echo resolved
    settings there. Capturing fd 2 (not the Python object) means every write
    to stderr — including os.write(2, ...) — lands in a private temp file
    whose contents are only ever classified, never emitted.
    """

    def __init__(self) -> None:
        self._saved_fd: int | None = None
        self._path: str | None = None

    def __enter__(self) -> "StderrCapture":
        fd, path = tempfile.mkstemp(prefix="jira-create-stderr-")
        self._path = path
        sys.stderr.flush()
        self._saved_fd = os.dup(2)
        os.dup2(fd, 2)
        os.close(fd)
        return self

    def text(self) -> str:
        if self._path is None:
            return ""
        try:
            return Path(self._path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def __exit__(self, *exc_info) -> None:
        sys.stderr.flush()
        if self._saved_fd is not None:
            os.dup2(self._saved_fd, 2)
            os.close(self._saved_fd)
            self._saved_fd = None
        if self._path is not None:
            try:
                os.unlink(self._path)
            except OSError:
                pass
            self._path = None


def classify_stderr(text: str) -> str:
    """Map captured shared-client stderr to a sanitized category."""
    lowered = text.lower()
    if "jira_mcp_url not set" in lowered:
        return "mcp_unavailable"
    if "timeout" in lowered or "timed out" in lowered:
        return "mcp_timeout"
    if any(marker in lowered for marker in
           ("connect", "refused", "unreachable", "name or service not known")):
        return "mcp_unavailable"
    return "mcp_tool_error"


def transport_failure(capture: StderrCapture, fallback: str) -> str:
    """Transport-level failures keep their category; tool errors fall back."""
    category = classify_stderr(capture.text())
    return category if category in ("mcp_unavailable", "mcp_timeout") else fallback


def resolve_client() -> tuple:
    """Import the shared Jira MCP client (no forked copy).

    The client lives beside the baked app: `$HOME/app/.claude/skills` in the
    sandbox and under the runner's working directory on the legacy path.
    """
    candidates = []
    home = os.environ.get("HOME")
    if home:
        candidates.append(Path(home) / "app" / ".claude" / "skills")
    candidates.append(Path.cwd() / ".claude" / "skills")
    for candidate in candidates:
        if (candidate / "jira_mcp.py").is_file():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            break
    else:
        raise HelperError("mcp_unavailable")
    from jira_mcp import jira_call, jira_cleanup

    return jira_call, jira_cleanup


def deterministic_marker(repo_key: str, pr: int, finding_id: str | None,
                         target_repository: str) -> str:
    """Compute the per-finding-and-target idempotency marker."""
    token = finding_id if finding_id else "pr-followup"
    slug = target_repository.replace("/", "-").lower()
    return f"{MARKER_PREFIX}{repo_key}-{pr}-{token}-{slug}"


def _require_string(value: object, max_chars: int) -> str:
    if not isinstance(value, str):
        raise HelperError("invalid_request")
    if not value.strip() or len(value) > max_chars:
        raise HelperError("invalid_request")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise HelperError("invalid_request")
    return value


def load_request(path: Path, repo_key: str, target_repository: str) -> dict:
    """Read and validate the public-content request file.

    Opens the file once with O_NOFOLLOW and validates the same open file
    description it reads (fstat, not a second path lookup), so the file
    cannot be swapped between the check and the read. The read is bounded to
    MAX_REQUEST_BYTES + 1 bytes so a file that grows after fstat is still
    rejected.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise HelperError("invalid_request") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise HelperError("invalid_request")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise HelperError("invalid_request")
        if info.st_size > MAX_REQUEST_BYTES:
            raise HelperError("invalid_request")
        raw = os.read(descriptor, MAX_REQUEST_BYTES + 1)
    except OSError:
        raise HelperError("invalid_request") from None
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if len(raw) > MAX_REQUEST_BYTES:
        raise HelperError("invalid_request")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HelperError("invalid_request") from None
    if not isinstance(data, dict) or set(data) != REQUEST_KEYS:
        raise HelperError("invalid_request")
    if data["authorization"] not in AUTHORIZATION_VALUES:
        raise HelperError("invalid_request")
    requester = _require_string(data["requester"], 100)
    pr = data["pr"]
    if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
        raise HelperError("invalid_request")
    finding_id = data["finding_id"]
    if finding_id is not None and (
            not isinstance(finding_id, str) or not FINDING_ID_RE.fullmatch(finding_id)):
        raise HelperError("invalid_request")
    marker = deterministic_marker(repo_key, pr, finding_id, target_repository)
    if not isinstance(data["marker_token"], str) or data["marker_token"] != marker:
        raise HelperError("invalid_request")
    summary = _require_string(data["summary"], MAX_SUMMARY_CHARS)
    description = _require_string(data["description"], MAX_REQUEST_BYTES)
    if marker not in description:
        raise HelperError("invalid_request")
    if data["target_repository"] != target_repository:
        raise HelperError("invalid_request")
    return {
        "authorization": data["authorization"],
        "requester": requester,
        "pr": pr,
        "finding_id": finding_id,
        "marker_token": marker,
        "summary": summary,
        "description": description,
        "target_repository": target_repository,
    }


def resolve_settings(repo_key: str, target_repository: str) -> dict:
    """Resolve the Jira profile settings in-process; never return them."""
    try:
        return resolve(repo_key, target_repository)
    except ReviewConfigError as exc:
        message = str(exc)
        if "not configured" in message or "No Jira team profile" in message:
            raise HelperError("profile_not_configured") from None
        raise HelperError("config_missing") from None


def _jql_safe(value: object) -> str:
    if not isinstance(value, str) or not JQL_VALUE_RE.fullmatch(value):
        raise HelperError("config_missing")
    return value


def _issues_of(result: object) -> list | None:
    if (not isinstance(result, dict) or result.get("isError")
            or result.get("success") is False):
        return None
    issues = result.get("issues")
    return issues if isinstance(issues, list) else None


def _issue_key(issue: object) -> str:
    if isinstance(issue, dict):
        key = issue.get("key") or issue.get("issue_key") or ""
        return key if isinstance(key, str) else ""
    return ""


def _issue_description(issue: object) -> str:
    if not isinstance(issue, dict):
        return ""
    fields = issue.get("fields")
    description = fields.get("description") if isinstance(fields, dict) else None
    if not isinstance(description, str):
        description = issue.get("description")
    return description if isinstance(description, str) else ""


def _issue_url(result: dict, key: str) -> str | None:
    """Return the issue's browse URL when the server provides one.

    The deployed server builds ``browse_url`` from its own configured Jira
    base URL as ``<base>/browse/<key>``. The REST ``self``/``url`` endpoint
    is deliberately not used or converted, and no URL is derived from any
    other host: without a server-provided browse URL the result carries
    ``url: null``.
    """
    for field in ("browse_url", "browseUrl"):
        value = result.get(field)
        if (isinstance(value, str)
                and value.startswith(("http://", "https://"))
                and value.endswith(f"/browse/{key}")):
            return value
    return None


def find_existing(jira_call, project: str, marker: str,
                  capture: StderrCapture) -> dict | None:
    """Return the issue carrying the exact marker, if one already exists."""
    jql = f'project = "{_jql_safe(project)}" AND description ~ "{marker}"'
    result = jira_call("jira_search", {"jql": jql, "fields": "description", "limit": 20})
    issues = _issues_of(result)
    if issues is None:
        raise HelperError(classify_stderr(capture.text()))
    for issue in issues:
        # Exact-substring guard: JQL text search can match issues whose
        # description only shares the marker's tokens in another order.
        if marker in _issue_description(issue):
            key = _issue_key(issue)
            if not ISSUE_KEY_RE.fullmatch(key):
                raise HelperError("mcp_tool_error")
            return {"status": "exists", "created": False,
                    "issue_key": key, "url": _issue_url(issue, key)}
    return None


def _option_entries(result: object) -> list | None:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        entries = result.get("options")
        if not isinstance(entries, list):
            entries = result.get("values")
        return entries if isinstance(entries, list) else None
    return None


def _field_value(issue: dict, field: str):
    """Read one field from a simplified or legacy issue shape."""
    fields = issue.get("fields")
    if isinstance(fields, dict) and field in fields:
        return fields.get(field)
    return issue.get(field)


def _team_match(value: object, team_id: str, team_name: str) -> tuple[str | None, bool]:
    """Classify a Team field value against the configured team.

    Returns ("id", matched) or ("name", matched) with the verification
    level, or (None, False) when the value cannot be read reliably.

    The deployed mcp-atlassian server returns issue custom fields as
    ``{"value": <processed value>, "name": <FIELD DISPLAY NAME>}``. For
    select-like fields the processed value is the option's display string
    and the option ID is discarded; preserved complex values (gh.team-style
    plugin fields) keep their ID under ``value``. The wrapper's ``name`` is
    the field's display name (for example "Team"), never the team, so it is
    never matched against the configured team name. A name-only object
    cannot be told apart from a field display name, so it fails closed.
    """
    if isinstance(value, dict):
        for key in ("id", "teamId", "team_id"):
            identifier = value.get(key)
            if (isinstance(identifier, (str, int)) and not isinstance(identifier, bool)
                    and str(identifier).strip()):
                return ("id", str(identifier).strip() == team_id)
        if "value" in value:
            return _team_match(value["value"], team_id, team_name)
        return (None, False)
    if isinstance(value, list):
        matches = [_team_match(item, team_id, team_name) for item in value]
        for level, matched in matches:
            if level is not None and matched:
                return (level, True)
        for level, matched in matches:
            if level is not None:
                return (level, matched)
        return (None, False)
    if isinstance(value, str) and value.strip():
        return ("name", value.strip() == team_name)
    return (None, False)


def confirm_team(jira_call, settings: dict, capture: StderrCapture) -> None:
    """Confirm Jira resolves the configured Team option; fail closed.

    Field options are tried first; ``project_key`` and ``issue_type`` are
    required on Server/DC and ignored on Cloud, so they are always passed.
    gh.team-style fields cannot be enumerated and come back empty, so the
    reference-issue fallback confirms the option id server-side through the
    equality JQL, then corroborates it on the fetched issue.
    """
    team_field = _jql_safe(settings["team_field"])
    team_id = _jql_safe(settings["team_id"])
    team_name = settings["team_name"]
    options = jira_call("jira_get_field_options", {
        "field_id": team_field,
        "project_key": settings["project"],
        "issue_type": ISSUE_TYPE,
    })
    entries = _option_entries(options)
    if entries:
        for entry in entries:
            if not isinstance(entry, dict) or str(entry.get("id", "")) != team_id:
                continue
            value = entry.get("value")
            if (isinstance(value, str) and value.strip()
                    and team_name and value.strip() != team_name):
                raise HelperError("team_option_unconfirmed")
            return
        # Enumerable options without the configured ID cannot be confirmed.
        raise HelperError("team_option_unconfirmed")
    jql = f'project = "{_jql_safe(settings["project"])}" AND cf[{team_field}] = "{team_id}"'
    result = jira_call("jira_search", {"jql": jql, "fields": team_field, "limit": 5})
    issues = _issues_of(result)
    if issues is None:
        raise HelperError(transport_failure(capture, "team_option_unconfirmed"))
    if not issues:
        raise HelperError("team_option_unconfirmed")
    issue = jira_call("jira_get_issue",
                      {"issue_key": _issue_key(issues[0]), "fields": "*all"})
    if not isinstance(issue, dict) or issue.get("isError"):
        raise HelperError("team_option_unconfirmed")
    level, matched = _team_match(_field_value(issue, team_field), team_id, team_name)
    if level is None or not matched:
        raise HelperError("team_option_unconfirmed")


def _component_entries(result: object) -> list | None:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        entries = result.get("components")
        if not isinstance(entries, list):
            entries = result.get("values")
        return entries if isinstance(entries, list) else None
    return None


def confirm_component(jira_call, settings: dict, capture: StderrCapture) -> None:
    """Confirm the configured component exists; fail closed."""
    result = jira_call("jira_get_project_components",
                       {"project_key": settings["project"]})
    entries = _component_entries(result)
    if entries is None:
        raise HelperError(transport_failure(capture, "component_missing"))
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("id", "")) == settings["component_id"]:
            return
    raise HelperError("component_missing")


def create_issue(jira_call, settings: dict, request: dict,
                 capture: StderrCapture) -> dict:
    """Create the issue and validate the write response."""
    additional = {
        settings["team_field"]: {"id": settings["team_id"]},
        "labels": settings["labels"],
    }
    if "component_id" in settings:
        additional["components"] = [{"id": settings["component_id"]}]
    result = jira_call("jira_create_issue", {
        "project_key": settings["project"],
        "summary": request["summary"],
        "description": request["description"],
        "issue_type": ISSUE_TYPE,
        "additional_fields": json.dumps(additional),
    })
    if (not isinstance(result, dict) or result.get("isError")
            or result.get("success") is False):
        raise HelperError(transport_failure(capture, "mcp_tool_error"))
    # The deployed tool wraps the created issue: {"message": ..., "issue": {...}}.
    issue = result.get("issue") if isinstance(result.get("issue"), dict) else result
    key = issue.get("key")
    if not isinstance(key, str) or not ISSUE_KEY_RE.fullmatch(key):
        # The issue may exist server-side even without a readable key; a
        # retry re-finds it through the marker search, or duplicates only
        # if Jira's text index still lags. Recorded as a known limitation.
        raise HelperError("create_no_key")
    return {"status": "created", "created": True,
            "issue_key": key, "url": _issue_url(issue, key)}


def recheck_team(jira_call, settings: dict, issue_key: str,
                 capture: StderrCapture) -> str:
    """Recheck the Team field on the created issue; never delete on mismatch.

    Returns the verification level. An id-level match is direct. A
    name-level match is accepted because Jira validates option IDs when the
    issue is created — the write that carried the configured team_id was
    accepted — and the read-back then confirms the displayed team name. A
    contradicting value is ``team_mismatch``; an unreadable one is
    ``post_create_unverified``. Both carry the issue key so a retry or an
    operator can find the issue; the marker search also re-finds it.
    """
    issue = jira_call("jira_get_issue", {"issue_key": issue_key, "fields": "*all"})
    if (not isinstance(issue, dict) or issue.get("isError")
            or issue.get("success") is False):
        raise HelperError(transport_failure(capture, "post_create_unverified"),
                          issue_key=issue_key)
    level, matched = _team_match(_field_value(issue, settings["team_field"]),
                                 settings["team_id"], settings["team_name"])
    if level is not None and not matched:
        # Sanitized report only; the issue is left for an operator.
        raise HelperError("team_mismatch", issue_key=issue_key)
    if level is None:
        raise HelperError("post_create_unverified", issue_key=issue_key)
    return level


def run(repo_key: str, target_repository: str, request_path: Path,
        client: tuple | None = None) -> dict:
    """Execute the trusted creation flow and return the sanitized result."""
    if not isinstance(repo_key, str) or not REPO_KEY_RE.fullmatch(repo_key):
        raise HelperError("invalid_request")
    if not isinstance(target_repository, str) or not REPO_RE.fullmatch(target_repository):
        raise HelperError("invalid_request")
    request = load_request(request_path, repo_key, target_repository)
    settings = resolve_settings(repo_key, target_repository)
    with StderrCapture() as capture:
        jira_cleanup = None
        try:
            # Resolve the client inside the capture so import-time output of
            # the shared client cannot reach the real stderr either.
            if client is None:
                client = resolve_client()
            jira_call, jira_cleanup = client
            existing = find_existing(jira_call, settings["project"],
                                     request["marker_token"], capture)
            if existing is not None:
                return existing
            confirm_team(jira_call, settings, capture)
            if "component_id" in settings:
                confirm_component(jira_call, settings, capture)
            created = create_issue(jira_call, settings, request, capture)
            created["team_verified"] = recheck_team(
                jira_call, settings, created["issue_key"], capture)
            return created
        finally:
            if jira_cleanup is not None:
                try:
                    jira_cleanup()
                except Exception:
                    pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-key", required=True)
    parser.add_argument("--target-repository", required=True)
    parser.add_argument("--request", required=True, type=Path,
                        help="Public-content request JSON created with mktemp")
    args = parser.parse_args()
    try:
        result = run(args.repo_key, args.target_repository, args.request)
    except HelperError as exc:
        failure = {"status": exc.status}
        if exc.issue_key is not None:
            failure["issue_key"] = exc.issue_key
        print(json.dumps(failure))
        raise SystemExit(1) from None
    except Exception:
        # Never leak raw exception text; it can echo settings or issue data.
        print(json.dumps({"status": "internal_error"}))
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
