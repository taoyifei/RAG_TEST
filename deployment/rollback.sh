#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root="/data/tyf/RAG"
env_file="${1:-${project_root}/shared/env/rag.env}"
shared_env_dir="${project_root}/shared/env"
releases_dir="${project_root}/releases"
rollback_file="${shared_env_dir}/rollback-images.env"
current_link="${project_root}/current"
compensation_env=""
new_env=""
old_env=""
current_new="${current_link}.rollback-new"
current_restore="${current_link}.rollback-restore"

fail() {
  echo "$1" >&2
  exit 1
}

cleanup() {
  if [[ -n "${compensation_env}" ]]; then
    rm -f -- "${compensation_env}"
  fi
  if [[ -n "${new_env}" ]]; then
    rm -f -- "${new_env}"
  fi
  if [[ -n "${old_env}" ]]; then
    rm -f -- "${old_env}"
  fi
  rm -f -- "${current_new}" "${current_restore}"
}
trap cleanup EXIT

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

container_running() {
  local container="$1"
  local state
  state="$(docker container inspect \
    --format '{{.State.Running}}' "${container}")"
  if [[ "${state}" != "true" && "${state}" != "false" ]]; then
    echo "容器运行状态无效：${container}" >&2
    return 1
  fi
  printf '%s\n' "${state}"
}

container_exists() {
  docker container inspect "$1" >/dev/null 2>&1
}

write_release_env() {
  local source="$1"
  local destination="$2"
  local app="$3"
  local ocr="$4"
  local qdrant="$5"
  local revision="$6"
  awk -F= \
    -v app="${app}" \
    -v ocr="${ocr}" \
    -v qdrant="${qdrant}" \
    -v revision="${revision}" '
      $1 == "RAG_APP_IMAGE" {$0 = "RAG_APP_IMAGE=" app}
      $1 == "RAG_OCR_IMAGE" {$0 = "RAG_OCR_IMAGE=" ocr}
      $1 == "RAG_QDRANT_IMAGE" {$0 = "RAG_QDRANT_IMAGE=" qdrant}
      $1 == "RAG_RELEASE_REVISION" {
        $0 = "RAG_RELEASE_REVISION=" revision
      }
      {print}
    ' "${source}" > "${destination}"
  chmod 0600 "${destination}"
}

verify_runtime_target() {
  local app_image="$1"
  local ocr_image="$2"
  local qdrant_image="$3"
  local worker_exists="$4"
  local worker_running="$5"
  local worker_image="$6"
  verify_container_image rag-app "${app_image}" || return 1
  verify_container_image rag-ocr "${ocr_image}" || return 1
  verify_container_image rag-qdrant "${qdrant_image}" || return 1
  if [[ "$(container_running rag-app)" != "true" \
    || "$(container_running rag-ocr)" != "true" \
    || "$(container_running rag-qdrant)" != "true" ]]; then
    echo "核心容器没有全部恢复为运行状态。" >&2
    return 1
  fi
  if [[ "${worker_exists}" == "true" ]]; then
    if ! container_exists rag-worker \
      || ! verify_container_image rag-worker "${worker_image}" \
      || [[ "$(container_running rag-worker)" != "${worker_running}" ]]; then
      echo "worker 未恢复到目标镜像或运行状态。" >&2
      return 1
    fi
  elif container_exists rag-worker; then
    echo "补偿目标原本没有 worker，但恢复后仍存在。" >&2
    return 1
  fi
}

