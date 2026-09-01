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
│   └── jira-cve-backlog/              # CVE backlog patching instance
│       └── agent/
│           ├── instance.yaml        # workflow: ./workflows/pr-label
│           ├── project-repos.json
│           └── workflows/pr-label/
└── README.md
```

One instance = one workflow. `rbac-config` still starts from Jira. `rbac-config-pr-label` watches open GitHub PRs labeled `dev-bot`, creates a memory-server task, and pushes commits / review suggestions onto that same PR. `jira-cve-backlog` scans a JIRA backlog for CVE tickets, deduplicates them, and patches affected repos by upgrading vulnerable dependencies (Go, Python, npm) and container base images (UBI9/Red Hat).

To run the PR-label or CVE-backlog instance, deploy a separate bot with the same image. Do not copy another bot's `BOT_LABEL` or `BOT_INSTANCE_ID`.

| Parameter | Required | Example |
|-----------|----------|---------|
| `BOT_CONFIG_PATH` | yes | `instance/rbac-config-pr-label` |
| `BOT_NAME` | yes | a name distinct from the Jira bot (e.g. `devbot-fabric-access-pr-label`) |
| `BOT_INSTANCE_ID` | yes, **must differ** from the Jira and Konflux bots | e.g. `fabric-access-pr-label` |
| `BOT_PR_LABEL` | no (defaults to `dev-bot`) | GitHub PR label to watch |
| `BOT_LABEL` | ignored by this workflow | leave as-is; it is the Jira ticket label |

The GitHub label is `BOT_PR_LABEL`, not `BOT_LABEL`, so a copied Jira deploy target cannot accidentally search for the Jira label on GitHub.

### CVE Backlog instance

| Parameter | Required | Example |
|-----------|----------|---------|
| `BOT_CONFIG_PATH` | yes | `instance/jira-cve-backlog` |
| `BOT_NAME` | yes | a name distinct from other bots (e.g. `devbot-fabric-access-jira-cve-backlog`) |
| `BOT_INSTANCE_ID` | yes, **must differ** from other bots | e.g. `fabric-access-jira-cve-backlog` |
| `JIRA_URL` | yes | base URL of the JIRA instance |
| `JIRA_TOKEN` | yes | JIRA personal access token or API token |

JIRA project, JQL filter, board ID, and repo mappings are configured in `instance/jira-cve-backlog/agent/jira-config.json`. Target repos are listed in `instance/jira-cve-backlog/agent/project-repos.json`.

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
