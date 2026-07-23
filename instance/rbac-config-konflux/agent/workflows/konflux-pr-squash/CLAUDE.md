# Bot Dependency Consolidation Workflow

## Purpose

Consolidate multiple dependency update PRs from bot authors (e.g., `red-hat-konflux[bot]`, `dependabot[bot]`) into a single PR per ecosystem for easier review and reduced CI load.

## Preflight

The preflight script `01-check-bot-prs.py` runs before this workflow and validates:
- `gh` CLI is installed and authenticated
- Agent is not at task capacity
- At least one repo in `project-repos.json` has 2+ open bot PRs to consolidate
- No existing consolidation task is already in progress for that repo

The preflight reads repos from `project-repos.json` (in the agent directory) and checks each GitHub repo for open bot PRs. Non-GitHub repos (e.g. GitLab) are skipped. The output contains a `repos` array — each entry has `repo` (owner/repo), `bot_url`, `pr_count`, `prs`, and `task_key`. Process each repo entry by passing `--repo <owner/repo>` to the consolidation script.

If preflight passes, all prerequisites are met. Do not re-check them.

## How to Run

Run the consolidation script from the target repository's root:

```bash
python skills/konflux-pr-squash.py
```

### Common Options

| Flag | Description |
|------|-------------|
| `--repo owner/repo` | Specify upstream repo (auto-detected by default) |
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

### Other failures
- If no PRs can be applied for an ecosystem, that ecosystem's branch is cleaned up
- If no consolidated PRs are created at all, the workflow exits with an error

## Agent Responsibilities

When running this workflow:

1. For each repo in the preflight output, `cd` into the target repository (clone it first if needed using the `bot_url` from the preflight data) and run the script with `--repo <owner/repo>`
2. **Always run with `--keep-originals`**. Original PRs are only closed after CI passes via task tracking. Never use `--close-originals`.
3. Run with `--dry-run` first if the user wants to preview
4. After the script completes, **check for any skipped PRs**. If any PRs were skipped due to conflicts or apply failures, follow the **Conflict Resolution** steps above to resolve them before pushing.
5. **Verify that the actual code changes match the bot PR titles**. For each consolidated PR, confirm the dependency name and version in the diff correspond to what the original bot PR title described. Flag any mismatches.
6. If an ecosystem group contains only 1 PR after grouping, **skip that group** — there is nothing to consolidate. Mention it in the report.
7. **Create a memory server task** for each consolidated PR (see Task Tracking above). This hands CI monitoring to `gh_pr_status.py` — do not poll `gh pr checks` in-session.
8. Report:
   - How many PRs were consolidated per ecosystem
   - How many PRs required manual conflict resolution (and what was done)
   - The URL(s) of the created PR(s)
   - Any PRs that could not be resolved despite best efforts, and why
   - Any single-PR ecosystem groups that were skipped
9. Do not modify the script itself — it handles all consolidation logic internally

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
- **Duplicate prevention.** The preflight skips if a task with this key is already `in_progress` or `pr_open`.
- **Capacity management.** The preflight respects the task capacity cap (default 10) to avoid overloading the agent.

### CI result handling

When `gh_pr_status.py` detects the CI outcome, it updates the task. On the next cycle:

- **CI passes** → task status becomes actionable. The agent should:
  - Close the original bot PRs with a comment linking to the consolidated PR
  - Update the task status to `done`
- **CI fails** → task includes failure details. The agent should:
  - Report the failure and suggest investigation
  - Do **not** close original bot PRs — leave them open as fallbacks
  - Delete the remote branch for the failed consolidated PR
  - Update the task status to reflect the failure

### Multiple ecosystems

If the script creates multiple consolidated PRs (one per ecosystem), create a separate task for each with a distinct external key:
- `konflux-pr-squash:<org/repo>:go`
- `konflux-pr-squash:<org/repo>:python`
- `konflux-pr-squash:<org/repo>:npm`
