---
description: Implements Turnpike changes in the Python/Flask reverse-proxy authentication gateway.
mode: subagent
model: rehor-openai/gpt-6-luna
request:
  body:
    reasoning:
      effort: medium
permissions:
  - { action: subagent, resource: "*", effect: deny }
---

## Turnpike Guidelines

You are working on **Turnpike**, a Python 3.11/Flask (pipenv) reverse-proxy
authentication gateway behind nginx `auth_request`. It evaluates a plugin
chain (SAML, OIDC, x509 client certificates, VPN restrictions, source-IP
allowlists, Red Hat identity injection) for every inbound request.

- Layout: `turnpike/__init__.py` (app factory, blueprint registration, plugin
  chain init), `config.py` (env-var config, Redis session/cache, SSO OIDC),
  `plugin.py` (`PolicyContext` dataclass, `TurnpikePlugin`/
  `TurnpikeAuthPlugin` base classes), `cache.py`, `metrics.py`, `plugins/`
  (`auth.py` orchestrates `AUTH_PLUGIN_CHAIN`; `vpn.py`, `source_ip.py`,
  `rh_identity.py`, `saml.py`, `x509.py`, `registry.py`, `oidc/`,
  `common/header_validator.py`), `views/` (`views.py` policy/identity/session,
  `saml/` ACS/login/logout/metadata/mock views).
- Build/test: `pipenv install --dev`; `pipenv run pytest`; `pipenv run black
  --check .`; `pipenv run mypy`; `pipenv run pre-commit run --all-files`.
- Conventions: all config via env vars in `config.py` (required: `SECRET_KEY`,
  `SSO_OIDC_HOST`, `SSO_OIDC_PORT`, `SSO_OIDC_PROTOCOL_SCHEME`,
  `SSO_OIDC_REALM`, `BACKENDS_CONFIG_MAP`). Plugin chains: `PLUGIN_CHAIN`
  (VPN → Auth → SourceIP → RHIdentity) and `AUTH_PLUGIN_CHAIN` (OIDC →
  Registry → SAML → x509); plugins loaded dynamically via
  `importlib.import_module`; a plugin short-circuits by setting
  `context.status_code` or passes data via `context.auth`/`context.headers`.
  Redis backs both sessions (`flask-session`) and caching (`flask-caching`).
  Follow the test style (unittest.TestCase or plain pytest) already used by
  each file; mock Flask `request` and external deps with `unittest.mock.patch`.
- Pitfalls: `create_app()` requires `BACKENDS` in config or raises
  `NotImplementedError` — tests must provide a backends list or YAML path;
  each test needs a unique `APP_NAME` (`uuid.uuid4()`) to avoid Prometheus
  metric registration collisions; `CACHE_TYPE` must be `SimpleCache` in tests;
  SAML views need the `xmlsec1` system dependency; `WEB_ENV` ("production" vs
  "stage") controls edge-host header validation for VPN checks.
- Test patterns: build a `test_config` dict for `create_app(test_config)`;
  `default_backend` fixtures with `name`, `origin`, `private`, `route`, and
  optional `auth` keys; mock request headers via
  `mock.patch("turnpike.plugins.<module>.request", request_mock)`;
  `assertLogs` for plugin logging; invalid backend configs in
  `tests/backends/invalid-configs/`.
- CI: Konflux/Tekton (6 pipelines for web/nginx/nginx-prometheus) plus GitHub
  Actions pre-commit, labeler, JSON/YAML validation, Quay image check, and
  Grype/Syft scans.
