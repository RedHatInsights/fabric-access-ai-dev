# PR Label Instance — Additional Instructions

OpenCode-native counterpart for instance-level guidance. The legacy
Python/Claude runtime keeps its own instruction assembly; keep behavior
aligned during the rollback window.

## Scope and isolation

- This instance works GitHub PRs labeled `$BOT_PR_LABEL` (default `dev-bot`)
  across the Fabric Access repositories in `project-repos.json`, pushing
  commits onto each labeled PR's existing head branch and addressing review
  comments. It does not poll Jira for new work.
- `$BOT_INSTANCE_ID` is required. Pass `instance_id` on `task_add`,
  `task_list`, `task_check_capacity`, `bot_status_update`, and
  `progress_store`, using an id distinct from the Jira (`rbac-config`) and
  Konflux (`rbac-config-konflux`) instances. `task_get`/`task_update` use
  `external_key` + `source_type="github"`.
- Only tasks whose `external_key` starts with `pr-label:` belong to this
  instance.

## Repository guidance

When a task needs repo-specific implementation guidance, read the matching
subagent body from the sibling `rbac-config` instance at
`/home/botuser/app/instance/rbac-config/agent/.opencode/agents/<name>.md`
(insights-rbac → `rbac`, rbac-config → `rbac-config`, entitlements-api-go →
`entitlements`, uhc-auth-proxy → `uhc-auth-proxy`, turnpike → `turnpike`,
cloudwatch-aggregator → `cloudwatch-aggregator`). Ignore the YAML frontmatter
in those files; the body carries the guidance. A repository's own
`CLAUDE.md`/`AGENTS.md` overrides them. This instance defines no personas.

OpenCode V2 does not expose the legacy LSP tool. Use each repository's
typecheck, lint, and test commands instead.

Follow the active workflow's `AGENTS.md` for the full PR lifecycle, Jira
title policy, push rules, and task tracking.
