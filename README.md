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
│   ├── rbac-project-coordinator/ # Jira parent/control-ticket coordinator
│   ├── rbac-config-pr-label/     # GitHub PR-label instance (label: dev-bot)
│   │   └── agent/
│   │       ├── instance.yaml        # workflow: ./workflows/pr-label
│   │       ├── project-repos.json
│   │       └── workflows/pr-label/
│   └── rbac-config-review/       # GitHub review-only instance (label: dev-bot-review)
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

`rbac-config-review` scans the GitHub repositories in its `project-repos.json`
for open PRs carrying the label configured by `review-context.yaml` (currently
`dev-bot-review`). It reviews at most one PR per cycle and publishes a GitHub
summary with inline comments for verified findings. The workflow is read-only
for source code. Its settings, review-context and dimensions YAML, selection
rules, and review stages are documented in the
[review bot README](instance/rbac-config-review/README.md).

The GitHub PR label is `review-context.yaml: review_label`; deployment
`BOT_LABEL` identifies the runner and does not control PR selection. The review
instance configuration lives under `instance/rbac-config-review` and is
deployed separately from the Jira and PR-label instances. This profile handles
GitHub PRs; the `app-interface` GitLab repository needs an MR adapter before it
can enter this queue.

No Dockerfile in this repo — Konflux points at `dev-bot/Dockerfile.runner`.

## Jira project coordinator

`instance/rbac-project-coordinator` is a separate bot instance using the
`project-coordinator` workflow. It discovers RHCLOUD control tickets labeled
`dev-bot-coordinator`, checks the parent and direct children against the
Jira Team ID, and posts project plans and progress on the control ticket. It
never writes code. Implementation stays with the `rbac-config` Jira bot.

Deploy another bot target using `deploy/template.yaml` or
`deploy/rbac-config-template.yaml`, with a distinct `BOT_NAME` and
`BOT_INSTANCE_ID`. Set these parameters for the coordinator target:

| Parameter | Value |
|---|---|
| `BOT_CONFIG_PATH` | `instance/rbac-project-coordinator` |
| `BOT_LABEL` | `dev-bot-coordinator` |
| `BOT_IMPLEMENTATION_LABEL` | The implementation bot's actual Jira label, currently `hcc-ai-platform-accessmanagement` |
| `BOT_JIRA_PROJECT` | `RHCLOUD` |
| `BOT_TEAM_FIELD_ID` | Jira's Team custom field key: `customfield_10001` (also used by this instance's review profiles) |
| `BOT_INCLUDE_BACKLOG` | Match the implementation bot's backlog setting; if `false`, runnable tickets need an active sprint |
| `BOT_EXECUTION_ENGINE` | `coordinator` (the runner uses OpenCode for this workflow) |
| `REHOR_OPENCODE_DEPLOYMENT_CONFIG` | `/home/botuser/app/instance/rbac-project-coordinator/opencode-deployment.json` |

No parent or control ticket key is hardcoded. Prepare Jira before the first
coordinator run:

1. **Configure the bot.** Set the Jira Team field ID, implementation label,
   and repository allowlist. The coordinator reads the Team option ID from each
   control ticket; it does not need a fixed Team ID in deployment config. Each
   `repo:<name>` allowed on the control ticket must match a key in
   `project-repos.json`.
2. **Prepare the parent and control ticket.** Use an Epic or supported parent
   issue with a clear project goal. Create exactly one direct child control
   ticket for this Team, set its Jira Team field, and add the `dev-bot-coordinator`
   label. The coordinator reads the Team ID from this field and uses it to
   scope the project; the Team does not need to be re-entered. Leave `repo:`,
   phase, and implementation labels off the control ticket. The coordinator
   proposes the repo setup and sets its first state label when it enrolls.
3. **Prepare implementation tickets.** Make each in-scope ticket a direct
   child of the same parent with the same Team ID. Give each a clear outcome
   and acceptance criteria. Every same-Team direct child other than the
   control ticket is treated as project work, so keep unrelated tickets out
   of this parent or include them in the plan.
4. **Approve the setup proposal.** The coordinator suggests a repository, one
   phase for each implementation ticket, and root tickets that need `relates
   to` links. Correct the suggestion if needed, then reply `Approved`. The
   coordinator applies the repo label, all phase labels, and root links. You do
   not need to edit labels or links. To ask the coordinator to add an ordering
   link, comment
   `/coord dependency RHCLOUD-123 blocks RHCLOUD-456` on the control ticket.
   The coordinator checks that both tickets are same-Team children and that
   the link will not create a cycle. It creates directional dependencies only
   when explicitly requested this way.
5. **Make first handoffs possible.** Tickets ready for handoff must be
   unassigned and in `New`, `Backlog`, `Refinement`, or `To Do`. They need an
   active sprint unless `BOT_INCLUDE_BACKLOG` is enabled. The coordinator
   chooses each ticket's repo from the control ticket's allowlist; make the
   ticket scope clear enough to identify the right repo.

