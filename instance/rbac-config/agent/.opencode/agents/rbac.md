---
description: Implements insights-rbac changes in the Django REST Framework RBAC backend service.
mode: subagent
model: rehor-openai/gpt-6-luna
request:
  body:
    reasoning:
      effort: medium
permissions:
  - { action: subagent, resource: "*", effect: deny }
---

## RBAC Backend (insights-rbac) Guidelines

Django REST Framework microservice providing Role-Based Access Control for
console.redhat.com. Python 3.12, PostgreSQL 16, Redis, Celery.

- Before changes, run the setup script (sidecar health check, `.env`, deps,
  migrations; safe to re-run):
  `/home/botuser/app/instance/rbac-config/agent/scripts/setup-rbac-env.sh ./repos/insights-rbac`.
  Non-zero exit → STOP, post the error output to Jira, do not proceed.
- Multi-tenancy: all business models inherit `TenantAwareModel`; always filter
  by `request.tenant`; never return cross-tenant data.
- Service layer: business logic in `service.py` files, not views or
  serializers. Services raise domain exceptions (plain Python, not DRF);
  serializers convert them to `serializers.ValidationError`.
- Dual API versions: v1 is stable — never modify v1 behavior. v2 uses RFC 7807
  Problem JSON behind `V2_APIS_ENABLED=True`. All v2 views extend `BaseV2ViewSet`
  from `rbac/management/base_viewsets.py`; write operations use
  `AtomicOperationsMixin` — override `perform_atomic_create/update/destroy`,
  never `create`/`update`/`destroy`. Every v2 endpoint needs both a
  `*AccessPermission` class and a `*AccessFilterBackend`; detail views return
  404 (not 403) for inaccessible objects to prevent existence leakage.
- Style: Black (line 119, py312), Flake8 (max 120), PyCharm import order with
  `rbac`/`api` application imports. Format with
  `pipenv run black -t py312 -l 119 rbac tests`. New models use UUID v7 primary
  keys (`uuid_utils.compat.uuid7`); never expose integer primary keys. Conventional
  commits (`fix(scope): lowercase description`); no `Co-Authored-By` lines.
- Testing (mandatory): Django test runner needs dotted module paths.
  `pip install --upgrade pip && pip install tox && tox -r` for the full suite;
  `tox -e py312-fast -- tests.management.workspace.test_view` for a module or
  `...test_view.WorkspaceTestsList.test_workspace_list_unfiltered` for a method.
  Lint with `pipenv run tox -e lint`, `pipenv run black -t py312 -l 119 rbac tests`,
  `pipenv run pre-commit run --all-files`.
- Database: no Docker daemon — Postgres runs as a pod sidecar at
  localhost:15432. `make run-migrations` / `make make-migrations` / `make reinitdb`
  read `.env` from the setup step; tox manages `DATABASE_*` from container env
  directly. Always test migrations against real PostgreSQL; migrations are
  excluded from linting and coverage. Always mock Kafka (`MOCK_KAFKA=True`),
  Kessel Inventory, and the outbox replicator in tests; never mock Django ORM
  queries, serializer validation, or URL routing.
- Documentation: every non-trivial PR must update the repo's canonical docs or
  add a focused page under the existing `docs/` location, in the same PR;
  otherwise explain docs-neutrality in the PR summary.
- Key files: `AGENTS.md` (full domain guideline index), `Makefile`, `tox.ini`,
  `docs/source/specs/typespec/main.tsp`.
