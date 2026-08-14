# GlitchTip-Jira Integration Workflow

Ingests unresolved errors from GlitchTip and creates Jira bug tickets with full error details.

## Quick Start

1. Set the required environment variables (see table below)
2. Run the preflight preview to verify grouping: `python preflight/02-preview-ticket-groups.py`
3. Run the duplicate check: `python preflight/01-check-duplicate-tickets.py`
4. Run the main script: `python skills/glitchtip-jira-integration.py`

## How it works

1. **Preflight — duplicate check** (`01-check-duplicate-tickets.py`): Searches Jira for existing tickets labeled `glitchtip-issue-{id}`. Duplicate issue IDs are written to a skip file.
2. **Preflight — group preview** (`02-preview-ticket-groups.py`): Shows how issues will be grouped and how many tickets would be created. No Jira access needed.
3. **Main script** (`glitchtip-jira-integration.py`): Fetches unresolved issues, groups them by normalized error pattern, enriches with event data, and creates Jira tickets via MCP.

## Issue Grouping

GlitchTip often creates separate issues for errors that differ only by dynamic values (port numbers, UUIDs, workspace IDs). The script normalizes error titles by stripping these values and groups issues with the same pattern into a single Jira ticket. Each ticket includes all grouped GlitchTip issue IDs and aggregated occurrence counts.

## Deduplication

Each Jira ticket is labeled `glitchtip-issue-{id}` for every issue in the group. The preflight script queries Jira with JQL to find existing tickets with those labels before the main script runs.

The skip file (`GLITCHTIP_SKIP_FILE`, default `.glitchtip-skip-ids.json`) stores `{glitchtip_issue_id: jira_ticket_key}`, not just a list of IDs — the ticket key is needed to comment on recurrences (see below). Older skip files that only contain a plain list of IDs are still loaded fine; those entries just have no known ticket key.

### Recurrence handling

Grouping happens on *all* fetched issues, including ones already in the skip file, so a brand-new GlitchTip issue can be matched against an already-ticketed error pattern. When that happens:
- If the existing ticket's key is known (tracked in the skip file), the main script adds a comment to that ticket noting the new occurrence(s) instead of creating a duplicate ticket.
- If the pattern is fully covered by already-ticketed issues with no new occurrences, it's skipped entirely.
- If the ticket key isn't known (e.g. a legacy list-format skip entry), the recurrence is skipped with a log line but no comment is posted — there's no way to attach it to the right ticket.

## DRY_RUN mode

Both scripts support dry-run mode for testing without Jira connectivity:
- Set `DRY_RUN=1` explicitly, or omit `JIRA_MCP_URL` — both trigger dry-run
- Preflight: lists all matching GlitchTip issues, skips Jira duplicate check
- Main script: fetches issues and events from GlitchTip, shows ticket previews instead of creating

## Required environment variables

| Variable | Purpose |
|----------|---------|
| `GLITCHTIP_ORG` | GlitchTip organization slug |
| `GLITCHTIP_TOKEN` | GlitchTip API token with `event:read`, `project:read`, `org:read` scopes |
| `JIRA_MCP_URL` | MCP Atlassian server endpoint (omit for dry-run) |
| `JIRA_PROJECT_KEY` | Jira project key for created tickets |
| `BOT_INSTANCE_ID` | Bot instance identifier |

## Optional environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `GLITCHTIP_URL` | `https://glitchtip.devshift.net` | GlitchTip base URL |
| `GLITCHTIP_PROJECTS` | _(all projects)_ | Comma-separated list of project names/slugs to monitor |
| `DRY_RUN` | `false` | Set to `1`/`true`/`yes` to preview without creating tickets |
| `MAX_TICKETS` | _(unlimited)_ | Maximum tickets to create per run |

## Priority logic

Priority is computed based on error level, occurrence count, and whether the project name contains "prod":
- `fatal`/`critical` → `Major`
- Non-prod projects → `Minor`
- Prod `error` with 100+ occurrences → `Major`
- Everything else → `Normal`

## Project filtering

`GLITCHTIP_PROJECTS` accepts project names or slugs (both are checked). Example:
```
GLITCHTIP_PROJECTS=my-app-prod,my-app-stage
```

## Jira ticket mapping

- **Summary**: `[GlitchTip] [project-name] error title`
- **Type**: Bug
- **Priority**: Computed from error level and occurrence count (see above)
- **Labels**: `glitchtip-issue-{id}` (one per grouped issue), `glitchtip`, project name, environment
- **Description**: Full error report with stacktrace, tags, context data, breadcrumbs, occurrence counts, and timestamps

## Shared implementation

`01-check-duplicate-tickets.py` and `02-preview-ticket-groups.py` both import their GlitchTip/Jira helper functions directly from `skills/glitchtip-jira-integration.py` (via `importlib`, since the filename isn't a valid module name), so there's a single implementation of the HTTP/pagination/retry logic shared by all three scripts.
