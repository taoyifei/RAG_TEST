#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root="/data/tyf/RAG"
env_file="${1:-${project_root}/shared/env/rag.env}"
shared_env_dir="${project_root}/shared/env"
releases_dir="${project_root}/releases"
rollback_file="${shared_env_dir}/rollback-images.env"
current_link="${project_root}/current"

fail() {
  echo "$1" >&2
  exit 1
}

exact_value() {
  local file="$1"
  local key="$2"
  local count
  count="$(awk -F= -v key="${key}" '$1 == key {count += 1} END {
      print count + 0
    }' "${file}")"
  if [[ "${count}" != "1" ]]; then
    echo "${key} 必须在 $(basename "${file}") 中恰好出现一次。" >&2
    return 1
  fi
  awk -F= -v key="${key}" '$1 == key {
      sub(/^[^=]*=/, "")
      print
    }' "${file}"
}

optional_value() {
  local file="$1"
  local key="$2"
  local count
  count="$(awk -F= -v key="${key}" '$1 == key {count += 1} END {
      print count + 0
    }' "${file}")"
  if [[ "${count}" -gt 1 ]]; then
    echo "${key} 在 $(basename "${file}") 中重复。" >&2
    return 1
  fi
  if [[ "${count}" == "1" ]]; then
    awk -F= -v key="${key}" '$1 == key {
        sub(/^[^=]*=/, "")
        print
      }' "${file}"
  fi
}

require_regular_file() {
  local path="$1"
  local label="$2"
  if [[ ! -f "${path}" || -L "${path}" ]]; then
    fail "${label} 必须是普通文件且不能是符号链接：${path}"
  fi
}

inspect_image_id() {
  local image="$1"
  local actual_id
  actual_id="$(docker image inspect --format '{{.Id}}' "${image}")"
  if [[ "${actual_id}" != "${image}" ]]; then
    fail "镜像 ID 与回滚记录不一致：${image}"
  fi
}

inspect_revision() {
  local image="$1"
  local expected_revision="$2"
  local actual_revision
  actual_revision="$(docker image inspect \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "${image}")"
  if [[ "${actual_revision}" != "${expected_revision}" ]]; then
    fail "镜像 OCI revision 与旧 release 不一致：${image}"
  fi
}

verify_container_image() {
  local container="$1"
  local expected_image="$2"
  local actual_image
  actual_image="$(docker container inspect --format '{{.Image}}' "${container}")"
  if [[ "${actual_image}" != "${expected_image}" ]]; then
    echo "容器镜像与旧 release 不一致：${container}" >&2
    return 1
  fi
}

require_regular_file "${env_file}" "共享环境文件"
require_regular_file "${rollback_file}" "回滚镜像记录"
env_file="$(realpath -e "${env_file}")"
if [[ "$(dirname "${env_file}")" != "${shared_env_dir}" ]]; then
  fail "共享环境文件必须位于 ${shared_env_dir}。"
fi

rollback_release_dir="$(exact_value "${rollback_file}" ROLLBACK_RELEASE_DIR)"
if [[ -z "${rollback_release_dir}" \
  || "${rollback_release_dir}" != "${releases_dir}/"* \
  || ! -d "${rollback_release_dir}" \
  || -L "${rollback_release_dir}" \
  || "$(realpath -e "${rollback_release_dir}")" != "${rollback_release_dir}" \
  || "$(dirname "${rollback_release_dir}")" != "${releases_dir}" ]]; then
  fail "上一版 release 路径无效。"
fi
require_regular_file \
  "${rollback_release_dir}/verify-offline.sh" \
  "旧 release verify-offline.sh"
require_regular_file "${rollback_release_dir}/compose.yaml" "旧 release Compose"
require_regular_file \
  "${rollback_release_dir}/SOURCE_REVISION" \
  "旧 release SOURCE_REVISION"
require_regular_file \
  "${rollback_release_dir}/QDRANT_SOURCE_IMAGE" \
  "旧 release QDRANT_SOURCE_IMAGE"

bash "${rollback_release_dir}/verify-offline.sh"
compose_file="${rollback_release_dir}/compose.yaml"
docker compose --env-file "${env_file}" -f "${compose_file}" config -q

source_revision="$(cat "${rollback_release_dir}/SOURCE_REVISION")"
if [[ ! "${source_revision}" =~ ^[0-9a-f]{40}$ \
  || "$(wc -l < "${rollback_release_dir}/SOURCE_REVISION")" != "1" ]]; then
  fail "旧 release SOURCE_REVISION 无效。"
