"""Ingest unresolved errors from GlitchTip and create Jira tickets.

Fetches issues via GlitchTip's Sentry-compatible REST API, enriches each
with the latest event data, and creates a Jira ticket with all available
error details. Skips issues flagged as duplicates by the preflight check.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error


for _var in ("GLITCHTIP_ORG", "GLITCHTIP_TOKEN"):
    if not os.environ.get(_var):
        sys.exit(f"ERROR: Required environment variable {_var} is not set")

GLITCHTIP_URL = os.environ.get("GLITCHTIP_URL", "https://glitchtip.devshift.net").rstrip("/")
GLITCHTIP_ORG = os.environ["GLITCHTIP_ORG"]
GLITCHTIP_TOKEN = os.environ["GLITCHTIP_TOKEN"]
JIRA_MCP_URL = os.environ.get("JIRA_MCP_URL", "")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "")
if not JIRA_PROJECT_KEY and JIRA_MCP_URL:
    sys.exit("ERROR: JIRA_PROJECT_KEY is required when JIRA_MCP_URL is set")
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes") or not JIRA_MCP_URL
MAX_TICKETS = int(os.environ.get("MAX_TICKETS", "50")) or None

GLITCHTIP_PROJECTS = set(
    p.strip() for p in os.environ.get("GLITCHTIP_PROJECTS", "").split(",") if p.strip()
)

LEVEL_PRIORITY_MAP = {
    "fatal": "Major",
    "critical": "Major",
    "error": "Major",
    "warning": "Normal",
    "info": "Minor",
    "debug": "Minor",
}


def compute_priority(issue: dict, project_name: str) -> str:
    level = issue.get("level", "error")
    count = int(issue.get("count", 0))
    is_prod = "prod" in project_name

    if level in ("fatal", "critical"):
        return "Major"
    if not is_prod:
        return "Minor"
    if level == "error" and count >= 100:
        return "Major"
    return "Normal"

MAX_PAGINATION_PAGES = 50
MAX_RETRIES = 3
RETRY_BACKOFF = 2
MAX_DESCRIPTION_LENGTH = 28000
REQUEST_TIMEOUT = 30


def _request_with_retry(req: urllib.request.Request) -> bytes:
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return resp.read(), resp
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF ** (attempt + 1)
                print(f"  Retrying after HTTP {e.code} (attempt {attempt + 1}/{MAX_RETRIES}, waiting {wait}s)...")
                time.sleep(wait)
                continue
            raise
        except (urllib.error.URLError, OSError) as e:
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_BACKOFF ** (attempt + 1)
                print(f"  Retrying after network error (attempt {attempt + 1}/{MAX_RETRIES}, waiting {wait}s): {e}")
                time.sleep(wait)
                continue
            raise


def glitchtip_get(path: str) -> any:
    url = f"{GLITCHTIP_URL}/api/0/{path}"
    headers = {"Accept": "application/json"}
    if GLITCHTIP_TOKEN:
        headers["Authorization"] = f"Bearer {GLITCHTIP_TOKEN}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    body, _ = _request_with_retry(req)
    return json.loads(body)


def glitchtip_get_paginated(path: str) -> list:
    results = []
    url = f"{GLITCHTIP_URL}/api/0/{path}"
    headers = {"Accept": "application/json"}
    if GLITCHTIP_TOKEN:
        headers["Authorization"] = f"Bearer {GLITCHTIP_TOKEN}"
    seen_urls = set()
    for _ in range(MAX_PAGINATION_PAGES):
        if not url or url in seen_urls:
            break
        seen_urls.add(url)
        req = urllib.request.Request(url, headers=headers, method="GET")
        body, resp = _request_with_retry(req)
        page = json.loads(body)
        if isinstance(page, list):
            results.extend(page)
        else:
            results.append(page)
            break
        link_header = resp.getheader("Link", "")
        url = _parse_next_link(link_header)
    return results


def _parse_next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        if 'rel="next"' in part and 'results="true"' in part:
            if "<" not in part or ">" not in part:
                continue
            start = part.index("<") + 1
            end = part.index(">")
            return part[start:end]
    return None


def call_jira_mcp(tool_name: str, arguments: dict) -> dict:
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }).encode()
    req = urllib.request.Request(
        JIRA_MCP_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    body, _ = _request_with_retry(req)
    return json.loads(body)


def extract_mcp_text(result: dict) -> str:
    error = result.get("error")
    if error:
        raise RuntimeError(f"MCP error: {error}")
    content = result.get("result", {}).get("content", [])
    if content and isinstance(content, list):
        return content[0].get("text", "")
    return ""


def fetch_project_slugs() -> dict:
    """Return a mapping of project slug -> project name for target projects."""
    projects = glitchtip_get_paginated(f"organizations/{GLITCHTIP_ORG}/projects/")
    slug_map = {}
    for p in projects:
        name = p.get("name", "")
        slug = p.get("slug", "")
        if not GLITCHTIP_PROJECTS or name in GLITCHTIP_PROJECTS or slug in GLITCHTIP_PROJECTS:
            slug_map[slug] = name
    return slug_map


def fetch_unresolved_issues(project_slug: str) -> list:
    return glitchtip_get_paginated(
        f"projects/{GLITCHTIP_ORG}/{project_slug}/issues/?query=is:unresolved"
    )


def fetch_latest_event(issue_id: str) -> dict:
    try:
        return glitchtip_get(f"issues/{issue_id}/events/latest/")
    except urllib.error.HTTPError:
        return {}




def load_skip_ids() -> set:
    skip_file = os.environ.get("GLITCHTIP_SKIP_FILE", ".glitchtip-skip-ids.json")
    if os.path.isfile(skip_file):
        with open(skip_file) as f:
            return set(str(i) for i in json.load(f))
    return set()


def sanitize_label(value: str) -> str:
    """Strip characters that Jira rejects in labels."""
    return re.sub(r"[^a-zA-Z0-9._-]", "-", value).strip("-")


_EXCEPTION_CLASS_RE = re.compile(
    r"^\*?_?[A-Z][a-zA-Z]*(?:\.[a-zA-Z]+)*(?:Error|Exception|Failure|Timeout|Warning):"
    r"|^\*[a-z]+\.\w+Error:"
    r"|^\*[a-z]+\.wrap\w+:"
)

_GENERIC_PREFIX_RE = re.compile(
    r"^("
    r"gRPC error[^:]*:"
    r"|Consumer (?:failed|error):"
    r"|CONSUMER STOPPING:"
    r"|Fetch to node [^ ]+ failed:"
    r"|Error (?:sending|processing|encountered|initializing|calling) \w+"
    r"|Failed to \w+"
    r"|Initialization of \w+"
    r"|Max (?:operation )?retries"
    r"|Heartbeat thread for"
    r"|Metadata refresh: failed"
    r"|Unable to (?:get|fetch|connect|connect for URL)\b"
    r"|Replication event failed for"
    r"|.+? connectivity error:"
    r"|LeaveGroup request for"
    r"|BOP error during \w+:"
    r"|Control server error:"
    r"|Reconnect failed"
    r"|CRITICAL:"
    r")"
)


def normalize_title(title: str) -> str:
    """Strip dynamic values from an error title to produce a stable grouping key."""
    s = title

    # DB CONTEXT/DETAIL suffixes that leak into truncated titles (real newlines and literal \n)
    s = re.sub(r"[\n\r]CONTEXT:.*", "", s, flags=re.DOTALL)
    s = re.sub(r"[\n\r]DETAIL:.*", "", s, flags=re.DOTALL)
    s = re.sub(r"\\nCONTEXT:.*", "", s, flags=re.DOTALL)
    s = re.sub(r"\\nDETAIL:.*", "", s, flags=re.DOTALL)

    # Kafka transport identifiers: [10.0.185.78:9096<-52446] -> [<transport>]
    s = re.sub(r"\[[\d.]+:\d+<-\d+\]", "[<transport>]", s)
    # IP:port pairs
    s = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?", "<ip>", s)
    # UUIDs (8-4-4-4-12 hex, including truncated ones from GlitchTip)
    s = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{2,12}", "<uuid>", s, flags=re.IGNORECASE)
    # Standalone hex strings (8+ chars)
    s = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", s, flags=re.IGNORECASE)
    # Hex-dash fragments left after partial UUID replacement (e.g. <hex>-f400-7d…)
    s = re.sub(r"(<hex>|<uuid>)(-[0-9a-f]{2,4})+", r"\1-<id>", s, flags=re.IGNORECASE)
    # Standalone numbers (port numbers, offsets, counts)
    s = re.sub(r"\b\d{4,}\b", "<n>", s)

    # Node/partition/offset/coordinator numbers (small digits missed by \d{4,})
    s = re.sub(r"(?<=node[_ -])\d+", "<n>", s)
    s = re.sub(r"(?<=coordinator-)\d+", "<n>", s)
    s = re.sub(r"(?<=partition[: ])\d+", "<n>", s)
    s = re.sub(r"(?<=offset[: ])\d+", "<n>", s)
    s = re.sub(r"(?<=Process )\d+", "<n>", s)

    # key=value pairs (client_id, node_id, host, etc.)
    s = re.sub(r"(\w+_id)=[\w.-]+", r"\1=<val>", s)
    s = re.sub(r"(?<=host=)[\w.-]+", "<host>", s)

    # Double-bracketed socket error details: [[Errno 32] Broken pipe] -> [<socket-error>]
    s = re.sub(r"\[\[.*?\]\]", "[<socket-error>]", s)

    # URLs: normalize hostnames (collapse stage/prod variants) and strip query strings
    s = re.sub(r"https?://[^\s\"'>]+", "<url>", s)

    # Go read tcp / dial tcp with IP already normalized
    s = re.sub(r"read tcp <ip>-><ip>", "read tcp <transport>", s)
    s = re.sub(r"dial tcp <ip>", "dial tcp <addr>", s)

    # Quoted identifiers that vary (role names, constraint names, table names, etc.)
    s = re.sub(r'"[^"]{20,}"', '"<name>"', s)

    # Named entity patterns: "for role X, UUID", "for group X", "for member X"
    s = re.sub(r"for (?:role|group|member|user|account) .+?,\s*(?=UUID|id|ID)", "for <entity>, ", s)

    # "system role: X with error:" / "system policy: X with error:" etc.
    s = re.sub(r"(?:system \w+|resource): .+ with error:", "<resource>: <name> with error:", s)

    # Message bus event wrappers — collapse varying inner messages
    s = re.sub(r"^(\w+(?:_\w+)*: (?:Error processing|Failed processing) \w+ (?:message|event)\s*:\s*).*",
               r"\1<detail>", s, flags=re.DOTALL)

    # Class+suffix variants: FooBarHandler, FooBarManager, FooBarService, etc.
    s = re.sub(r"([A-Z][a-z]+(?:[A-Z][a-z]+)+)\w*(?:Handler|Manager|Service|Processor|Consumer|Producer|Connector)",
               lambda m: m.group(1) + "*", s)

    # Kafka member/group IDs
    s = re.sub(r"kafka-python-[\w.-]+", "kafka-python-<id>", s)
    s = re.sub(r"(?<=member )\S+", "<member>", s)

    # XML/angle-bracket-wrapped objects: <BrokerConnection ...>, <KafkaSSLTransport ...>
    s = re.sub(r"^(<[A-Z]\w+ .+?>).*", r"\1: <detail>", s)

    # MCP tool call timeouts — normalize tool name
    s = re.sub(r"tools/call tool='[^']+'", "tools/call tool='<tool>'", s)

    # Strip "for request id: ..." suffixes
    s = re.sub(r"for request id:\s*\S+", "for request id: <id>", s)

    # Truncate after PascalCase exception class names (FooError:, BarException:, etc.)
    m = _EXCEPTION_CLASS_RE.match(s)
    if m:
        s = m.group(0) + " <detail>"

    # Truncate after generic action prefixes (Error sending..., Failed to..., etc.)
    m = _GENERIC_PREFIX_RE.match(s)
    if m:
        s = m.group(1) + " <detail>"

    # Collapse trailing whitespace/ellipsis after truncation
    s = re.sub(r"\s*…$", "…", s)
    return s


def group_issues(issues: list) -> list[dict]:
    """Group issues by normalized title. Returns list of group dicts."""
    groups = {}
    for issue in issues:
        title = issue.get("title", "unknown")
        key = normalize_title(title)
        if key not in groups:
            groups[key] = {
                "issues": [],
                "total_count": 0,
                "representative": issue,
            }
        groups[key]["issues"].append(issue)
        count = int(issue.get("count", 0))
        groups[key]["total_count"] += count
        if count > int(groups[key]["representative"].get("count", 0)):
            groups[key]["representative"] = issue
    return list(groups.values())


def format_stacktrace(event: dict) -> str:
    entries = event.get("entries", [])
    for entry in entries:
        if entry.get("type") == "exception":
            values = entry.get("data", {}).get("values", [])
            lines = []
            for exc in values:
                exc_type = exc.get("type", "Exception")
                exc_value = exc.get("value", "")
                lines.append(f"*{exc_type}*: {exc_value}")
                stacktrace = exc.get("stacktrace", {})
                frames = stacktrace.get("frames", [])
                for frame in frames:
                    filename = frame.get("filename", "?")
                    lineno = frame.get("lineNo", "?")
                    function = frame.get("function", "?")
                    context_line = frame.get("context_line", "").strip()
                    lines.append(f"  {filename}:{lineno} in {function}")
                    if context_line:
                        lines.append(f"    {{code}}{context_line}{{code}}")
            return "\n".join(lines)
    return "_No stacktrace available_"


def format_tags(event: dict) -> str:
    tags = event.get("tags", [])
    if not tags:
        return "_No tags_"
    lines = []
    for tag in tags:
        key = tag.get("key", "?")
        value = tag.get("value", "?")
        lines.append(f"* *{key}*: {value}")
    return "\n".join(lines)


def format_contexts(event: dict) -> str:
    contexts = event.get("contexts", {})
    if not contexts:
        return "_No context data_"
    lines = []
    for ctx_name, ctx_data in contexts.items():
        if isinstance(ctx_data, dict):
            lines.append(f"*{ctx_name}*:")
            for k, v in ctx_data.items():
                if k != "type":
                    lines.append(f"  * {k}: {v}")
    return "\n".join(lines) if lines else "_No context data_"


def format_breadcrumbs(event: dict) -> str:
    entries = event.get("entries", [])
    for entry in entries:
        if entry.get("type") == "breadcrumbs":
            crumbs = entry.get("data", {}).get("values", [])
            if not crumbs:
                continue
            lines = []
            for crumb in crumbs[-10:]:
                ts = crumb.get("timestamp", "")
                category = crumb.get("category", "")
                message = crumb.get("message", "")
                level = crumb.get("level", "")
                lines.append(f"  [{ts}] {category} ({level}): {message}")
            if len(crumbs) > 10:
                lines.insert(0, f"  _Showing last 10 of {len(crumbs)} breadcrumbs_")
            return "\n".join(lines)
    return "_No breadcrumbs_"


def glitchtip_issue_url(issue_id) -> str:
    return f"{GLITCHTIP_URL}/{GLITCHTIP_ORG}/issues/{issue_id}"


def build_ticket_description(issue: dict, event: dict, project_name: str, group: dict = None) -> str:
    issue_id = issue.get("id", "?")
    title = issue.get("title", "Unknown error")
    culprit = issue.get("culprit", "")
    level = issue.get("level", "error")
    platform = issue.get("platform", "unknown")
    first_seen = issue.get("firstSeen", "unknown")
    last_seen = issue.get("lastSeen", "unknown")
    count = issue.get("count", 0)
    user_count = issue.get("userCount", 0)
    issue_url = glitchtip_issue_url(issue_id)

    event_id = event.get("eventID", "?")
    release = event.get("release", {})
    release_version = release.get("version", "unknown") if isinstance(release, dict) else str(release or "unknown")
    environment = event.get("environment", "unknown")
    sdk_info = event.get("sdk", {})
    sdk_name = sdk_info.get("name", "unknown") if isinstance(sdk_info, dict) else "unknown"
    sdk_version = sdk_info.get("version", "?") if isinstance(sdk_info, dict) else "?"

    stacktrace = format_stacktrace(event)
    tags = format_tags(event)
    contexts = format_contexts(event)
    breadcrumbs = format_breadcrumbs(event)

    description = f"""h2. GlitchTip Error Report

