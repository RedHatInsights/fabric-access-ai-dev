---
description: Implements uhc-auth-proxy changes in the Go cluster-operator authentication proxy.
mode: subagent
model: rehor-openai/gpt-6-luna
request:
  body:
    reasoning:
      effort: medium
permissions:
  - { action: subagent, resource: "*", effect: deny }
---

## uhc-auth-proxy Guidelines

You are working on **uhc-auth-proxy**, a Go service authenticating OpenShift
cluster operators against the UHC account management API. It validates
operator user-agent strings and bearer tokens, caches identities in memory,
and returns identity JSON for the Hybrid Cloud Console.

- Requires Go 1.26.x (`goenv shell 1.26.2` or any compatible 1.26.x; 1.26.5 is
  installed). No Makefile — use Go commands directly:
  `go build ./...`, `go test -v -race ./...` (CI parity), and
  `golangci-lint run` (matches GitHub Actions).
- Layout: `main.go` → `cmd/` (Cobra root, `start` HTTP server, `run` CLI
  one-shot), `server/` (chi router, middleware, `RootHandler`, Prometheus
  counters), `cache/` (in-memory TTL cache, 2-hour expiry, mutex-guarded),
  `requests/client/` (`HTTPWrapper`/`Wrapper` interface for all outbound HTTP,
  SSO token refresh, config), `requests/cluster/` (identity/account facade,
  types, test fakes), `logger/` (zap JSON logger with optional CloudWatch tee).
- Conventions: Viper with `AutomaticEnv()`, defaults in package-level `init()`
  functions, no `.env` files. Package names short/lowercase/singular; JSON
  field names `snake_case` matching the UHC API; Prometheus metrics prefixed
  `uhc_auth_proxy_`; import alias `l` for logger in `server`; `fmt.Errorf`
  with `%w` for wrapping; reuse the shared `client` singleton — never call
  `http.DefaultClient` directly. The `client.Wrapper` interface is the seam
  for all outbound HTTP (production `HTTPWrapper`; tests `FakeWrapper`,
  `ErrorWrapper`, `ErrorWithBodyWrapper` from `requests/cluster/types.go`).
- Pitfalls: forgetting `cache.Clear()` in test `BeforeEach` pollutes state;
  `operatorPrefixes` in `server.go` and `validOperatorAgents` in tests must
  stay in sync; `wrapper.Do` dereferences `resp.StatusCode` before the
  `err != nil` check — a nil `resp` panics; Viper `init()` ordering means
  non-default values in tests must use environment variables; never rename
  the `Identity` struct's `snake_case` JSON tags — downstream consumers depend
  on them.
- Tests use Ginkgo v2/Gomega with per-package `*_suite_test.go` bootstrap;
  `gostub` stubs package-level variables.
- Architecture: stateless except the in-memory cache (2-hour TTL) — pod
  restarts clear it. `RootHandler` validates user-agent and bearer token →
  checks cache → calls UHC API on miss → caches and returns identity JSON.
  Adding a new operator requires updating the `operatorPrefixes` allowlist.
- CI: Konflux/Tekton builds the image and runs `go test -v -race ./...` via
  `konflux_unit_test.sh`; GitHub Actions runs golangci-lint, JSON/YAML
  validation, and Grype/Syft scans.
