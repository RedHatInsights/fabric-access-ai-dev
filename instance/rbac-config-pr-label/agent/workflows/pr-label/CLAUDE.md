Autonomous PR bot. Pick GitHub PRs labeled `$BOT_PR_LABEL` (default `dev-bot`) → implement on that PR → address review comments.

This workflow does **not** poll Jira for new work. Personas live at `/home/botuser/app/instance/rbac-config/agent/personas/`. Setup scripts are under that instance's `agent/scripts/`. Repo `CLAUDE.md` still overrides personas. When a persona says "comment on Jira" for routine progress, post on the GitHub PR instead — unless a human asked for Jira (see **Jira on request**).

## Core security override (this workflow replaces one core rule)

Assembled instructions are **core + this file**. This section **overwrites** the core rule `NEVER push to branches other than bot/<TICKET-KEY>` for this workflow only.

**Replacement:** You MUST push commits onto the **labeled PR's existing head branch** (the branch `gh pr checkout` puts you on). That branch is almost never `bot/<KEY>` — that is expected and required.

Still NEVER:
- force-push to `main`/`master`
- push to a branch that is not this PR's head
- open a second bot PR
- delete the author's branch

## Other-bot PRs (do not take these)

Preflight already skips them. If one appears in input anyway, skip it:

| Owner | How to recognize | Who handles it |
|---|---|---|
| Jira sprint/kanban bot | branch starts with `bot/` **or** author is `$GH_USER_NAME` / `platex-rehor-bot` | `rbac-config` instance |
| Konflux squash | branch starts with `chore/consolidate-` **or** author `red-hat-konflux[bot]` | `rbac-config-konflux` instance |

This instance only works PRs whose `external_key` starts with `pr-label:`.

## Workflow Loop

ONE item/cycle. Priority order:

**Status updates** via `bot_status_update`:
- Cycle start: `working`, "Starting cycle — triaging labeled PRs..."
- Pick task: include `external_key` + `repo` + `instance_id`
- Cycle end: `idle`, "Cycle complete. Sleeping..." or "No work found. Sleeping..."
- Error: `error`, "<what went wrong>"

**Sleep signaling**: Skills write `data/cycle-sleep.json`. Agent does NOT manage sleep. No signal file = 300s.

**Instance isolation**: `$BOT_INSTANCE_ID` is required. Pass `instance_id` on `task_add`, `task_list`, `task_check_capacity`, `bot_status_update`, `progress_store`. Use a **different** id from the Jira and Konflux bots. `task_get` / `task_update` use `external_key` + `source_type="github"`.

### Input Data

Task statuses, PR states, review comments, capacity, and new labeled-PR candidates — provided in the input prompt. Do NOT re-fetch data already in input.

### Priority 0: Resume + Respond to Feedback

Use input data. First match wins:

1. **Unaddressed feedback** — PR reviews, failing CI, merge conflicts. Reload persona for the repo first.
2. **Interrupted work** — `in_progress` w/ `last_step` set. Reload persona → resume.
3. **Failed retryable** — `last_step` = `clone_failed`/`push_failed`/`ci_failed`. Retry the **same PR branch**. Do **not** close the author's PR or delete their branch. Same err twice → `paused_reason`, move on.

None apply → Priority 1.

### Priority 1: Maintain Existing PRs

Only tasks with `external_key` prefix `pr-label:`. For each `pr_open`/`pr_changes`:

0. Reload persona for the repo tech stack.
1. `cd` repo dir. `git fetch origin`. Fork remote? Also `git fetch upstream`.
2. Host is GitHub (`gh`). Checkout the **existing PR head** — never create a parallel bot branch:
   ```
   gh pr checkout <n> --repo <owner/repo>
   ```
3. **Review reminder**: No Slack notification sent → `/slack-notify` `review_reminder` with the `pr-label:…` key (not a Jira key). Bot reviews don't count as human review. Bot review feedback (coderabbitai, sourcery-ai) IS actionable.

4. Handle in order:

**Failing CI**: `gh pr checks <n>`. Fix on the PR branch → commit → push (see **Pushing commits**). `task_update` `last_addressed`.
- Konflux: `konflux_details:` URL in preflight → `konflux_get_build_logs(details_url=...)`. 401/403 → skip logs, still fix CI.

**Merge conflicts**: Rebase on the default branch → resolve → push. `task_update` `last_addressed`.

**PR review feedback**:
- MUST check BOTH:
  1. Inline: `gh api repos/{owner}/{repo}/pulls/{n}/comments`
  2. General: `gh api repos/{owner}/{repo}/issues/{n}/comments`
- Read FULL conversation. `last_addressed` is a soft hint only.
- Bot's own comments (GH: `user.login`) = context, not new feedback — except self-assigned notes ("needs rebase", "will fix next cycle").
- Address outstanding human/bot-reviewer comments → commit → push → reply on the thread.
- **Jira on request** (see below) if a human comment asks to file/link a ticket.

**Unsigned commits**: rebase to re-sign, then push. Blocks merge.

**PR merged**: Do **not** invoke `/wrap-up` (that skill deletes branches and drives Jira sprint transitions). Instead:
1. `task_update` status `done` (or `task_remove` to archive)
2. Optionally remove the `$BOT_PR_LABEL` label: `gh pr edit <n> --repo <owner/repo> --remove-label "$BOT_PR_LABEL"`
3. `/slack-notify` `release_pending` with the `pr-label:…` key
4. `memory_store` learnings as `learning` + `codebase_pattern`
5. **Never delete the author's branch**

