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

## Coordinator control tickets

Never claim or implement a ticket labeled `dev-bot-control`, even if it also
has this instance's primary implementation label. Remove only the accidental
implementation label, preserve `dev-bot-control` and all other labels, and
comment on the control ticket so its coordinator can report the correction.

## Implementation ticket labels

Use the per-repository Jira label mapping in the synced config repository at
`data/remote-config/instance/rbac-config-review/agent/review-context.yaml`.
Match the implementation ticket's repository key to
`repositories.<repo>.dev-bot-labels`; do not infer labels from a global label
or from the ticket summary. The matched YAML list is authoritative; use every
label in that list and do not fall back to `BOT_IMPLEMENTATION_LABEL`.

Add the mapped labels to an existing implementation ticket once the plan has
at least a defined first phase and its dependencies are clear, and the ticket
is ready to start. Do this automatically when the coordinator reaches that
decision. Do not label tickets that are only proposed for later phases or the
coordinator control ticket itself.

Also process new human comments on the coordinator ticket. If a comment asks
to start a ticket, phase, or to mark tickets ready, resolve the requested
tickets' repository keys and apply the same mapping when the phase and
dependencies are clear. A comment asking only to revise the plan or
dependencies is not approval to start work; update the plan first and recheck
readiness. Preserve every existing Jira label: fetch the current ticket labels,
add only missing configured labels, then update the ticket. Report the ticket
keys and labels applied in a comment on the coordinator ticket.

Jira uses a shared bot identity, so distinguish user requests from prior bot
comments by the comment content, not by author. If the config file or
repository mapping is missing, or the target repository is ambiguous, do not
guess or update labels. Ask for clarification in a coordinator-ticket comment.

## Specialist subagents

Use the profile-local subagents in `.opencode/agents/` when ticket scope
matches their descriptions. Each subagent contains its native role guidance
for one Fabric Access repository (insights-rbac, rbac-config,
entitlements-api-go, uhc-auth-proxy, turnpike, cloudwatch-aggregator). Select
one relevant subagent; do not delegate recursively.

## Review-bot PR feedback

The instance preflight wakes this Jira-sprint workflow for new comments by
`platex-rehor-bot` that contain `dev-review-bot`, `fabric-access-finding`, or
`fabric-access-review`. Treat these marked comments as external reviewer
feedback even though they use the bot account. Read and validate the finding
before acting; the preflight only selects the task and does not confirm that a
finding is correct.

The shared Jira workflow may still mention `personas/<name>/prompt.md`. This
profile has no persona directory; map those role names to matching native
subagents and do not search for deleted persona files.

OpenCode V2 does not expose the legacy LSP tool. Use each repository's
typecheck, lint, and test commands instead.
