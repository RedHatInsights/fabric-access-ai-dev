#!/bin/bash
# Idempotent RBAC environment setup — run before working on insights-rbac.
# Usage: setup-rbac-env.sh [path/to/insights-rbac]
#
# Handles: postgres/redis sidecar health checks, .env creation, pipenv install,
# migrations, Celery worker (background daemon).
# Ends with a validation summary so failures are immediately visible in output.
# Safe to run multiple times (skips steps already done).

set -euo pipefail

REPO_DIR="$(cd "${1:-.}" && pwd)"
cd "$REPO_DIR"

CELERY_PID=/tmp/celery-rbac-worker.pid
CELERY_LOG=/tmp/celery-rbac-worker.log

# ─── helpers ──────────────────────────────────────────────────────────────────

PASS=0; WARN=0; FAIL=0

ok()   { echo "  [OK]   $*"; PASS=$((PASS+1)); }
warn() { echo "  [WARN] $*"; WARN=$((WARN+1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

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

# ─── 2. Wait for redis sidecar (up to 30s) ────────────────────────────────────

echo "[rbac-setup] Waiting for Redis sidecar at localhost:6379..."
for i in $(seq 1 15); do
    if python3 -c "import socket; s=socket.create_connection(('localhost',6379),1); s.close()" 2>/dev/null; then
        break
    fi
    if [ "$i" -eq 15 ]; then
        echo "[rbac-setup] ERROR: Redis sidecar not ready after 30s. Aborting." >&2
        exit 1
    fi
    sleep 2
done

# ─── 3. Ensure .env exists with sidecar DATABASE_* values ─────────────────────

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

# ─── 4. Install Python dependencies ───────────────────────────────────────────

echo "[rbac-setup] Installing Python dependencies..."
PIPENV_VERBOSITY=-1 pipenv install --dev

# ─── 5. Apply pending migrations ──────────────────────────────────────────────

echo "[rbac-setup] Checking migrations..."
if ! DJANGO_READ_DOT_ENV_FILE=True pipenv run python rbac/manage.py migrate --check 2>/dev/null; then
    echo "[rbac-setup] Applying pending migrations..."
    DJANGO_READ_DOT_ENV_FILE=True pipenv run python rbac/manage.py migrate
fi

# ─── 6. Start Celery worker (background daemon, idempotent) ───────────────────

celery_running() {
    [ -f "$CELERY_PID" ] && kill -0 "$(cat "$CELERY_PID")" 2>/dev/null
}

if celery_running; then
    echo "[rbac-setup] Celery worker already running (pid $(cat "$CELERY_PID"))."
else
    echo "[rbac-setup] Starting Celery worker..."
    mkdir -p /tmp/prometheus_multiproc
    # Run from rbac/ subdir — matches docker-compose working_dir: /opt/rbac/rbac
    # pipenv searches Pipfile up the tree so it still finds $REPO_DIR/Pipfile
    (
        cd "$REPO_DIR/rbac"
        DJANGO_READ_DOT_ENV_FILE=True \
        PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus_multiproc \
        pipenv run \
            celery --broker=redis://localhost:6379/0 -A rbac.celery worker \
                --loglevel=INFO \
                --pidfile="$CELERY_PID" \
                --logfile="$CELERY_LOG" \
                --detach
    )
    # Poll for PID file (up to 15s) instead of fixed sleep
    for i in $(seq 1 15); do
        celery_running && break
        sleep 1
    done
    if celery_running; then
        echo "[rbac-setup] Celery worker started (pid $(cat "$CELERY_PID"))."
    else
        echo "[rbac-setup] ERROR: Celery worker failed to start. Last log lines:" >&2
        tail -20 "$CELERY_LOG" >&2 2>/dev/null || true
        exit 1
    fi
fi

# ─── 7. Validation summary ────────────────────────────────────────────────────

echo ""
echo "┌─────────────────────────────────────────────────────────────┐"
echo "│  RBAC Environment Validation                                │"
echo "└─────────────────────────────────────────────────────────────┘"

# Python version
PY_VER=$(PIPENV_DONT_LOAD_ENV=1 PIPENV_VERBOSITY=-1 \
    pipenv run python -c "import sys; print(sys.version.split()[0])" 2>/dev/null || echo "unknown")
if [[ "$PY_VER" == 3.12* ]]; then
    ok "Python          $PY_VER"
else
    fail "Python          $PY_VER (expected 3.12.x)"
fi

# Key Python packages — format: "import_name:pip_name"
PKG_CHECKS=(
    "django:django"
    "psycopg2:psycopg2"
    "rest_framework:djangorestframework"
    "celery:celery"
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
        ok "pkg: ${pip_name}       $VER"
    else
        fail "pkg: ${pip_name}       not installed"
    fi
done

# PostgreSQL connectivity — actual query, not just port check
PG_VER=$(PGPASSWORD=postgres psql -h localhost -p 15432 -U postgres -d postgres \
    -tAc "SELECT version();" 2>/dev/null | head -1 || true)
if [[ "$PG_VER" == *PostgreSQL* ]]; then
    PG_SHORT=$(echo "$PG_VER" | grep -oE 'PostgreSQL [0-9]+\.[0-9]+')
    ok "PostgreSQL      $PG_SHORT  (localhost:15432)"
else
    fail "PostgreSQL      cannot connect to localhost:15432"
fi

# Migration status
MIGRATION_OUT=$(DJANGO_READ_DOT_ENV_FILE=True \
    pipenv run python rbac/manage.py showmigrations 2>/dev/null || true)
# showmigrations lines have a leading space: " [X] name" — no ^ anchor
PENDING=$(echo "$MIGRATION_OUT" | grep -c " \[ \]" || true)
TOTAL=$(echo "$MIGRATION_OUT"   | grep -cE " \[.?\]" || true)
if [ "$PENDING" -eq 0 ]; then
    ok "Migrations      $TOTAL/$TOTAL applied"
else
    fail "Migrations      $PENDING pending out of $TOTAL (run make run-migrations)"
fi

# Django system check
DJANGO_CHECK=$(DJANGO_READ_DOT_ENV_FILE=True \
    pipenv run python rbac/manage.py check 2>&1 | tail -1 || true)
if echo "$DJANGO_CHECK" | grep -q "no issues"; then
    ok "Django check    $DJANGO_CHECK"
else
    fail "Django check    $DJANGO_CHECK"
fi

# Redis connectivity
if python3 -c "import socket; s=socket.create_connection(('localhost',6379),1); s.close()" 2>/dev/null; then
    ok "Redis           localhost:6379"
else
    fail "Redis           localhost:6379  not reachable"
fi

# Celery worker — PID alive + inspect ping (confirms broker connection)
if celery_running; then
    CELERY_PID_VAL=$(cat "$CELERY_PID")
    # inspect ping returns pong if worker is connected to broker; timeout avoids hangs
    # timeout is 'gtimeout' on macOS (brew coreutils); fall back to no timeout
    TIMEOUT_BIN=$(command -v timeout || command -v gtimeout || true)
    PING=$(cd "$REPO_DIR/rbac" && \
        DJANGO_READ_DOT_ENV_FILE=True \
        ${TIMEOUT_BIN:+$TIMEOUT_BIN 10} pipenv run celery --broker=redis://localhost:6379/0 \
            -A rbac.celery inspect ping 2>/dev/null || true)
    if echo "$PING" | grep -q "pong"; then
        ok "Celery worker   pid=$CELERY_PID_VAL  broker=connected"
    else
        warn "Celery worker   pid=$CELERY_PID_VAL  broker=not yet responding (still starting?)"
    fi
else
    fail "Celery worker   not running (pid file missing or process dead)"
fi

# .env sanity — DATABASE_PORT must be 15432
ENV_PORT=$(grep "^DATABASE_PORT=" .env 2>/dev/null | cut -d= -f2 || echo "missing")
if [ "$ENV_PORT" = "15432" ]; then
    ok ".env            DATABASE_PORT=$ENV_PORT"
else
    fail ".env            DATABASE_PORT=$ENV_PORT (expected 15432)"
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
    echo "[rbac-setup] Ready (with warnings)."
else
    echo "[rbac-setup] All checks passed. RBAC environment ready."
fi
