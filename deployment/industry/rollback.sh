#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"

[[ "$#" -eq 1 ]] \
  || industry_fail "用法: rollback.sh /absolute/previous-rag-industry.env"
require_industry_env "$1"
env_file="$(realpath "$1")"
compose_file="$(industry_compose_file "${env_file}")"
[[ "${compose_file}" == */releases/*/compose.yaml ]] \
  || industry_fail "previous env 未绑定 Industry release。"

compose=(
  docker compose
  -p rag-industry
  --env-file "${env_file}"
  -f "${compose_file}"
)
port="$(exact_env_value "${env_file}" RAG_PORT)"
if ! wait_industry_http "http://127.0.0.1:${port}/live" 10; then
  printf 'RAG_INDUSTRY_ROLLBACK_PRECHECK_UNHEALTHY\n' >&2
fi
backup_path="$(exact_env_value "${env_file}" RAG_BACKUP_PATH)"
descriptor="${backup_path}/last-index-rollback.json"
current_revision="$(docker container inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  rag-industry-app 2>/dev/null || true)"
descriptor_revision=""
if [[ -f "${descriptor}" && ! -L "${descriptor}" ]]; then
  descriptor_revision="$(python3 - "${descriptor}" <<'PY'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
revision = value.get("current_revision")
print(revision if isinstance(revision, str) else "")
PY
)"
fi
if [[ -n "${current_revision}" \
  && "${descriptor_revision}" == "${current_revision}" ]]; then
  "${compose[@]}" stop rag-industry-app \
    || industry_fail "无法停止当前 Industry app 以恢复索引状态。"
  "${compose[@]}" --profile index run --rm --no-deps \
    --user 0:0 \
    --cap-add DAC_OVERRIDE \
    --cap-add CHOWN \
    --entrypoint python \
    --volume "${script_dir}/runtime_check.py:/runtime_check.py:ro" \
    --volume "${backup_path}:/backup:ro" \
    rag-industry-worker \
    /runtime_check.py restore-index-rollback \
    /backup/last-index-rollback.json >/dev/null \
    || industry_fail "Industry alias/manifest state 恢复失败。"
fi
ocr_mode="$(exact_env_value "${env_file}" RAG_OCR_MODE)"
if [[ "${ocr_mode}" == "dedicated" ]]; then
  "${compose[@]}" --profile dedicated-ocr up -d \
    --no-build --pull never \
    rag-industry-qdrant rag-industry-ocr rag-industry-app
  wait_industry_health rag-industry-ocr 300
elif [[ "${ocr_mode}" == "external" ]]; then
  "${compose[@]}" up -d --no-build --pull never \
    rag-industry-qdrant rag-industry-app
else
  industry_fail "上一 release 的 OCR mode 无效。"
fi
wait_industry_health rag-industry-qdrant 180
wait_industry_health rag-industry-app 180
wait_industry_http "http://127.0.0.1:${port}/live" 60 \
  || industry_fail "回滚后 Industry /live 未恢复。"
last_good_temp="$(mktemp "${backup_path}/.last-good.XXXXXX")"
trap 'rm -f -- "${last_good_temp}"' EXIT
cp -- "${env_file}" "${last_good_temp}"
chmod 600 "${last_good_temp}"
mv -f -- "${last_good_temp}" "${backup_path}/last-good.env"
trap - EXIT
printf 'RAG_INDUSTRY_ROLLBACK_OK\n'
