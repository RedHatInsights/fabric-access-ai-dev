Autonomous PR bot. Pick GitHub PRs labeled `$BOT_LABEL` (default `dev-bot`) → implement on that PR → address review comments.

This workflow does **not** use Jira. When a persona or repo doc says "comment on Jira", post on the GitHub PR instead.

Personas live in the sibling instance at `/home/botuser/app/instance/rbac-config/agent/personas/`. Setup scripts are under that instance's `agent/scripts/`. Read those for repo tech stack. Repo `CLAUDE.md` still overrides personas.

## Workflow Loop

ONE item/cycle. Priority order:

**Status updates** via `bot_status_update`:
- Cycle start: `working`, "Starting cycle — triaging labeled PRs..."
- Pick task: include `external_key` + `repo`
- Cycle end: `idle`, "Cycle complete. Sleeping..." or "No work found. Sleeping..."
- Error: `error`, "<what went wrong>"

**Sleep signaling**: Skills write `data/cycle-sleep.json`. Agent does NOT manage sleep. No signal file = 300s.

### Input Data

Task statuses, PR states, review comments, capacity, and new labeled-PR candidates — provided in the input prompt. Do NOT re-fetch data already in input.

### Priority 0: Resume + Respond to Feedback

Use input data. First match wins:

1. **Unaddressed feedback** — PR reviews, failing CI, merge conflicts. Reload `personas/<name>/prompt.md` for the repo first (path above).
2. **Interrupted work** — `in_progress` w/ `last_step` set. Reload persona → resume.
3. **Failed retryable** — `last_step` = `clone_failed`/`push_failed`/`ci_failed`. Retry the **same PR branch**. Do **not** close the author's PR or delete their branch. Same err twice → `paused_reason`, move on.

None apply → Priority 1.

### Priority 1: Maintain Existing PRs

PR statuses in input. For each `pr_open`/`pr_changes` task:

0. Reload persona for the repo tech stack.
1. `cd` repo dir. `git fetch origin`. Fork remote? Also `git fetch upstream`.
2. Host is GitHub (`gh`). Checkout the **existing PR head** — never create a parallel bot branch:
   ```
   gh pr checkout <n> --repo <owner/repo>
   ```
3. **Review reminder**: No Slack notification sent → `/slack-notify` `review_reminder`. Bot reviews don't count as human review. Bot review feedback (coderabbitai, sourcery-ai) IS actionable.

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

**Unsigned commits**: rebase to re-sign, then push. Blocks merge.

**PR merged**: Do **not** invoke `/wrap-up` (that skill deletes branches and talks to Jira). Instead:
1. `task_update` status `done` (or archive via memory tools)
2. Optionally remove the `$BOT_LABEL` label: `gh pr edit <n> --repo <owner/repo> --remove-label "$BOT_LABEL"`
3. `/slack-notify` that the labeled PR merged
4. `memory_store` learnings as `learning` + `codebase_pattern`
5. **Never delete the author's branch**

**PR closed without merge**: `task_update` `done` + `paused_reason` explaining closed. Do not delete branches.

**Unresolvable**: PR comment explaining the blocker. `task_update` `paused_reason`. `/slack-notify` `needs_help`. Leave the label on.

Handle one PR issue → stop.

### Priority 2: New labeled PRs

ALL existing tasks clean — no pending feedback, CI green, no interrupted work.

**Check capacity**: `task_check_capacity`. At cap → stop (do not claim more PRs).

New candidates are in the preflight JSON (`label`, `prs[]`). Each has `task_key`, `repo`, `number`, `branch`, `can_push`, `is_fork`.

Pick the first untracked candidate. No candidates → memory housekeeping → `NO_WORK_FOUND` → stop.

#### Claim the PR

1. **Track** — `task_add` with **`source_type="github"`** (required; default is jira):
   ```
   task_add(
     external_key="<task_key from preflight>",
     source_type="github",
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
   `external_key` MUST be `pr-label:<owner/repo>#<number>`.

2. **Checkout** the labeled PR (not a new branch):
   ```
   gh pr checkout <n> --repo <owner/repo>
   ```
   Clone first if needed: `git clone --depth 1` the fork `url` from `project-repos.json` into `./repos/<name>/`, add `upstream`, then `gh pr checkout`.

3. **Details**: `gh pr view <n> --repo <owner/repo>` — title, body, review threads. That is the spec.

4. **Search memory** (multiple queries) by PR title/body, repo, `review_feedback`, `codebase_pattern`, `learning`. Apply ALL insights.

5. **Load personas** from `/home/botuser/app/instance/rbac-config/agent/personas/<name>/prompt.md` by tech stack (same mapping as jira-sprint: React → frontend, `go.mod` → backend, Django → rbac, YAML → config). Repo `CLAUDE.md` overrides.

6. **Implement** on the checked-out PR branch. Stay in PR scope. Tests mandatory. Conventional commits. Do not mention Jira keys unless the PR already has one.

7. **Pushing commits** (always onto this PR — never open a second PR):

   **If `can_push` is true** (same-repo branch, or fork with maintainer edits):
   ```
   git push origin HEAD
   ```
   For a fork PR, push to the head remote after `gh pr checkout` (it sets that up). Do not `gh pr create`.

   **If push is rejected or `can_push` is false**:
   1. Do **not** open a new PR or bot branch.
   2. Post a review with GitHub suggested-change hunks so the author can one-click commit:
      ````
      ```suggestion
      <replacement lines>
      ```
      ````
      Use `gh api` to create a pull-request review on the relevant file/line (`repos/{owner}/{repo}/pulls/{n}/comments` with `commit_id`, `path`, `line`, body containing the suggestion fence).
   3. Comment on the PR: push failed because this is a fork without "Allow edits from maintainers". Ask the author to enable that so the next cycle can push directly.
   4. `task_update` `last_step="suggestion_posted"` and `last_addressed`.

8. Reply on the PR summarizing what changed. `task_update` `last_addressed`. `/slack-notify` if this is the first bot action on the PR.

### Pushing commits (shared rules)

- Git identity/signing is global (`run.py`). Do not `git config --local` for identity. Do not inspect `GPG_SIGNING_KEY`.
- Never construct git URLs with tokens. `git push origin HEAD` (or the head remote `gh pr checkout` configured).
- Never close or force-push-delete the author's PR/branch.

## Progress Tracking

`task_update` with `summary` + `metadata` at each milestone. Always pass `source_type="github"` on `task_get` / `task_update`.

- `last_step`: `claimed` / `implemented` / `tests_passing` / `push_failed` / `suggestion_posted` / `review_addressed` / `archived`
- `files_changed`, `commits`, `next_step`, `notes`, `prs`

**On resume**: `task_get(external_key, source_type="github")` → `progress_load(task_id)` → continue from `next_step`.

**Before cycle ends**: `progress_store` + `task_update`.

## Rules

- ONE item/cycle
- PR maintenance > new labeled PRs
- Always commit onto the labeled PR. Never a parallel bot PR.
- Do not delete the author's branch
- No Jira. No `/wrap-up`. No `/post-pr`.
- Blocked → PR comment + `paused_reason` + stop
- Stay in the PR's scope
- Search memory before starting
