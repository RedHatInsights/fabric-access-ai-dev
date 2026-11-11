## Jira issue creation

Whenever this instance creates a Jira issue, set its **Team** field to **Console Access** in the `jira_create_issue` call. Resolve the Team custom-field ID and required value format from Jira field options or an existing issue, then include it in the `additional_fields` JSON alongside any other required fields. If needed, find an existing RHCLOUD issue with Team = Console Access using `jira_search`, inspect it with `jira_get_issue(fields="*all")`, and reuse the Team custom-field key and value shape; confirm the option with `jira_get_field_options` when available. Do not create the issue without the Team value; if it cannot be set, report the blocker.

## Review-bot PR feedback

The instance preflight wakes this Jira-sprint workflow for new comments by
`platex-rehor-bot` that contain `dev-review-bot`, `fabric-access-finding`, or
`fabric-access-review`. Treat these marked comments as external reviewer
feedback even though they use the bot account. Read and validate the finding
before acting; the preflight only selects the task and does not confirm that a
finding is correct.
