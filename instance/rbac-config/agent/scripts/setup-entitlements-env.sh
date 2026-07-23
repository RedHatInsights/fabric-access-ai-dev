#!/bin/bash
# Idempotent entitlements-api-go environment setup — run before working on entitlements-api-go.
# Usage: setup-entitlements-env.sh [path/to/entitlements-api-go]
#
# Handles: Go version switch, code generation, build validation.
# Safe to run multiple times (skips steps already done).

set -euo pipefail

REPO_DIR="$(cd "${1:-.}" && pwd)"
cd "$REPO_DIR"

GO_VERSION="1.26.5"

# ─── helpers ──────────────────────────────────────────────────────────────────

PASS=0; WARN=0; FAIL=0

ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

# ─── 1. Switch to required Go version ────────────────────────────────────────

echo ""
echo "[entitlements-setup] Switching to Go $GO_VERSION..."
if command -v goenv >/dev/null 2>&1; then
    eval "$(goenv init -)"
    goenv shell "$GO_VERSION"
else
    echo "[entitlements-setup] WARN: goenv not found, assuming Go $GO_VERSION is already on PATH"
fi

# ─── 2. Generate code (oapi-codegen) ─────────────────────────────────────────

echo "[entitlements-setup] Running make generate..."
make generate

# ─── 3. Build ─────────────────────────────────────────────────────────────────

echo "[entitlements-setup] Building..."
make build

# ─── 4. Validation summary ───────────────────────────────────────────────────

echo ""
echo "┌─────────────────────────────────────────────────────────────┐"
echo "│  Entitlements Environment Validation                        │"
echo "└─────────────────────────────────────────────────────────────┘"

# Go version
CURRENT_GO=$(go version 2>/dev/null | grep -oE 'go[0-9]+\.[0-9]+\.[0-9]+' | sed 's/go//' || echo "unknown")
if [[ "$CURRENT_GO" == "$GO_VERSION" ]]; then
    ok "Go version      $CURRENT_GO"
else
    fail "Go version      $CURRENT_GO (expected $GO_VERSION)"
fi

# Generated files
if [ -f api/types.gen.go ] && [ -f api/server.gen.go ]; then
    ok "Generated code  api/*.gen.go present"
else
    fail "Generated code  api/*.gen.go missing (run make generate)"
fi

# Binaries
if [ -f entitlements-api-go ]; then
    ok "Binary          entitlements-api-go built"
else
    fail "Binary          entitlements-api-go not found"
fi

# ─── Result ───────────────────────────────────────────────────────────────────

echo ""
echo "  Passed: $PASS  Warnings: $WARN  Failed: $FAIL"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "[entitlements-setup] FAILED — fix the items above before proceeding." >&2
    exit 1
fi

echo "[entitlements-setup] All checks passed. Entitlements environment ready."
