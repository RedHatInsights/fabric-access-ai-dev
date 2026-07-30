## Turnpike Guidelines

You are working on **Turnpike**, a Python/Flask reverse-proxy authentication gateway for the Hybrid Cloud Console. It sits behind nginx (`auth_request`) and evaluates a plugin chain to authenticate and authorize every inbound request — supporting SAML, OIDC, x509 client certificates, VPN restrictions, source-IP allowlists, and Red Hat identity injection.

### Project Structure

```
turnpike/
  __init__.py              # Flask app factory create_app(), blueprint registration, plugin chain init
  config.py                # Env-var based config, Redis session/cache, SSO OIDC settings
  plugin.py                # PolicyContext dataclass, TurnpikePlugin / TurnpikeAuthPlugin base classes
  cache.py                 # Flask-Caching instance (Redis-backed)
  metrics.py               # Prometheus counter for request counts
  plugins/
    auth.py                # AuthPlugin — orchestrates AUTH_PLUGIN_CHAIN (OIDC, Registry, SAML, x509)
    vpn.py                 # VPNPlugin — enforces VPN for private backends via edge-host header
    source_ip.py           # SourceIPPlugin — IP allowlist enforcement
    rh_identity.py         # RHIdentityPlugin — injects x-rh-identity header
    saml.py                # SAMLAuthPlugin — python3-saml integration
    x509.py                # X509AuthPlugin — client certificate authentication
    registry.py            # RegistryAuthPlugin — container registry auth
    oidc/                  # OIDCAuthPlugin — OpenID Connect with service account scopes
    common/
      header_validator.py  # Edge-host header validation helpers
  views/
    views.py               # policy_view (nginx auth_request), identity, session, nginx_config_data
    saml/                  # SAML ACS, login, logout, metadata, mock assertion views
```

### Build and Test

- **Python version**: 3.11 (from Pipfile)
- **Package manager**: Pipfile / pipenv
- **`pipenv install --dev`** — Install all dependencies including dev tools.
- **`pipenv run pytest`** — Run the test suite.
- **`pipenv run black --check .`** — Check code formatting.
- **`pipenv run mypy`** — Type checking.
- **`pipenv run pre-commit run --all-files`** — Run all pre-commit hooks.

### CI Pipeline

- **Konflux/Tekton** (`.tekton/`): 6 pipelines for three components (web, nginx, nginx-prometheus), each with pull-request and push variants.
- **GitHub Actions**:
  - `pre-commit.yml` — runs pre-commit hooks on every PR
  - `labeler.yml` — auto-labels PRs
  - `json-yaml-validation.yml` — validates JSON/YAML files
  - `quay-image-check.yml` — checks Quay container image
  - `security-workflow-template.yml` — Grype/Syft vulnerability scan

### Code Conventions

- All configuration via environment variables, loaded in `config.py`. Required: `SECRET_KEY`, `SSO_OIDC_HOST`, `SSO_OIDC_PORT`, `SSO_OIDC_PROTOCOL_SCHEME`, `SSO_OIDC_REALM`, `BACKENDS_CONFIG_MAP`.
- Plugin chain pattern: `PLUGIN_CHAIN` list (VPN → Auth → SourceIP → RHIdentity) and `AUTH_PLUGIN_CHAIN` sub-chain (OIDC → Registry → SAML → x509).
- Plugins are loaded dynamically via `importlib.import_module` from dotted class paths.
- `PolicyContext` dataclass flows through the chain — plugins set `status_code` to short-circuit, or `auth`/`headers` to pass data downstream.
- Redis for both session storage (`flask-session`) and caching (`flask-caching`).
- Code formatting: `black` (line length default). Type checking: `mypy`.
- Tests use both `unittest.TestCase` and plain `pytest` functions — follow whichever style the test file already uses.
- Use `unittest.mock.patch` for mocking Flask `request` and external dependencies.

### Common Pitfalls

- `create_app()` requires `BACKENDS` in config or it raises `NotImplementedError`. Tests must provide a backends list or a valid backends YAML file path.
- Each test needs a unique `APP_NAME` (use `uuid.uuid4()`) to avoid Prometheus metric registration collisions across test cases.
- `CACHE_TYPE` must be set to `SimpleCache` in tests (not `RedisCache`) to avoid needing a Redis connection.
- SAML views require `xmlsec1` system dependency — tests that touch SAML may fail without it.
- The `WEB_ENV` config controls edge-host header validation behavior — "production" vs "stage" changes which hosts are considered valid for VPN checks.

### Testing Patterns

- Tests use `unittest.TestCase` with `unittest.mock` for mocking.
- Test config pattern: build a `test_config` dict with all required Flask config keys, pass to `create_app(test_config)`.
- Backend fixtures: define `default_backend` dicts with `name`, `origin`, `private`, `route`, and optional `auth` keys.
- Mock `request.headers` via `mock.patch("turnpike.plugins.<module>.request", request_mock)` to simulate incoming requests.
- `assertLogs` context manager used to verify plugin logging behavior.
- Backend config files for edge cases stored in `tests/backends/invalid-configs/`.

### Architecture

- **nginx auth_request flow**: nginx receives a request → sends a subrequest to Turnpike's `/auth/` endpoint → Turnpike runs the plugin chain → returns a status code (200 = allow, 401/403 = deny) and optional headers.
- **Plugin chain**: sequential processing — each plugin can set `context.status_code` to short-circuit the chain, or set `context.headers` to forward headers upstream.
- **Auth sub-chain**: if a backend requires `auth`, the `AuthPlugin` delegates to the `AUTH_PLUGIN_CHAIN` — first plugin to set `context.auth` wins.
- **Stateless** except for Redis-backed session and cache. Pod restart loses in-memory state but Redis persists sessions.