verify_rollback_runtime() {
  verify_container_image rag-app "${rollback_app_image}" || return 1
  verify_container_image rag-ocr "${rollback_ocr_image}" || return 1
  verify_container_image rag-qdrant "${rollback_qdrant_image}" || return 1
  if [[ "$(container_running rag-app)" != "true" \
    || "$(container_running rag-ocr)" != "true" \
    || "$(container_running rag-qdrant)" != "true" ]]; then
    echo "回滚后的核心容器没有全部运行。" >&2
    return 1
  fi
  if [[ "${rollback_worker_was_running}" == "true" ]]; then
    if ! container_exists rag-worker \
      || ! verify_container_image rag-worker "${rollback_app_image}" \
      || [[ "$(container_running rag-worker)" != "true" ]]; then
      echo "回滚后的 worker 镜像或运行状态无效。" >&2
      return 1
    fi
  elif container_exists rag-worker \
    && [[ "$(container_running rag-worker)" != "false" ]]; then
    echo "部署前未运行的 worker 在回滚后仍在运行。" >&2
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
require_regular_file \
  "${rollback_release_dir}/IMAGE_ARCHIVES.tsv" \
  "旧 release IMAGE_ARCHIVES.tsv"

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
qdrant_manifest_image_id="$(awk -F '\t' '
  $1 == "images/qdrant-linux-amd64.tar" {
    count += 1
    value = $3
  }
  END {
    if (count == 1) {
      print value
    }
  }
' "${rollback_release_dir}/IMAGE_ARCHIVES.tsv")"
if [[ ! "${qdrant_manifest_image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  fail "旧 release Qdrant 本地 image ID 记录无效。"
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
if [[ "${rollback_qdrant_image}" != "${qdrant_manifest_image_id}" ]]; then
  fail "Qdrant 本地 image ID 与旧 release TSV 记录不一致。"
fi

for key in \
  RAG_APP_IMAGE \
  RAG_OCR_IMAGE \
  RAG_QDRANT_IMAGE \
  RAG_RELEASE_REVISION; do
  exact_value "${env_file}" "${key}" >/dev/null
done
original_env_app_ref="$(exact_value "${env_file}" RAG_APP_IMAGE)"
original_env_ocr_ref="$(exact_value "${env_file}" RAG_OCR_IMAGE)"
original_env_qdrant_ref="$(exact_value "${env_file}" RAG_QDRANT_IMAGE)"
rollback_worker_was_running="$(
  exact_value "${rollback_file}" ROLLBACK_WORKER_WAS_RUNNING
)"
if [[ "${rollback_worker_was_running}" != "true" \
  && "${rollback_worker_was_running}" != "false" ]]; then
  fail "ROLLBACK_WORKER_WAS_RUNNING 必须为 true 或 false。"
fi

if [[ ! -L "${current_link}" ]]; then
  fail "current 必须是指向当前 release 的符号链接。"
fi
original_release_dir="$(readlink -f "${current_link}")"
if [[ "${original_release_dir}" != "${releases_dir}/"* \
  || ! -d "${original_release_dir}" \
  || "$(dirname "${original_release_dir}")" != "${releases_dir}" ]]; then
  fail "current 指向无效 release。"
fi

original_compose="${original_release_dir}/compose.yaml"
original_source_revision_file="${original_release_dir}/SOURCE_REVISION"
require_regular_file "${original_compose}" "回滚调用前的 Compose"
require_regular_file \
  "${original_source_revision_file}" \
  "回滚调用前的 SOURCE_REVISION"
original_revision="$(cat "${original_source_revision_file}")"
if [[ ! "${original_revision}" =~ ^[0-9a-f]{40}$ \
  || "$(wc -l < "${original_source_revision_file}")" != "1" \
  || "$(exact_value "${env_file}" RAG_RELEASE_REVISION)" \
    != "${original_revision}" ]]; then
  fail "回滚调用前的 release revision 无效或与 shared env 不一致。"
fi

original_app_image="$(
  docker container inspect --format '{{.Image}}' rag-app
)"
original_ocr_image="$(
  docker container inspect --format '{{.Image}}' rag-ocr
)"
original_qdrant_image="$(
  docker container inspect --format '{{.Image}}' rag-qdrant
)"
for image in \
  "${original_app_image}" \
  "${original_ocr_image}" \
  "${original_qdrant_image}"; do
  if [[ ! "${image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    fail "回滚调用前的核心容器 image ID 无效：${image}"
  fi
done
if [[ "$(container_running rag-app)" != "true" \
  || "$(container_running rag-ocr)" != "true" \
  || "$(container_running rag-qdrant)" != "true" ]]; then
  fail "回滚调用前的核心容器必须全部运行。"
fi

original_worker_exists=false
original_worker_running=false
original_worker_image=""
if container_exists rag-worker; then
  original_worker_exists=true
  original_worker_running="$(container_running rag-worker)"
  original_worker_image="$(
    docker container inspect --format '{{.Image}}' rag-worker
  )"
  if [[ ! "${original_worker_image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    fail "回滚调用前的 worker image ID 无效。"
  fi
fi

new_env="$(mktemp "${shared_env_dir}/rag.env.rollback-new.XXXXXXXX")"
old_env="$(mktemp "${shared_env_dir}/rag.env.rollback-old.XXXXXXXX")"
compensation_env="$(mktemp \
  "${shared_env_dir}/rag.env.rollback-compensation.XXXXXXXX")"
cp -- "${env_file}" "${old_env}"
chmod 0600 "${old_env}"
write_release_env \
  "${env_file}" \
  "${new_env}" \
  "${rollback_app_image}" \
  "${rollback_ocr_image}" \
  "${rollback_qdrant_image}" \
  "${source_revision}"
write_release_env \
  "${env_file}" \
  "${compensation_env}" \
  "${original_app_image}" \
  "${original_ocr_image}" \
  "${original_qdrant_image}" \
  "${original_revision}"

if [[ "$(exact_value "${new_env}" RAG_APP_IMAGE)" \
    != "${rollback_app_image}" \
  || "$(exact_value "${new_env}" RAG_OCR_IMAGE)" \
    != "${rollback_ocr_image}" \
  || "$(exact_value "${new_env}" RAG_QDRANT_IMAGE)" \
    != "${rollback_qdrant_image}" \
  || "$(exact_value "${new_env}" RAG_RELEASE_REVISION)" \
    != "${source_revision}" ]]; then
  fail "临时回滚环境文件镜像值无效。"
fi

compose_command=(
  docker compose
  --env-file "${new_env}"
  -f "${compose_file}"
)
if [[ "${rollback_worker_was_running}" == "true" ]]; then
  compose_command+=(--profile index)
fi
rollback_services=(rag-qdrant rag-ocr rag-app)
if [[ "${rollback_worker_was_running}" == "true" ]]; then
  rollback_services+=(rag-worker)
fi
port="$(optional_value "${new_env}" RAG_PORT)"
port="${port:-8088}"
compensation_port="$(optional_value "${old_env}" RAG_PORT)"
compensation_port="${compensation_port:-8088}"
if [[ ! "${port}" =~ ^[0-9]+$ \
  || ! "${compensation_port}" =~ ^[0-9]+$ ]]; then
  fail "回滚或补偿端口无效。"
fi

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
  verify_rollback_runtime
  docker compose --env-file "${env_file}" -f "${persisted_compose}" ps
  curl -fsS --max-time 10 "http://127.0.0.1:${port}/live" >/dev/null
}

restore_compensation_runtime() {
  local services=(rag-qdrant rag-ocr rag-app)
  local command=(
    docker compose
    --env-file "${compensation_env}"
    -f "${original_compose}"
  )
  if [[ "${original_worker_exists}" == "true" ]]; then
    command+=(--profile index)
    services+=(rag-worker)
  fi
  "${command[@]}" up -d --no-build --pull never "${services[@]}" \
    || return 1
  if [[ "${original_worker_exists}" == "true" \
    && "${original_worker_running}" == "false" ]]; then
    docker compose --profile index \
      --env-file "${compensation_env}" \
      -f "${original_compose}" \
      stop rag-worker \
      || return 1
  elif [[ "${original_worker_exists}" == "false" ]] \
    && container_exists rag-worker; then
    docker container rm -f rag-worker || return 1
  fi
  verify_runtime_target \
    "${original_app_image}" \
    "${original_ocr_image}" \
    "${original_qdrant_image}" \
    "${original_worker_exists}" \
    "${original_worker_running}" \
    "${original_worker_image}" \
    || return 1
  curl -fsS --max-time 10 \
    "http://127.0.0.1:${compensation_port}/live" >/dev/null
}

restore_compensation_metadata() {
  mv "${old_env}" "${env_file}" || return 1
  old_env=""
  ln -s "${original_release_dir}" "${current_restore}" || return 1
  mv -T "${current_restore}" "${current_link}" || return 1
  if [[ "$(exact_value "${env_file}" RAG_APP_IMAGE)" \
      != "${original_env_app_ref}" \
    || "$(exact_value "${env_file}" RAG_OCR_IMAGE)" \
      != "${original_env_ocr_ref}" \
    || "$(exact_value "${env_file}" RAG_QDRANT_IMAGE)" \
      != "${original_env_qdrant_ref}" \
    || "$(exact_value "${env_file}" RAG_RELEASE_REVISION)" \
      != "${original_revision}" \
    || "$(readlink -f "${current_link}")" \
      != "${original_release_dir}" ]]; then
    return 1
  fi
}

perform_rollback() {
  "${compose_command[@]}" up -d --no-build --pull never \
    "${rollback_services[@]}" \
    || return 1
  if [[ "${rollback_worker_was_running}" == "false" ]] \
    && container_exists rag-worker; then
    docker compose --profile index \
      --env-file "${new_env}" \
      -f "${compose_file}" \
      stop rag-worker \
      || return 1
  fi
  verify_rollback_runtime || return 1
  "${compose_command[@]}" ps || return 1
  curl -fsS --max-time 10 \
    "http://127.0.0.1:${port}/live" >/dev/null \
    || return 1
  mv "${new_env}" "${env_file}" || return 1
  new_env=""
  ln -s "${rollback_release_dir}" "${current_new}" || return 1
  mv -T "${current_new}" "${current_link}" || return 1
  verify_persisted_state
}

runtime_mutated=true
if ! perform_rollback; then
  compensation_ok=false
  runtime_recovered=false
  metadata_recovered=false
  if [[ "${runtime_mutated}" == "true" ]] \
    && restore_compensation_runtime; then
    runtime_recovered=true
  fi
  if restore_compensation_metadata; then
    metadata_recovered=true
  fi
  if [[ "${runtime_recovered}" == "true" \
    && "${metadata_recovered}" == "true" ]] \
    && verify_runtime_target \
      "${original_app_image}" \
      "${original_ocr_image}" \
      "${original_qdrant_image}" \
      "${original_worker_exists}" \
      "${original_worker_running}" \
      "${original_worker_image}"; then
    compensation_ok=true
  fi
  if [[ "${compensation_ok}" == "true" ]]; then
    echo "ROLLBACK_FAILED_RECOVERED" >&2
    exit 1
  fi
  echo "ROLLBACK_FAILED_RECOVERY_FAILED" >&2
  exit 70
fi

rm -f -- "${old_env}" "${compensation_env}"
old_env=""
compensation_env=""
echo "已持久切回上一版镜像；SQLite、Qdrant 和语料 bind mount 未删除。"
