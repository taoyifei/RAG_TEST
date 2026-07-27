#!/usr/bin/env bash
set -euo pipefail

bundle_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${1:-${bundle_dir}/.env}"
compose_file="${bundle_dir}/compose.yaml"
rollback_file="${bundle_dir}/.rollback-images.env"

rollback_value() {
  local key="$1"
  awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print}' \
    "${rollback_file}" \
    | tail -n 1
}

if [[ ! -f "${env_file}" || ! -f "${rollback_file}" ]]; then
  echo "缺少部署环境或可恢复的上一版镜像记录。" >&2
  exit 1
fi

rollback_app_image="$(rollback_value ROLLBACK_APP_IMAGE)"
rollback_ocr_image="$(rollback_value ROLLBACK_OCR_IMAGE)"
rollback_qdrant_image="$(rollback_value ROLLBACK_QDRANT_IMAGE)"
for image in \
  "${rollback_app_image}" \
  "${rollback_ocr_image}" \
  "${rollback_qdrant_image}"; do
  if [[ ! "${image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "上一版镜像记录无效。" >&2
    exit 1
  fi
  docker image inspect "${image}" >/dev/null
done

RAG_APP_IMAGE="${rollback_app_image}" \
RAG_OCR_IMAGE="${rollback_ocr_image}" \
RAG_QDRANT_IMAGE="${rollback_qdrant_image}" \
docker compose \
  --env-file "${env_file}" \
  -f "${compose_file}" \
  up -d --no-build --pull never
docker compose \
  --env-file "${env_file}" \
  -f "${compose_file}" \
  ps

echo "容器已切回上一版镜像；rag-state 与 rag-qdrant-data 卷未删除。"