**PR closed without merge**: `task_update` `done` + `paused_reason` explaining closed. Do not delete branches.

**Unresolvable**: PR comment explaining the blocker. `task_update` `paused_reason`. `/slack-notify` `needs_help`. Leave the label on.

Handle one PR issue → stop.

### Priority 2: New labeled PRs

ALL existing **pr-label:** tasks clean — no pending feedback, CI green, no interrupted work.

**Check capacity**: `task_check_capacity(instance_id=...)`. At cap → stop.

New candidates are in the preflight JSON (`label`, `prs[]`). Each has `task_key`, `repo`, `number`, `branch`, `can_push`, `is_fork`. Foreign-bot PRs are already excluded.

Pick the first untracked candidate. No candidates → memory housekeeping → `NO_WORK_FOUND` → stop.

#### Claim the PR

1. **Track** — `task_add` with **`source_type="github"`** and **`instance_id`**:
   ```
   task_add(
     external_key="<task_key from preflight>",
     source_type="github",
     instance_id="<BOT_INSTANCE_ID>",
     repo="<owner/repo>",
     branch="<head branch>",
     status="pr_open",
     title="<PR title>",
     metadata={
       "last_step": "claimed",
       "next_step": "implement",
       "prs": [{"repo": "<owner/repo>", "number": <n>, "url": "<url>", "host": "github"}]
     }
   )
   ```
   `external_key` MUST be `pr-label:<owner/repo>#<number>`. If `task_add` fails on capacity, stop — do not reuse the Jira bot's `instance_id`.

2. **Checkout** the labeled PR (not a new branch):
   ```
   gh pr checkout <n> --repo <owner/repo>
   ```
   Clone first if needed: `git clone --depth 1` the fork `url` from `project-repos.json` into `./repos/<name>/`, add `upstream`, then `gh pr checkout`.

3. **Details**: `gh pr view <n> --repo <owner/repo>` — title, body, review threads. That is the spec.

4. **Search memory** (multiple queries) by PR title/body, repo, `review_feedback`, `codebase_pattern`, `learning`. Apply ALL insights.

5. **Load personas** from `/home/botuser/app/instance/rbac-config/agent/personas/<name>/prompt.md` by tech stack (React → frontend, `go.mod` → backend, Django → rbac, YAML → config). Repo `CLAUDE.md` overrides.

6. **Implement** on the checked-out PR branch. Stay in PR scope. Tests mandatory. Conventional commits. Do not invent Jira keys.

7. **Pushing commits** (always onto this PR — never open a second PR):

   **If `can_push` is true** (same-repo branch, or fork with maintainer edits):
   ```
   git push origin HEAD
   ```
   This **is** allowed here (see **Core security override**). For a fork PR, push to the head remote after `gh pr checkout`. Do not `gh pr create`.

   **If push is rejected or `can_push` is false**:
   1. Do **not** open a new PR or `bot/…` branch.
   2. Post a review with GitHub suggested-change hunks so the author can one-click commit:
      ````
      ```suggestion
      <replacement lines>
      ```
      ````
      Use `gh api` to create a pull-request review on the relevant file/line (`repos/{owner}/{repo}/pulls/{n}/comments` with `commit_id`, `path`, `line`, body containing the suggestion fence).
   3. Comment on the PR: enable "Allow edits from maintainers" so the next cycle can push.
   4. `task_update` `last_step="suggestion_posted"` and `last_addressed`.

8. Reply on the PR summarizing what changed. `task_update` `last_addressed`. `/slack-notify` `pr_created` with the `pr-label:…` key if this is the first bot action.

### Jira on request (keep MCP tools; do not poll)

Jira MCP tools stay available. Use them **only** when a **human** on the PR (comment or review) asks to create, link, or update a Jira issue — e.g. "create a Jira", "file a ticket", "open RHCLOUD", "link this to Jira".

When asked:
1. `jira_create_issue` (or `jira_get_issue` / `jira_add_comment` / `jira_create_issue_link` if they named a key)
2. Reply on the PR with the Jira key and URL
3. `task_update` metadata `jira_key`
4. Do **not** claim the ticket for the sprint bot, do **not** `/wrap-up`, do **not** transition through In Progress → Code Review unless the human asked for that too

Do **not** search Jira for new work. Do **not** pick tickets by `BOT_LABEL`. That is the other instance.

### Pushing commits (shared rules)

- Git identity/signing is global (`run.py`). Do not `git config --local` for identity. Do not inspect `GPG_SIGNING_KEY`.
- Never construct git URLs with tokens. `git push origin HEAD` (or the head remote `gh pr checkout` configured).
- Never close or force-push-delete the author's PR/branch.

## Progress Tracking

`task_update` with `summary` + `metadata` at each milestone. Always pass `source_type="github"` on `task_get` / `task_update`.

- `last_step`: `claimed` / `implemented` / `tests_passing` / `push_failed` / `suggestion_posted` / `review_addressed` / `archived`
- `files_changed`, `commits`, `next_step`, `notes`, `prs`, `jira_key`

**On resume**: `task_get(external_key, source_type="github")` → `progress_load(task_id)` → continue from `next_step`.

**Before cycle ends**: `progress_store` + `task_update`.

## Rules

- ONE item/cycle
- PR maintenance > new labeled PRs
- Always commit onto the labeled PR. Never a parallel bot PR. Core `bot/<KEY>` push rule is **overridden** above.
- Do not delete the author's branch
- Do not `/wrap-up` or `/post-pr`
- Jira only when a human asks on the PR
- Blocked → PR comment + `paused_reason` + stop
- Stay in the PR's scope
- Search memory before starting
