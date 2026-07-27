#!/usr/bin/env bash
set -euo pipefail
umask 077

bundle_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${1:-${bundle_dir}/.env}"
compose_file="${bundle_dir}/compose.yaml"
rollback_file="${bundle_dir}/.rollback-images.env"

env_value() {
  local key="$1"
  awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print}' \
    "${env_file}" \
    | tail -n 1
}

if [[ ! -f "${env_file}" ]]; then
  echo "缺少部署环境文件：${env_file}" >&2
  exit 1
fi
if grep -Eq 'REPLACE_' "${env_file}"; then
  echo "环境文件仍含占位符，拒绝部署。" >&2
  exit 1
fi

bash "${bundle_dir}/verify-offline.sh"

port="$(env_value RAG_PORT)"
port="${port:-8088}"
app_image="$(env_value RAG_APP_IMAGE)"
ocr_image="$(env_value RAG_OCR_IMAGE)"
qdrant_image="$(env_value RAG_QDRANT_IMAGE)"
for image in "${app_image}" "${ocr_image}" "${qdrant_image}"; do
  if [[ ! "${image}" =~ ^[A-Za-z0-9._/@:+-]+$ ]]; then
    echo "环境文件中的镜像引用无效。" >&2
    exit 1
  fi
done
if [[ ! "${port}" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
  echo "RAG_PORT 无效：${port}" >&2
  exit 1
fi
if ! docker ps -a --format '{{.Names}}' | grep -qx 'rag-app'; then
  if ss -H -lnt "sport = :${port}" | grep -q .; then
    echo "端口 ${port} 已被非 rag-app 服务占用。" >&2
    exit 1
  fi
fi

old_app_id=""
old_ocr_id=""
old_qdrant_id=""
if docker image inspect "${app_image}" >/dev/null 2>&1; then
  old_app_id="$(docker image inspect --format '{{.Id}}' "${app_image}")"
fi
if docker image inspect "${ocr_image}" >/dev/null 2>&1; then
  old_ocr_id="$(docker image inspect --format '{{.Id}}' "${ocr_image}")"
fi
if docker image inspect "${qdrant_image}" >/dev/null 2>&1; then
  old_qdrant_id="$(docker image inspect \
    --format '{{.Id}}' "${qdrant_image}")"
fi
if [[ -n "${old_app_id}" \
  && -n "${old_ocr_id}" \
  && -n "${old_qdrant_id}" ]]; then
  {
    printf 'ROLLBACK_APP_IMAGE=%s\n' "${old_app_id}"
    printf 'ROLLBACK_OCR_IMAGE=%s\n' "${old_ocr_id}"
    printf 'ROLLBACK_QDRANT_IMAGE=%s\n' "${old_qdrant_id}"
  } > "${rollback_file}"
fi

docker load --input "${bundle_dir}/images/rag-images-linux-amd64.tar"
docker compose \
  --env-file "${env_file}" \
  -f "${compose_file}" \
  config -q
docker compose \
  --env-file "${env_file}" \
  -f "${compose_file}" \
  up -d --no-build --pull never
docker compose \
  --env-file "${env_file}" \
  -f "${compose_file}" \
  ps

echo "部署已启动；只有 rag-app 暴露业务端口 ${port}。"
