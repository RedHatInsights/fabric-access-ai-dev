## Jira issue creation

Whenever this instance creates a Jira issue, set its **Team** field to **Console Access** in the `jira_create_issue` call. Resolve the Team custom-field ID and required value format from Jira field options or an existing issue, then include it in the `additional_fields` JSON alongside any other required fields. If needed, find an existing RHCLOUD issue with Team = Console Access using `jira_search`, inspect it with `jira_get_issue(fields="*all")`, and reuse the Team custom-field key and value shape; confirm the option with `jira_get_field_options` when available. Do not create the issue without the Team value; if it cannot be set, report the blocker.

Never claim or implement a ticket labeled `dev-bot-control`, even if it also
has this instance's primary implementation label. Remove only the accidental
implementation label, preserve `dev-bot-control` and all other labels, and
comment on the control ticket so its coordinator can report the correction.