On first enrollment, the coordinator analyzes all same-Team implementation
tickets and posts a proposed order, phase suggestions, dependencies, parallel
groups, repo choices, gaps, and questions on the control ticket. It normally
sets `dev-bot-coordinator:needs-plan` and waits for your approval. Replying
`Approved` lets it apply the proposed repo label, all phase labels, and root
`relates to` links. It does not create suggested `blocks` links without an
explicit dependency request.
After a scheduled run sees the plan gate pass, it sets
`dev-bot-coordinator:active` and releases the unblocked tickets in the earliest
incomplete phase to the implementation bot. It takes the handoff labels from
`instance/rbac-config-review/agent/review-context.yaml` for each selected repo.
When an implementation ticket has an open PR titled with its Jira key in
brackets, the coordinator adds the same YAML's `review_label` (currently
`dev-bot-review`) so the review bot can pick it up. It reports each handoff and
newly labeled PR on the control ticket. Handoffs can start in the same run that
applies an approved setup if the plan and intake gates pass.
The control ticket has one coordinator state label:
`dev-bot-coordinator:needs-plan`, `dev-bot-coordinator:active`,
`dev-bot-coordinator:blocked`, or `dev-bot-coordinator:complete`.

The coordinator proposes missing tickets, edits, and closures; it creates a
ticket only when a later comment says `/coord create P-1` for a named proposal.
`/coord start` asks it to recheck the plan gate and hand off currently runnable
tickets. `/coord status` requests a fresh summary; `/coord complete project`
can finish the project after all same-Team implementation tickets are complete.
It never edits or closes implementation tickets.

The preflight is read-only. It starts a coordinator session on first enrollment,
when project data, a matching open PR, or user comments change, or when a
coordinator state label is missing. Otherwise, it skips the project. The
app-interface deployment
currently schedules the instance on weekdays during its configured Europe and
US operating windows; no manual run command is needed. The workflow's
`actions.py` revalidates Jira state before writes and verifies GitHub PR labels
after adding them. New deployment parameters must be supplied by the deployment
target; this repository does not deploy the coordinator by itself.

### Coordination cycle

```mermaid
flowchart TD
    S["Scheduled scan<br/>Jira + saved snapshot"] --> C{"First run, project changed,<br/>or state missing?"}
    C -->|No| W["Wait for next scan"]
    C -->|Yes| R["Read one project<br/>parent, control, same-Team tickets"]
    R --> P["Propose repo, phases,<br/>and root links"]
    P --> A{"User approves setup?"}
    A -->|No| Q["Answer questions or revise proposal<br/>on the control ticket"]
    A -->|Yes| X["Apply repo label, phase labels,<br/>and root relates links"]
    X --> G{"Plan and intake ready?"}
    G -->|No| B["Report blockers<br/>and wait"]
    G -->|Yes| H["Hand off ready tickets<br/>to implementation bot"]
    Q --> U["Save snapshot and handled comments"]
    B --> U
    X --> U
    H --> U
    U --> S
```

| Step | Inputs | What happens |
|---|---|---|
| 1. Scan | Project, control label, Team field key, repo allowlist, backlog setting, and saved memory snapshot. | Finds control tickets and their parent issues. Reads each control ticket's Team ID and includes only direct children with that same ID. |
| 2. Detect change | Parent and control details; same-Team child status, labels, links, ownership, sprint, and human control comments. | Skips unchanged projects. Starts a cycle for a first run, changed project, or missing state label. |
| 3. Review | Changed project payload: ticket keys, phases, statuses, dependencies, gaps, ready/waiting tickets, allowed repos, and new comments. | Coordinator rereads relevant Jira tickets, answers setup questions, and proposes a repo, phases, and root links. |
| 4. Approve setup | The latest proposal and the user's approval comment. | One approval lets the validated action add the control repo label, implementation phase labels, and root `relates to` links. It does not create `blocks` dependencies. |
| 5. Check readiness | Approved setup, acceptance criteria, ticket assignees/statuses, active sprint or backlog setting. | Waits and reports any gaps. It hands off only unassigned, not-started tickets that also meet the sprint/backlog requirement. |
| 6. Act and save | Explicit dependency request or ready ticket; latest Jira state. | Validated actions apply requested dependencies or hand off ready work. Saves the snapshot and handled comment IDs for the next scan. |

On the control ticket, users can request:

```text
/coord status
/coord approve setup
/coord dependency RHCLOUD-123 blocks RHCLOUD-456
/coord create P-1
/coord complete project
```

`/coord create P-1` applies only to a previously proposed ticket;
`/coord complete project` requires all in-scope tickets to be complete.
During planning, the control comment lists every in-scope ticket with its
current status and phase, plus suggested phases, dependencies, parallel
groups, and repo routing. Dependency changes are applied only when the user
requests the exact `blocks` direction in Jira; suggested phases remain
suggestions. Replying `Approved` also works for the current setup proposal. Jira
remains the source of truth; the coordinator rereads relevant issues before
acting. The shared Jira MCP service must expose `jira_create_issue_link` for
setup links and requested dependencies.

Every coordinator comment ends with a **Possible next steps** footer. Its
suggestions are generated from the current ticket statuses, plan gaps, approval
requests, and handoff prerequisites. When no user action is needed, the footer
points to the next scheduled scan.

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
