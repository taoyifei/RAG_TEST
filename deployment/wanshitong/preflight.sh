#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
env_file="${1:-${script_dir}/.env}"
compose_file="${script_dir}/compose.yaml"

fail() {
  echo "preflight=failed reason=$1" >&2
  exit 1
}

command -v docker >/dev/null || fail "docker-missing"
docker compose version >/dev/null || fail "docker-compose-missing"
[[ -f "${env_file}" && ! -L "${env_file}" ]] || fail "env-file-invalid"

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a
[[ "${WANSHITONG_ROOT:-}" = "/data/tyf/wanshitong" ]] || \
  fail "unexpected-root"
[[ "${WANSHITONG_PORT:-}" =~ ^[0-9]+$ ]] || fail "port-invalid"
((WANSHITONG_PORT >= 1024 && WANSHITONG_PORT <= 65535)) || \
  fail "port-out-of-range"

for relative in data qdrant secrets logs artifacts corpus ops deployment src; do
  path="${WANSHITONG_ROOT}/${relative}"
  [[ -d "${path}" && ! -L "${path}" ]] || fail "directory-${relative}-invalid"
done

for name in master-key admin-bootstrap-token qdrant-api-key qdrant.yaml; do
  path="${WANSHITONG_ROOT}/secrets/${name}"
  [[ -f "${path}" && ! -L "${path}" ]] || fail "secret-${name}-invalid"
  [[ "$(stat -c '%a' -- "${path}")" = "600" ]] || \
    fail "secret-${name}-mode-invalid"
done

docker image inspect "${RAG_APP_IMAGE:?required}" >/dev/null || \
  fail "app-image-missing"
docker image inspect "${RAG_QDRANT_IMAGE:?required}" >/dev/null || \
  fail "qdrant-image-missing"
docker compose --env-file "${env_file}" -f "${compose_file}" config -q

if ss -H -lnt "sport = :${WANSHITONG_PORT}" | grep -q .; then
  owner="$(docker ps --filter name='^/wanshitong-app$' --format '{{.Names}}')"
  [[ "${owner}" = "wanshitong-app" ]] || fail "port-in-use"
fi

docker compose --env-file "${env_file}" -f "${compose_file}" run \
  --rm --no-deps --entrypoint sh wanshitong-app \
  -c 'test -w /data && test -r /run/rag-secrets/master-key' >/dev/null

echo "preflight=passed root=${WANSHITONG_ROOT} port=${WANSHITONG_PORT}"
