# GitHub PR review bot

This directory configures the `rbac-config-review` instance of the Fabric Access
bot. It reviews open GitHub pull requests and publishes a review summary and
anchored inline comments. The workflow is read-only with respect to source code:
it does not edit files, commit, push, approve, request changes, or merge PRs.

## Configuration files

| File | Purpose |
|---|---|
| [`agent/instance.yaml`](agent/instance.yaml) | Selects the scheduled workflow, runtime, provider, and model. |
| [`agent/project-repos.json`](agent/project-repos.json) | Allowlist of repositories whose GitHub PR queues are scanned. |
| [`agent/review-context.yaml`](agent/review-context.yaml) | GitHub label to watch and per-repository context, required skills, and comment format. |
| [`agent/skills/dimensional-review/dimensions.yaml`](agent/skills/dimensional-review/dimensions.yaml) | Review levels, dimensions, applicability rules, and dynamic recipe sources. |
| [`agent/workflows/review-only/`](agent/workflows/review-only/) | Queue, context preparation, verification, and GitHub comment publishing. |

`project-repos.json` controls **where to look for PRs**. `review-context.yaml`
controls **which PRs to select and what context to load for them**. A repository
listed only under `related` is context for a review; it does not become another
PR queue.

## `review-context.yaml`

The file is at `agent/review-context.yaml`. Its top-level `review_label` is the
GitHub label required on an open PR for it to enter the queue. The current value
is `dev-bot-review`. This is distinct from the deployment variable `BOT_LABEL`,
which labels the bot runner and is not used to select PRs.

Each key under `repositories` should match a key in `project-repos.json`. For
example, `insights-rbac` refers to
`https://github.com/project-kessel/insights-rbac.git`.

```yaml
review_label: dev-bot-review
repositories:
  insights-rbac:
    related:
      - id: rbac-config
        url: https://github.com/project-kessel/rbac-config
        relationship: Role and permission configuration consumed by RBAC.
    docs:
      - id: rbac-config-readme
        url: https://github.com/project-kessel/rbac-config/blob/master/README.md
    skills: []
    format: standard
```

The per-repository settings are:

- **`related`** (optional): GitHub repositories whose pinned source may help
  check cross-repository contracts. Each entry needs a unique lowercase `id`,
  an HTTPS GitHub repository `url`, and a short `relationship` description.
  Related repositories are cloned read-only for the review context; they do not
  expand the PR queue.
- **`docs`** (optional): direct GitHub `blob` links to Markdown (`.md`) files.
  Each has a unique `id`. The context builder fetches the configured documents
  at a pinned revision so a review can cite the version it read.
- **`skills`** (optional): dimension names from `dimensions.yaml` that must run
  for this repository. These are always included, even when the dimension has
  `default: false`, is outside the chosen level, or does not match automatic
  language/file applicability. For instance, adding `goose-kiss-review` runs
  that optional review recipe for this repository.
- **`format`** (optional): `standard` (the default) uses the bot's standard
  finding layout. `custom` requires at least one entry in `skills`; every skill
  in that list is also expected to define a usable output layout for the review
  summary and inline comments, and must complete for publication to proceed.
  Use `custom` with skills whose output structure should control the comments.

The loader validates the label, repository mappings, dimension names, unique
IDs, and URL shapes before polling GitHub. Related repository links must be
plain GitHub repository URLs; document links must point directly to a `.md`
file. Keep repository keys synchronized with `project-repos.json` so each PR
gets the intended context and skills; the loader validates the shape of each
mapping but does not currently reject unmatched repository keys.

In the current configuration, `project-repos.json` scans `insights-rbac`,
`rbac-config`, `entitlements-api-go`, `uhc-auth-proxy`, `turnpike`, and
`cloudwatch-aggregator`. Context entries exist for `insights-rbac` and
`rbac-config`; linked documents are configured for `insights-rbac`. The other
allowlisted repositories can still be reviewed, but do not get those extra
context sources unless entries are added for them.

## `dimensions.yaml`

The catalog lives at `agent/skills/dimensional-review/dimensions.yaml`. It
defines the available review passes and how automatic selection works.