fi
qdrant_source_image="$(cat "${rollback_release_dir}/QDRANT_SOURCE_IMAGE")"
if [[ ! "${qdrant_source_image}" =~ @sha256:[0-9a-f]{64}$ \
  || "$(wc -l < "${rollback_release_dir}/QDRANT_SOURCE_IMAGE")" != "1" ]]; then
  fail "旧 release QDRANT_SOURCE_IMAGE 无效。"
fi

rollback_app_image="$(exact_value "${rollback_file}" ROLLBACK_APP_IMAGE)"
rollback_ocr_image="$(exact_value "${rollback_file}" ROLLBACK_OCR_IMAGE)"
rollback_qdrant_image="$(exact_value "${rollback_file}" ROLLBACK_QDRANT_IMAGE)"
for image in \
  "${rollback_app_image}" \
  "${rollback_ocr_image}" \
  "${rollback_qdrant_image}"; do
  if [[ ! "${image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    fail "上一版镜像记录无效：${image}"
  fi
  inspect_image_id "${image}"
done
inspect_revision "${rollback_app_image}" "${source_revision}"
inspect_revision "${rollback_ocr_image}" "${source_revision}"
if [[ "${rollback_qdrant_image}" != "${qdrant_source_image##*@}" ]]; then
  fail "Qdrant 镜像身份与旧 release 记录不一致。"
fi

for key in RAG_APP_IMAGE RAG_OCR_IMAGE RAG_QDRANT_IMAGE; do
  exact_value "${env_file}" "${key}" >/dev/null
done
optional_value "${env_file}" RAG_RELEASE_REVISION >/dev/null
release_revision_present="$(awk -F= \
  '$1 == "RAG_RELEASE_REVISION" {count += 1} END {print count + 0}' \
  "${env_file}")"

if [[ ! -L "${current_link}" ]]; then
  fail "current 必须是指向当前 release 的符号链接。"
fi
original_release_dir="$(readlink -f "${current_link}")"
if [[ "${original_release_dir}" != "${releases_dir}/"* \
  || ! -d "${original_release_dir}" \
  || "$(dirname "${original_release_dir}")" != "${releases_dir}" ]]; then
  fail "current 指向无效 release。"
fi

worker_was_running=false
if docker container inspect rag-worker >/dev/null 2>&1; then
  worker_state="$(docker container inspect \
    --format '{{.State.Running}}' rag-worker)"
  if [[ "${worker_state}" == "true" ]]; then
    worker_was_running=true
  elif [[ "${worker_state}" != "false" ]]; then
    fail "无法判断 rag-worker 的运行状态。"
  fi
fi

new_env="$(mktemp "${shared_env_dir}/rag.env.rollback-new.XXXXXXXX")"
old_env="$(mktemp "${shared_env_dir}/rag.env.rollback-old.XXXXXXXX")"
current_new="${current_link}.rollback-new"
current_restore="${current_link}.rollback-restore"
env_replaced=false
current_replaced=false

cleanup() {
  rm -f -- "${new_env}" "${old_env}" "${current_new}" "${current_restore}"
}
trap cleanup EXIT

cp -- "${env_file}" "${old_env}"
chmod 0600 "${old_env}"
awk -F= \
  -v app="${rollback_app_image}" \
  -v ocr="${rollback_ocr_image}" \
  -v qdrant="${rollback_qdrant_image}" \
  -v revision="${source_revision}" '
    $1 == "RAG_APP_IMAGE" {$0 = "RAG_APP_IMAGE=" app}
    $1 == "RAG_OCR_IMAGE" {$0 = "RAG_OCR_IMAGE=" ocr}
    $1 == "RAG_QDRANT_IMAGE" {$0 = "RAG_QDRANT_IMAGE=" qdrant}
    $1 == "RAG_RELEASE_REVISION" {$0 = "RAG_RELEASE_REVISION=" revision}
    {print}
  ' "${env_file}" > "${new_env}"
chmod 0600 "${new_env}"

if [[ "$(exact_value "${new_env}" RAG_APP_IMAGE)" \
    != "${rollback_app_image}" \
  || "$(exact_value "${new_env}" RAG_OCR_IMAGE)" \
    != "${rollback_ocr_image}" \
  || "$(exact_value "${new_env}" RAG_QDRANT_IMAGE)" \
    != "${rollback_qdrant_image}" ]]; then
  fail "临时回滚环境文件镜像值无效。"
fi
if [[ "${release_revision_present}" == "1" \
  && "$(exact_value "${new_env}" RAG_RELEASE_REVISION)" \
    != "${source_revision}" ]]; then
  fail "临时回滚环境文件 revision 无效。"
fi

compose_command=(
  docker compose
  --env-file "${new_env}"
  -f "${compose_file}"
)
if [[ "${worker_was_running}" == "true" ]]; then
  compose_command+=(--profile index)
fi
rollback_services=(rag-qdrant rag-ocr rag-app)
if [[ "${worker_was_running}" == "true" ]]; then
  rollback_services+=(rag-worker)
fi
if ! "${compose_command[@]}" up -d --no-build --pull never \
  "${rollback_services[@]}"; then
  fail "旧 release Compose 启动失败。"
fi
verify_container_image rag-app "${rollback_app_image}"
verify_container_image rag-ocr "${rollback_ocr_image}"
verify_container_image rag-qdrant "${rollback_qdrant_image}"
if [[ "${worker_was_running}" == "true" ]]; then
  verify_container_image rag-worker "${rollback_app_image}"
fi
"${compose_command[@]}" ps
port="$(optional_value "${new_env}" RAG_PORT)"
port="${port:-8088}"
if [[ ! "${port}" =~ ^[0-9]+$ ]] \
  || ! curl -fsS --max-time 10 "http://127.0.0.1:${port}/live" >/dev/null; then
  fail "旧 release 应用存活检查失败。"
fi

restore_metadata() {
  local restore_failed=0
  if [[ "${current_replaced}" == "true" ]]; then
    if ln -s "${original_release_dir}" "${current_restore}" \
      && mv -T "${current_restore}" "${current_link}"; then
      current_replaced=false
    else
      echo "补偿失败：无法恢复原 current。" >&2
      restore_failed=1
    fi
  fi
  if [[ "${env_replaced}" == "true" ]]; then
    if mv "${old_env}" "${env_file}"; then
      env_replaced=false
    else
      echo "补偿失败：无法恢复原共享环境文件。" >&2
      restore_failed=1
    fi
  fi
  return "${restore_failed}"
}

verify_persisted_state() {
  if [[ "$(exact_value "${env_file}" RAG_APP_IMAGE)" \
      != "${rollback_app_image}" \
    || "$(exact_value "${env_file}" RAG_OCR_IMAGE)" \
      != "${rollback_ocr_image}" \
    || "$(exact_value "${env_file}" RAG_QDRANT_IMAGE)" \
      != "${rollback_qdrant_image}" \
    || "$(readlink -f "${current_link}")" != "${rollback_release_dir}" ]]; then
    return 1
  fi
  persisted_compose="${current_link}/compose.yaml"
  docker compose --env-file "${env_file}" -f "${persisted_compose}" config -q
  docker compose --env-file "${env_file}" -f "${persisted_compose}" \
    up -d --no-build --pull never rag-qdrant rag-ocr rag-app
  verify_container_image rag-app "${rollback_app_image}"
  verify_container_image rag-ocr "${rollback_ocr_image}"
  verify_container_image rag-qdrant "${rollback_qdrant_image}"
  if [[ "${worker_was_running}" == "true" ]]; then
    verify_container_image rag-worker "${rollback_app_image}"
  fi
  docker compose --env-file "${env_file}" -f "${persisted_compose}" ps
  curl -fsS --max-time 10 "http://127.0.0.1:${port}/live" >/dev/null
}

if ! mv "${new_env}" "${env_file}"; then
  fail "无法原子替换共享环境文件。"
fi
env_replaced=true
if ! ln -s "${rollback_release_dir}" "${current_new}" \
  || ! mv -T "${current_new}" "${current_link}"; then
  if ! restore_metadata; then
    fail "current 切换失败，且共享环境文件补偿失败。"
  fi
  fail "current 切换失败；共享环境文件已恢复。"
fi
current_replaced=true
if ! verify_persisted_state; then
  if ! restore_metadata; then
    fail "持久状态复核失败，且元数据补偿失败。"
  fi
  fail "持久状态复核失败；env 与 current 已恢复。"
fi

env_replaced=false
current_replaced=false
echo "已持久切回上一版镜像；SQLite、Qdrant 和语料 bind mount 未删除。"
