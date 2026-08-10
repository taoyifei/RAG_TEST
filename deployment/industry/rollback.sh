#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"

[[ "$#" -eq 1 ]] \
  || industry_fail "用法: rollback.sh /absolute/industry-backup-dir"
backup_path="$1"
[[ "${backup_path}" == /* && -d "${backup_path}" \
  && ! -L "${backup_path}" ]] \
  || industry_fail "Industry backup path 无效。"
backup_path="$(realpath "${backup_path}")"
last_good="$(industry_last_good_identity "${backup_path}")" \
  || industry_fail "LAST_GOOD_POINTER_INVALID"
env_file="$(python3 - "${last_good}" <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
env_path = value.get("env_path")
if not isinstance(env_path, str):
    raise SystemExit("LAST_GOOD_ENV_INVALID")
print(env_path)
PY
)" || industry_fail "LAST_GOOD_ENV_INVALID"
state_path="$(python3 - "${last_good}" <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
state_path = value.get("state_path")
if not isinstance(state_path, str):
    raise SystemExit("LAST_GOOD_STATE_INVALID")
print(state_path)
PY
)" || industry_fail "LAST_GOOD_STATE_INVALID"
require_industry_env "${env_file}"
compose_file="$(industry_compose_file "${env_file}")"
[[ "${compose_file}" == */releases/*/compose.yaml ]] \
  || industry_fail "previous env 未绑定 Industry release。"

validate_industry_compose "${env_file}" "${compose_file}" \
  || industry_fail "INDUSTRY_COMPOSE_CANONICAL_CONFIG_INVALID"
port="$(exact_env_value "${env_file}" RAG_PORT)"
if ! wait_industry_http "http://127.0.0.1:${port}/live" 10; then
  printf 'RAG_INDUSTRY_ROLLBACK_PRECHECK_UNHEALTHY\n' >&2
fi
env_backup_path="$(exact_env_value "${env_file}" RAG_BACKUP_PATH)"
[[ "${env_backup_path}" == "${backup_path}" ]] \
  || industry_fail "last-good env backup path 不一致。"
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
  run_industry_compose "${env_file}" "${compose_file}" \
    stop rag-industry-app \
    || industry_fail "无法停止当前 Industry app 以恢复索引状态。"
  run_industry_compose "${env_file}" "${compose_file}" \
    --profile index run --rm --no-deps \
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
  run_industry_compose "${env_file}" "${compose_file}" \
    --profile dedicated-ocr up -d \
    --no-build --pull never \
    rag-industry-qdrant rag-industry-ocr
  wait_industry_health rag-industry-ocr 300
elif [[ "${ocr_mode}" == "external" ]]; then
  run_industry_compose "${env_file}" "${compose_file}" \
    up -d --no-build --pull never rag-industry-qdrant
else
  industry_fail "上一 release 的 OCR mode 无效。"
fi
wait_industry_health rag-industry-qdrant 180
run_industry_compose "${env_file}" "${compose_file}" up -d \
  --no-deps --no-build --pull never --force-recreate rag-industry-app
wait_industry_health rag-industry-app 180
wait_industry_http "http://127.0.0.1:${port}/live" 60 \
  || industry_fail "回滚后 Industry /live 未恢复。"
verify_industry_app_identity "${env_file}" true \
  || industry_fail "回滚后 Industry app identity 未完整恢复。"
state_temp="$(mktemp "${backup_path}/.deployment-state.XXXXXX")"
trap 'rm -f -- "${state_temp}"' EXIT
cp -- "${state_path}" "${state_temp}"
chmod 600 "${state_temp}"
mv -f -- "${state_temp}" "${backup_path}/deployment-state.json"
trap - EXIT
printf 'RAG_INDUSTRY_ROLLBACK_OK\n'