### Review levels

`review_levels.default` is `high`. Unless a run explicitly supplies another
level, the bot uses that preset:

| Level | Automatic selection |
|---|---|
| `light` | `correctness` and `security`, when applicable. |
| `medium` | `correctness`, `security`, `performance`, `error-handling`, `test-quality`, `api-contract`, `architecture`, `documentation`, `observability`, `db-schema`, `db-query`, and `cross-repo-dependencies`, when applicable. |
| `high` | Every applicable dimension. This is the current default. |

The workflow may pass `--level <name>` to override the catalog default for a
run. The default can also be changed in `dimensions.yaml`.

### Dimension entries

Each item under `dimensions` describes one review pass. Common fields are:

- **`name`**: stable identifier used in `review-context.yaml` and findings.
- **`default`**: `true` makes it eligible for automatic selection; `false`
  keeps it opt-in unless explicitly focused or required by repository config.
- **`category`** and **`description`**: organize and explain the review area.
- **`languages`**: languages to which the pass applies, or `all`.
- **`file_patterns`** (optional): further restricts automatic selection to
  matching changed paths.
- **`skip_documentation_only`** (optional): suppresses the pass when every
  changed file is documentation. Explicitly required or focused passes still
  run.
- **`source.path`**: local reviewer guide for a built-in dimension. The
  `plugin` and `repo` fields identify its source attribution.
