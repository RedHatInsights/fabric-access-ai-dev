# fabric-access-ai-dev

Custom bot runner instance for the Fabric Access team, built on [dev-bot](https://github.com/RedHatInsights/platform-frontend-ai-dev).

Focused on [insights-rbac](https://github.com/RedHatInsights/insights-rbac) — a Django REST Framework microservice providing Role-Based Access Control for console.redhat.com.

## Architecture

Uses dev-bot as a git submodule. The submodule ships `Dockerfile.runner` which builds the full bot image and runs instance-specific customization hooks from this repo.

```
fabric-access-ai-dev/
├── dev-bot/        # Git submodule (don't modify)
├── setup.sh        # Custom build steps (dnf install, pip install, etc.)
├── instance/       # Extra files COPYed into the image
│   ├── rbac-config/              # Jira-sprint instance
│   │   └── agent/
│   │       ├── instance.yaml        # workflow: jira-sprint
│   │       ├── project-repos.json
│   │       ├── mcp.json
│   │       └── personas/
│   ├── rbac-config-konflux/      # Konflux PR-squash instance
│   ├── rbac-config-pr-label/     # GitHub PR-label instance (label: dev-bot)
│   │   └── agent/
│   │       ├── instance.yaml        # workflow: ./workflows/pr-label
│   │       ├── project-repos.json
│   │       └── workflows/pr-label/
│   └── rbac-config-review/       # GitHub review-only instance (label: bot-review)
│       └── agent/
│           ├── instance.yaml        # workflow: ./workflows/review-only
│           ├── project-repos.json   # PR review allowlist
│           ├── review-context.yaml  # related repos and direct Markdown links
│           └── workflows/review-only/
└── README.md
```

One instance = one workflow. `rbac-config` still starts from Jira. `rbac-config-pr-label` watches open GitHub PRs labeled `dev-bot`, creates a memory-server task, and pushes commits / review suggestions onto that same PR. It requires a bracketed Jira key in each PR title, creates and links a Jira Task when a labeled PR has no key, and updates that Jira issue to **Release Pending** when the PR merges.

To run the PR-label instance, deploy a **second** bot with the same image. Do not copy the Jira bot's `BOT_LABEL` or `BOT_INSTANCE_ID`.

| Parameter | Required | Example |
|-----------|----------|---------|
| `BOT_CONFIG_PATH` | yes | `instance/rbac-config-pr-label` |
| `BOT_NAME` | yes | a name distinct from the Jira bot (e.g. `devbot-fabric-access-pr-label`) |
| `BOT_INSTANCE_ID` | yes, **must differ** from the Jira and Konflux bots | e.g. `fabric-access-pr-label` |
| `BOT_PR_LABEL` | no (defaults to `dev-bot`) | GitHub PR label to watch |
| `BOT_LABEL` | ignored by this workflow | leave as-is; it is the Jira ticket label |

The GitHub label is `BOT_PR_LABEL`, not `BOT_LABEL`, so a copied Jira deploy target cannot accidentally search for the Jira label on GitHub.

### Review-only instance

`rbac-config-review` scans the GitHub repositories in its
`project-repos.json` for open PRs with the `bot-review` label. It reviews one
PR per cycle and posts a comment review. It never edits or pushes code,
approves, requests changes, merges, or creates Jira work. It tracks reviewed
head/base SHA pairs and human feedback in the shared memory server.
The context builder writes an impact report of changed identifiers and
candidate usages in related repositories. A separate verifier subagent tries
to refute each proposed finding; an evidence validator permits at most seven
findings and renders only confirmed findings with pinned code references.

The instance's `review-context.yaml` defines related repositories and any
number of direct GitHub `.md` file URLs for each reviewed repository. For an
`insights-rbac` PR it initially reads RBAC Config, HBI, and Kessel Inventory
context. Edit this file to add or remove related repositories and Markdown
links; no runner image change is needed when the remote config repo is synced.
The workflow bundles a `review-pr` skill.

Deploy another bot workload using `deploy/rbac-config-template.yaml` with
these instance-specific parameters. Start with `BOT_REPLICAS=0`; enable it
after the config and GitHub label are ready. Reuse the existing shared proxy,
memory server, and model settings.

| Parameter | Value |
|-----------|-------|
| `BOT_NAME` | `devbot-fabric-access-review` |
| `BOT_INSTANCE_ID` | `fabric-access-review` |
| `BOT_CONFIG_REPO` | `https://github.com/RedHatInsights/fabric-access-ai-dev.git` |
| `BOT_CONFIG_PATH` | `instance/rbac-config-review` |
| `BOT_EXECUTION_ENGINE` | `coordinator` |
| `REHOR_OPENCODE_DEPLOYMENT_CONFIG` | `instance/rbac-config-review/opencode-deployment.json` |
| `BOT_LABEL` | `bot-review` (runner label; PR label comes from `review-context.yaml`) |

This profile currently handles GitHub PRs. The existing `app-interface`
GitLab repository needs an MR adapter before it can enter this review queue.

No Dockerfile in this repo — Konflux points at `dev-bot/Dockerfile.runner`.

## Build

```bash
git submodule update --init --recursive
docker build -f dev-bot/Dockerfile.runner -t fabric-access-ai-dev:local .
```

Or use the helper script:

```bash
./build.sh
```

## Customization

- **setup.sh** — runs as root during build. Install packages, write config, etc.
- **instance/** — files COPYed to `/home/botuser/app/instance/` in the image.

## Updating dev-bot

```bash
cd dev-bot && git pull origin master && cd ..
git add dev-bot
git commit -m "chore: update dev-bot submodule"
```

Konflux also opens automated PRs when dev-bot merges new features.

## Konflux

```yaml
dockerfile: dev-bot/Dockerfile.runner
path-context: .
```
