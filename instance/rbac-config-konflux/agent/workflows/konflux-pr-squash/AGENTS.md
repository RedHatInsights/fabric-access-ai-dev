# Bot Dependency Consolidation Workflow

OpenCode-native instructions for this custom workflow. Legacy `CLAUDE.md` stays
for Python/Claude rollback; this file contains active OpenCode workflow rules.

## Purpose and scope

Apply **major-tier** dependency update PRs from bot authors (e.g.
`red-hat-konflux[bot]`, `dependabot[bot]`) one at a time via the consolidation
script, each producing its own solo PR with a breaking-change investigation,
for easier review and reduced CI load. Minor and patch bumps are explicitly out
of scope — never process, report, close, or comment on them. Major bumps are
never batched together, even with other major bumps for the same ecosystem.

## Preflight

Two preflight scripts run in order: `01-gh-pr-status.py` (CI status on existing
`pr_open` tasks) and `02-check-bot-prs.py` (finds repos with actionable
major-tier bot PRs). The second validates task capacity, at least one open
major-tier bot PR targeting a repo's default branch changing a supported
dependency manifest (`go.mod`/`Pipfile`/`package.json`), no failed
`on-pull-request` check on the source PR, no Go self-replacement directive
moved to a new major module path, no in-progress consolidation task for the
repo, and no open `chore(deps): consolidate` PR or `chore/consolidate-*`
branch. Tier classification prefers the actual old/new versions parsed from
the PR diff over title/body text; title/body parsing is fallback only. PRs
changing no supported manifest are never candidates.

Output is a `repos` array; each entry has `repo`, `bot_url`, `prs`, `groups`
(tier always `major`, count always `1`), and `task_key`. Process each repo
entry by passing `--repo <owner/repo>` to the consolidation script once per
group. `skip` with open bot PRs just means none are major-tier. If preflight
passes, all prerequisites are met — do not re-check them. Never combine two
groups into one script invocation; never feed a minor/patch PR into the script.

## Running the script

For each repo in the preflight output, `cd` into that repo's checkout (clone
first via the `bot_url` if needed) and run:

```bash
python skills/konflux-pr-squash.py --repo <owner/repo>
```

Options: `--bot` (different bot author), `--dry-run` (preview),
`--close-originals`/`--keep-originals` (default keeps originals),
`--no-regenerate-locks`. Never use `--close-originals`.

The script skips PRs labeled/titled DO-NOT-MERGE or abandoned, groups by
ecosystem via diff analysis, applies bumps natively (Go: `go get` + `go mod
tidy`, preserving the `go` directive — a Go toolchain bump instead updates the
`go` line and syncs the hummingbird builder image tag across `Dockerfile*` and
`.tekton/*.yaml`; Python: `Pipfile` + `pipenv lock`; npm: `package.json` +
`npm install`; unknown: `git apply --3way`), regenerates locks once per
directory, pushes the branch, and creates the consolidated PR. It detects
subdirectories and handles monorepos.

## Tier verification and breaking changes

Every PR the preflight hands you should already be major tier — verify before
invoking the script, every run. Major means: Go leading version change or
module-path change (`/v2` → `/v3`); Python/npm leading segment change (treat
0.x → 0.(x+1) as possibly major); a new pinned version violating an existing
caret/tilde range (bump severity up); or an explicit breaking-change marker
(`feat!:`, `fix(deps)!:`, a "breaking" label — always overrides numbers).
Extract the current version from the checked-out manifest, not the PR title.
A title stating only a `0.` target version with no source version is not
actionable — flag it for manual follow-up. A misclassified-looking PR in the
preflight output is a suspected misclassification to report, not to process.

For each major bump:

1. Run the script for that PR alone on its own branch.
2. Research breaking changes: read the bot PR body
   (`gh pr view <n> --repo <owner>/<repo> --json body -q .body`); check
   releases between versions (`gh api repos/<dep-owner>/<dep-repo>/releases`);
   sanity-check with registry CLIs (`npm view <pkg> versions`,
   `pip index versions <pkg>`, `go list -m -versions <module>`) — never raw
   web fetch, which is unavailable.
3. Investigate whether the codebase needs code changes — do not rely on CI
   alone. `grep -rn` for removed/renamed symbols, config keys, and import
   paths; for a Go module-path bump, update every import to the new path in
   the same change (mandatory). If no explicit breaking-change list exists,
   skim release notes for "removed", "renamed", "deprecated", "default",
   "behavior" and note the fallback skim in the PR body. Apply needed code
   changes directly in the branch before pushing; if they are large or
   ambiguous, say so explicitly in the PR body.
4. Create the solo consolidated PR titled e.g.
   `chore(deps)!: <package> major version bump to vX` with a "⚠️ Breaking
   Changes" section, a "Code changes made" section (or "none required"), and a
   checklist item for a human reviewer to confirm behavior beyond green CI.
5. Never auto-close the original bot PR on green CI alone. Leave it open and
   flag it for human sign-off; close only after explicit human approval and
   merge of the consolidated PR.

## Conflict resolution