- **`model_preference`** (optional): selects the model worker for a pass.
  `terra` runs in the primary `review-runner` session, and `luna` runs in the
  read-only `review-luna` subagent. A missing preference uses the catalog's
  Terra default. `build_context.py` resolves each selected pass to
  `review_agent` in `manifest.json`; the workflow dispatches passes sequentially
  and records worker failures instead of silently switching models. The
  configured aliases are `luna` → `rehor-openai/gpt-6-luna` and `terra` →
  `rehor-openai/gpt-6.1-terra` in the
  [OpenCode deployment config](https://github.com/RedHatInsights/fabric-access-ai-dev/blob/main/instance/rbac-config-review/opencode-deployment.json).
  Terra is the default for broad or high-judgment review. Luna is used for
  dimensions whose configured preference calls for a focused Luna pass. The
  dimension list in `dimensions.yaml` is the source of those preferences:
  correctness, architecture, security, concurrency, database schema, and
  cross-repository dependencies currently use Terra; code quality,
  documentation, observability, performance, error handling, test quality,
  API contract, database queries, integrations, vulnerability, Go idioms,
  React/PatternFly, and learning extraction use Luna. The opt-in KISS recipe
  has no preference and therefore uses Terra.

`language_extensions` maps changed-file suffixes to language names. Automatic
selection uses changed paths, the chosen level, `default`, languages, file
patterns, and documentation-only rules. Built-in entries currently have
`default: true`; `goose-kiss-review` is a dynamic entry with `default: false`.

The catalog's `config` holds review-wide model routing, deduplication, and
consensus settings. `default_model_preference` controls the worker for
dimensions without an explicit preference, and `model_routing` maps each
preference to an OpenCode agent. `consensus_threshold` is the number of
dimensions that must agree for a consensus finding, and `dedup_jaccard` /
`dedup_line_range` help group findings that describe the same issue. Consensus
affects ranking; the workflow also filters for confidence, verifies proposed
findings, and caps publication at seven actionable findings.
The dimensional-review skill's pre-filter currently requires confidence of at
least `0.7` before findings are handed to verification and evidence checks.

`version` identifies the catalog format and `last_updated` records when the
catalog was last revised. Update those when maintaining the catalog.

### Dynamic recipe dimensions

A dynamic entry can point to a direct GitHub YAML recipe, for example
`goose-kiss-review` points to the KISS review recipe. The bot's own Python
processor reads and validates the recipe, pins its source revision, and builds
a per-run reviewer guide with the same PR and cross-repository context used by
the other passes. It does not invoke the Goose compiler or enable recipe
extensions; recipe suites using `sub_recipes` are not accepted as a single
dimension. Dynamic entries should normally stay `default: false` and can be
enabled for a repository through its `skills` list.

### Selection precedence

1. Start with the configured level (currently `high`) unless a run supplies an
   override.
2. Automatically select default-enabled dimensions included by that level and
   applicable to the changed languages and paths.
3. Skip dimensions marked `skip_documentation_only` when the change contains
   only documentation.
4. Add the repository's `skills` unconditionally. A focused run (`--focus`)
   selects the focused dimensions plus repository skills; the focus list
   replaces ordinary automatic level selection for that run.
5. Remove duplicates and run each selected pass sequentially. A verifier then
   challenges proposed findings before publication.

## How a review runs

```text
project-repos.json + review-context.yaml
                 │
                 ▼
  Find open, labeled PRs (one PR per cycle)
                 │
                 ▼
  Pin PR head/base and build review context
  (diff, related repositories, docs, impact candidates)
                 │
                 ▼
  Select dimensions → review changed code → verify findings
                 │
                 ▼
  Check changed-line anchors and pinned evidence
                 │
                 ▼
  Publish/update GitHub summary and inline comments
```

The queue only considers GitHub repositories in `project-repos.json`, open PRs
with `review_label`, and at most one eligible PR in a cycle. It skips drafts,
the bot's own PRs, and entries without head/base revisions. It tracks work in
the shared task-state service, so interrupted reviews can resume and later
reviews can update the existing summary instead of creating duplicate review
summaries.

Context preparation pins the PR and fetched references. Impact analysis finds
candidate usages of changed identifiers in declared related repositories; a
candidate is context for reviewers, not a finding by itself. Selected
dimensions review the changed code, then `review-verifier` tries to refute each
proposed finding. The evidence gate only publishes findings anchored to the PR
diff and supported by pinned references. Finding IDs (`F1`, `F2`, …) are kept
stable across the summary, inline comments, and later review updates.

The user-facing output is GitHub comments: one review summary and anchored
inline threads for confirmed findings. Review requests can be made with a new
top-level PR comment containing exactly `/review-bot re-review`; the workflow
checks that the requester is an organization member before starting a fresh
pass. It preserves finding IDs and includes the review number in the summary.

## Deployment settings

The instance's workflow, runtime, provider, and model are configured in
`agent/instance.yaml` (`scheduled`, `opencode`, `rehor-openai`,
`gpt-6.1-terra`). App-interface supplies the deployment environment, including
the config path, instance identity, bot label, and execution engine. The
review deployment settings are:

| Variable | Value |
|---|---|
| `BOT_NAME` | `devbot-fabric-access-review` |
| `BOT_INSTANCE_ID` | `fabric-access-review` |
| `BOT_CONFIG_REPO` | `https://github.com/RedHatInsights/fabric-access-ai-dev.git` |
| `BOT_CONFIG_PATH` | `instance/rbac-config-review` |
| `BOT_EXECUTION_ENGINE` | `coordinator` |
| `BOT_LABEL` | `Prokurátor Velký Bratr` |

`BOT_LABEL` is passed to the bot as a deployment label; it is separate from the
GitHub PR label selected by `review-context.yaml: review_label`.

The primary `review-runner` runs on Terra. Passes marked `luna` use the
`review-luna` subagent on Luna; passes marked `terra` or with no preference
use the primary. The separate `review-verifier` subagent also runs on Terra
and is reserved for refuting proposed findings. See the [runner config](https://github.com/RedHatInsights/fabric-access-ai-dev/blob/main/instance/rbac-config-review/agent/.opencode/agents/review-runner.md),
[Luna worker config](https://github.com/RedHatInsights/fabric-access-ai-dev/blob/main/instance/rbac-config-review/agent/.opencode/agents/review-luna.md),
and [verifier config](https://github.com/RedHatInsights/fabric-access-ai-dev/blob/main/instance/rbac-config-review/agent/.opencode/agents/review-verifier.md).

See the root [project README](../../README.md) for the overall bot repository
and deployment layout, and [the workflow guide](agent/workflows/review-only/WORKFLOW.md)
for detailed queue, evidence, ledger, and publishing behavior. The workload
template is [`deploy/rbac-config-template.yaml`](../../deploy/rbac-config-template.yaml).
