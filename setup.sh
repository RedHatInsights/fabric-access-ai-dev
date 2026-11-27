#!/bin/bash
set -e

echo "fabric-access-ai-dev" > /home/botuser/app/.instance-id

# Bake in Redis and PostgreSQL for on-demand startup, not at image build or boot.
# Keep libpq-devel for psycopg2; nss_wrapper supports initdb under unmapped UIDs.
ubi_dnf=(dnf --disableplugin=subscription-manager --disablerepo='*' --enablerepo='ubi-*')
"${ubi_dnf[@]}" -y module enable redis:7
"${ubi_dnf[@]}" -y --nodocs --setopt=install_weak_deps=False install \
    redis nss_wrapper libpq-devel openssl-devel libicu-devel zlib-devel \
    gcc make pkgconf-pkg-config tar gzip
"${ubi_dnf[@]}" clean all

# UBI does not publish a PostgreSQL 16 server RPM. Verify the upstream release
# archive before building the server and clients into an isolated prefix.
(
    pg_version=16.16
    pg_sha256=3e930f3366f675ab26e873d16201201f8e9b6d9bc13686e4465380271145ea2d
    pg_archive="postgresql-$pg_version.tar.gz"
    pg_prefix=/opt/rbac-postgres
    build_dir=$(mktemp -d)
    trap 'rm -rf -- "$build_dir"' EXIT
    cd "$build_dir"
    curl --fail --silent --show-error --location --retry 3 \
        --proto '=https' --proto-redir '=https' --output "$pg_archive" \
        "https://ftp.postgresql.org/pub/source/v$pg_version/$pg_archive"
    printf '%s  %s\n' "$pg_sha256" "$pg_archive" | sha256sum --check --strict
    tar -xzf "$pg_archive"
    cd "postgresql-$pg_version"
    # UBI does not ship readline-devel; psql still supports noninteractive use.
    ./configure --prefix="$pg_prefix" --with-openssl --without-readline
    make -j"$(nproc)"
    make install
    # insights-rbac migration management.0084 enables pg_trgm for GIN indexes.
    make -C contrib/pg_trgm -j"$(nproc)"
    make -C contrib/pg_trgm install
    for bin in postgres initdb pg_ctl pg_isready psql pg_dump pg_dumpall pg_restore createdb dropdb createuser dropuser; do
        ln -sfn "$pg_prefix/bin/$bin" "/usr/local/bin/$bin"
    done
)

# pipenv — RBAC project uses pipenv for virtualenv and dependency management
pip3.12 install --no-cache-dir pipenv

# entitlements-api-go requires Go 1.26.5 (goenv preset installs 1.24.2 + 1.25.7)
export GOENV_ROOT="${GOENV_ROOT:-/usr/local/goenv}"
export PATH="$GOENV_ROOT/bin:$PATH"
eval "$(goenv init -)"
goenv install 1.26.5

echo "Instance setup complete: fabric-access-ai-dev"
