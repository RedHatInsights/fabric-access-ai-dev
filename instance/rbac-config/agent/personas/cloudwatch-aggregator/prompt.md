## CloudWatch Aggregator Guidelines

You are working on **cloudwatch-aggregator**, a Python/Flask REST API service that manages batch logging to AWS CloudWatch and Splunk. It accepts log data via HTTP POST requests with JSON payloads and forwards them to configured logging platforms in a non-blocking manner.

### Project Structure

```
cloudwatch-aggregator/
  app/
    __init__.py          # Flask app creation, strict_slashes disabled
    log.py               # Routes (/ping, /log/<stream>), CloudWatch/Splunk handler setup, Cache class
    utils.py             # truthy_string() helper
  run-server.sh          # Entrypoint — Flask dev server or gunicorn with gevent workers
  Dockerfile             # UBI Python 3.13, pipenv system install
  Pipfile                # Dependencies: flask, watchtower, gunicorn, gevent, splunk-handler, app-common-python
  docker-compose.yml     # Local dev setup
  scripts/
    build/               # Build scripts
    run/                 # Run scripts
  templates/             # Template files
```

### Build and Test

- **Python version**: 3.13 (from Pipfile)
- **Package manager**: Pipfile / pipenv
- **`pipenv install --dev`** — Install all dependencies including dev tools.
- **No test suite exists yet** — this repo has no `tests/` directory and no test framework in dev dependencies.
- **`pipenv run pre-commit run --all-files`** — Run all pre-commit hooks.
- **Local dev**: `FLASK_ENV=development flask run --host=0.0.0.0 --reload`
- **Production**: `gunicorn -w 4 -k gevent -b 0.0.0.0:$PUBLIC_PORT app:app`

### CI Pipeline

- **Konflux/Tekton** (`.tekton/`): 4 pipelines — `cloudwatch-aggregator` (pull-request + push) and `cloudwatch-aggregator-sc` (pull-request + push).
- **GitHub Actions**:
  - `pre-commit.yml` — runs pre-commit hooks on every PR
  - `labeler.yml` — auto-labels PRs
  - `json-yaml-validation.yml` — validates JSON/YAML files
  - `quay-image-check.yml` — checks Quay container image
  - `security-workflow-template.yml` — Grype/Syft vulnerability scan

### Code Conventions

- Configuration via environment variables. Clowder-aware: uses `app-common-python` `LoadedConfig` when `isClowderEnabled()` is true, falls back to env vars otherwise.
- Required env vars for CloudWatch: `AWS_LOG_GROUP`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION_NAME`.
- Feature flags via env vars: `LOG_TO_CLOUDWATCH`, `LOG_TO_SPLUNK` — evaluated with `truthy_string()` (accepts "true" or "1").
- `CLOUD_WATCH_ALLOWED_STREAMS` — comma-separated allowlist of valid log stream names.
- Code formatting: `pre-commit` hooks configured.
- Small codebase — all application logic lives in three files under `app/`.

### Common Pitfalls

- `Cache.allowed_streams` is set at module import time from `CLOUD_WATCH_ALLOWED_STREAMS`. Changing the env var after import has no effect.
- `boto_client` is created at module level when `LOG_TO_CLOUDWATCH` is true — tests that import `app.log` without mocking boto3 will fail if AWS credentials are not set.
- The `log_stream` parameter in the `/log/<log_stream>` route is validated against the allowlist — requests to unlisted streams get a 403.
- Thread safety: `Cache.lock` guards handler creation to avoid duplicate handlers per stream.

### Testing Patterns

- **No existing tests.** When adding tests, use `pytest` with Flask's test client (`app.test_client()`).
- Mock `boto3.Session` and `SplunkHandler` to avoid external service dependencies.
- Use `unittest.mock.patch.dict(os.environ, ...)` to control env-var-based configuration.
- The `truthy_string()` utility in `app/utils.py` is a good candidate for initial unit tests — it's pure logic with no external dependencies.

### Architecture

- **Stateless logging proxy**: receives log data via REST, forwards to CloudWatch and/or Splunk.
- **Handler caching**: `Cache.active_stream_handlers` dict keeps one logger per log stream, creating CloudWatch/Splunk handlers on first use. Thread-safe via `Cache.lock`.
- **Clowder integration**: `app-common-python` provides AWS credentials and config when running in the Hybrid Cloud Console platform.
- **Non-blocking**: gunicorn with gevent workers handles concurrent requests without blocking on log delivery.
