# PR Label Workflow

OpenCode-native instructions for this custom workflow. Legacy `CLAUDE.md` stays
for Python/Claude rollback; this file contains active OpenCode workflow rules.

Autonomous PR bot. Pick GitHub PRs labeled `$BOT_PR_LABEL` (default
`dev-bot`) → implement on that PR → address review comments. This workflow
does **not** poll Jira for new work. Repo-specific guidance: read the matching
subagent body from the sibling rbac-config instance
(`/home/botuser/app/instance/rbac-config/agent/.opencode/agents/<name>.md` —
see the instance AGENTS.md for the name mapping); this profile has no persona
directory. A repo's own `CLAUDE.md`/`AGENTS.md` overrides that guidance. When
guidance says "comment on Jira" for routine progress, post on the GitHub PR
instead — unless the title/merge policy or a human request requires Jira.

## PR title and Jira policy

Every PR handled by this workflow MUST contain a Jira key in square brackets
in its title (e.g. `feat: add new feature [RHCLOUD-12345]`, matching
`[PROJECT-12345]`). Check before implementing or updating a claimed PR.

- Title already has a key → use that issue; save it as task metadata
  `jira_key`.
- No key → create one Jira Task reflecting the PR's actual changes, using
  `$BOT_JIRA_PROJECT` when set, otherwise `RHCLOUD`. Include the PR URL,
  original title/body, changed files, and an implementation summary. Set the
  **Team** field to **Console Access** in `jira_create_issue` (resolve the
  custom-field ID and value shape from field options or an existing issue via
  `jira_search`/`jira_get_issue(fields="*all")`; include it in
  `additional_fields`; do not create the issue without it — report the
  blocker if it cannot be set). Capture the PR author's login from the PR
  data, then make a best-effort `jira_update_issue` assignment
  (`fields='{"assignee":"<PR_AUTHOR_LOGIN>"}'`) — never assign the bot; a
  failure is non-fatal, leave unassigned and record it.
- After creation, update the PR title with `gh pr edit` so the key is
  present, reply on the PR with the key and Jira URL, and store the key with
  `task_update` metadata. Do not create a second ticket on later cycles —
  first check task metadata and the current PR title.

A PR with a missing key is actionable work, not a reason to skip it.

## Core security override

This workflow replaces the core rule "NEVER push to branches other than
`bot/<TICKET-KEY>`". You MUST push commits onto the **labeled PR's existing
head branch** (the branch `gh pr checkout` puts you on) — that branch is
almost never `bot/<KEY>`, and that is expected. Still NEVER: force-push to
`main`/`master`, push to a branch that is not this PR's head, open a second
bot PR, or delete the author's branch.

## Other-bot PRs (do not take these)

Preflight already skips them; skip any that appear anyway: branch starting
with `bot/` or author `$GH_USER_NAME`/`platex-rehor-bot` → the `rbac-config`
Jira instance; branch starting with `chore/consolidate-` or author
`red-hat-konflux[bot]` → the `rbac-config-konflux` instance. This instance
only works PRs whose `external_key` starts with `pr-label:`.

## Workflow loop

ONE item per cycle. Priority: existing-PR maintenance and feedback first,
then new labeled PRs. Use `bot_status_update`: `working` at cycle start,
`idle` ("Cycle complete"/"No work found") at cycle end, `error` on failure.
Include `external_key` + `repo` + `instance_id` when picking a task. Skills
write `data/cycle-sleep.json` for sleep signaling — the agent does not manage
sleep; no signal file means 300s. Use input-prompt data (task statuses, PR
states, review comments, capacity, candidates); do not re-fetch what is
already in the input.

### Priority 0: resume + respond to feedback

First match wins: (1) unaddressed feedback — reviews, failing CI, merge
conflicts; reload repo guidance first; (2) interrupted work — `in_progress`
with `last_step`; reload guidance and resume; (3) failed retryable —
`clone_failed`/`push_failed`/`ci_failed`; retry the same PR branch, never
close the author's PR or delete their branch; same error twice → set
`paused_reason` and move on.

### Priority 1: maintain existing PRs (`pr-label:` tasks only)

