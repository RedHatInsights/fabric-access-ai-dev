# Bot Dependency Consolidation Workflow

## Purpose

Consolidate multiple dependency update PRs from bot authors (e.g., `red-hat-konflux[bot]`, `dependabot[bot]`) into a single PR per ecosystem for easier review and reduced CI load.

## Preflight

Two preflight scripts run in order:
1. `01-gh-pr-status.py` — monitors CI status on existing `pr_open` tasks and updates them (passed/failed/conflicts)
2. `02-check-bot-prs.py` — finds repos with consolidatable bot PRs

The `02-check-bot-prs.py` script validates:
- Agent is not at task capacity
- At least one repo in `project-repos.json` has 2+ open bot PRs to consolidate
- No existing consolidation task is already in progress for that repo
- No open PR already exists in the repo with a `chore(deps): consolidate` title or `chore/consolidate-*` branch (checked directly against GitHub — a backstop for when a prior run's `task_add` never landed, since the task store is otherwise the only de-dup signal and originals are kept open via `--keep-originals`)

The preflight reads repos from `project-repos.json` (in the agent directory) and checks each GitHub repo for open bot PRs. Non-GitHub repos (e.g. GitLab) are skipped. The output contains a `repos` array — each entry has `repo` (owner/repo), `bot_url`, `pr_count`, `prs`, and `task_key`. Process each repo entry by passing `--repo <owner/repo>` to the consolidation script.

If preflight passes, all prerequisites are met. Do not re-check them.

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
   - **Go**: `go get <module>@<version>`, then `go mod tidy` (preserves the original `go` version directive)
   - **Python**: Updates version in `Pipfile`, then `pipenv lock`
   - **npm**: Updates version in `package.json`, then `npm install`
   - **Unknown**: Falls back to `git apply --3way` patch application
5. Regenerates lock files once per directory (not per PR)
6. Pushes the branch and creates a consolidated PR
7. Optionally closes original bot PRs with a comment linking to the consolidated PR

## Directory Awareness

The script detects which subdirectory each dependency file lives in (e.g., `./Pipfile` vs `./typespec/package.json`) and runs lock commands in the correct directory. Monorepos with multiple package managers are handled natively.

## Major and Minor Version Bumps

The script does **not** distinguish major, minor, or patch version bumps — it applies and lumps them all into the same ecosystem consolidation. This isn't just a major-bump problem: a minor bump can also carry behavior changes that break CI (e.g. a library changing validation error paths or default behavior in a way that requires test-assertion updates). Bundling that minor bump with unrelated patch-level bumps in one PR means a single failure blocks the whole batch and a reviewer can't tell which dependency caused it. The agent must classify every PR into **major / minor / patch** and route each tier into its own consolidation batch — never mix tiers in one branch/PR.

**This classification and split is mandatory and unconditional — do it before invoking the script at all, for every run, regardless of what CI outcome you expect.** It is not a fallback for when CI is failing, and it is not something to skip because "the changes look small" or "CI will probably pass anyway." A minor or even patch-level bump can pass CI while still being wrong (e.g. a silently-changed default that no test covers) — see **Handling a detected minor bump** and **Handling a detected major bump** below. Green CI is a reason to eventually close an original PR, never a reason to skip splitting by tier in the first place.

### Detecting the bump tier

Before running the consolidation script, or while reviewing its `[Step 2] Grouping PRs by ecosystem...` output, compare each PR's current vs. target version:

- **Go**: major = the leading version number changing (`v1.x` → `v2.x`) **or** the module path itself changing (e.g. `github.com/foo/bar` → `github.com/foo/bar/v2`, `.../v2` → `.../v3`). Minor = the second segment changing with the leading segment unchanged (`v1.2.x` → `v1.3.x`). Patch = only the third segment changes. The module-path case is easy to miss because `go get module/v2@version` succeeds even though every import of that module in the codebase still points at the old path and needs updating — `go mod tidy` will not catch this, it only fixes the dependency graph.
- **Python**: major = leading segment changes (e.g. `4.x` → `5.x`). Minor = second segment changes, leading segment unchanged (e.g. `3.17.x` → `3.18.x`). Patch = only the third segment changes (e.g. `2.66.1` → `2.66.2`). Watch for packages that don't follow strict semver (year-based versions, 0.x packages where a "minor" bump is treated as breaking by convention) — treat any 0.x → 0.(x+1) bump as a possible major bump, not minor.
- **npm**: same segment logic as Python for major/minor/patch. Also check for range operators in the original `package.json` entry (`^1.2.3`) — if the *new pinned version* violates the existing caret/tilde range, treat the update as at least one tier more severe than the raw numbers indicate, since the maintainer's own range already assumed that boundary wouldn't be crossed.

Extract the "current" version from the manifest in the checked-out repo (`go.mod`, `Pipfile`, `package.json`) before applying the PR, not from the PR title alone — titles are sometimes imprecise about the starting version.

Also treat a PR as major (regardless of version numbers) if it carries an explicit breaking-change signal: a `!` after the type/scope in a conventional-commit-style title (e.g. `feat!:`, `fix(deps)!:`), or a label containing "breaking" (e.g. `breaking-change`). Some bots flag breaking changes this way even for what looks like a minor/patch version bump — the explicit signal always overrides the version-number heuristic.

### Grouping by tier

For each ecosystem, split candidate PRs into up to three sub-groups — major, minor, patch — and run the consolidation script (or manual handling) once per non-empty sub-group, never combining tiers in one invocation:

- **Patch batch**: consolidate normally via the script, on its own branch.
- **Minor batch**: consolidate via the script too (minor bumps are still low-risk enough to automate), but as its **own** branch/PR, separate from the patch batch. This isolates a minor bump's CI failure so it doesn't block unrelated patch updates, and gives a reviewer a single suspect if something breaks.
- **Major batch**: also run through the script, on its own branch/PR, separate from minor/patch — the script can mechanically apply the version bump and regenerate lock files the same as any other tier. What makes major different is not "don't automate the bump," it's that the agent must follow up with the code-change investigation in **Handling a detected major bump** below before treating the batch as done; the script only bumps the manifest/lock, it does not know whether the codebase calls any of the APIs that changed.

If a tier ends up with only one PR for an ecosystem, the **Single-PR ecosystem groups** rule still applies — skip the script for that tier and note it in the report rather than letting it crash on a 1-PR group.

### Handling a detected minor bump

Minor bumps get their own consolidated PR, separate from patch bumps, but the treatment is lighter than a major bump:

1. **Run the consolidation script for the minor batch on its own branch**, e.g. `chore/consolidate-python-deps-minor-<date>`, distinct from the patch batch's branch.
2. **Skim for breaking-change signals** (same sources as step 2 of the major-bump flow — PR body, upstream release notes) but this is a lighter pass, not mandatory deep research. If you find explicit breaking-change language despite the version being "minor," re-classify the PR as major and route it through the major-bump flow instead.
3. **Proactively check for and apply any code changes the bump requires — do not wait for CI to surface them.** Even at a light-pass level: for anything the step-2 skim flagged as changed (a new default, a deprecated method, a changed signature), `grep -rn` the codebase for usages and update call sites in the same branch, the same way major bumps do in step 3 of **Handling a detected major bump**. The initial `go get`/`pipenv`/`npm install` bump the script performs is a manifest/lock update only — it never touches call sites, so relying on it alone leaves any required code change out of the PR entirely.
4. **Additionally fix CI failures caused by the minor bump directly** (e.g. update test assertions to match new library behavior) rather than treating them as blockers requiring human sign-off — this is expected maintenance for a minor bump, unlike a major bump where a passing-but-silently-wrong test is the concern. This is on top of step 3's proactive check, not a replacement for it — CI is not guaranteed to catch a silently-changed default.
5. **Note any code changes made (or explicitly state none were required) in the consolidated PR body**, the same way major-bump PRs document a "Code changes made" section.
6. **Green CI is not sufficient to close the original bot PR** — same rule as every other tier now (see **CI result handling** below): originals are only closed once the consolidated PR is actually merged, not merely once CI passes.

### Handling a detected major bump

1. **Do not include it in the same script invocation as minor/patch bumps for that ecosystem.** Run the consolidation script for the major-bump PR(s) on their own branch, separate from the minor and patch batches, the same way you would for minor — the script applies the manifest/lock bump; it does not know whether the codebase uses anything that changed.
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
- The consolidation script does **not** internally skip ecosystems with only 1 PR — it will still create a branch and attempt the lock/tidy step, which can crash (e.g. if the relevant package manager binary, such as `npm`, isn't installed in this environment) and leave an orphaned local branch. Before invoking the script, check the preflight's per-ecosystem PR counts; if you can determine a given ecosystem has only 1 PR ahead of time, skip invoking consolidation for it entirely rather than letting the script attempt and fail. If it does crash, verify the branch was actually pushed (`git ls-remote --heads origin <branch>`) before attempting `git push origin --delete` — deleting a never-pushed branch is a harmless no-op but indicates the check was skipped.

### Other failures
- If no PRs can be applied for an ecosystem, that ecosystem's branch is cleaned up
- If no consolidated PRs are created at all, the workflow exits with an error

## Agent Responsibilities

**CRITICAL — stop after creating the task. Do NOT close original PRs. Do NOT set task status to `done`. The `gh_pr_status.py` preflight handles CI monitoring, original PR closure, and task completion on subsequent cycles — not this cycle.**

When running this workflow:

1. For each repo in the preflight output, `cd` into the target repository (clone it first if needed using the `bot_url` from the preflight data). Before running the script, classify each PR's current-vs-target version into major/minor/patch per the **Major and Minor Version Bumps** section above and split them into separate tier groups — do not let major, minor, and patch PRs for the same ecosystem enter the same script invocation. **Do this on every run, unconditionally** — never skip classification/splitting because CI looks like it will pass, is currently green, or the bumps "look safe." Tier splitting and CI status are unrelated: splitting always happens up front; CI status only ever affects what happens *after* a tier's PR is created (see **CI result handling**).
2. Run the script once per tier group with `--repo <owner/repo>` — once for the patch batch, once for the minor batch, and once for the major batch, each on its own branch/PR. The major and minor batches additionally require the code-change investigation in **Handling a detected major bump** / **Handling a detected minor bump** below, applied directly in that batch's branch before pushing — the script itself only bumps the manifest/lock, it never updates call sites, so a major or minor consolidation PR that skips this step ships as a bare package bump even when the update requires code changes.
3. Never use `--close-originals`. The script defaults to keeping originals open. Original PRs are only closed on a later cycle **after the consolidated PR is merged** via task tracking — CI passing is never sufficient on its own, for any tier.
4. Run with `--dry-run` first if the user wants to preview
5. After the script completes, **check for any skipped PRs**. If any PRs were skipped due to conflicts or apply failures, follow the **Conflict Resolution** steps above to resolve them before pushing.
6. **Verify that the actual code changes match the bot PR titles**. For each consolidated PR, confirm the dependency name and version in the diff correspond to what the original bot PR title described. Flag any mismatches. The script's own "Applied successfully" message is not sufficient proof — it only confirms the file content changed, not that it changed to the *correct* version. Re-check the manifest (`Pipfile`/`package.json`/`go.mod`) against each source PR's intended version before trusting the count of consolidated PRs. Any PR whose version doesn't match must be treated as unresolved, not consolidated — do not let it be closed as if it were successfully merged.
7. If a tier group contains only 1 PR after grouping, **skip that group** — there is nothing to consolidate. Mention it in the report.
8. For each major bump set aside in step 1, follow **Major and Minor Version Bumps** above to research breaking changes and produce its own separate PR. For each minor bump, follow the lighter **Handling a detected minor bump** flow above.
9. **Create a memory server task** with `status="pr_open"` for each consolidated PR — patch, minor, and major alike (see Task Tracking below). This hands CI monitoring to `gh_pr_status.py` — do not poll `gh pr checks` in-session.
10. **STOP.** The cycle ends here. Do not close originals, do not set task to `done`. The next cycle's preflight detects CI results and triggers follow-up.
11. Report:
   - How many PRs were consolidated per ecosystem
   - How many PRs required manual conflict resolution (and what was done)
   - The URL(s) of the created PR(s)
   - Any major version bumps detected, which PR they landed in, and a summary of the breaking-change research
   - Any PRs that could not be resolved despite best efforts, and why
   - Any single-PR ecosystem groups that were skipped
12. Do not modify the script itself — it handles all consolidation logic internally

## Task Tracking

This workflow uses the memory server task system. The preflight script checks tasks before starting — do not duplicate these checks.

### Creating a task after PR creation

After pushing the consolidated PR, call the `task_add` MCP tool (from `bot-memory`) so `gh_pr_status.py` monitors CI automatically:

```
task_add(
    external_key="konflux-pr-squash:<org/repo>",
    repo="<org/repo>",
    branch="<consolidation_branch_name>",
    status="pr_open",
    source_type="github",
    title="Consolidate <N> <ecosystem> dependency updates",
    metadata={
        "prs": [{"repo": "<org/repo>", "number": <pr_number>, "host": "github"}],
        "original_prs": [<list of original bot PR numbers>],
        "ecosystem": "<go|python|npm>",
        "is_major_bump": <true|false>,
        "is_minor_bump": <true|false>
    }
)
```

Set `is_major_bump: true` for any task created from a major-version-bump PR, and `is_minor_bump: true` for a minor-version-bump PR (see **Major and Minor Version Bumps** above). Neither flag changes *whether* the originals get closed — that always waits for the consolidated PR to be merged (see **CI result handling** below) — but `is_major_bump` still gates the extra human-review step before merge is even sought.

The `external_key` must be `konflux-pr-squash:<org/repo>` — this is what the preflight checks to avoid duplicate consolidation runs. `task_add` fails if 10+ active tasks already exist for this instance — the preflight's capacity check should have already ruled this out.

### Why this matters

- **No in-session CI polling.** The built-in `gh_pr_status.py` preflight monitors `pr_open` tasks for free — no AI tokens spent waiting for CI.
- **Duplicate prevention.** The preflight skips a repo if a task with its key is already `in_progress`, `pr_open`, or `pr_changes`.
- **Capacity management.** The preflight respects the task capacity cap (default 10) to avoid overloading the agent.

### CI result handling (happens on a LATER cycle, not the creation cycle)

`gh_pr_status.py` monitors `pr_open` tasks automatically. When it detects CI results, it wakes the agent on a subsequent cycle. **Across every tier — patch, minor, and major — CI passing is never sufficient by itself to close the original bot PRs. The originals are only closed once the consolidated PR is actually merged.** This matters even for low-risk patch/minor batches: a green consolidated PR can still sit un-merged for days (awaiting a human reviewer, a merge freeze, etc.), and closing the originals early would strand the repo with no working fallback if the consolidated PR is later abandoned or force-pushed over.

- **CI passes**, task's `is_major_bump` is not `true` (patch or minor tier) → the agent should:
  - Leave the original bot PRs open
  - Update the task status to `pr_changes` (not `pr_open`) with a `metadata.awaiting_merge: true` marker, so subsequent wakes know CI already passed and this task just needs merge-status polling, not re-triage
- **CI passes**, task's `is_major_bump` is `true` → the agent should **not** move straight to awaiting-merge. Green CI does not confirm the absence of breaking changes for a major bump (see **Major and Minor Version Bumps** above). Instead, on the *first* wake after CI passes:
  - Post a comment on the consolidated PR summarizing the breaking-change research already done, tagging it as ready for human review
  - Update the task status to `pr_changes` with a `metadata.awaiting_human_review: true` marker — this distinguishes "waiting on a human sign-off before merge" from "waiting on merge alone" so it's identifiable on later wakes, though it still counts against capacity like any other active task (see caveat below)
- **On a later wake for a task with `awaiting_merge: true` or `awaiting_human_review: true`**, check merge status instead of re-running consolidation logic: `gh pr view <consolidated_pr_number> --repo <owner/repo> --json state,mergedAt`
  - If merged → close the original bot PR(s) with a comment linking to the merged consolidated PR, set task status to `done`
  - If closed without merging (a human rejected it) → delete the remote branch if it still exists, set task status to reflect rejection (e.g. `failed`), and leave the original bot PR(s) open so the change can be revisited later
  - If still open → leave everything as-is, do nothing further this cycle
  - **Capacity caveat**: a task sitting in `pr_changes` awaiting merge or human review counts toward the capacity cap and blocks new consolidation runs for that same repo/tier (its `external_key` stays "active") for as long as it's pending. This is intentional — the workflow should not run further consolidations against a repo with an unmerged consolidated PR — but if a task ever seems stuck for an unreasonable time, surface it in the report rather than silently absorbing a permanent capacity slot.
- **CI fails** → the agent should:
  - Investigate and fix the failure (rebase, resolve conflicts, re-push)
  - Do **not** close original bot PRs — leave them open as fallbacks
  - If unfixable, delete the remote branch and update the task status to reflect the failure

### Multiple ecosystems and tiers

If the workflow creates multiple consolidated PRs (one per ecosystem, and now potentially one per tier within an ecosystem), create a separate task for each with a distinct external key. Append the tier only when it's not the default patch batch, to avoid colliding keys when a repo has both a patch and a minor consolidation active for the same ecosystem:
- `konflux-pr-squash:<org/repo>:go` (patch tier, or untiered)
- `konflux-pr-squash:<org/repo>:go:minor`
- `konflux-pr-squash:<org/repo>:go:major`
- `konflux-pr-squash:<org/repo>:python`
- `konflux-pr-squash:<org/repo>:python:minor`
- `konflux-pr-squash:<org/repo>:python:major`
- `konflux-pr-squash:<org/repo>:npm`
- `konflux-pr-squash:<org/repo>:npm:minor`
- `konflux-pr-squash:<org/repo>:npm:major`
