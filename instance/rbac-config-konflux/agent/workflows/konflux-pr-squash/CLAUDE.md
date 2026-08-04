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

1. Finds all open PRs from the bot author, skipping any with "DO NOT MERGE" or "do-not-merge" labels
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

1. For each repo in the preflight output, `cd` into the target repository (clone it first if needed using the `bot_url` from the preflight data) and run the script with `--repo <owner/repo>`
2. Never use `--close-originals`. The script defaults to keeping originals open. Original PRs are only closed on a later cycle after CI passes via task tracking.
3. Run with `--dry-run` first if the user wants to preview
4. After the script completes, **check for any skipped PRs**. If any PRs were skipped due to conflicts or apply failures, follow the **Conflict Resolution** steps above to resolve them before pushing.
5. **Verify that the actual code changes match the bot PR titles**. For each consolidated PR, confirm the dependency name and version in the diff correspond to what the original bot PR title described. Flag any mismatches. The script's own "Applied successfully" message is not sufficient proof — it only confirms the file content changed, not that it changed to the *correct* version. Re-check the manifest (`Pipfile`/`package.json`/`go.mod`) against each source PR's intended version before trusting the count of consolidated PRs. Any PR whose version doesn't match must be treated as unresolved, not consolidated — do not let it be closed as if it were successfully merged.
6. If an ecosystem group contains only 1 PR after grouping, **skip that group** — there is nothing to consolidate. Mention it in the report.
7. **Create a memory server task** with `status="pr_open"` for each consolidated PR (see Task Tracking below). This hands CI monitoring to `gh_pr_status.py` — do not poll `gh pr checks` in-session.
8. **STOP.** The cycle ends here. Do not close originals, do not set task to `done`. The next cycle's preflight detects CI results and triggers follow-up.
9. Report:
   - How many PRs were consolidated per ecosystem
   - How many PRs required manual conflict resolution (and what was done)
   - The URL(s) of the created PR(s)
   - Any PRs that could not be resolved despite best efforts, and why
   - Any single-PR ecosystem groups that were skipped
10. Do not modify the script itself — it handles all consolidation logic internally
11. If you find a PR or multiple PRs with the label `DO NOT MERGE` make sure these are filtered out or skipped and not included in the final consolidated PR

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
        "ecosystem": "<go|python|npm>"
    }
)
```

The `external_key` must be `konflux-pr-squash:<org/repo>` — this is what the preflight checks to avoid duplicate consolidation runs. `task_add` fails if 10+ active tasks already exist for this instance — the preflight's capacity check should have already ruled this out.

### Why this matters

- **No in-session CI polling.** The built-in `gh_pr_status.py` preflight monitors `pr_open` tasks for free — no AI tokens spent waiting for CI.
- **Duplicate prevention.** The preflight skips a repo if a task with its key is already `in_progress`, `pr_open`, or `pr_changes`.
- **Capacity management.** The preflight respects the task capacity cap (default 10) to avoid overloading the agent.

### CI result handling (happens on a LATER cycle, not the creation cycle)

`gh_pr_status.py` monitors `pr_open` tasks automatically. When it detects CI results, it wakes the agent on a subsequent cycle:

- **CI passes** → the agent should:
  - Close the original bot PRs with a comment linking to the consolidated PR
  - Update the task status to `done`
- **CI fails** → the agent should:
  - Investigate and fix the failure (rebase, resolve conflicts, re-push)
  - Do **not** close original bot PRs — leave them open as fallbacks
  - If unfixable, delete the remote branch and update the task status to reflect the failure

### Multiple ecosystems

If the script creates multiple consolidated PRs (one per ecosystem), create a separate task for each with a distinct external key:
- `konflux-pr-squash:<org/repo>:go`
- `konflux-pr-squash:<org/repo>:python`
- `konflux-pr-squash:<org/repo>:npm`