For each `pr_open`/`pr_changes` task: reload repo guidance; `cd` into the repo
(`git fetch origin`, plus `git fetch upstream` for forks); check out the
existing PR head with `gh pr checkout <n> --repo <owner>/<repo>` — never
create a parallel bot branch. If no Slack notification was sent for a human
review, `/slack-notify` `review_reminder` with the `pr-label:…` key (bot
reviews don't count; coderabbitai/sourcery-ai feedback IS actionable). Handle
in order:

- **Failing CI**: `gh pr checks <n>`; fix on the PR branch, commit, push,
  `task_update` `last_addressed`. For Konflux failures use
  `konflux_get_build_logs(details_url=...)` from the preflight's
  `konflux_details:` URL; on 401/403 skip the logs and still fix CI.
- **Merge conflicts**: rebase on the default branch, resolve, push,
  `task_update` `last_addressed`.
- **Review feedback**: check BOTH inline comments
  (`gh api repos/{owner}/{repo}/pulls/{n}/comments`) and general comments
  (`gh api repos/{owner}/{repo}/issues/{n}/comments`); read the full
  conversation (`last_addressed` is a soft hint). The bot's own comments are
  context, not feedback, except self-assigned notes. Address outstanding
  human/bot-reviewer comments, commit, push, reply on the thread. File/link a
  Jira issue if a human comment asks for one (see **Additional Jira
  requests**).
- **Unsigned commits**: rebase to re-sign, then push.
- **PR merged**: do NOT `/wrap-up` (it deletes branches and drives Jira
  sprint transitions). Instead: resolve `jira_key` from task metadata or the
  merged PR title (create the Jira Task first if neither exists, including
  the best-effort author assignment); fetch transitions with
  `jira_get_transitions` and move the issue to **Release Pending** with
  `jira_transition_issue` (skip only if already in the appropriate final
  state); add a `jira_add_comment` with the merged PR URL and summary;
  `task_update` status `done` (or `task_remove`) retaining `metadata.jira_key`;
  optionally remove the label with
  `gh pr edit <n> --repo <owner>/<repo> --remove-label "$BOT_PR_LABEL"`;
  `/slack-notify` `release_pending`; `memory_store` learnings as `learning` +
  `codebase_pattern`; never delete the author's branch.
- **PR closed without merge**: `task_update` `done` + `paused_reason`
  explaining the closure; do not delete branches.
- **Unresolvable**: PR comment explaining the blocker, `task_update`
  `paused_reason`, `/slack-notify` `needs_help`, leave the label on.

Handle one PR issue, then stop.

### Priority 2: new labeled PRs

Only when all existing `pr-label:` tasks are clean. Check capacity with
`task_check_capacity(instance_id=...)`; at cap → stop. Candidates come from
the preflight JSON (`label`, `prs[]` with `task_key`, `repo`, `number`,
`branch`, `can_push`, `is_fork`); foreign-bot PRs are already excluded. Pick
the first untracked candidate; none → memory housekeeping → `NO_WORK_FOUND` →
stop.

Claim: (1) `task_add` with `source_type="github"`, `instance_id`,
`external_key` = the preflight `task_key` (MUST be
`pr-label:<owner>/<repo>#<number>`), `repo`, `branch`, `status="pr_open"`,
`title`, and metadata `last_step`/`next_step`/`jira_key`/`prs`; on capacity
failure stop — do not reuse the Jira bot's `instance_id`. (2) Check out the
labeled PR with `gh pr checkout <n> --repo <owner>/<repo>`, cloning first via
the fork `url` from `project-repos.json` into `./repos/<name>/` and adding
`upstream` if needed. (3) `gh pr view <n> --repo <owner>/<repo>` — title,
body, review threads are the spec. (4) Search memory (multiple queries) by PR
title/body, repo, `review_feedback`, `codebase_pattern`, `learning`; apply
all insights. (5) Load repo guidance per the instance AGENTS.md mapping.
(6) Implement on the checked-out PR branch; stay in PR scope; tests
mandatory; conventional commits; never invent Jira keys. Documentation is part
of implementation: update the repo's canonical docs page or add a focused
page in its established docs location, in the same PR; state docs-neutrality
in the PR summary when no update is needed. (7) Push:

- `can_push` true (same-repo branch or fork with maintainer edits):
  `git push origin HEAD` (allowed under the security override; for a fork PR
  push to the head remote `gh pr checkout` configured). Never `gh pr create`.
- Push rejected or `can_push` false: do not open a new PR or `bot/…` branch;
  post a review with ```suggestion``` hunks via
  `gh api repos/{owner}/{repo}/pulls/{n}/comments` (with `commit_id`, `path`,
  `line`); comment on the PR asking the author to enable "Allow edits from
  maintainers"; `task_update` `last_step="suggestion_posted"` and
  `last_addressed`.

(8) Reply on the PR summarizing changes; `task_update` `last_addressed`;
`/slack-notify` `pr_created` with the `pr-label:…` key if this is the first
bot action.

## Additional Jira requests

Jira MCP tools stay available. Beyond the mandatory title/merge lifecycle
above, use them only when a human on the PR asks to create, link, or update a
Jira issue: `jira_create_issue` (or `jira_get_issue`/`jira_add_comment`/
`jira_create_issue_link` if they named a key), reply on the PR with the key
and URL, `task_update` metadata `jira_key`. Do not claim the ticket for the
sprint bot, do not `/wrap-up` or `/post-pr`, and do not transition through In
Progress → Code Review unless the human asked. Do not search Jira for new
work and do not pick tickets by `BOT_LABEL` — that is the other instance.

## Pushing commits (shared rules)

Git identity/signing is global (`run.py`); do not `git config --local` for
identity and do not inspect `GPG_SIGNING_KEY`. Never construct git URLs with
tokens — use `git push origin HEAD` (or the head remote `gh pr checkout`
configured). Never close or force-push-delete the author's PR/branch.

## Progress tracking

`task_update` with `summary` + `metadata` at each milestone, always with
`source_type="github"` on `task_get`/`task_update`. `last_step` values:
`claimed` / `implemented` / `tests_passing` / `push_failed` /
`suggestion_posted` / `review_addressed` / `archived`; also `files_changed`,
`commits`, `next_step`, `notes`, `prs`, `jira_key`. On resume:
`task_get(external_key, source_type="github")` → `progress_load(task_id)` →
continue from `next_step`. Before cycle end: `progress_store` +
`task_update`.

## Rules

- ONE item per cycle; PR maintenance over new labeled PRs.
- Always commit onto the labeled PR; never a parallel bot PR; the core
  `bot/<KEY>` push rule is overridden above.
- Never delete the author's branch; never `/wrap-up` or `/post-pr`.
- Jira only per the title/merge lifecycle or when a human asks on the PR.
- Blocked → PR comment + `paused_reason` + stop.
- Stay in the PR's scope; search memory before starting.
