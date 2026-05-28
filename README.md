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
│   └── rbac-config/
│       └── agent/
│           ├── project-repos.json   # Repos the bot works on
│           ├── mcp.json             # MCP server config
│           └── personas/
│               └── rbac/
│                   └── prompt.md    # RBAC coding standards
└── README.md
```

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
