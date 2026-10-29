---
description: Implements rbac-config changes in the declarative roles, permissions, and KSL schema repository.
mode: subagent
model: rehor-openai/gpt-6-luna
request:
  body:
    reasoning:
      effort: medium
permissions:
  - { action: subagent, resource: "*", effect: deny }
---

## rbac-config Guidelines

You are working on **rbac-config**, the declarative configuration repository
for roles and permissions seeded into every Hybrid Cloud Console RBAC tenant,
plus Kessel Schema Language (KSL) files compiling to SpiceDB authorization
schemas. No application code — only JSON configs, KSL schemas, and CI
automation. Default branch: `master`. Requires Go for building KSL and
permissions tools.

- Layout: `configs/stage/` (deploys Tuesdays) and `configs/prod/` (deploys
  Thursdays) with `permissions/<app>.json` (4-space indent, filename = app
  namespace) and `roles/<app>.json` (2-space indent, `roles` array with `name`,
  `display_name`, `description`, `system`, `version`, `access`);
  `configs/*/schemas/src/*.ksl`, `migrated_apps.lst`, `hostsonly_apps.lst`,
  and generated `schema.zed`; `schemas/permissions.schema` and `roles.schema`;
  gitignored `_private/` artifacts and `rbac_v1_permissions.json`.
- Stage-first: changes go to `configs/stage/` first, promoted to
  `configs/prod/` after validation. If a change applies to both environments,
  update both — they may intentionally diverge.
- Version bumping is mandatory: the role `version` integer triggers re-seeding;
  forgetting it silently ignores the change in production. New roles start at
  version 2+. The `name` field is the immutable database primary key — rename
  via `display_name`, never `name`.
- Permission/role files are not 1:1: some apps have permissions without roles;
  role files (e.g. `rhel.json`) may reference permissions from many apps. Most
  apps need the wildcard `"*": [{"verb": "*"}]` resource for admin roles
  referencing `app:*:*`.
- KSL: one `.ksl` file per app; `rbac.ksl` defines core types and macros
  (`@rbac.add_v1_based_permission`, `add_contingent_permission`,
  `add_v1only_permission`, `add_unified_permission`); hyphens in app/resource
  names become underscores. Never edit `schema.zed` directly — CI overwrites it
  from `.ksl` sources.
- Build/test: `make init` once (installs `ksl` and
  `generate-v1-only-permissions`); validate locally with
  `make ksl-test-schema-stage` and `make ksl-test-schema-prod` (safe,
  writes to `_private/test-schema/`); `make check-go-tools` verifies tools.
  NEVER run `make ksl-schema-stage`/`ksl-schema-prod` locally — they overwrite
  committed `schema.zed` files.
- CI: `pr.yml` runs JSON Schema validation (permissions strict, roles
  non-strict), permission dependency validation (`requires` integrity),
  V1-only generation + KSL compilation, and SpiceDB validation via
  `authzed/action-spicedb-validate`. `master.yml` regenerates ConfigMaps and
  opens an automated PR from the `configmaps-schema` branch, which must also
  be merged; deployment then needs an app-interface `ref` bump MR.
- Documentation: every non-trivial PR must update canonical docs or add a
  focused page under the existing `docs/` location in the same PR, covering
  changed roles/permissions/KSL relations, affected environments, version
  bumps, and validation commands; otherwise explain docs-neutrality in the PR
  summary. Never document generated artifacts as source of truth.
- Common mistakes: missing version bump; renaming `name` instead of
  `display_name`; editing `schema.zed`; using non-test schema targets locally;
  changing only one environment; missing wildcard `"*"` resource.
