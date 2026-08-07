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

compose=(
  docker compose
  -p rag-industry
  --env-file "${env_file}"
  -f "${compose_file}"
)
ocr_mode="$(exact_env_value "${env_file}" RAG_OCR_MODE)"
backup_path="$(exact_env_value "${env_file}" RAG_BACKUP_PATH)"
mkdir -p -- "${backup_path}"
previous_env=""
if docker inspect rag-industry-app >/dev/null 2>&1; then
  state="$(docker inspect --format \
    '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    rag-industry-app)"
  if [[ "${state}" == "healthy" ]]; then
    candidate_previous="${backup_path}/last-good.env"
    if [[ -f "${candidate_previous}" && ! -L "${candidate_previous}" ]]; then
      previous_env="${backup_path}/previous.env"
      previous_temp="$(mktemp "${backup_path}/.previous.XXXXXX")"
      trap 'rm -f -- "${previous_temp}"' EXIT
      cp -- "${candidate_previous}" "${previous_temp}"
      chmod 600 "${previous_temp}"
      mv -f -- "${previous_temp}" "${previous_env}"
      trap - EXIT
    fi
  fi
fi

deploy_ok=true
if [[ "${ocr_mode}" == "dedicated" ]]; then
  "${compose[@]}" --profile dedicated-ocr up -d \
    --no-build --pull never \
    rag-industry-qdrant rag-industry-ocr rag-industry-app \
    || deploy_ok=false
elif [[ "${ocr_mode}" == "external" ]]; then
  "${compose[@]}" up -d --no-build --pull never \
    rag-industry-qdrant rag-industry-app \
    || deploy_ok=false
else
  industry_fail "RAG_OCR_MODE 只能是 dedicated 或 external。"
fi

if [[ "${deploy_ok}" == true ]]; then
  wait_industry_health rag-industry-qdrant 180
  if [[ "${ocr_mode}" == "dedicated" ]]; then
    wait_industry_health rag-industry-ocr 300
  fi
  wait_industry_health rag-industry-app 180
  port="$(exact_env_value "${env_file}" RAG_PORT)"
  wait_industry_http "http://127.0.0.1:${port}/live" 60 \
    || deploy_ok=false
fi

if [[ "${deploy_ok}" != true ]]; then
  if [[ -n "${previous_env}" && -f "${previous_env}" ]]; then
    bash "${release_dir}/rollback.sh" "${previous_env}" \
      || industry_fail "部署失败且上一 Industry release 恢复失败。"
    industry_fail "部署失败，已恢复上一 Industry release。"
  fi
  industry_fail "首次 Industry 部署失败；未触碰 training 服务。"
fi

last_good="${backup_path}/last-good.env"
temporary="$(mktemp "${backup_path}/.last-good.XXXXXX")"
cp --preserve=mode,ownership,timestamps -- "${env_file}" "${temporary}"
chmod 600 "${temporary}"
mv -f -- "${temporary}" "${last_good}"
printf 'RAG_INDUSTRY_DEPLOY_OK\n'
printf 'next=bash %s/run-index.sh %s\n' "${release_dir}" "${env_file}"