[View in GlitchTip|{issue_url}]

||Field||Value||
|GlitchTip Issue ID|{issue_id}|
|Event ID|{event_id}|
|Project|{project_name}|
|Platform|{platform}|
|Environment|{environment}|
|Level|{level}|
|Occurrences|{count}|
|Affected Users|{user_count}|
|First Seen|{first_seen}|
|Last Seen|{last_seen}|
|Release|{release_version}|
|SDK|{sdk_name} {sdk_version}|
|Culprit|{culprit}|
"""

    if group and len(group["issues"]) > 1:
        grouped_ids = [str(i.get("id", "?")) for i in group["issues"]]
        description += f"""
h3. Grouped Issues
This ticket represents *{len(group["issues"])} GlitchTip issues* with the same error pattern.
* *Total occurrences across all issues:* {group["total_count"]}
* *GlitchTip issue IDs:* {", ".join(grouped_ids[:20])}{"... and {} more".format(len(grouped_ids) - 20) if len(grouped_ids) > 20 else ""}
"""

    description += f"""
h3. Stacktrace
{{noformat}}
{stacktrace}
{{noformat}}

h3. Tags
{tags}

h3. Context Data
{contexts}

h3. Breadcrumbs
{{noformat}}
{breadcrumbs}
{{noformat}}

