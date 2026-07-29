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
recovery_env=""
current_new="${current_link}.new"
current_recovery="${current_link}.deploy-recovery-new"
rollback_new=""

cleanup() {
  if [[ -n "${recovery_env}" ]]; then
    rm -f -- "${recovery_env}"
  fi
  if [[ -n "${rollback_new}" ]]; then
    rm -f -- "${rollback_new}"
  fi
  rm -f -- "${current_new}" "${current_recovery}"
}
trap cleanup EXIT

env_value() {
  local key="$1"
  awk -F= -v key="${key}" '$1 == key {sub(/^[^=]*=/, ""); print}' \
    "${env_file}" \
    | tail -n 1
}

exact_env_value() {
  local key="$1"
  local count
  count="$(awk -F= -v key="${key}" '$1 == key {count += 1} END {
      print count + 0
    }' "${env_file}")"
  if [[ "${count}" != "1" ]]; then
    echo "环境文件中的 ${key} 必须恰好出现一次。" >&2
    return 1
  fi
  awk -F= -v key="${key}" '$1 == key {
      sub(/^[^=]*=/, "")
      print
    }' "${env_file}"
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
    return 1
  fi
  if [[ "${expected_revision}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    if [[ "${image_id}" != "${expected_revision}" ]]; then
      echo "镜像 ID 与 runtime 包固定 digest 不一致：${image}" >&2
      return 1
    fi
  elif [[ "${expected_revision}" != "-" ]]; then
    actual_revision="$(docker image inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "${image}")"
    if [[ "${actual_revision}" != "${expected_revision}" ]]; then
      echo "镜像 revision 与 runtime 包不一致：${image}" >&2
      return 1
    fi
  fi
}

container_image() {
  local container="$1"
  docker container inspect --format '{{.Image}}' "${container}"
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

verify_container_target() {
  local container="$1"
  local expected_image="$2"
  if [[ "$(container_image "${container}")" != "${expected_image}" \
    || "$(container_running "${container}")" != "true" ]]; then
    echo "容器未恢复到目标镜像或运行状态：${container}" >&2
    return 1
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
release_revision="$(exact_env_value RAG_RELEASE_REVISION)"
if [[ ! "${release_revision}" =~ ^[0-9a-f]{40}$ \
  || "${release_revision}" != "${source_revision}" ]]; then
  echo "RAG_RELEASE_REVISION 必须等于 release SOURCE_REVISION。" >&2
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

app_image="$(exact_env_value RAG_APP_IMAGE)"
ocr_image="$(exact_env_value RAG_OCR_IMAGE)"
qdrant_image="$(exact_env_value RAG_QDRANT_IMAGE)"
if [[ "${app_image}" \
  != "$(image_manifest_value images/docx-rag-linux-amd64.tar 2)" \
  || "${ocr_image}" \
  != "$(image_manifest_value images/docx-rag-ocr-linux-amd64.tar 2)" \
  || "${qdrant_image}" \
  != "$(image_manifest_value images/qdrant-linux-amd64.tar 2)" ]]; then
  echo "环境文件镜像引用与 runtime 包白名单不一致。" >&2
  exit 1
fi

container_names="$(docker ps -a --format '{{.Names}}')"
existing_count="$(printf '%s\n' "${container_names}" \
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
worker_exists=false
if grep -qx 'rag-worker' <<< "${container_names}"; then
  worker_exists=true
fi
worker_was_running=false
if [[ "${worker_exists}" == "true" ]]; then
  worker_was_running="$(container_running rag-worker)"
fi

old_runtime_available=false
old_release=""
old_compose=""
old_revision=""
old_app_image=""
old_ocr_image=""
old_qdrant_image=""
worker_only_runtime=false
if [[ "${existing_count}" == "3" ]]; then
  old_runtime_available=true
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
  old_compose="${old_release}/compose.yaml"
  if [[ ! -f "${old_compose}" || -L "${old_compose}" \
    || ! -f "${old_release}/SOURCE_REVISION" \
    || -L "${old_release}/SOURCE_REVISION" ]]; then
    echo "current release 缺少可恢复的 Compose 或 revision。" >&2
    exit 1
  fi
  old_revision="$(cat "${old_release}/SOURCE_REVISION")"
  if [[ ! "${old_revision}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "current release SOURCE_REVISION 无效。" >&2
    exit 1
  fi
  old_app_image="$(container_image rag-app)"
  old_ocr_image="$(container_image rag-ocr)"
  old_qdrant_image="$(container_image rag-qdrant)"
  for old_image in \
    "${old_app_image}" \
    "${old_ocr_image}" \
    "${old_qdrant_image}"; do
    if [[ ! "${old_image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
      echo "部署前核心容器 image ID 无效：${old_image}" >&2
      exit 1
    fi
  done
  if [[ "${worker_was_running}" == "true" \
    && "$(container_image rag-worker)" != "${old_app_image}" ]]; then
    echo "部署前 worker 未使用当前 app 镜像。" >&2
    exit 1
  fi
  recovery_env="$(mktemp \
    "${shared_env_dir}/rag.env.deploy-recovery.XXXXXXXX")"
  write_release_env \
    "${env_file}" \
    "${recovery_env}" \
    "${old_app_image}" \
    "${old_ocr_image}" \
    "${old_qdrant_image}" \
    "${old_revision}"
  docker compose --env-file "${recovery_env}" \
    -f "${old_compose}" config -q
  rollback_new="$(mktemp \
    "${shared_env_dir}/rollback-images.env.new.XXXXXXXX")"
  {
    printf 'ROLLBACK_RELEASE_DIR=%s\n' "${old_release}"
    printf 'ROLLBACK_APP_IMAGE=%s\n' "${old_app_image}"
    printf 'ROLLBACK_OCR_IMAGE=%s\n' "${old_ocr_image}"
    printf 'ROLLBACK_QDRANT_IMAGE=%s\n' "${old_qdrant_image}"
    printf 'ROLLBACK_WORKER_WAS_RUNNING=%s\n' "${worker_was_running}"
  } > "${rollback_new}"
  chmod 0600 "${rollback_new}"
  mv "${rollback_new}" "${rollback_file}"
  rollback_new=""
elif [[ "${worker_was_running}" == "true" ]]; then
  worker_only_runtime=true
  if [[ ! -L "${current_link}" ]]; then
    echo "运行中的孤立 worker 缺少 current release 链接。" >&2
    exit 1
  fi
  old_release="$(readlink -f "${current_link}")"
  old_compose="${old_release}/compose.yaml"
  if [[ "${old_release}" != "${project_root}/releases/"* \
    || ! -f "${old_compose}" \
    || -L "${old_compose}" \
    || ! -f "${old_release}/SOURCE_REVISION" \
    || -L "${old_release}/SOURCE_REVISION" ]]; then
    echo "运行中的孤立 worker 缺少可恢复的旧 release。" >&2
    exit 1
  fi
  old_revision="$(cat "${old_release}/SOURCE_REVISION")"
  old_app_image="$(container_image rag-worker)"
  if [[ ! "${old_revision}" =~ ^[0-9a-f]{40}$ \
    || ! "${old_app_image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "运行中的孤立 worker 恢复信息无效。" >&2
    exit 1
  fi
  recovery_env="$(mktemp \
    "${shared_env_dir}/rag.env.deploy-recovery.XXXXXXXX")"
  write_release_env \
    "${env_file}" \
    "${recovery_env}" \
    "${old_app_image}" \
    "${ocr_image}" \
    "${qdrant_image}" \
    "${old_revision}"
  docker compose --env-file "${recovery_env}" \
    -f "${old_compose}" config -q
fi

perform_deploy() {
  if [[ "${worker_was_running}" == "true" ]]; then
    docker compose --profile index \
      --env-file "${recovery_env}" \
      -f "${old_compose}" \
      stop rag-worker \
      || return 1
    if [[ "$(container_running rag-worker)" != "false" ]]; then
      echo "旧 worker 未停止，拒绝升级核心服务。" >&2
      return 1
    fi
  fi
  docker load --input \
    "${release_dir}/images/docx-rag-linux-amd64.tar" \
    || return 1
  docker load --input \
    "${release_dir}/images/docx-rag-ocr-linux-amd64.tar" \
    || return 1
  docker load --input \
    "${release_dir}/images/qdrant-linux-amd64.tar" \
    || return 1
  inspect_image "${app_image}" "${source_revision}" || return 1
  inspect_image "${ocr_image}" "${source_revision}" || return 1
  inspect_image \
    "${qdrant_image}" \
    "$(image_manifest_value images/qdrant-linux-amd64.tar 3)" \
    || return 1
  new_app_image_id="$(
    docker image inspect --format '{{.Id}}' "${app_image}"
  )" || return 1
  new_ocr_image_id="$(
    docker image inspect --format '{{.Id}}' "${ocr_image}"
  )" || return 1
  new_qdrant_image_id="$(
    docker image inspect --format '{{.Id}}' "${qdrant_image}"
  )" || return 1
  docker compose --env-file "${env_file}" \
    -f "${compose_file}" config -q \
    || return 1
  docker compose --env-file "${env_file}" \
    -f "${compose_file}" \
    up -d --no-build --pull never \
    || return 1
  docker compose --env-file "${env_file}" \
    -f "${compose_file}" ps \
    || return 1
  verify_container_target rag-app "${new_app_image_id}" || return 1
  verify_container_target rag-ocr "${new_ocr_image_id}" || return 1
  verify_container_target rag-qdrant "${new_qdrant_image_id}" || return 1
  if docker container inspect rag-worker >/dev/null 2>&1 \
    && [[ "$(container_running rag-worker)" != "false" ]]; then
    echo "新核心服务启动后 worker 不得继续运行。" >&2
    return 1
  fi
  ln -s "${release_dir}" "${current_new}" || return 1
  mv -T "${current_new}" "${current_link}" || return 1
}

verify_old_runtime() {
  verify_container_target rag-app "${old_app_image}" || return 1
  verify_container_target rag-ocr "${old_ocr_image}" || return 1
  verify_container_target rag-qdrant "${old_qdrant_image}" || return 1
  if [[ "${worker_was_running}" == "true" ]]; then
    verify_container_target rag-worker "${old_app_image}" || return 1
  elif docker container inspect rag-worker >/dev/null 2>&1 \
    && [[ "$(container_running rag-worker)" != "false" ]]; then
    echo "补偿后 worker 状态与部署前不一致。" >&2
    return 1
  fi
}

recover_existing_runtime() {
  local recovery_services=(rag-qdrant rag-ocr rag-app)
  if [[ "${worker_was_running}" == "true" ]]; then
    recovery_services+=(rag-worker)
  fi
  local recovery_command=(
    docker compose
    --env-file "${recovery_env}"
    -f "${old_compose}"
  )
  if [[ "${worker_was_running}" == "true" ]]; then
    recovery_command+=(--profile index)
  fi
  "${recovery_command[@]}" up -d --no-build --pull never \
    "${recovery_services[@]}" \
    || return 1
  if [[ "${worker_was_running}" != "true" \
    && "${worker_exists}" == "true" ]]; then
    docker compose --profile index \
      --env-file "${recovery_env}" \
      -f "${old_compose}" \
      stop rag-worker \
      || return 1
  fi
  verify_old_runtime || return 1
  curl -fsS --max-time 10 \
    "http://127.0.0.1:${port}/live" >/dev/null \
    || return 1
  mv "${recovery_env}" "${env_file}" || return 1
  recovery_env=""
  ln -s "${old_release}" "${current_recovery}" || return 1
  mv -T "${current_recovery}" "${current_link}" || return 1
  if [[ "$(exact_env_value RAG_APP_IMAGE)" != "${old_app_image}" \
    || "$(exact_env_value RAG_OCR_IMAGE)" != "${old_ocr_image}" \
    || "$(exact_env_value RAG_QDRANT_IMAGE)" != "${old_qdrant_image}" \
    || "$(exact_env_value RAG_RELEASE_REVISION)" != "${old_revision}" \
    || "$(readlink -f "${current_link}")" != "${old_release}" ]]; then
    echo "补偿后的 env/current 与旧运行目标不一致。" >&2
    return 1
  fi
  verify_old_runtime
}

recover_empty_runtime() {
  local container
  for container in rag-app rag-ocr rag-qdrant; do
    if docker container inspect "${container}" >/dev/null 2>&1; then
      docker container rm -f "${container}" || return 1
    fi
  done
  for container in rag-app rag-ocr rag-qdrant; do
    if docker container inspect "${container}" >/dev/null 2>&1; then
      echo "首次部署补偿后仍存在核心容器：${container}" >&2
      return 1
    fi
  done
  if [[ "${worker_only_runtime}" == "true" ]]; then
    docker compose --profile index \
      --env-file "${recovery_env}" \
      -f "${old_compose}" \
      up -d --no-deps --no-build --pull never rag-worker \
      || return 1
    verify_container_target rag-worker "${old_app_image}" || return 1
  elif [[ "${worker_exists}" != "true" ]] \
    && docker container inspect rag-worker >/dev/null 2>&1; then
    echo "首次部署补偿错误创建了 worker。" >&2
    return 1
  fi
}

if ! perform_deploy; then
  if [[ "${old_runtime_available}" == "true" ]]; then
    recovered=false
    if recover_existing_runtime; then
      recovered=true
    fi
  else
    recovered=false
    if recover_empty_runtime; then
      recovered=true
    fi
  fi
  if [[ "${recovered}" == "true" ]]; then
    echo "DEPLOY_FAILED_RECOVERED" >&2
    exit 1
  fi
  echo "DEPLOY_FAILED_RECOVERY_FAILED" >&2
  exit 70
fi

echo "release ${release_id} 已启动；仅 rag-app 暴露宿主机端口 ${port}。"
