#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"

[[ "$#" -eq 2 ]] \
  || industry_fail "用法: deploy.sh /absolute/rag-industry.env /absolute/release-dir"
require_industry_env "$1"
require_release_directory "$2"
env_file="$(realpath "$1")"
release_dir="$(realpath "$2")"
compose_file="$(industry_compose_file "${env_file}")"
[[ "${compose_file}" == "${release_dir}/compose.yaml" ]] \
  || industry_fail "env compose path 必须指向当前 release。"

for key in RAG_DOCS_PATH RAG_REFERENCE_PATH RAG_CONFIG_PATH RAG_STATE_PATH; do
  value="$(exact_env_value "${env_file}" "${key}")"
  [[ -d "${value}" && ! -L "${value}" ]] \
    || industry_fail "${key} 尚未由 install.sh 安装。"
done
python3 "${release_dir}/package_selfcheck.py" release "${release_dir}" \
  >/dev/null || industry_fail "PACKAGE_SELFCHECK_FAILED"

validate_industry_compose "${env_file}" "${compose_file}" \
  || industry_fail "INDUSTRY_COMPOSE_CANONICAL_CONFIG_INVALID"
ocr_mode="$(exact_env_value "${env_file}" RAG_OCR_MODE)"
backup_path="$(exact_env_value "${env_file}" RAG_BACKUP_PATH)"
mkdir -p -- "${backup_path}"
previous_last_good="$(industry_last_good_identity \
  "${backup_path}" 2>/dev/null || true)"
write_industry_release_state "${env_file}" candidate \
  || industry_fail "INDUSTRY_CANDIDATE_STATE_FAILED"

deploy_ok=true
if [[ "${ocr_mode}" == "dedicated" ]]; then
  run_industry_compose "${env_file}" "${compose_file}" \
    --profile dedicated-ocr up -d \
    --no-build --pull never \
    rag-industry-qdrant rag-industry-ocr \
    || deploy_ok=false
elif [[ "${ocr_mode}" == "external" ]]; then
  run_industry_compose "${env_file}" "${compose_file}" \
    up -d --no-build --pull never rag-industry-qdrant \
    || deploy_ok=false
else
  industry_fail "RAG_OCR_MODE 只能是 dedicated 或 external。"
fi

if [[ "${deploy_ok}" == true ]]; then
  wait_industry_health rag-industry-qdrant 180
  if [[ "${ocr_mode}" == "dedicated" ]]; then
    wait_industry_health rag-industry-ocr 300
  fi
  run_industry_compose "${env_file}" "${compose_file}" up -d \
    --no-deps --no-build --pull never --force-recreate rag-industry-app \
    || deploy_ok=false
fi

if [[ "${deploy_ok}" == true ]]; then
  wait_industry_health rag-industry-app 180
  port="$(exact_env_value "${env_file}" RAG_PORT)"
  wait_industry_http "http://127.0.0.1:${port}/live" 60 \
    || deploy_ok=false
  verify_industry_app_identity "${env_file}" false || deploy_ok=false
fi

if [[ "${deploy_ok}" != true ]]; then
  if [[ -n "${previous_last_good}" ]]; then
    bash "${release_dir}/rollback.sh" "${backup_path}" \
      || industry_fail "部署失败且上一 Industry release 恢复失败。"
    industry_fail "部署失败，已恢复上一 Industry release。"
  fi
  industry_fail "首次 Industry 部署失败；未触碰 training 服务。"
fi

write_industry_release_state "${env_file}" deployed \
  || industry_fail "INDUSTRY_DEPLOYED_STATE_FAILED"
printf 'RAG_INDUSTRY_DEPLOY_OK\n'
printf 'next=bash %s/run-index.sh %s\n' "${release_dir}" "${env_file}"
