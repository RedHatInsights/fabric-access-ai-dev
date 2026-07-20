# Bot Dependency Consolidation Workflow

## Purpose

Consolidate multiple dependency update PRs from bot authors (e.g., `red-hat-konflux[bot]`, `dependabot[bot]`) into a single PR per ecosystem for easier review and reduced CI load.

## Preflight

The preflight script `01-check-bot-prs.py` runs before this workflow and validates:
- `gh` CLI is installed and authenticated
- Current directory is a git repository
- Upstream repo is detectable
- At least 2 open bot PRs exist to consolidate

If preflight passes, all prerequisites are met. Do not re-check them.

## How to Run

Run the consolidation script from the target repository's root:

```bash
python skills/bot-consolidation.py
```

### Common Options

| Flag | Description |
|------|-------------|
| `--repo owner/repo` | Specify upstream repo (auto-detected by default) |
| `--bot "dependabot[bot]"` | Use a different bot author (default: `red-hat-konflux[bot]`) |
| `--dry-run` | Preview what would be consolidated without creating PRs |
| `--close-originals` | Close the original bot PRs after consolidation (off by default — originals are left open) |
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

## WSL Fallback

On Windows, `pipenv lock` and `npm install` prefer WSL when available for consistent dependency resolution. If WSL is unavailable or fails, they fall back to native executables.

## Failure Handling

- PRs that fail to apply are skipped individually; the workflow continues with remaining PRs
- If `pipenv lock` or `npm install` fails, a warning is printed but the commit still proceeds
- If no PRs can be applied for an ecosystem, that ecosystem's branch is cleaned up
- If no consolidated PRs are created at all, the workflow exits with an error

## Agent Responsibilities

When running this workflow:

1. `cd` into the target repository before running the script
2. Run with `--dry-run` first if the user wants to preview
3. After the script completes, **verify that the actual code changes match the bot PR titles**. For each consolidated PR, confirm the dependency name and version in the diff correspond to what the original bot PR title described. Flag any mismatches.
4. Report:
   - How many PRs were consolidated per ecosystem
   - The URL(s) of the created PR(s)
   - Any PRs that were skipped and why
5. If `--close-originals` was used, confirm which original PRs were closed
6. Do not modify the script itself — it handles all consolidation logic internally
