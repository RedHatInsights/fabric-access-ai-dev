# HyperShell Migration — fabric-access-ai-dev

Draft migration of the five Fabric Access instances (rbac-config,
rbac-config-konflux, rbac-config-pr-label, rbac-config-review, and
rbac-project-coordinator) from template-managed deployments with sidecars to
the Rehor agent dispatcher and OpenShell sandboxes. The first three are the
initial cutover set; review and project-coordinator remain behind the separate
gate described below.

## What changes here

- **Runner image**: the UBI 9 runner build includes PostgreSQL 16 and Redis 7,
  matching the legacy sidecars' major versions. Redis comes from UBI AppStream.
  UBI does not publish a PostgreSQL server RPM, so the build compiles a
  checksum-pinned upstream PostgreSQL release using UBI dependencies. The
  build does not require access to the subscription-only sidecar images.
- **On-demand services**: nothing starts at image boot.
  `instance/rbac-config/agent/scripts/start-rbac-services.sh` starts both
  servers (PostgreSQL on localhost:15432 with data under the writable runtime
  home, Redis on localhost:6379 without persistence). Run the existing
  `setup-rbac-env.sh [--no-env] [path/to/insights-rbac]` command inside the
  sandbox to start them before applying migrations. In legacy deployments
  the same command waits for sidecars instead. The endpoints and local
  development credentials are unchanged.
- **Dispatcher profiles**: every instance declares `instance.yaml` for the
  dispatcher (OpenCode runtime, rehor-openai provider, declarative
  preflights, `latest` image tag) and a KEDA-style `schedule:` mirroring the
  legacy windows: Europe/Prague Mon-Fri 09:00-23:00 OR America/New_York
  Mon-Fri 09:00-18:00 — the deploy template's defaults, which all five
  app-interface targets use unchanged. The dispatcher starts no sessions
  outside those windows; in-flight sessions drain.
  Review-only and project-coordinator use the same two schedule windows and
  have their own registered preflights.
- **Preflights**: `rbac-config` reuses the registered jira-sprint set. The
  custom checks are ported to the central TypeScript registry
  (OpenShift-Fleet/rehor): `fabric-pr-label` (labeled-PR discovery) and
  `fabric-konflux-bot-prs` (bot-PR consolidation), plus optional
  `gh-pr-status` parameters covering standard and pr-label review-bot
  feedback markers, plus pr-label task-key scoping. `fabric-project-state`
  checks for changed project snapshots. `fabric-review-queue` sends a compact
  reference for tracked reviews; the sandbox must run `restore-review-ledger`
  before work.
  No ledger body or dispatcher-local path crosses the dispatch envelope.
  Stale pending publications and retries missing their companion artifacts
  need reconciliation; they must not start a fresh review silently.

## Pre-merge gate

Legacy runners sync this repository during cycles, so merging can change their
loaded profiles before dispatcher cutover. Before merging, verify the existing
runner supports the updated profiles and that the model proxy permits Sol,
Terra, and Luna, including the review profile's Sol root. Otherwise leave this
PR draft and unmerged.

## Rollout gates

