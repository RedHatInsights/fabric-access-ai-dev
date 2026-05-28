## RBAC Backend Guidelines

Django REST Framework microservice providing Role-Based Access Control for console.redhat.com. Python 3.12, PostgreSQL 16, Redis, Celery.

### Before changes
- Verify the database is running: `pg_isready -h localhost -p 15432`. Fails → STOP, report on Jira, do not proceed.
- `pipenv install --dev` to ensure dependencies are up to date.

### Architecture

- **Multi-tenancy**: All business models inherit `TenantAwareModel`. Always filter queries by `request.tenant`. Never return cross-tenant data.
- **Service layer**: Business logic goes in `service.py` files, not views or serializers. Services raise domain exceptions (plain Python, not DRF). Serializers catch domain exceptions and convert to `serializers.ValidationError`.
- **Dual API versions**: V1 is stable — do not modify v1 API behavior. V2 uses RFC 7807 Problem JSON, requires `V2_APIS_ENABLED=True` feature flag.
- **V2 base class**: All v2 views must extend `BaseV2ViewSet` from `rbac/management/base_viewsets.py`. Write operations must use `AtomicOperationsMixin` — override `perform_atomic_create`/`perform_atomic_update`/`perform_atomic_destroy`, never override `create`/`update`/`destroy` directly.
- **Two-layer access control (v2)**: Every v2 endpoint needs both a `*AccessPermission` class (endpoint-level 403) and a `*AccessFilterBackend` (queryset-level filtering). Detail views return 404 for inaccessible objects to prevent existence leakage.

### Development

- **Code style**: Black (line length 119, target py312). Flake8 (max line length 120).
- Format before committing: `pipenv run black -t py312 -l 119 rbac tests`
- **Import order**: PyCharm style, application imports are `rbac` and `api`.
- **UUIDs**: New models must use UUID v7 as primary key (`uuid_utils.compat.uuid7`). Never expose integer primary keys in APIs.
- **Commit messages**: Conventional commits: `type(scope): short description in lowercase`. Types: `fix`, `feat`, `test`, `refactor`, `style`, `docs`, `chore`. Do NOT include `Co-Authored-By` lines.

### Testing — MANDATORY

Django's test runner requires **dotted module paths**, not file paths.

```bash
# Run all tests (with coverage)
pipenv run tox -e py312

# Run all tests without coverage (faster)
pipenv run tox -e py312-fast

# Run a specific test module
pipenv run tox -e py312-fast -- tests.management.workspace.test_view

# Run a single test method
pipenv run tox -e py312-fast -- tests.management.workspace.test_view.WorkspaceTestsList.test_workspace_list_unfiltered
```

### Linting

```bash
pipenv run tox -e lint                             # flake8 + black --check
pipenv run black -t py312 -l 119 rbac tests        # auto-format
pipenv run pre-commit run --all-files              # full pre-commit suite
```

### What NOT to Do

- Do not modify v1 API behavior — it is stable and widely consumed.
- Do not add v2 routes without checking the `V2_APIS_ENABLED` flag.
- Do not put business logic in serializers or views — use the service layer.
- Do not skip pre-commit hooks or bypass formatting.
- Do not raise DRF exceptions from services — raise domain exceptions instead.
- Do not override `create`/`update`/`destroy` on v2 viewsets using `AtomicOperationsMixin` — override `perform_atomic_*` methods.
- Do not expose integer primary keys in APIs — use UUIDs.
- Do not return 403 for inaccessible v2 detail resources — return 404 to prevent existence leakage.
- Do not mock Django ORM queries, serializer validation, or URL routing in tests.
- Always mock Kafka (`MOCK_KAFKA=True`), Kessel Inventory, and outbox replicator.

### Database

```bash
make start-db           # Postgres container on port 15432
make run-migrations     # Apply migrations
make make-migrations    # Generate new migration files
make reinitdb           # Drop + recreate + migrate
```

- Migrations are excluded from linting and coverage.
- Always test migrations against real PostgreSQL — SQLite is not used.

### Key Files

| File | Purpose |
|------|---------|
| `AGENTS.md` | Full AI agent guidance with domain guideline index |
| `Makefile` | Build, test, migration, and Docker commands |
| `tox.ini` | Test environments, linting config |
| `docs/source/specs/typespec/main.tsp` | TypeSpec source for v2 API contract |
