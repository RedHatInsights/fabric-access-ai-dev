# GitHub PR review bot

This directory configures the `rbac-config-review` instance of the Fabric Access
bot. It reviews open GitHub pull requests and publishes a review summary and
anchored inline comments. The workflow is read-only with respect to source code:
it does not edit files, commit, push, approve, request changes, or merge PRs.

## Configuration files

| File | Purpose |
|---|---|
| [`agent/instance.yaml`](agent/instance.yaml) | Selects the scheduled workflow, runtime, provider, and model. |
| [`opencode-deployment.json`](opencode-deployment.json) | Defines the Rehor provider and available Luna and Terra models. |
| [`agent/.opencode/agents/`](agent/.opencode/agents/) | Configures the runner, review workers, missing-context detector, and verifier models and permissions. |
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
GitHub label required on an open PR for it to enter the queue. A new GitHub
event adding that label authorizes a full review; an already-present label does
not. The current value is `dev-bot-review`. This is distinct from the deployment variable `BOT_LABEL`,
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
  inline finding layout. `custom` requires at least one entry in `skills`; the
  selected skills define the detailed inline layout for findings attributed to
  them and must complete for publication. Other findings use the standard
  inline layout. Both formats use the same status summary template: a short
  PR-purpose sentence, a Findings table with inline-thread links, and help for
  requesting a re-review or explanation.

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

## Comment writing and context questions

Write comments for developers working on this project: direct, technically
precise, and easy to scan. Use short sentences and standard terminology. This
applies to status comments, inline findings, context questions, and answer
follow-ups. The status comment has a short description of what the PR changes,
then a Findings heading with counts and a table of plain-text finding IDs,
severity, dimension, resolution state, and separate inline-comment links. It
ends with the re-review command and a prompt for explanation questions.
Detailed scenarios, evidence, and fixes belong in the inline comments.

Every finding and feedback comment that describes behavior includes a realistic
sequence of steps that reproduces it. Cite pinned evidence inline beside the
claim it supports. Review headings use `Review N` without a `#` before the
number. Use these severity values: `blocking` for issues that should
stop merge, `suggestion` for meaningful non-blocking improvements, `nit` for
small polish items, and `question` for missing-context requests.

When an author asks for an explanation or asks “how”, the review agent must
load [`agent/skills/explanatory-answer/SKILL.md`](agent/skills/explanatory-answer/SKILL.md)
before replying. For longer replies, use these headings in order; omit optional
sections when they add no value:

1. **Answer** — direct response.
2. **Why or how** — mechanism in a clear sequence.
3. **Reasoning** — connect pinned facts to the conclusion; cite evidence.
4. **Example** — include only when it helps.
5. **Implication** — say what follows or what to do, when relevant.

A simple answer may use only **Answer**. Evidence links sit beside the claims
they support. If a commenter asks for a diagram or graph, include a concise
Mermaid diagram after the explanation when pinned context supports an accurate
one. Otherwise, include one only when it materially improves understanding.
Diagrams are for explanation replies, not status summaries or original finding
comments. Every explanation reply includes the hidden marker
`<!-- dev-review-bot:fabric-access-explanation:<source-comment-id> -->` so a
retry can detect an answer already posted.

