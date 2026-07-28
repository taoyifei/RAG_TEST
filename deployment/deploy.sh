#!/usr/bin/env bash
set -euo pipefail
umask 077

release_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
env_file="${1:-/data/tyf/RAG/shared/env/rag.env}"
compose_file="${release_dir}/compose.yaml"
project_root="/data/tyf/RAG"
shared_env_dir="${project_root}/shared/env"
rollback_file="${shared_env_dir}/rollback-images.env"
current_link="${project_root}/current"

env_value() {
  local key="$1"
  awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print}' \
    "${env_file}" \
    | tail -n 1
}

image_manifest_value() {
  local archive="$1"
  local field="$2"
  awk -F '\t' -v archive="${archive}" -v field="${field}" \
    '$1 == archive {print $field}' \
    "${release_dir}/IMAGE_ARCHIVES.tsv"
}

inspect_image() {
  local image="$1"
  local expected_revision="$2"
  local actual_revision
  local architecture
  local image_id
  local operating_system
  architecture="$(docker image inspect --format '{{.Architecture}}' "${image}")"
  operating_system="$(docker image inspect --format '{{.Os}}' "${image}")"
  image_id="$(docker image inspect --format '{{.Id}}' "${image}")"
  if [[ "${architecture}/${operating_system}" != "amd64/linux" \
    || ! "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "镜像平台或 ID 无效：${image}" >&2
    exit 1
  fi
  if [[ "${expected_revision}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    if [[ "${image_id}" != "${expected_revision}" ]]; then
      echo "镜像 ID 与 runtime 包固定 digest 不一致：${image}" >&2
      exit 1
    fi
  elif [[ "${expected_revision}" != "-" ]]; then
    actual_revision="$(docker image inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "${image}")"
    if [[ "${actual_revision}" != "${expected_revision}" ]]; then
      echo "镜像 revision 与 runtime 包不一致：${image}" >&2
      exit 1
    fi
  fi
}

if [[ ! -f "${env_file}" || -L "${env_file}" ]]; then
  echo "缺少外置部署环境文件：${env_file}" >&2
  exit 1
fi
if [[ "$(dirname "$(realpath "${env_file}")")" != "${shared_env_dir}" ]]; then
  echo "环境文件必须位于 ${shared_env_dir}。" >&2
  exit 1
fi
if [[ "${release_dir}" != "${project_root}/releases/"* ]]; then
  echo "runtime release 必须位于 ${project_root}/releases/。" >&2
  exit 1
fi
if grep -Eq 'REPLACE_' "${env_file}"; then
  echo "环境文件仍含 REPLACE_ 占位符，拒绝部署。" >&2
  exit 1
fi

bash "${release_dir}/verify-offline.sh"
release_id="$(cat "${release_dir}/RELEASE_ID")"
source_revision="$(cat "${release_dir}/SOURCE_REVISION")"
if [[ "$(basename "${release_dir}")" != "${release_id}" ]]; then
  echo "release 目录名与 RELEASE_ID 不一致。" >&2
  exit 1
fi

state_path="$(env_value RAG_STATE_PATH)"
qdrant_path="$(env_value RAG_QDRANT_PATH)"
docs_path="$(env_value RAG_DOCS_PATH)"
if [[ "${state_path}" != "${project_root}/data/state" \
  || "${qdrant_path}" != "${project_root}/data/qdrant" \
  || "${docs_path}" != "${project_root}/shared/corpora/"*/docs ]]; then
  echo "状态、Qdrant 或 DOCX 路径未固定在 ${project_root}。" >&2
  exit 1
fi
if [[ ! -d "${state_path}" || -L "${state_path}" \
  || ! -d "${qdrant_path}" || -L "${qdrant_path}" ]]; then
  echo "state 与 Qdrant 必须是预先创建的真实目录。" >&2
  exit 1
fi
if [[ ! -d "${docs_path}" || -L "${docs_path}" ]]; then
  echo "语料 docs 目录不存在或是符号链接：${docs_path}" >&2
  exit 1
fi
if [[ "$(stat -c '%u' "${state_path}")" != "10001" \
  || "$(stat -c '%a' "${state_path}")" != "700" ]]; then
  echo "state 目录必须归 UID 10001 所有且权限为 0700。" >&2
  exit 1
fi
if find "${docs_path}" -xdev ! -uid 10001 -print -quit | grep -q .; then
  echo "语料目录必须全部归 UID 10001 所有。" >&2
  exit 1
fi

port="$(env_value RAG_PORT)"
port="${port:-8088}"
if [[ ! "${port}" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
  echo "RAG_PORT 无效：${port}" >&2
  exit 1
fi
if ! docker ps -a --format '{{.Names}}' | grep -qx 'rag-app' \
  && ss -H -lnt "sport = :${port}" | grep -q .; then
  echo "端口 ${port} 已被非 rag-app 服务占用。" >&2
  exit 1
fi

app_image="$(env_value RAG_APP_IMAGE)"
ocr_image="$(env_value RAG_OCR_IMAGE)"
qdrant_image="$(env_value RAG_QDRANT_IMAGE)"
if [[ "${app_image}" \
  != "$(image_manifest_value images/docx-rag-linux-amd64.tar 2)" \
  || "${ocr_image}" \
  != "$(image_manifest_value images/docx-rag-ocr-linux-amd64.tar 2)" \
  || "${qdrant_image}" \
  != "$(image_manifest_value images/qdrant-linux-amd64.tar 2)" ]]; then
  echo "环境文件镜像引用与 runtime 包白名单不一致。" >&2
  exit 1
fi

existing_count="$(docker ps -a --format '{{.Names}}' \
  | awk '$0 == "rag-app" || $0 == "rag-ocr" || $0 == "rag-qdrant" {
      count += 1
    }
    END {
      print count + 0
    }')"
if [[ "${existing_count}" != "0" && "${existing_count}" != "3" ]]; then
  echo "已有 rag 核心容器不完整，拒绝覆盖部署。" >&2
  exit 1
fi
if [[ "${existing_count}" == "3" ]]; then
  if [[ ! -L "${current_link}" ]]; then
    echo "已有 rag 容器但缺少 current release 链接。" >&2
    exit 1
  fi
  old_release="$(readlink -f "${current_link}")"
  if [[ "${old_release}" != "${project_root}/releases/"* \
    || ! -d "${old_release}" ]]; then
    echo "current 指向无效 release。" >&2
    exit 1
  fi
  {
    printf 'ROLLBACK_RELEASE_DIR=%s\n' "${old_release}"
    printf 'ROLLBACK_APP_IMAGE=%s\n' \
      "$(docker container inspect --format '{{.Image}}' rag-app)"
    printf 'ROLLBACK_OCR_IMAGE=%s\n' \
      "$(docker container inspect --format '{{.Image}}' rag-ocr)"
    printf 'ROLLBACK_QDRANT_IMAGE=%s\n' \
      "$(docker container inspect --format '{{.Image}}' rag-qdrant)"
  } > "${rollback_file}.new"
  mv "${rollback_file}.new" "${rollback_file}"
fi

docker load --input "${release_dir}/images/docx-rag-linux-amd64.tar"
docker load --input "${release_dir}/images/docx-rag-ocr-linux-amd64.tar"
docker load --input "${release_dir}/images/qdrant-linux-amd64.tar"
inspect_image "${app_image}" "${source_revision}"
inspect_image "${ocr_image}" "${source_revision}"
inspect_image \
  "${qdrant_image}" \
  "$(image_manifest_value images/qdrant-linux-amd64.tar 3)"

docker compose --env-file "${env_file}" -f "${compose_file}" config -q
docker compose --env-file "${env_file}" -f "${compose_file}" \
  up -d --no-build --pull never
docker compose --env-file "${env_file}" -f "${compose_file}" ps
ln -s "${release_dir}" "${current_link}.new"
mv -T "${current_link}.new" "${current_link}"

echo "release ${release_id} 已启动；仅 rag-app 暴露宿主机端口 ${port}。"
