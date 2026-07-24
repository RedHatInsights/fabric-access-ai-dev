#!/bin/bash
set -e

echo "fabric-access-ai-dev" > /home/botuser/app/.instance-id

# RBAC (insights-rbac) runtime dependencies:
#   postgresql  → pg_isready client tool (sidecar health check)
#   libpq-devel → psycopg2 source build (insights-rbac Pipfile uses psycopg2, not binary)
#   openssl-devel → cryptography package compilation
dnf install -y --nodocs postgresql libpq-devel openssl-devel && dnf clean all

# pipenv — RBAC project uses pipenv for virtualenv and dependency management
pip3.12 install --no-cache-dir pipenv

# entitlements-api-go requires Go 1.26.5 (goenv preset installs 1.24.2 + 1.25.7)
export GOENV_ROOT="${GOENV_ROOT:-/usr/local/goenv}"
export PATH="$GOENV_ROOT/bin:$PATH"
eval "$(goenv init -)"
goenv install 1.26.5

echo "Instance setup complete: fabric-access-ai-dev"
