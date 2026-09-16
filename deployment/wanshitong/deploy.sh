#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
env_file="${1:-${script_dir}/.env}"
compose_file="${script_dir}/compose.yaml"

compose=(docker compose --env-file "${env_file}" -f "${compose_file}")
"${script_dir}/preflight.sh" "${env_file}"

set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a

"${compose[@]}" up -d wanshitong-qdrant
for _ in $(seq 1 36); do
  status="$(docker inspect -f '{{.State.Health.Status}}' wanshitong-qdrant 2>/dev/null || true)"
  [[ "${status}" = "healthy" ]] && break
  sleep 5
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' wanshitong-qdrant)" = "healthy" ]]

first_report="${WANSHITONG_ROOT}/ops/model-config-first.json"
second_report="${WANSHITONG_ROOT}/ops/model-config-second.json"
"${compose[@]}" run --rm --no-deps wanshitong-app \
  wanshitong configure-internal-models >"${first_report}"
"${compose[@]}" run --rm --no-deps wanshitong-app \
  wanshitong configure-internal-models >"${second_report}"
chmod 0600 -- "${first_report}" "${second_report}"
cmp -s "${first_report}" "${second_report}" || {
  echo "deploy=failed reason=model-config-not-idempotent" >&2
  exit 1
}

"${compose[@]}" up -d wanshitong-app
for _ in $(seq 1 36); do
  if curl -fsS --max-time 3 \
    "http://127.0.0.1:${WANSHITONG_PORT}/live" >/dev/null; then
    break
  fi
  sleep 5
done
curl -fsS --max-time 5 \
  "http://127.0.0.1:${WANSHITONG_PORT}/live" >/dev/null
curl -fsS --max-time 5 \
  "http://127.0.0.1:${WANSHITONG_PORT}/ready" >/dev/null
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
"${compose[@]}" logs --no-color --tail=200 wanshitong-app \
  >"${WANSHITONG_ROOT}/logs/app-${timestamp}.log"
chmod 0600 -- "${WANSHITONG_ROOT}/logs/app-${timestamp}.log"
echo "deploy=passed port=${WANSHITONG_PORT}"
