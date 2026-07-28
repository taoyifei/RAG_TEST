#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root="/data/tyf/RAG"
env_file="${1:-${project_root}/shared/env/rag.env}"
shared_env_dir="${project_root}/shared/env"
rollback_file="${shared_env_dir}/rollback-images.env"
current_link="${project_root}/current"

rollback_value() {
  local key="$1"
  awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print}' \
    "${rollback_file}" \
    | tail -n 1
}

if [[ ! -f "${env_file}" || ! -f "${rollback_file}" \
  || -L "${env_file}" || -L "${rollback_file}" ]]; then
  echo "缺少外置环境文件或上一版镜像记录。" >&2
  exit 1
fi
rollback_release_dir="$(rollback_value ROLLBACK_RELEASE_DIR)"
if [[ "${rollback_release_dir}" != "${project_root}/releases/"* \
  || ! -d "${rollback_release_dir}" \
  || -L "${rollback_release_dir}" ]]; then
  echo "上一版 release 路径无效。" >&2
  exit 1
fi
compose_file="${rollback_release_dir}/compose.yaml"

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

env \
  RAG_APP_IMAGE="${rollback_app_image}" \
  RAG_OCR_IMAGE="${rollback_ocr_image}" \
  RAG_QDRANT_IMAGE="${rollback_qdrant_image}" \
  docker compose --env-file "${env_file}" -f "${compose_file}" \
  up -d --no-build --pull never
docker compose --env-file "${env_file}" -f "${compose_file}" ps
ln -s "${rollback_release_dir}" "${current_link}.new"
mv -T "${current_link}.new" "${current_link}"

echo "容器已切回上一版镜像；SQLite、Qdrant 和语料 bind mount 未删除。"