The `missing-context-detector` subagent asks only when missing information
could change the review conclusion or requested action; it must not assume a
defect. It posts one question per PR comment with an impact, a realistic
scenario, a hidden marker, and a Q-ID.
The PR author answers with `Answer Q-ID: ...`. On the next feedback check, the
bot checks the answer, verifies the author's organization membership, and
uses the explanatory-answer skill for “how” or explanation questions. It posts
one marked response stating the next action or that no change is needed.
If more context is still needed, it posts the answer explanation and the new
focused question as separate comments.
Preflight checks PR issue/review comments before starting a feedback session;
bot-authored comments alone do not launch a model run. It stores comment IDs at
the timestamp boundary so the same answer is not processed twice.

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
- **`model_preference`** (optional): routes the pass using
  `config.model_routing` in `dimensions.yaml`. `terra` runs in the read-only
  `review-terra` subagent and `luna` runs in the read-only `review-luna`
  subagent. A missing preference uses the configured Luna default.
  `build_context.py` resolves each selected pass to `review_agent` in
  `review_plan`; passes run in batches of at most four concurrent workers, and
  an unavailable worker is recorded as a failure instead of silently
  switching models. The configured aliases
  are `luna` → `rehor-openai/gpt-6-luna` and `terra` →
  `rehor-openai/gpt-6.1-terra` in the
  [OpenCode deployment config](https://github.com/RedHatInsights/fabric-access-ai-dev/blob/main/instance/rbac-config-review/opencode-deployment.json).
  Luna is the default for orchestration and missing preferences. Terra remains
  assigned to broad or high-judgment dimensions, while Luna handles focused
  dimensions. The current assignments are: correctness,
  architecture, security, concurrency, database schema, and cross-repository
  dependencies use Terra; code quality, documentation, observability,
  performance, error handling, test quality, API contract, database queries,
  integrations, vulnerability, Go idioms, React/PatternFly, and learning
  extraction use Luna. The opt-in KISS recipe has no preference and therefore
  uses Luna.

`language_extensions` maps changed-file suffixes to language names. Automatic
selection uses changed paths, the chosen level, `default`, languages, file
patterns, and documentation-only rules. Built-in entries currently have
`default: true`; `goose-kiss-review` is a dynamic entry with `default: false`.

The catalog's `config` holds review-wide model routing, deduplication, and
consensus settings. `model_routing.default` selects the worker for dimensions
without an explicit preference, and `model_routing.agents` maps each profile
to an OpenCode agent. Here the default is Luna, with `terra: review-terra`
and `luna: review-luna`. `review_skills.py` records the resolved profile and
agent for each selected pass; an unknown profile stops plan generation instead
of silently using another model. `consensus_threshold` is the number of
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
5. Remove duplicates and run selected passes in plan-ordered batches of at most
   four concurrent workers. A verifier then challenges proposed findings
   before publication.

## How a review runs

```text
project-repos.json + review-context.yaml
                 │
                 ▼
  Find open labeled PRs and check review authorization
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
with `review_label`, and at most one eligible PR in a cycle. A newly added
`review_label` event is the only automatic authorization for a full review;
the label merely being present does not authorize one. A changed head/base SHA
or re-review request requires explicit organization-member approval before
review work starts. It skips drafts, the bot's own PRs, and entries without
head/base revisions. It tracks work in the shared task-state service, so
interrupted authorized reviews can resume and later reviews can update the
existing summary instead of creating duplicate review summaries.

Task status follows the PR lifecycle: a review that has been published is
`pr_open` while the PR awaits changes or merge. The queue checks tracked PRs
for lifecycle changes; it sets `done` only after GitHub confirms a non-null
`mergedAt`. A PR closed without merging is `paused`, and can return to
`pr_open` if reopened. This also reconciles older tasks that were marked done
immediately after publishing a review.

Context preparation pins the PR and fetched references. Impact analysis finds
candidate usages of changed identifiers in declared related repositories; a
candidate is context for reviewers, not a finding by itself. Selected
dimensions review the changed code, then `review-verifier` tries to refute each
proposed finding. The evidence gate only publishes findings anchored to the PR
diff and supported by pinned references. Finding IDs (`F1`, `F2`, …) are kept
stable across the summary, inline comments, and later review updates.

The user-facing output is GitHub comments: one review summary and anchored
inline threads for confirmed findings. The summary has a short description of
the PR, a Findings table with inline-thread links, and prompts for requesting
a re-review or explanation. A new top-level PR comment containing exactly
`/review-bot re-review` asks for approval; it does not start a review. The bot
posts a revision-bound approval ID, and any verified member of the repository's
GitHub organization can reply `Review bot: yes <RA-ID>` or
`Review bot: no <RA-ID>`. A verified yes starts the pass and preserves finding
IDs while incrementing the review number in the summary.

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

| Profile | Agent | Configured model |
|---|---|---|
| `terra` | `review-terra` | `rehor-openai/gpt-6.1-terra` |
| `luna` | `review-runner`, `review-luna`, `review-verifier` | `rehor-openai/gpt-6-luna` |

The primary `review-runner` uses Luna for orchestration. Dimension passes are
dispatched in batches of at most four concurrent workers to `review-terra` or
`review-luna` according to the resolved model preference. Results are recorded
by dimension before the next batch. The separate `review-verifier` subagent
also uses Luna to refute proposed findings. See the [runner config](agent/.opencode/agents/review-runner.md),
[Terra reviewer config](agent/.opencode/agents/review-terra.md),
[Luna reviewer config](agent/.opencode/agents/review-luna.md),
[verifier config](agent/.opencode/agents/review-verifier.md), and
[OpenCode deployment config](opencode-deployment.json).

See the root [project README](../../README.md) for the overall bot repository
and deployment layout, and [the workflow guide](agent/workflows/review-only/WORKFLOW.md)
for detailed queue, evidence, ledger, and publishing behavior. The workload
template is [`deploy/rbac-config-template.yaml`](../../deploy/rbac-config-template.yaml).
