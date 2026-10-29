# Fabric Access Instance — Additional Instructions

OpenCode-native counterpart to `CLAUDE.md`. Keep both files aligned during the
Python/Claude rollback window.

## Jira issue creation

Whenever this instance creates a Jira issue, set its **Team** field to
**Console Access** in the `jira_create_issue` call. Resolve the Team
custom-field ID and required value format from Jira field options or an
existing issue, then include it in the `additional_fields` JSON alongside any
other required fields. If needed, find an existing RHCLOUD issue with
Team = Console Access using `jira_search`, inspect it with
`jira_get_issue(fields="*all")`, and reuse the Team custom-field key and value
shape; confirm the option with `jira_get_field_options` when available. Do not
create the issue without the Team value; if it cannot be set, report the
blocker.

## Specialist subagents

Use the profile-local subagents in `.opencode/agents/` when ticket scope
matches their descriptions. Each subagent contains its native role guidance
for one Fabric Access repository (insights-rbac, rbac-config,
entitlements-api-go, uhc-auth-proxy, turnpike, cloudwatch-aggregator). Select
one relevant subagent; do not delegate recursively.

The shared Jira workflow may still mention `personas/<name>/prompt.md`. This
profile has no persona directory; map those role names to matching native
subagents and do not search for deleted persona files.

OpenCode V2 does not expose the legacy LSP tool. Use each repository's
typecheck, lint, and test commands instead.