----
_Auto-generated by glitchtip-jira-integration workflow_
"""

    if len(description) > MAX_DESCRIPTION_LENGTH:
        truncated = description[:MAX_DESCRIPTION_LENGTH]
        truncated += f"\n\n{{noformat}}\n\n_Description truncated. [View full error details in GlitchTip|{issue_url}]_\n"
        return truncated

    return description


def create_jira_ticket(issue: dict, event: dict, project_name: str, group: dict = None) -> str:
    issue_id = str(issue.get("id", ""))
    title = issue.get("title", "Unknown error")
    level = issue.get("level", "error")
    environment = event.get("environment", "unknown")

    summary = f"[GlitchTip] [{project_name}] {title}"
    if len(summary) > 255:
        summary = summary[:252] + "..."

    description = build_ticket_description(issue, event, project_name, group)
    priority_issue = issue
    if group:
        priority_issue = dict(issue, count=group["total_count"])
    priority = compute_priority(priority_issue, project_name)

    labels = [
        sanitize_label("glitchtip"),
        sanitize_label(project_name),
    ]
    if group:
        for gi in group["issues"]:
            labels.append(f"glitchtip-issue-{gi.get('id', '')}")
    else:
        labels.append(f"glitchtip-issue-{issue_id}")
    if environment and environment != "unknown":
        labels.append(sanitize_label(environment))

    result = call_jira_mcp("jira_create_issue", {
        "project_key": JIRA_PROJECT_KEY,
        "issue_type": "Bug",
        "summary": summary,
        "description": description,
        "priority": priority,
        "labels": labels,
    })
    text = extract_mcp_text(result)
    if text:
        try:
            ticket = json.loads(text)
        except json.JSONDecodeError:
            raise RuntimeError(f"Unexpected Jira MCP response: {text[:500]}")
        return ticket.get("key", "unknown")
    return "unknown"


def dry_run_ticket(issue: dict, event: dict, project_name: str, group: dict = None):
    issue_id = str(issue.get("id", ""))
    title = issue.get("title", "Unknown error")
    level = issue.get("level", "error")
    count = group["total_count"] if group else issue.get("count", 0)
    first_seen = issue.get("firstSeen", "unknown")
    last_seen = issue.get("lastSeen", "unknown")
    environment = event.get("environment", "unknown")
    priority_issue = issue
    if group:
        priority_issue = dict(issue, count=group["total_count"])
    priority = compute_priority(priority_issue, project_name)

    summary = f"[GlitchTip] [{project_name}] {title}"
    if len(summary) > 255:
        summary = summary[:252] + "..."

    labels = [
        sanitize_label("glitchtip"),
        sanitize_label(project_name),
    ]
    if group:
        labels.append(f"glitchtip-issue-{issue_id} (+{len(group['issues']) - 1} more)")
    else:
        labels.append(f"glitchtip-issue-{issue_id}")
    if environment and environment != "unknown":
        labels.append(sanitize_label(environment))

    print(f"    --- TICKET PREVIEW ---")
    print(f"    Project:     {JIRA_PROJECT_KEY}")
    print(f"    Type:        Bug")
    print(f"    Summary:     {summary}")
    print(f"    Priority:    {priority}")
    print(f"    Labels:      {', '.join(labels)}")
    print(f"    Level:       {level}")
    if group and len(group["issues"]) > 1:
        print(f"    Grouped:     {len(group['issues'])} GlitchTip issues with same error pattern")
    print(f"    Occurrences: {count}")
    print(f"    First seen:  {first_seen}")
    print(f"    Last seen:   {last_seen}")
    print(f"    Environment: {environment}")
    print(f"    Stacktrace:  {format_stacktrace(event)[:200]}...")
    print(f"    ----------------------")


def main():
    if DRY_RUN:
        print("[DRY RUN] Jira ticket creation disabled (no JIRA_MCP_URL or DRY_RUN=1)")
    if MAX_TICKETS:
        print(f"Ticket limit: {MAX_TICKETS}")

    skip_ids = load_skip_ids()

    print("Fetching projects from GlitchTip...")
    slug_map = fetch_project_slugs()
    if not slug_map:
        print("No matching projects found.")
        return
    print(f"Target projects: {', '.join(slug_map.values())}")

    created = []
    total_skipped = 0
    failed = []

    for slug, project_name in slug_map.items():
        if MAX_TICKETS and len(created) >= MAX_TICKETS:
            break

        print(f"\nFetching unresolved issues for {project_name}...")
        issues = fetch_unresolved_issues(slug)
        if not issues:
            print(f"  No unresolved issues.")
            continue

        print(f"  Found {len(issues)} unresolved issue(s).")

        # Filter out issues already in Jira
        filtered = []
        project_skipped = 0
        for issue in issues:
            issue_id = str(issue.get("id", ""))
            if issue_id in skip_ids:
                project_skipped += 1
            else:
                filtered.append(issue)

        if project_skipped:
            print(f"  Skipped {project_skipped} issue(s) with existing Jira tickets.")
        total_skipped += project_skipped

        # Group by normalized title
        groups = group_issues(filtered)

        # Skip groups where ALL issues are already covered by skip_ids
        # (handles partial overlap: if some issues in a group were skipped
        # but the group still formed from remaining issues, check whether
        # any issue in the FULL group pattern already has a Jira ticket)
        deduped_groups = []
        for group in groups:
            all_ids = [str(i.get("id", "")) for i in group["issues"]]
            if any(iid in skip_ids for iid in all_ids):
                print(f"  Skipping group (related issues already have Jira tickets): "
                      f"{group['representative'].get('title', 'unknown')}")
                total_skipped += len(group["issues"])
                continue
            deduped_groups.append(group)
        groups = deduped_groups

        print(f"  Grouped into {len(groups)} unique error pattern(s).")

        for group in groups:
            if MAX_TICKETS and len(created) >= MAX_TICKETS:
                print(f"  Reached ticket limit ({MAX_TICKETS}), stopping.")
                break

            issue = group["representative"]
            issue_id = str(issue.get("id", ""))
            title = issue.get("title", "unknown")
            group_size = len(group["issues"])

            if group_size > 1:
                print(f"  Processing group ({group_size} issues): {title}")
            else:
                print(f"  Processing issue {issue_id}: {title}")

            event = fetch_latest_event(issue_id)

            if DRY_RUN:
                dry_run_ticket(issue, event, project_name, group)
                created.append({"issue_id": issue_id, "ticket": "DRY-RUN", "title": title,
                                "group_size": group_size, "project": project_name})
            else:
                try:
                    ticket_key = create_jira_ticket(issue, event, project_name, group)
                    print(f"    Created Jira ticket: {ticket_key}")
                    created.append({"issue_id": issue_id, "ticket": ticket_key, "title": title,
                                    "group_size": group_size, "project": project_name})
                    all_group_ids = [str(i.get("id", "")) for i in group["issues"]]
                    skip_ids.update(all_group_ids)
                except Exception as e:
                    print(f"    FAILED to create ticket: {e}")
                    failed.append({"issue_id": issue_id, "title": title, "error": str(e),
                                   "project": project_name})

    # Persist skip IDs so re-runs don't create duplicates for already-processed issues
    if not DRY_RUN and created:
        skip_file = os.environ.get("GLITCHTIP_SKIP_FILE", ".glitchtip-skip-ids.json")
        with open(skip_file, "w") as f:
            json.dump(sorted(skip_ids), f)

    action = "would be created" if DRY_RUN else "created"
    print(f"\nSummary: {len(created)} ticket(s) {action}, "
          f"{total_skipped} skipped (duplicates), {len(failed)} failed")

    projects_seen = dict.fromkeys(
        item["project"] for item in created + failed
    )
    for proj in projects_seen:
        proj_items = [item for item in created if item["project"] == proj]
        proj_failures = [item for item in failed if item["project"] == proj]
        print(f"\n  [{proj}] {len(proj_items)} ticket(s) {action}, {len(proj_failures)} failed")
        for item in proj_items:
            print(f"    {item['ticket']}: {item['title']} (GlitchTip #{item['issue_id']})")
        for item in proj_failures:
            print(f"    FAILED: {item['title']} (GlitchTip #{item['issue_id']}): {item['error']}")


if __name__ == "__main__":
    main()
