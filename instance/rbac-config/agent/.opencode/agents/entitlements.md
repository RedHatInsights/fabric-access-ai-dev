---
description: Implements entitlements-api-go changes in the Go subscription and compliance proxy service.
mode: subagent
model: rehor-openai/gpt-6-luna
request:
  body:
    reasoning:
      effort: medium
permissions:
  - { action: subagent, resource: "*", effect: deny }
---

## entitlements-api-go Guidelines

You are working on **entitlements-api-go**, a Go service proxying subscription,
compliance, and entitlement checks to backend Red Hat IT services. It serves
`/subscriptions`, `/compliance`, and the obsolete `/seats` endpoints.

- Requires Go 1.26.5: `goenv shell 1.26.5` before building or testing. Run
  `bash scripts/setup-entitlements-env.sh <repo-path>` (from this instance's
  `agent/scripts/`) for automated environment setup.
- `make generate` regenerates `api/types.gen.go` and `api/server.gen.go`; MUST
  run after cloning and after any change to `apispec/api.spec.json`. Never edit
  or commit `*.gen.go` — they are gitignored and regenerated.
- `make build` builds `entitlements-api-go` and `bundle-sync`;
  `make test` runs `make generate` then `go test -v ./...`;
  `make test-all` adds `--race -p 1` with coverage (what CI runs);
  `make debug-run` runs with mock AMS/BOP clients (`ENT_DEBUG=1`).
- Layout: `main.go`, `ams/`, `api/` (generated), `apispec/`, `bop/`,
  `bundle_sync/` (CLI syncing bundle SKU config to Feature Service),
  `bundles/` (`bundles.yml` is gitignored; only `bundles.example.yml` is
  committed), `config/` (Viper), `controllers/`, `server/` (chi), `types/`,
  `test_data/`.
- Conventions: Viper config with `ENT_` prefix (`GLITCHTIP_DSN` exception via
  `os.Getenv`); singleton patterns (`config.GetConfig()`, `logger.InitLogger()`,
  `controllers.getClient()`); interfaces named by role (`AMSInterface`, `Bop`);
  import aliases `l`, `v1`, `ocmErrors`; `fmt.Errorf` with `%w` for wrapping;
  reuse `controllers.getClient()` instead of new `http.Client` instances.
- Pitfalls: missing `make generate` breaks tests; mutating package-level vars
  after startup (`bundleInfo`, `featuresQuery`, `paidFeatureSuffix`, HTTP client
  singleton) causes races; `idObj.User` can be nil for Service Accounts —
  nil-check before `User.*`; seats API is obsolete
  (`DisableSeatManager: true`) — do not enable it.
- Tests use Ginkgo/Gomega; interface mocks and function-variable mocks
  coexist; `MakeRequest` helper uses functional options; `toPtr[T]` converts
  to pointers for generated API types.
- CI: Konflux/Tekton is the primary gate (`make generate` + `make test-all`
  with race detector); GitHub Actions validates JSON/YAML and scans images.
