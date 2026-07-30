## rbac-config Guidelines

You are working on **rbac-config**, the declarative configuration repository for roles and permissions that the Red Hat Hybrid Cloud Console RBAC service seeds into every tenant. It also contains Kessel Schema Language (KSL) files that compile to SpiceDB authorization schemas for the V2 authorization model. There is no application code here — only JSON configs, KSL schemas, and CI automation.

### Project Structure

```
configs/
  stage/                        # Stage environment (deploys Tuesdays)
  prod/                         # Production environment (deploys Thursdays)
    permissions/<app>.json      # One file per app; filename = app namespace
    roles/<app>.json            # One file per app; contains a "roles" array
    schemas/
      src/*.ksl                 # KSL source files for Kessel/SpiceDB
      migrated_apps.lst         # Apps using V2 authorization
      hostsonly_apps.lst        # Apps with host-only permissions
      schema.zed                # GENERATED — never edit manually
schemas/
  permissions.schema            # JSON Schema for permission files
  roles.schema                  # JSON Schema for role files
_private/                       # Generated artifacts — never edit
Makefile                        # Build targets for KSL/schema tools
```

### Build and Test

- **Requires Go** for building KSL and permissions tools
- **Default branch**: `master`
- **`make init`** — Install `ksl` (from `project-kessel/ksl-schema-language`) and `generate-v1-only-permissions` (from `RedHatInsights/rbac-config-actions`). Must run once before schema builds.
- **`make ksl-test-schema-stage`** — Build and validate stage schema → writes to `_private/test-schema/` (safe for local use).
- **`make ksl-test-schema-prod`** — Build and validate prod schema → writes to `_private/test-schema/` (safe for local use).
- **`make check-go-tools`** — Verify required Go tools are installed.
- **NEVER use** `make ksl-schema-stage` or `make ksl-schema-prod` locally — these overwrite committed `schema.zed` files.

### CI Pipeline

- **GitHub Actions** (`.github/workflows/`):
  - `pr.yml` — PR validation with 5 sequential steps:
    1. JSON Schema validation for permissions (`schemas/permissions.schema`)
    2. JSON Schema validation for roles (`schemas/roles.schema`, non-strict)
    3. Permission dependency validation (`requires` field integrity)
    4. V1-only permissions generation + KSL schema compilation (stage + prod)
    5. SpiceDB schema validation via `authzed/action-spicedb-validate` (stage + prod)
  - `master.yml` — Post-merge automation:
    1. Installs Go tools (`make init`)
    2. Generates V1-only permissions for stage and prod
    3. Converts configs to OpenShift ConfigMaps (`kubectl create configmap --dry-run`)
    4. Compiles KSL schemas to `schema.zed` (both environments)
    5. Validates SpiceDB schemas
    6. Pushes results to `configmaps-schema` branch (signed commit)
    7. Creates automated PR from `configmaps-schema` → `master` (must also be merged)
- **Key GitHub Actions used**:
  - `RedHatInsights/rbac-config-actions/generate-v1-only-permissions@main` — generates `rbac_v1_permissions.json` from permissions + `.lst` files
  - `RedHatInsights/rbac-config-actions/validate-schema@main` — compiles KSL and validates
  - `RedHatInsights/rbac-config-actions/validate-permission-dependencies@main` — checks `requires` field integrity
  - `authzed/action-spicedb-validate@v1` — validates generated `schema.zed` against SpiceDB

### Schema Build Pipeline

The config-to-deployment pipeline has multiple stages:

```
permissions/*.json + *.lst files
       ↓
generate-v1-only-permissions
       ↓
rbac_v1_permissions.json (intermediate, gitignored)
       ↓
ksl (KSL compiler) + *.ksl source files
       ↓
schema.zed (SpiceDB authorization schema)
       ↓
authzed/action-spicedb-validate (validation)
       ↓
ConfigMaps (kubectl --dry-run) → configmaps-schema branch → merge PR
       ↓
app-interface ref bump → deployment
```

### KSL (Kessel Schema Language)

KSL files define the V2 authorization model for SpiceDB. Key concepts:

- **Namespaces**: one `.ksl` file per app (e.g., `advisor.ksl`, `compliance.ksl`)
- **`rbac.ksl`**: core namespace defining `workspace`, `role`, `role_binding`, `tenant`, `group`, `principal` types
- **Macros** (defined in `rbac.ksl`, used everywhere):
  - `@rbac.add_v1_based_permission(app, resource, verb, v2_perm)` — maps a V1 permission to a V2 relation
  - `@rbac.add_contingent_permission(first, second, contingent)` — creates intersection-based permissions (e.g., "can view advisor results IF has inventory host view")
  - `@rbac.add_v1only_permission(perm)` — deprecated V1-only permission (no V2 equivalent)
  - `@rbac.add_unified_permission(app, resource, verb)` — V2 permission with auto-generated name
- **Naming**: hyphens in app/resource names become underscores in KSL (e.g., `malware-detection` → `malware_detection`)
- **`migrated_apps.lst`**: apps that have fully migrated to V2 authorization — stage and prod can differ
- **`hostsonly_apps.lst`**: apps with host-scoped permissions only

### Code Conventions

- **Stage-first**: all changes go to `configs/stage/` first, promote to `configs/prod/` after validation. Stage and prod can intentionally diverge.
- **Permission files** (`configs/*/permissions/*.json`): 4-space indentation. Filename = app namespace in `app:resource:verb` triple.
- **Role files** (`configs/*/roles/*.json`): 2-space indentation. Must have `roles` array with objects containing `name`, `display_name`, `description`, `system`, `version`, `access`.
- **Version bumping is mandatory**: the `version` integer triggers re-seeding. If you change a role and don't increment `version`, the change is silently ignored in production. New roles start at version 2+.
- **`name` field is immutable**: it's the database primary key. Changing it creates a duplicate. Use `display_name` to change what users see.
- **Permission/role files are NOT 1:1**: some apps have permissions but no roles (consumed by other files). Some role files reference permissions from many apps (e.g., `rhel.json`).
- **Wildcard `"*"` resource**: most apps need `"*": [{"verb": "*"}]` for admin roles referencing `app:*:*`.

### Common Pitfalls

- Forgetting to bump `version` — the most common mistake. Changes won't take effect.
- Renaming `name` instead of `display_name` — creates ghost duplicate in DB.
- Editing `schema.zed` directly — it gets overwritten by CI. Edit `.ksl` source files instead.
- Using `make ksl-schema-*` locally instead of `make ksl-test-schema-*` — overwrites committed files.
- Changing only one environment — if the change applies to both, update both stage and prod.
- Missing wildcard `"*"` resource in permissions file — admin roles referencing `app:*:*` won't resolve.
- Editing `rbac_v1_permissions.json` — it's a build artifact, gitignored.

### Testing Patterns

- Run `make ksl-test-schema-stage` and `make ksl-test-schema-prod` locally before PRing.
- Verify `make init` succeeds (requires Go and network access to install tools).
- JSON schema validation: `configs/*/permissions/*.json` must match `schemas/permissions.schema`, roles must match `schemas/roles.schema`.
- SpiceDB validation happens automatically in CI via `authzed/action-spicedb-validate`.

### Deployment Flow

1. PR against `master` — CI validates schemas for both environments.
2. After merge — automated workflow generates ConfigMaps and creates PR from `configmaps-schema` branch.
3. That automated PR must also be merged.
4. A separate MR in `app-interface` (GitLab) bumps the `ref` to trigger actual deployment.
5. Stage promoted Tuesdays, prod promoted Thursdays (review same day).