When the script skips a PR due to conflict or apply failure, do not accept the
skip. Identify the failed PR(s) from "Warning: ... skipping" output, then:
fetch and merge the PR branch (`git fetch origin <pr_branch> && git merge
--no-commit FETCH_HEAD`); on conflicts, take ours for lock files (regenerated
anyway) and theirs for manifests and other files (bot PRs are single-purpose
bumps); stage and continue. If the PR touches only infra/pipeline paths
(`.tekton/`, `.github/`), prefer `git checkout FETCH_HEAD -- <path>/` over a
full merge when the merge pulls unrelated churn (abort with `git merge
--abort`). If merge still fails, cherry-pick the PR commits with
`--no-commit`; if all else fails, apply the dependency bump manually by
editing the manifest. Regenerate locks (`go mod tidy` / `pipenv lock` /
`npm install`) and amend the consolidation commit (`git add -A && git commit
--amend --no-edit`). Skipping 1-2 PRs → resolve manually; skipping most →
investigate root cause and re-run; two bot PRs bumping the same package →
keep the higher version. Note truly unresolvable PRs in the PR description.

## Failure handling

- **npm lock failure**: retry `npm install --legacy-peer-deps`; check for
  version constraint conflicts; drop the lower version if needed.
- **pipenv lock failure**: first check whether the conflict is pre-existing by
  running the same `pipenv lock` against `origin/master`'s Pipfile (scratch
  worktree or stash). If pre-existing, leave `Pipfile.lock` unregenerated,
  name the conflicting packages in the PR body, and proceed with manifest
  changes only. Never hand-reconstruct lock entries; never run
  `pipenv upgrade <package>` (it corrupts the manifest with wildcards and
  duplicate lines — edit the pinned line directly); always
  `git diff origin/master -- Pipfile` before amending to confirm intended
  pins in their original positions.
- **go mod tidy failure**: try `go mod tidy -e`, then inspect `go.mod`.
- **Branch/PR cleanup**: if the consolidated PR fails CI or cannot be created,
  delete the remote branch (`git push origin --delete <branch>`, verifying it
  was pushed first via `git ls-remote --heads origin <branch>`), clean the
  local branch, and clean up per-ecosystem branches with no applied PRs. No
  consolidated PRs at all → the workflow exits with an error.
- **Commit signing**: if push fails with a signing error, check
  `git config commit.gpgsign` and ensure GPG is configured before retrying.
- **`gh pr create` "can't find git"**: create the PR via the API directly
  (`gh api repos/<owner>/<repo>/pulls -X POST -f title=... -f head=... -f
  base=... -f body=...`).
- **Single-PR groups**: expected shape, not a signal to skip. A script crash
  (e.g. missing package-manager binary) may leave an orphaned local branch;
  verify it was pushed before deleting anything.

## Agent responsibilities — stop after task creation

**CRITICAL: stop after creating the task. Do NOT close original PRs. Do NOT
set task status to `done`. The `gh_pr_status.py` preflight handles CI
monitoring, original PR closure, and task completion on subsequent cycles.**

1. For each repo entry, clone if needed via `bot_url`, then run the script
   once per group with `--repo <owner>/<repo>`. Trust the preflight's
   grouping; flag suspected misclassifications instead of processing them.
2. Every invocation gets the full breaking-change investigation above applied
   in the PR branch before pushing.
3. Never use `--close-originals`.
4. `--dry-run` first only if the user wants a preview.
5. After the script completes, check for skipped PRs and resolve conflicts as
   above before pushing.
6. Verify the actual code change matches the bot PR title — re-check the
   manifest version against the source PR's intended version; the script's
   "Applied successfully" only confirms the file changed, not that it changed
   to the correct version. A mismatch is unresolved work.
7. Create a memory server task with `status="pr_open"` for each PR produced
   (see Task tracking).
8. **STOP.** The cycle ends here.
9. Report: major-tier PRs processed per ecosystem, manual conflict
   resolutions performed, created PR URLs, per-bump breaking-change research
   summaries, and unresolvable PRs with reasons.
10. Never modify the consolidation script itself.

## Task tracking

This workflow uses the memory server task system; the preflight checks tasks
before starting — do not duplicate those checks. After pushing each PR, call
`task_add` (from `bot-memory`) with:

- `external_key="konflux-pr-squash:<org/repo>:<ecosystem>:major:<original_pr_number>"`
  (must include the original bot PR number — several independent major PRs
  can be open for the same repo/ecosystem at once)
- `repo`, `branch`, `status="pr_open"`, `source_type="github"`
- `title="Major bump: <package> to <target_version> (<ecosystem>)"`
- `metadata={"prs": [{"repo": "<org/repo>", "number": <n>, "host": "github"}],
  "original_prs": [<original bot PR number>], "ecosystem": "<go|python|npm>",
  "is_major_bump": true}`

`is_major_bump` is always `true` and gates the extra human-review step.
`task_add` fails at 10+ active tasks — the preflight's capacity check should
have ruled this out.

## CI result handling (later cycles, not the creation cycle)

`gh_pr_status.py` monitors `pr_open` tasks and wakes the agent on results.
CI passing is never sufficient by itself to close originals.

- **CI passes** → on the first wake: post a comment on the consolidated PR
  summarizing the breaking-change research, tagged ready for human review;
  update the task to `pr_changes` with `metadata.awaiting_human_review: true`.
- **Later wake with `awaiting_human_review: true`** → check merge status with
  `gh pr view <n> --repo <owner>/<repo> --json state,mergedAt`. Merged → close
  the original bot PR(s) with a linking comment, set task `done`. Closed
  without merging → delete the remote branch if it still exists, set task
  `failed`, leave originals open. Still open → do nothing this cycle. A task
  stuck in review blocks further consolidations for that repo/ecosystem by
  design; surface unreasonable stalls in the report.
- **CI fails** → investigate, fix, re-push. Do not close originals. If
  unfixable, delete the remote branch and update the task status.

Multiple PRs in one cycle (across ecosystems or multiple independent major
bumps in one ecosystem) each get a separate task with a distinct external key.
