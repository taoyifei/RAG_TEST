#!/bin/sh
set -eu

# 固定版 WeKnora 读取环境变量；只在容器内从 Docker Secret 加载。
read_secret() {
    path="/run/secrets/$1"
    if [ ! -s "$path" ]; then
        printf '缺少必需 Secret: %s\n' "$1" >&2
        exit 1
    fi
    cat "$path"
}

DB_PASSWORD="$(read_secret db_password)"
REDIS_PASSWORD="$(read_secret redis_password)"
JWT_SECRET="$(read_secret jwt_secret)"
SYSTEM_AES_KEY="$(read_secret system_aes_key)"
SYSTEM_SIGNING_KEY="$(read_secret system_signing_key)"

if [ "${#SYSTEM_AES_KEY}" -ne 32 ]; then
    printf 'system_aes_key 必须为 32 字节 ASCII 文本\n' >&2
    exit 1
fi

export DB_PASSWORD REDIS_PASSWORD JWT_SECRET SYSTEM_AES_KEY SYSTEM_SIGNING_KEY
exec /app/scripts/docker-entrypoint.sh "$@"