1. Done: the rehor preflight port is merged (core #680) and the `dev-bot`
   submodule pin tracks rehor `master`, currently
   `b7bfd8b29bfbc1a2e04758194ce17b2f20c87054` — a pure fast-forward over the
   #680 squash merge that adds tier-2 `sandbox.env` secret rendering (#681),
   the runner identity-header fix (#682), native sandbox env delivery (#688),
   a Grype dependency bump (#683), Renovate preflights and shared GitHub changes
   (#689), and constrained profile egress support (#690). Advanced in
   this branch to verified rehor `master`
   `6a473e923f0ae144e480894eeeb6e4a7c08ea34a` — a pure fast-forward over
   b7bfd8b that adds memory-server task documents (#693), per-entry
   preflight parameter secret rendering (#694), declared environment
   forwarding to the OpenCode child (#691), and GitLab transport
   documentation (#695).
2. Deploy a dispatcher that includes KEDA-style schedules (rehor #674) and
   the fabric preflights, with proxy routes, memory access, and the
   `hcc-accessmanagement-tenant/fabric-access-ai-dev` image promoted so the
   `latest` tag resolves to a build containing the baked-in servers. Audit
   centralized preflight parity against current main before any cutover,
   including the PR-label workflow and the deployed `GH_USER_NAME` exclusion.
   The independent audit reports the current published `latest` image lacks
   this draft's PostgreSQL/Redis changes; do not use it as evidence of readiness.
3. GitOps: move the initial three app-interface targets
   (`fabric-access-ai-dev-deploy.yml`, `fabric-access-ai-konflux-deploy.yml`,
   `fabric-access-ai-pr-label-deploy.yml` in `services/insights/rbac`) from
   the sidecar template to the dispatcher-managed configuration; preserve
   the exact `BOT_INSTANCE_ID`, `BOT_LABEL`, and workflow-selection values.
   `fabric-access-ai-review-deploy.yml` and
   `fabric-access-ai-coordinator-deploy.yml` stay on the sidecar templates
   until gate 7.
4. Validate a promoted image: postgres/redis start on demand via
   `setup-rbac-env.sh`, RBAC migrations apply, the Celery worker connects,
   and the sandbox filesystem constraints (read-only rootfs, private writable
   `$HOME/app/{repos,data}` and `/tmp`) hold.
5. Schedule parity: confirm the EU+US windows still match team intent
   before cutover so legacy runners and the dispatcher never process the
   same instance simultaneously. Instance IDs must stay distinct across the
   five profiles (the pr-label workflow scopes tasks by `pr-label:` key
   prefix to tolerate a shared instance, but distinct IDs remain required by
   the deployment contract).
6. Run no-work and representative cycles: a Jira ticket through
   rbac-config, a konflux-authored PR consolidation, and a labeled PR with a
   bracketed Jira key through pr-label, including review-comment feedback
   and the Jira Release Pending transition on merge.
7. **Review and coordinator cutover blocked.** Before migrating either, verify
   GitHub proxy ownership and membership checks, sandbox ledger restoration,
   pending-publication retry behavior, and coordinator snapshot parity with
   live Jira and GitHub responses. Audit centralized preflight behavior against
   the current review queue and ledger, PR-label workflow, and coordinator
   plan/approval changes before cutover. Leave both legacy deployments in
   place until those checks pass. Coordinator REST-vs-MCP plan snapshot/hash
   compatibility and the dispatcher's 64 KiB input limit remain unresolved.
   Review approval must also fail closed until revision continuity and the
   latest approval are verifiable; the central follow-up is not yet deployed.
   Review pending-publication artifacts cannot currently be reliably restored
   after sandbox deletion. Require exact durable retry recovery before review
   cutover; never regenerate missing artifacts or store private review bundles
   in shared task metadata without a confidentiality and concurrency decision.
   Incomplete legacy pending publications require manual reconciliation.
8. When a preflight starts and another errors, the dispatcher starts the
   session but does not include the error details in its prompt. Confirm error
   reporting and operational handling before cutover; a start is not proof
   that every check succeeded.
9. Before enabling `rbac-config` or `rbac-config-pr-label`, obtain owner
   approval for their shared Slack webhook. The standalone app-interface ESO
   and RBAC integration is proposed in [draft MR !220723](https://gitlab.cee.redhat.com/service/app-interface/-/merge_requests/220723)
   (`5b6afe9493`); it is not merged or synced. It maps all 16 keys of the
   `fabric-access-rehor` Secret, including the owner-confirmed
   `bot-jira-team-name-insights-rbac`. Its Vault policy grants the ESO
   identity read access to the entire source object, not only the selected key.
   Obtain explicit source-owner and security approval for that scope, or use a
   dedicated Vault object, before merging. Verify ESO sync of the
   `fabric-access-rehor` Secret and its exact key set in the dispatcher
   namespace, the Secret's `rehor.dev/secret-tier=2` label, and dispatcher RBAC
   scoped to that exact Secret. Verify sandbox delivery without exposing the
   webhook or any other mapped value. The Konflux profile does not reference
   this Secret.
10. Before enabling the coordinator, verify its OpenCode child receives
    `BOT_LABEL`, `BOT_TEAM_FIELD_ID`, and `BOT_IMPLEMENTATION_LABEL`.
    Rehor #691 (merged as c66c16d, contained in the advanced pin 6a473e9)
    replaced the exact-name allowlist with generic config-driven
    forwarding: the Python bridge collects the variable NAMES the selected
    agent's configuration declares — workflow manifest
    `requires.env_vars`/`optional_env_vars`, active env preset manifests,
    the profile `env` (or legacy `preflight.env`) category, and the
    `sandbox.env` declarations — and the OpenCode child receives the
    declared names, with sandbox-mode tier-2 values taken from the trusted
    render. Names only cross the bridge; values never do, protected
    platform names stay blocked, and nothing Fabric-specific was added to
    shared core. What remains is deployment and verification, not core
    code: the stage dispatcher image predates #691/#694, and end-to-end
    sandbox delivery must be verified with synthetic values before
    cutover. Review Jira creation also needs 13 nonsecret
    project/team/label/component settings from app-interface; they ride
    the same declared-name mechanism through the review profile's
    `FABRIC_` aliases, and internal values still must not be published in
    this repository. The renderer rejects reserved `BOT_` names in
    `sandbox.env`, so Secret references under those names are not a
    working solution. The interim resolution stands: the review,
    coordinator, and PR-label profiles declare nonreserved `FABRIC_`
    aliases in `sandbox.env`, rendered from the 16 `fabric-access-rehor`
    Secret keys (app-interface MR !220723, merged). The helpers
    for review and coordinator are mode-aware: under
    `REHOR_INPUT_SOURCE=sandbox` their configured `FABRIC_` deliveries are
    required, with no `BOT_` or default fallback. The PR-label workflow
    instructs the agent to stop if its project setting is absent; this is
    not enforced by a helper. Legacy deployments keep the `BOT_` fallback.
    Verify end-to-end sandbox delivery with synthetic values before relying
    on the `FABRIC_` path.
11. DONE in this branch, not rolled out: Rehor #694 (merged upstream as
    a1a03a1, contained in the advanced pin 6a473e9) renders
    `${{ secret(...) }}` references in preflight `with:` maps per entry,
    immediately before each check executes, against the latest ESO-synced
    Secret. The coordinator's `preflights[].with.project` and
    `with.teamFieldId` now carry
    `${{ secret('fabric-access-rehor', 'bot-jira-project') }}` and
    `${{ secret('fabric-access-rehor', 'bot-team-field-id') }}` — the same
    references as its `sandbox.env` deliveries. Drift is detected, not
    made impossible: every entry reads its references fresh, only
    start-outcome entries bind the values they observed, two starts that
    observed different values fail the dispatch as a transient error, and
    the sandbox launch render is compared against the bound values — a
    difference refuses the launch. Skip and error observations never bind;
    values referenced only by preflight parameters (never by
    `sandbox.env`) get no launch-time re-check, while these two keys are
    referenced by both and therefore compared at launch. The owner accepts
    that resolved tier-2 values, including these Jira project and Team
    field settings, are intentionally available to the agent and may
    appear in its transcripts (the `fabric-project-state` start content
    carries the Team field id and control keys); the trusted-helper flow
    and the mode-aware delivery safeguards are unchanged. The remaining
    `with` literals (control and implementation labels, `includeBacklog`)
    are public non-secret values and stay literal; the public top-level
    `BOT_JIRA_PROJECT` and `BOT_TEAM_FIELD_ID` env entries remain only
    for legacy deployments and the legacy Python preflight. The dev-bot
    pin is advanced to 6a473e9 with the dispatcher
    and preflights dist rebuilt, and the focused tests exercise deferred
    validation, synthetic rendering, and the resolved-parameter schema.
    Deployment gates remain: the stage dispatcher image predates #694,
    end-to-end sandbox validation is pending, and centralized preflight
    parity work (Rehor #692 — coordinator planning/eligibility and review
    approval reuse) is still a draft PR, so registered preflight behavior
    may still change. Do not read this branch as rollout-ready.
12. Before sandbox cutover, verify approved effective network access for source
    clones, package installs, and official release notes and advisories; public
    egress may be disabled. Verify effective agent permissions and network
    policy together: per-agent `webfetch`/`websearch` permissions may override
    global defaults, and Code Mode `execute` can fetch separately. Confirm
    source access is appropriately scoped and external content is treated as
    untrusted. Do not broaden egress or tool permissions without approval.
13. `jira_config.py` prints the resolved per-repository Jira settings
    (project, team ID/name, labels, component, Team field) into its tool
    output, which flows into agent transcripts and subagent prompts. Once
    those settings are Vault-sourced through the `FABRIC_` sandbox
    deliveries, rendered values can therefore appear in transcripts
    end-to-end — do not claim otherwise. DECISION (owner): keep these
    values in a trusted helper — Vault-backed Jira project/team settings
    must NOT appear in model tool output or transcripts, and Jira creation
    must be adapted to a trusted helper flow. A separate read-only agent is
    designing that helper/flow; do not refactor the review workflow's Jira
    creation concurrently while that design is underway. Until the exposure
    is resolved, this config branch must not be published. The Slack
    notification flow is unchanged by this decision.

## Legacy merge-safety

The existing sidecar deployment templates remain the rollback path, subject to
the pre-merge gate above:

- `deploy/template.yaml` and `deploy/rbac-config-template.yaml` are not
  modified. Image, sidecar, KEDA, and NetworkPolicy behavior stays identical.
- Nothing starts PostgreSQL or Redis at image boot. The new
  `start-rbac-services.sh` is called only when `REHOR_INPUT_SOURCE=sandbox`;
  otherwise `setup-rbac-env.sh` waits for the legacy sidecars on the same
  localhost endpoints.
- The proposed baked-in servers are additive; the legacy path keeps using its
  sidecars. However, running legacy agents can load changed configuration on
  their next sync, including model selection, before any GitOps cutover.
- Identities, labels, workflow selection, and provider are preserved from
  the authoritative main branch. Model tiers are role-based: Sol for the
  standard, review, and project coordinators; Terra for the Konflux/PR-label
  substantive runners and implementers; Luna for the existing bounded review
  workers. Each profile catalog declares every model its instance and primary
  agent select.

## Validation status

The UBI 9 image build, on-demand PostgreSQL and Redis startup, RBAC migrations,
and Celery connection require validation for this branch. No image was
published or promoted by this update.

Current branch validation covers the five `instance.yaml` profiles through
the real dispatcher discovery and the populated TypeScript preflight registry,
matched non-empty preflights, the EU+US schedule windows, and focused
`restore-review-ledger` tests. The `FABRIC_` sandbox aliases additionally
pass synthetic checks at the pinned runtime: dispatcher discovery accepts
the names, rendering resolves every one of the 16 mapped Secret keys from
synthetic data, one missing key (the RBAC team name) fails the whole render
closed, and the `jira_config`/coordinator helpers resolve sandbox names,
legacy `BOT_` fallbacks, and the Team-field indirection from synthetic
values, including the mode-aware requirement that a sandbox delivery is
mandatory (a legacy `BOT_` value, default, or public literal never stands
in) while legacy deployments keep their fallbacks. Discovery and schema validation do not establish
behavioral parity with the legacy preflights. Runtime sandbox, proxy
ownership/membership, centralized parity, and live GitHub/Jira behavior remain
rollout-gate work. Current main also has failing coordinator and review tests;
the same failures reproduce on its unchanged checkout.

## Rollback

Remove the repository from dispatcher configuration and let in-flight
sessions drain, then restore the sidecar template deployments from
app-interface. Keep the previous runner image available; the baked-in
servers are additive and do not affect the legacy deployment path. Because
the templates and legacy branches are unchanged, rollback is a GitOps
repoint, not an image or repository revert. Review and project-coordinator
never leave their legacy deployments until gate 7 passes.
