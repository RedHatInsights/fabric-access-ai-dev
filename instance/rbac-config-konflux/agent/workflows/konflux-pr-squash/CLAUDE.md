# Bot Dependency Consolidation Workflow

## Purpose

Apply **major-tier** dependency update PRs from bot authors (e.g., `red-hat-konflux[bot]`, `dependabot[bot]`) one at a time via the consolidation script, each producing its own solo PR with a breaking-change investigation, for easier review and reduced CI load. Minor and patch bumps are explicitly out of scope for this workflow and are never processed or reported — they are left for whatever other process (or manual review) handles them. Major bumps are never batched together, even with other major bumps for the same ecosystem — each one is isolated in its own branch/PR.

## Preflight

Two preflight scripts run in order:
1. `01-gh-pr-status.py` — monitors CI status on existing `pr_open` tasks and updates them (passed/failed/conflicts)
2. `02-check-bot-prs.py` — finds repos with actionable major-tier bot PRs

The `02-check-bot-prs.py` script validates:
- Agent is not at task capacity
- At least one repo in `project-repos.json` has an open **major-tier** bot PR targeting that repo's default branch and changing a supported dependency manifest — see **Grouping by tier** below. There is no minimum count: a single major-tier PR is enough to act on, since each is handled solo anyway.
- The source bot PR has no failed `on-pull-request` check.
- Go PRs do not move a self-replacement directive to a new major module path; that requires transitive dependency review before automated consolidation.
- No existing consolidation task is already in progress for that repo
- No open PR already exists in the repo with a `chore(deps): consolidate` title or `chore/consolidate-*` branch (checked directly against GitHub — a backstop for when a prior run's `task_add` never landed, since the task store is otherwise the only de-dup signal and originals are kept open via `--keep-originals`)

To classify a PR's ecosystem and tier, the preflight prefers the changed dependency manifest (`go.mod`/`Pipfile`/`package.json`) and the *actual* old/new version parsed directly from its diff over anything stated in the title or body — title-only text like "Update dependency X to vY" states just the target version and can't distinguish tiers by itself. Title and then body-table parsing are only a fallback for when a diff can't be fetched. PRs that change no supported dependency manifest are not candidates, even if their body contains version strings.

The preflight reads repos from `project-repos.json` (in the agent directory) and checks each GitHub repo for open bot PRs. Non-GitHub repos (e.g. GitLab) are skipped. It classifies every PR by ecosystem and bump tier, discards anything that isn't major tier, and emits one solo group per remaining major-tier PR — major PRs are never combined with each other or with anything else. The output contains a `repos` array — each entry has `repo` (owner/repo), `bot_url`, eligible `pr_count`, eligible `prs`, `groups` (ecosystem/tier/count — tier is always `"major"` and count is always `1`), and `task_key`. Process each repo entry by passing `--repo <owner/repo>` to the consolidation script, once per group.

If preflight passes, all prerequisites are met. Do not re-check them.

If preflight reports no groups, output `skip` is expected even when a repo has open bot PRs — that just means none of them are major-tier. Do not start a session for minor-only, patch-only, or unknown-tier-only PRs.

## How to Run

For each repo in the preflight's `repos` array, `cd` into that repo's checkout and run:

```bash
python skills/konflux-pr-squash.py --repo <owner/repo>
```

### Common Options

| Flag | Description |
|------|-------------|
| `--repo owner/repo` | Upstream repo — always pass this explicitly using the `repo` value from the preflight output |
| `--bot "dependabot[bot]"` | Use a different bot author (default: `red-hat-konflux[bot]`) |
| `--dry-run` | Preview what would be consolidated without creating PRs |
| `--close-originals` | Close the original bot PRs after consolidation (default: enabled) |
| `--keep-originals` | Keep original bot PRs open after consolidation |
| `--no-regenerate-locks` | Skip lock file regeneration (`pipenv lock`, `npm install`, `go mod tidy`) |

## What the Script Does

1. Finds all open PRs from the bot author, skipping any with "DO NOT MERGE" or "do-not-merge" labels, or with "abandoned" in the title
2. Groups PRs by ecosystem (Go, Python/Pipfile, npm) using PR diff analysis with title-pattern fallback
3. For each ecosystem group, creates a separate consolidation branch from `main`/`master`
4. Applies each PR's dependency update natively:
   - **Go**: `go get <module>@<version>`, then `go mod tidy` (preserves the original `go` version directive). If the bot PR instead bumps the `go` directive itself (a Go toolchain version bump, not a module dependency), the script updates go.mod's `go` line to the new version *and* syncs the hummingbird builder image tag (`registry.access.redhat.com/hi/go:<version>...`) to match, across any `Dockerfile*` and `.tekton/*.yaml` files in the repo. The version is swapped in place regardless of variant suffix — `-fips-builder`, `-builder`, `-fips`, or no suffix at all — so the build image never drifts out of sync with the toolchain version declared in go.mod
   - **Python**: Updates version in `Pipfile`, then `pipenv lock`
   - **npm**: Updates version in `package.json`, then `npm install`
   - **Unknown**: Falls back to `git apply --3way` patch application
5. Regenerates lock files once per directory (not per PR)
6. Pushes the branch and creates a consolidated PR
7. Optionally closes original bot PRs with a comment linking to the consolidated PR

## Directory Awareness

The script detects which subdirectory each dependency file lives in (e.g., `./Pipfile` vs `./typespec/package.json`) and runs lock commands in the correct directory. Monorepos with multiple package managers are handled natively.

## Major Version Bumps Only

This workflow only consolidates **major-tier** bumps. The script does **not** distinguish major, minor, or patch version bumps itself — it applies and lumps together whatever set of PRs it's given — so the preflight (`02-check-bot-prs.py`) does the classification up front and discards anything that isn't major tier before this agent ever sees it. Minor and patch bumps never appear in the preflight's `groups` output and must never be fed into the consolidation script by this workflow.

**Every PR the preflight hands you should already be major tier — verify this before invoking the script, for every run, regardless of what CI outcome you expect.** A major bump can pass CI while still being wrong (e.g. a deprecated-but-still-compiling API silently misbehaving) — see **Handling a detected major bump** below. If you find a PR in the preflight's output that doesn't actually look major-tier on inspection, treat that as a suspected misclassification (see **Agent Responsibilities** below) rather than proceeding with it.

### Confidence required for solo action

Because a single major-tier PR is now enough to trigger a live consolidation run (no 2+ threshold to absorb a wrong guess — see **Grouping by tier** below), the preflight only treats a PR as actionable "major" when the classification is backed by an explicit signal: a real manifest diff, an explicit old→new version pair (from the title, the PR body's changelog table, or a Go module-path change like `.../v2` → `.../v3`), or an explicit breaking-change marker (`feat!:`, a `breaking-change` label).

One classification path is deliberately excluded from this: a title stating only a target version that happens to start with `0.` (e.g. "Update dependency X to v0.3.0"), with no source version available from a diff or body to confirm the bump actually crossed a 0.x minor boundary. Per 0.x semver convention this is *probably* a breaking change, but it's a guess, not a fact — the preflight tags this `major_unconfirmed` internally and drops it rather than surfacing it in `groups`. If you independently notice a PR like this in a repo's bot PR list, don't treat it as actionable on your own — flag it in the report for manual follow-up instead.

### Detecting the bump tier

The preflight already does this classification (preferring the real old/new version from each PR's manifest diff over title/body prose — see **Preflight** above) and hands you its `groups` output, which should contain only major-tier PRs. The heuristics below are for double-checking a borderline case or handling a PR the preflight couldn't classify from a diff (e.g. a `git apply --3way` fallback case with no manifest diff to read):

Before running the consolidation script, or while reviewing its `[Step 2] Grouping PRs by ecosystem...` output, compare each PR's current vs. target version:

- **Go**: major = the leading version number changing (`v1.x` → `v2.x`) **or** the module path itself changing (e.g. `github.com/foo/bar` → `github.com/foo/bar/v2`, `.../v2` → `.../v3`). Minor = the second segment changing with the leading segment unchanged (`v1.2.x` → `v1.3.x`). Patch = only the third segment changes. The module-path case is easy to miss because `go get module/v2@version` succeeds even though every import of that module in the codebase still points at the old path and needs updating — `go mod tidy` will not catch this, it only fixes the dependency graph.
- **Python**: major = leading segment changes (e.g. `4.x` → `5.x`). Minor = second segment changes, leading segment unchanged (e.g. `3.17.x` → `3.18.x`). Patch = only the third segment changes (e.g. `2.66.1` → `2.66.2`). Watch for packages that don't follow strict semver (year-based versions, 0.x packages where a "minor" bump is treated as breaking by convention) — treat any 0.x → 0.(x+1) bump as a possible major bump, not minor.
- **npm**: same segment logic as Python for major/minor/patch. Also check for range operators in the original `package.json` entry (`^1.2.3`) — if the *new pinned version* violates the existing caret/tilde range, treat the update as at least one tier more severe than the raw numbers indicate, since the maintainer's own range already assumed that boundary wouldn't be crossed.

Extract the "current" version from the manifest in the checked-out repo (`go.mod`, `Pipfile`, `package.json`) before applying the PR, not from the PR title alone — titles are sometimes imprecise about the starting version.

Also treat a PR as major (regardless of version numbers) if it carries an explicit breaking-change signal: a `!` after the type/scope in a conventional-commit-style title (e.g. `feat!:`, `fix(deps)!:`), or a label containing "breaking" (e.g. `breaking-change`). Some bots flag breaking changes this way even for what looks like a minor/patch version bump — the explicit signal always overrides the version-number heuristic.

### Grouping by tier

The preflight (`02-check-bot-prs.py`) does this classification and grouping for you — its `groups` output per repo already reflects the policy below. Trust its grouping rather than re-deriving it, but understand the logic so you can sanity-check the output and handle each batch correctly:

- **Major PRs**: each major-tier PR gets its own solo group and its own branch/PR — **never** grouped with another major-tier PR, even for the same ecosystem, and even if that means running the script twice (or more) for the same ecosystem in one cycle. There is no minimum-batch-size concept for major: a group of one is the expected, normal shape, not a special case to skip. The script mechanically applies the version bump and regenerates lock files for that single PR; what makes major different is that the agent must follow up with the code-change investigation in **Handling a detected major bump** below before treating it as done — the script only bumps the manifest/lock, it does not know whether the codebase calls any of the APIs that changed. See **Single-PR ecosystem groups** in Failure Handling for the operational risk of running the script on a 1-PR batch (e.g. a missing package-manager binary) — that's a script-crash concern, not a reason to skip.

- **Minor/patch PRs**: out of scope for this workflow entirely. The preflight discards them before grouping — they never appear in its `groups` output and this workflow takes no action on them (does not consolidate, does not close, does not comment).

### Handling a detected major bump

1. **Run the consolidation script for the major-bump PR on its own branch, by itself** — never combined with any other PR, major or otherwise. The script applies the manifest/lock bump; it does not know whether the codebase uses anything that changed.
2. **Research the breaking changes before or immediately after applying:**
   - Read the bot's own PR body first — `gh pr view <number> --repo <owner/repo> --json body -q .body`. Konflux/mintmaker-style bots frequently embed release notes or a changelog excerpt directly in the PR description; check for a "Breaking Changes" / "BREAKING CHANGE" section.
   - Check for GitHub releases between the two versions: `gh api repos/<owner>/<repo>/releases` (substitute the *dependency's* repo, not the consuming repo) and scan release bodies for breaking-change notes.
   - Use registry CLIs (not raw web fetch — unavailable in this environment) to confirm what versions exist in between and sanity-check the jump: `npm view <pkg> versions`, `pip index versions <pkg>`, `go list -m -versions <module>`.
3. **Investigate whether the codebase actually needs code changes as a result of the bump — do not rely on CI alone to surface this.** A major bump can leave code that still compiles/passes tests while relying on now-deprecated or subtly-changed behavior, so treat this as active investigation, not a wait-and-see:
   - For each breaking change or removed/renamed API called out in step 2's research, `grep -rn` the codebase for usages of that symbol, method, config key, or import path.
   - For a Go module path bump (`/v2`, `/v3`, ...), grep the repo for the old import path (`grep -rl "old/module/path"`) and update every import to the new path as part of the same change — this is mandatory, not optional, or the build will silently keep using the old major version.
   - For Python/npm, check for usages of any function/class/parameter the release notes list as removed, renamed, or behavior-changed (e.g. a default value flip, a signature change, a removed argument) and update call sites accordingly.
   - If research turned up no explicit breaking-change list (changelog silent or unavailable), still skim the diff between the old and new major version's changelog/release notes for the words "removed", "renamed", "deprecated", "default", "behavior" as a fallback signal, and note in the PR body that this fallback skim was done.
   - Apply whatever code changes are needed directly in the major-bump branch, alongside the dependency bump, before pushing — do not leave them as a follow-up.
   - If the required code changes are large, ambiguous, or you're not confident they're complete, say so explicitly in the PR body rather than guessing — this is exactly the case the human-review gate in step 5 exists for.
4. **Create a separate consolidated PR for major bumps** (even if it's just one PR) with its title/body clearly marked, e.g. `chore(deps)!: <package> major version bump to vX`, and include in the PR body:
   - A "⚠️ Breaking Changes" section summarizing what you found in step 2 (or explicitly stating "no breaking changes found in release notes" if research turned up nothing)
   - A "Code changes made" section listing what you changed in step 3 as a result of the bump (or explicitly stating "no code changes were required" if the investigation found none)
   - A checklist item for a human reviewer to confirm behavior, not just that CI is green
5. **Never auto-close the original bot PR for a major bump on green CI alone.** Passing CI does not prove the absence of breaking changes (e.g. behavioral changes not covered by tests, deprecated-but-still-compiling APIs) — this is precisely why step 3's proactive investigation matters even when CI is green. Leave the original open and flag it in the report for human sign-off; only close it once a human has explicitly approved the major-bump PR **and** it has been merged (see **CI result handling** below).

## Conflict Resolution

When the script skips a PR due to a conflict or apply failure, **do not accept the skip**. Instead, attempt to resolve the conflict manually before moving on:

1. **Identify the failed PR(s)** from the script output (look for "Warning: ... skipping" messages)
2. **For each skipped PR**, try the following resolution steps in order:
   a. **Fetch and merge the PR branch**:
      ```bash
      git fetch origin <pr_branch>
      git merge --no-commit FETCH_HEAD
      ```
   b. **If merge conflicts occur**, resolve them:
      - **Lock files** (`go.sum`, `Pipfile.lock`, `package-lock.json`, `yarn.lock`): Accept ours with `git checkout --ours <file>` — they get regenerated anyway
      - **Manifest files** (`go.mod`, `Pipfile`, `package.json`): Accept theirs with `git checkout --theirs <file>` — the bot's version bump is what we want
      - **Other files**: Accept theirs with `git checkout --theirs <file>` — bot PRs are single-purpose dep bumps
      - Stage all resolved files: `git add <resolved_files>`
   b1. **If the PR branch touches only infra/pipeline paths** (e.g. `.tekton/`, `.github/`, CI config) rather than manifest/lock files, prefer a targeted checkout of just those paths over a full merge:
      ```bash
      git fetch origin <pr_branch>
      git checkout FETCH_HEAD -- <touched_path>/
      git add <touched_path>/
      ```
      A full `git merge`/cherry-pick against a heavily diverged bot branch can pull in large amounts of unrelated churn (e.g. renovated lockfiles, unrelated manifest edits) — if `git diff --cached --stat` after a merge attempt shows changes far outside the PR's actual diff (`gh pr diff <pr_number> --name-only`), abort (`git merge --abort`) and use the targeted checkout instead.
   c. **If the merge still fails**, try cherry-picking individual commits from the PR branch:
      ```bash
      git merge --abort
      git cherry-pick --no-commit <commit_sha>
      ```
      Resolve conflicts the same way as above.
   d. **If all else fails**, apply the dependency change manually:
      - Read the PR diff to identify the package name and target version
      - Edit the manifest file directly to bump the version
      - Stage the change
3. **After resolving all skipped PRs**, regenerate lock files for the affected ecosystem:
   - Go: `go mod tidy`
   - Python: `pipenv lock`
   - npm: `npm install`
4. **Amend the consolidation commit** to include the newly resolved changes:
   ```bash
   git add -A
   git commit --amend --no-edit
   ```
5. If a PR truly cannot be resolved (e.g., the dependency is incompatible or removed), note it in the PR description as a skipped item with the reason.

### When to re-run vs. manually fix

- If the script skips **1-2 PRs**: resolve them manually as described above
- If the script skips **most PRs**: investigate root cause (stale main branch, network issues) and re-run after fixing
- If conflicts are between two bot PRs updating the same package to different versions: keep the higher version

## Failure Handling

### Lock file regeneration failures
- **npm**: If `npm install` fails, retry with `--legacy-peer-deps`. If that also fails, check the error for version constraint conflicts between the consolidated dependencies — you may need to drop the lower version.
- **pipenv**: If `pipenv lock` fails, check for Python version constraints or conflicting package versions in the error output. Try removing `Pipfile.lock` and re-running `pipenv lock` from scratch.
  - **Before doing anything else, check whether the conflict is pre-existing**: run the same `pipenv lock` attempt against `origin/master`'s `Pipfile`/`Pipfile.lock` (e.g. in a scratch worktree or after `git stash`). If it fails the same way there, the conflict is unrelated to this consolidation and cannot be fixed by relocking.
  - In that case, do **not** hand-reconstruct `Pipfile.lock` entries from other PRs' diffs or by querying PyPI — this is fragile (easy to get hashes/transitive deps subtly wrong) and time-consuming. Instead: leave `Pipfile.lock` unregenerated, note in the consolidated PR body that the lock file needs manual regeneration due to a pre-existing constraint conflict (name the conflicting packages), and proceed with just the `Pipfile` manifest changes.
  - **Do not run `pipenv upgrade <package>` as a recovery step.** It rewrites the `Pipfile` itself, not just the lock — observed behavior is that it can replace a pinned version with a wildcard (`"*"`) and append the entry as a new line at the bottom instead of updating it in place, silently corrupting the manifest and requiring manual line-by-line repair. If you need to bump a version in `Pipfile`, edit the existing line directly; never let a package manager's own "fix it for me" command touch the manifest.
  - **Always diff `Pipfile` against `origin/master` after any lock-recovery attempt** (`git diff origin/master -- Pipfile`) before amending — confirm every changed package shows its intended pinned version in its original position, with no new wildcard or duplicate entries introduced.
- **go mod tidy**: If it fails, check for incompatible module versions. Try `go mod tidy -e` to proceed past errors, then inspect `go.mod` for issues.

### Branch and PR cleanup
- If the consolidated PR fails CI or cannot be created, **delete the remote branch**:
  ```bash
  git push origin --delete <branch_name>
  ```
- If the local branch is no longer needed, clean it up:
  ```bash
  git checkout main
  git branch -D <branch_name>
  ```

### Commit signing
- If `git push` fails with a signing error, the repo may require signed commits. Check with `git config commit.gpgsign`. If signing is required, ensure GPG is configured before retrying.

### No outbound web access
- `WebFetch` and similar internet-lookup tools are not available in this execution environment — do not use them to check package versions or changelogs. Use `gh pr diff`/`gh api` against the source bot PRs, or `pip index versions <package>` / `npm view <package> versions` (registry CLIs, not raw HTTP fetch) instead.

### `gh pr create` fails with "can't find git"
- If `gh pr create` errors that it can't find a git repository (seen intermittently in this environment even when run from inside a valid checkout), fall back to creating the PR via the API directly:
  ```bash
  gh api repos/<owner>/<repo>/pulls -X POST \
    -f title="<title>" \
    -f head="<branch_name>" \
    -f base="<default_branch>" \
    -f body="<body>"
  ```
  This bypasses `gh`'s local git-context detection entirely.

### Single-PR ecosystem groups
- Every major-tier group is a single PR by design (see **Grouping by tier** above) — this is expected, not a signal to skip. But the consolidation script still creates a branch and attempts the lock/tidy step for that one PR the same as it would for a larger batch, which can crash (e.g. if the relevant package manager binary, such as `npm`, isn't installed in this environment) and leave an orphaned local branch. If it crashes, verify the branch was actually pushed (`git ls-remote --heads origin <branch>`) before attempting `git push origin --delete` — deleting a never-pushed branch is a harmless no-op.

### Other failures
- If no PRs can be applied for an ecosystem, that ecosystem's branch is cleaned up
- If no consolidated PRs are created at all, the workflow exits with an error

## Agent Responsibilities

**CRITICAL — stop after creating the task. Do NOT close original PRs. Do NOT set task status to `done`. The `gh_pr_status.py` preflight handles CI monitoring, original PR closure, and task completion on subsequent cycles — not this cycle.**

When running this workflow:

1. For each repo in the preflight output, `cd` into the target repository (clone it first if needed using the `bot_url` from the preflight data). The preflight's `groups` array already classifies PRs to major tier only, one PR per group, per **Grouping by tier** above — trust it rather than re-deriving groups yourself. If a PR in the preflight's output doesn't actually look major-tier on inspection, flag a suspected misclassification in the report instead of proceeding with it — never consolidate a minor/patch PR through this workflow. Never combine two of the preflight's groups into one script invocation, even if they're both major-tier PRs for the same ecosystem — each major PR must get its own separate script run, branch, and PR.
2. Run the script once per group reported by the preflight with `--repo <owner/repo>` — every invocation targets exactly one major-tier PR and produces its own branch/PR. Every invocation additionally requires the code-change investigation in **Handling a detected major bump** below, applied directly in that PR's branch before pushing — the script itself only bumps the manifest/lock, it never updates call sites, so a major PR that skips this step ships as a bare package bump even when the update requires code changes.
3. Never use `--close-originals`. The script defaults to keeping originals open. Original PRs are only closed on a later cycle **after the consolidated PR is merged** via task tracking — CI passing is never sufficient on its own.
4. Run with `--dry-run` first if the user wants to preview
5. After the script completes, **check for any skipped PRs**. If the PR was skipped due to a conflict or apply failure, follow the **Conflict Resolution** steps above to resolve it before pushing.
6. **Verify that the actual code change matches the bot PR title**. For the resulting PR, confirm the dependency name and version in the diff correspond to what the original bot PR title described. Flag any mismatch. The script's own "Applied successfully" message is not sufficient proof — it only confirms the file content changed, not that it changed to the *correct* version. Re-check the manifest (`Pipfile`/`package.json`/`go.mod`) against the source PR's intended version. A PR whose version doesn't match must be treated as unresolved — do not let it be closed as if it were successfully merged.
7. For each major bump, follow **Handling a detected major bump** above to research breaking changes and produce its own separate PR.
8. **Create a memory server task** with `status="pr_open"` for each PR produced (see Task Tracking below). This hands CI monitoring to `gh_pr_status.py` — do not poll `gh pr checks` in-session.
9. **STOP.** The cycle ends here. Do not close originals, do not set task to `done`. The next cycle's preflight detects CI results and triggers follow-up.
10. Report:
   - How many major-tier PRs were processed, and for which ecosystems
   - Any PR that required manual conflict resolution (and what was done)
   - The URL(s) of the created PR(s)
   - A summary of the breaking-change research for each major version bump, and which PR it landed in
   - Any PRs that could not be resolved despite best efforts, and why
11. Do not modify the script itself — it handles all consolidation logic internally

## Task Tracking

This workflow uses the memory server task system. The preflight script checks tasks before starting — do not duplicate these checks.

### Creating a task after PR creation

After pushing the PR for a major bump, call the `task_add` MCP tool (from `bot-memory`) so `gh_pr_status.py` monitors CI automatically:

```
task_add(
    external_key="konflux-pr-squash:<org/repo>:<ecosystem>:major:<original_pr_number>",
    repo="<org/repo>",
    branch="<branch_name>",
    status="pr_open",
    source_type="github",
    title="Major bump: <package> to <target_version> (<ecosystem>)",
    metadata={
        "prs": [{"repo": "<org/repo>", "number": <pr_number>, "host": "github"}],
        "original_prs": [<original bot PR number>],
        "ecosystem": "<go|python|npm>",
        "is_major_bump": true
    }
)
```

Every task from this workflow is a major-version bump, so `is_major_bump` is always `true` — it gates the extra human-review step before merge is even sought (see **CI result handling** below).

The `external_key` must include the **original bot PR number**, not just the repo and ecosystem — since major bumps are never batched, a repo can have several independent major-tier PRs open for the same ecosystem at once (e.g. two unrelated Python packages each needing a major bump), and each gets processed and tracked as its own task in the same cycle. A key scoped only to `<org/repo>:<ecosystem>` would collide the moment a second major PR for that ecosystem is processed. `task_add` fails if 10+ active tasks already exist for this instance — the preflight's capacity check should have already ruled this out.

### Why this matters

- **No in-session CI polling.** The built-in `gh_pr_status.py` preflight monitors `pr_open` tasks for free — no AI tokens spent waiting for CI.
- **Duplicate prevention.** The preflight skips a repo if a task with its key is already `in_progress`, `pr_open`, or `pr_changes`.
- **Capacity management.** The preflight respects the task capacity cap (default 10) to avoid overloading the agent.

### CI result handling (happens on a LATER cycle, not the creation cycle)

`gh_pr_status.py` monitors `pr_open` tasks automatically. When it detects CI results, it wakes the agent on a subsequent cycle. **CI passing is never sufficient by itself to close the original bot PRs. The originals are only closed once the consolidated PR is actually merged.** A green consolidated PR can still sit un-merged for days (awaiting a human reviewer, a merge freeze, etc.), and closing the originals early would strand the repo with no working fallback if the consolidated PR is later abandoned or force-pushed over.

- **CI passes** → the agent should **not** move straight to awaiting-merge. Green CI does not confirm the absence of breaking changes for a major bump (see **Major Version Bumps Only** above). Instead, on the *first* wake after CI passes:
  - Post a comment on the consolidated PR summarizing the breaking-change research already done, tagging it as ready for human review
  - Update the task status to `pr_changes` with a `metadata.awaiting_human_review: true` marker — this distinguishes "waiting on a human sign-off before merge" from "waiting on merge alone" so it's identifiable on later wakes, though it still counts against capacity like any other active task (see caveat below)
- **On a later wake for a task with `awaiting_human_review: true`**, check merge status instead of re-running consolidation logic: `gh pr view <consolidated_pr_number> --repo <owner/repo> --json state,mergedAt`
  - If merged → close the original bot PR(s) with a comment linking to the merged consolidated PR, set task status to `done`
  - If closed without merging (a human rejected it) → delete the remote branch if it still exists, set task status to reflect rejection (e.g. `failed`), and leave the original bot PR(s) open so the change can be revisited later
  - If still open → leave everything as-is, do nothing further this cycle
  - **Capacity caveat**: a task sitting in `pr_changes` awaiting merge or human review counts toward the capacity cap and blocks new consolidation runs for that same repo/ecosystem (its `external_key` stays "active") for as long as it's pending. This is intentional — the workflow should not run further consolidations against a repo with an unmerged consolidated PR — but if a task ever seems stuck for an unreasonable time, surface it in the report rather than silently absorbing a permanent capacity slot.
- **CI fails** → the agent should:
  - Investigate and fix the failure (rebase, resolve conflicts, re-push)
  - Do **not** close original bot PRs — leave them open as fallbacks
  - If unfixable, delete the remote branch and update the task status to reflect the failure

### Multiple major bumps in one cycle

If the workflow produces multiple PRs in the same cycle — whether across different ecosystems or multiple independent major bumps within the same ecosystem — create a separate task for each with a distinct external key, e.g.:
- `konflux-pr-squash:<org/repo>:go:major:1234`
- `konflux-pr-squash:<org/repo>:python:major:5678`
- `konflux-pr-squash:<org/repo>:python:major:5679` (a second, unrelated major Python bump in the same cycle)
