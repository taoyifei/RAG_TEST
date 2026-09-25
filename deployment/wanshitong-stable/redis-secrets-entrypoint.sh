#!/bin/sh
set -eu

if [ ! -s /run/secrets/redis_password ]; then
    printf '缺少必需 Secret: redis_password\n' >&2
    exit 1
fi

password="$(cat /run/secrets/redis_password)"
exec /usr/local/bin/docker-entrypoint.sh redis-server \
    --appendonly yes --requirepass "$password"
