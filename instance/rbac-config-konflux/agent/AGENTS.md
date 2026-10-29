# Konflux PR Consolidation Instance — Additional Instructions

OpenCode-native counterpart for instance-level guidance. The legacy
Python/Claude runtime keeps its own instruction assembly; keep behavior
aligned during the rollback window.

## Scope

- This instance consolidates **major-tier** bot dependency PRs
  (`red-hat-konflux[bot]`, `dependabot[bot]`) across the Fabric Access
  repositories listed in `project-repos.json`, one solo PR per major bump.
- Repositories are checked out under `./repos/<name>/` using the `bot_url`
  fork with `upstream` remotes added.

## Environment constraints

- Node.js via nvm and Go via goenv are available; switch versions to match
  each repo (`go.mod`, `.nvmrc`, `package.json` engines) before building or
  regenerating lock files.
- Non-interactive: never ask the user; all cycle inputs come from preflight.
- One consolidation group per script invocation; never batch two major bumps
  together, even for the same ecosystem.

Follow the active workflow's `AGENTS.md` for the full task lifecycle,
breaking-change investigation, conflict resolution, and task tracking rules.
