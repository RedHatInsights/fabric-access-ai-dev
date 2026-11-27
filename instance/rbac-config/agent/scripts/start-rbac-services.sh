#!/bin/bash
# Start sandbox-local services on demand; never run from the image entrypoint.
set -euo pipefail
umask 077

root="${BOT_DATA_DIR:-$HOME/data}/rbac-services"
mkdir -p "$root/socket"
exec 9>"$root/start.lock"
flock 9

if ! getent passwd "$(id -u)" >/dev/null; then
    # initdb needs an account name even when the sandbox runs as an arbitrary UID.
    cp /etc/passwd "$root/passwd"
    printf 'rbac-local:x:%s:%s:RBAC sandbox:%s:/bin/bash\n' "$(id -u)" "$(id -g)" "$HOME" >> "$root/passwd"
    export NSS_WRAPPER_PASSWD="$root/passwd" NSS_WRAPPER_GROUP=/etc/group
    export LD_PRELOAD="/usr/lib64/libnss_wrapper.so${LD_PRELOAD:+:$LD_PRELOAD}"
fi

pgdata="$root/postgres"
if ! pg_ctl -D "$pgdata" status >/dev/null 2>&1; then
    if [[ ! -f "$pgdata/PG_VERSION" ]]; then
        initdb -D "$pgdata" -U postgres --encoding=UTF8 --locale=C.utf8 \
            --auth-local=trust --auth-host=scram-sha-256 \
            --pwfile=<(printf '%s\n' postgres) >/dev/null
    fi
    pg_ctl -D "$pgdata" -w -t 60 -l "$root/postgres.log" \
        -o "-p 15432 -k '$root/socket' -c listen_addresses=localhost" start 9>&-
fi
PGPASSWORD=postgres psql -X -h localhost -p 15432 -U postgres -d postgres \
    -v ON_ERROR_STOP=1 -c 'SELECT 1' >/dev/null

redis_ready() { [[ "$(redis-cli -h 127.0.0.1 -p 6379 ping 2>/dev/null)" == PONG ]]; }
if ! redis_ready; then
    redis-server --daemonize yes --bind 127.0.0.1 -::1 --port 6379 \
        --save '' --appendonly no --dir "$root" --pidfile "$root/redis.pid" \
        --logfile "$root/redis.log" 9>&-
    for ((attempt=0; attempt<30; attempt++)); do
        redis_ready && break
        sleep 1
    done
fi
if ! redis_ready; then
    echo "[rbac-services] Redis did not start. See $root/redis.log" >&2
    exit 1
fi
echo '[rbac-services] PostgreSQL and Redis are ready on loopback.'
