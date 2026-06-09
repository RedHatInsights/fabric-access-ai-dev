#!/bin/bash
# Idempotent RBAC environment setup — run before working on insights-rbac.
# Usage: setup-rbac-env.sh [path/to/insights-rbac]
#
# Handles: postgres sidecar health check, .env creation, pipenv install, migrations.
# Ends with a validation summary so failures are immediately visible in output.
# Safe to run multiple times (skips steps already done).

set -euo pipefail

REPO_DIR="$(cd "${1:-.}" && pwd)"
cd "$REPO_DIR"

# ─── helpers ──────────────────────────────────────────────────────────────────

PASS=0; WARN=0; FAIL=0

ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

# check_cmd: run a command, return its output; capture exit code without set -e
check() { "$@" 2>&1; }

# ─── 1. Wait for postgres sidecar (up to 60s) ─────────────────────────────────

echo ""
echo "[rbac-setup] Waiting for PostgreSQL sidecar at localhost:15432..."
for i in $(seq 1 30); do
    if pg_isready -h localhost -p 15432 -U postgres -q 2>/dev/null; then
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "[rbac-setup] ERROR: PostgreSQL sidecar not ready after 60s. Aborting." >&2
        exit 1
    fi
    sleep 2
done

# ─── 2. Ensure .env exists with sidecar DATABASE_* values ─────────────────────

if [ ! -f .env ]; then
    cp .env.example .env
    echo "[rbac-setup] Created .env from .env.example"
fi

patch_env() {
    local key="$1" val="$2" tmp
    tmp=$(mktemp)
    if grep -q "^${key}=" .env 2>/dev/null; then
        sed "s|^${key}=.*|${key}=${val}|" .env > "$tmp" && mv "$tmp" .env
    else
        cp .env "$tmp" && printf '%s=%s\n' "$key" "$val" >> "$tmp" && mv "$tmp" .env
    fi
}

patch_env DATABASE_HOST     localhost
patch_env DATABASE_PORT     15432
patch_env DATABASE_USER     postgres
patch_env DATABASE_PASSWORD postgres
patch_env DATABASE_NAME     postgres

# ─── 3. Install Python dependencies ───────────────────────────────────────────

echo "[rbac-setup] Installing Python dependencies..."
PIPENV_VERBOSITY=-1 pipenv install --dev

# ─── 4. Apply pending migrations ──────────────────────────────────────────────

echo "[rbac-setup] Checking migrations..."
if ! DJANGO_READ_DOT_ENV_FILE=True pipenv run python rbac/manage.py migrate --check 2>/dev/null; then
    echo "[rbac-setup] Applying pending migrations..."
    DJANGO_READ_DOT_ENV_FILE=True pipenv run python rbac/manage.py migrate
fi

# ─── 5. Validation summary ────────────────────────────────────────────────────

echo ""
echo "┌─────────────────────────────────────────────────────────────┐"
echo "│  RBAC Environment Validation                                │"
echo "└─────────────────────────────────────────────────────────────┘"

# Python version — suppress pipenv env-loading noise with PIPENV_DONT_LOAD_ENV
PY_VER=$(PIPENV_DONT_LOAD_ENV=1 PIPENV_VERBOSITY=-1 \
    pipenv run python -c "import sys; print(sys.version.split()[0])" 2>/dev/null || echo "unknown")
if [[ "$PY_VER" == 3.12* ]]; then
    ok "Python        $PY_VER"
else
    fail "Python        $PY_VER (expected 3.12.x)"
fi

# Key Python packages — format: "import_name:pip_name"
PKG_CHECKS=(
    "django:django"
    "psycopg2:psycopg2"
    "rest_framework:djangorestframework"
    "tox:tox"
    "coverage:coverage"
)
for entry in "${PKG_CHECKS[@]}"; do
    import_name="${entry%%:*}"
    pip_name="${entry##*:}"
    if PIPENV_DONT_LOAD_ENV=1 PIPENV_VERBOSITY=-1 \
            pipenv run python -c "import ${import_name}" 2>/dev/null; then
        VER=$(PIPENV_DONT_LOAD_ENV=1 PIPENV_VERBOSITY=-1 \
            pipenv run python -c \
            "import importlib.metadata; print(importlib.metadata.version('${pip_name}'))" 2>/dev/null || echo "?")
        ok "pkg: ${pip_name}    $VER"
    else
        fail "pkg: ${pip_name}    not installed"
    fi
done

# PostgreSQL connectivity — actual query to confirm auth works, not just port open
PG_VER=$(PGPASSWORD=postgres psql -h localhost -p 15432 -U postgres -d postgres \
    -tAc "SELECT version();" 2>/dev/null | head -1 || true)
if [[ "$PG_VER" == *PostgreSQL* ]]; then
    # grep -oE is POSIX-compatible (works on macOS BSD grep and Linux GNU grep)
    PG_SHORT=$(echo "$PG_VER" | grep -oE 'PostgreSQL [0-9]+\.[0-9]+')
    ok "PostgreSQL    $PG_SHORT  (localhost:15432)"
else
    fail "PostgreSQL    cannot connect to localhost:15432"
fi

# Migration status
MIGRATION_OUT=$(DJANGO_READ_DOT_ENV_FILE=True \
    pipenv run python rbac/manage.py showmigrations 2>/dev/null || true)
# showmigrations lines have a leading space: " [X] name" — no ^ anchor
PENDING=$(echo "$MIGRATION_OUT" | grep -c " \[ \]" || true)
TOTAL=$(echo "$MIGRATION_OUT"   | grep -cE " \[.?\]" || true)
if [ "$PENDING" -eq 0 ]; then
    ok "Migrations    $TOTAL/$TOTAL applied"
else
    fail "Migrations    $PENDING pending out of $TOTAL (run make run-migrations)"
fi

# Django system check
DJANGO_CHECK=$(DJANGO_READ_DOT_ENV_FILE=True \
    pipenv run python rbac/manage.py check 2>&1 | tail -1 || true)
if echo "$DJANGO_CHECK" | grep -q "no issues"; then
    ok "Django check  $DJANGO_CHECK"
else
    fail "Django check  $DJANGO_CHECK"
fi

# Redis — optional (ACCESS_CACHE_ENABLED=False so tests work without it)
if python3 -c "import socket; s=socket.create_connection(('localhost',6379),1); s.close()" 2>/dev/null; then
    ok "Redis         localhost:6379  (cache + Celery available)"
else
    warn "Redis         localhost:6379  not reachable — cache/Celery disabled, tests still pass"
fi

# .env sanity — DATABASE_PORT must be 15432
ENV_PORT=$(grep "^DATABASE_PORT=" .env 2>/dev/null | cut -d= -f2 || echo "missing")
if [ "$ENV_PORT" = "15432" ]; then
    ok ".env          DATABASE_PORT=$ENV_PORT"
else
    fail ".env          DATABASE_PORT=$ENV_PORT (expected 15432)"
fi

# ─── Result ───────────────────────────────────────────────────────────────────

echo ""
echo "  Passed: $PASS  Warnings: $WARN  Failed: $FAIL"
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "[rbac-setup] FAILED — fix the items above before proceeding." >&2
    exit 1
fi

if [ "$WARN" -gt 0 ]; then
    echo "[rbac-setup] Ready (with warnings — Redis not running, cache/Celery disabled)."
else
    echo "[rbac-setup] All checks passed. RBAC environment ready."
fi
