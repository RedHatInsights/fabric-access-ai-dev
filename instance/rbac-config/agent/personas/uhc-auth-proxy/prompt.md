## UHC Auth Proxy (uhc-auth-proxy) Guidelines

You are working on **uhc-auth-proxy**, a Go service that authenticates OpenShift cluster operators against the UHC (Unified Hybrid Cloud) account management API. It validates operator user-agent strings and bearer tokens, caches identities in memory, and returns identity JSON payloads for downstream consumption by the Hybrid Cloud Console.

### Project Structure

```
uhc-auth-proxy/
  main.go                     # Entrypoint — delegates to cmd.Execute()
  cmd/
    root.go                   # Cobra root command, Viper config init
    start.go                  # `start` subcommand — launches the HTTP server
    run.go                    # `run` subcommand — CLI one-shot identity fetch
  server/
    server.go                 # chi router, middleware stack, RootHandler, Prometheus counters
  cache/
    cache.go                  # In-memory TTL cache (2-hour expiry, mutex-guarded)
  requests/
    client/
      wrapper.go              # HTTPWrapper / Wrapper interface — all outbound HTTP
      access.go               # SSO token refresh with mutex-guarded caching
      config.go               # Viper defaults for ACCESS_TOKEN_URL, TIMEOUT_SECONDS
      types.go                # HttpError type
    cluster/
      cluster.go              # GetIdentity / GetCurrentAccount facade
      types.go                # Registration, Account, Identity, test fakes
      config.go               # Viper defaults for UHC API URLs
  logger/
    logger.go                 # zap JSON logger with optional CloudWatch tee
```

### Build and Test

- **Go version**: This repo requires Go 1.26.2. Switch with: `goenv shell 1.26.2` or use any compatible 1.26.x version (1.26.5 is installed). Installed via goenv in `setup.sh`.
- **No Makefile** — build and test with Go commands directly.
- **`go build ./...`** — Builds the binary.
- **`go test -v -race ./...`** — Runs tests with race detector (matches CI).
- **`go test -v -race --coverprofile=coverage.txt --covermode=atomic ./...`** — Full CI test command.
- **`golangci-lint run`** — Lint (matches GitHub Actions).

### CI Pipeline

- **Konflux/Tekton** (`.tekton/`): Primary CI gate. Builds container image and runs `go test -v -race ./...` via `konflux_unit_test.sh`.
- **GitHub Actions** — `golangci-lint`: Runs on every PR. Fix all lint errors before pushing.
- **GitHub Actions** — `json-yaml-validation`: Validates all JSON/YAML files.
- **GitHub Actions** — `security-workflow-template`: Grype/Syft container image vulnerability scan.

### Code Conventions

- All configuration uses Viper with `AutomaticEnv()`. Defaults set in package-level `init()` functions. No `.env` files.
- Package names are short, lowercase, singular (`cache`, `server`, `logger`).
- JSON field names use `snake_case` to match the UHC API.
- Prometheus metric names are prefixed `uhc_auth_proxy_`.
- Import alias: `l` for logger in server package.
- Use `fmt.Errorf` with `%w` for error wrapping.
- Use the shared `client` singleton for HTTP — never call `http.DefaultClient` directly.
- The `client.Wrapper` interface is the seam for all outbound HTTP. Production uses `HTTPWrapper`; tests use `FakeWrapper`, `ErrorWrapper`, or `ErrorWithBodyWrapper` (defined in `requests/cluster/types.go`).

### Common Pitfalls

- Forgetting to call `cache.Clear()` in test `BeforeEach` — stale cache causes test pollution.
- Adding operators to `operatorPrefixes` without updating `validOperatorAgents` in tests — they must stay in sync.
- Nil-pointer in `wrapper.Do` — `resp.StatusCode` is accessed at line 61 before the `err != nil` check. A nil `resp` will panic.
- Viper `init()` ordering — multiple packages set defaults in `init()`. Use environment variables for non-default values in tests.
- JSON field mismatches — the `Identity` struct uses `snake_case` tags that downstream consumers depend on. Do not rename tags.

### Testing Patterns

- Tests use Ginkgo v2 / Gomega framework.
- Test fakes: `FakeWrapper`, `ErrorWrapper`, `ErrorWithBodyWrapper` implement `client.Wrapper` for injection.
- `gostub` library used for stubbing package-level variables in tests.
- Each test package has a `*_suite_test.go` bootstrapping Ginkgo.

### Architecture

- Stateless service — in-memory cache only (2-hour TTL). Pod restart clears all cached identities.
- Data flow: `RootHandler` validates user-agent and bearer token → checks cache → calls UHC API on miss → caches and returns identity JSON.
- `operatorPrefixes` in `server.go` form a user-agent allowlist. Adding a new operator requires updating this array.
