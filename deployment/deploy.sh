#!/usr/bin/env bash
set -euo pipefail
umask 077

release_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
project_root="/data/tyf/RAG"
shared_env_dir="${project_root}/shared/env"
candidate_dir="${shared_env_dir}/candidates"
active_env="${shared_env_dir}/rag.env"
rollback_file="${shared_env_dir}/rollback-images.env"
current_link="${project_root}/current"
compose_file="${release_dir}/compose.yaml"
candidate_env="${1:-}"
active_new=""
rollback_new=""
old_env_snapshot=""
current_new="${current_link}.new"
current_restore="${current_link}.deploy-restore"

fail() {
  echo "$1" >&2
  exit 1
}

cleanup() {
  for path in "${active_new}" "${rollback_new}" "${old_env_snapshot}"; do
    if [[ -n "${path}" ]]; then
      rm -f -- "${path}"
    fi
  done
  rm -f -- "${current_new}" "${current_restore}"
}
trap cleanup EXIT

assert_no_symlink_ancestors() {
  local path="$1"
  local current="${path}"
  while [[ "${current}" != "/" ]]; do
    if [[ -L "${current}" ]]; then
      fail "路径及其祖先不能是符号链接：${path}"
    fi
    current="$(dirname "${current}")"
  done
}

require_regular_0600() {
  local path="$1"
  local label="$2"
  if [[ ! -f "${path}" || -L "${path}" ]]; then
    fail "${label} 必须是普通文件且不能是符号链接。"
  fi
  assert_no_symlink_ancestors "${path}"
  if [[ "$(stat -c '%a' "${path}")" != "600" ]]; then
    fail "${label} 权限必须为 0600。"
  fi
}

exact_env_value() {
  local file="$1"
  local key="$2"
  local count
  count="$(awk -F= -v key="${key}" '$1 == key {count += 1} END {
      print count + 0
    }' "${file}")"
  if [[ "${count}" != "1" ]]; then
    echo "环境文件中的 ${key} 必须恰好出现一次。" >&2
    return 1
  fi
  awk -F= -v key="${key}" '$1 == key {
      sub(/^[^=]*=/, "")
      print
    }' "${file}"
}

optional_env_value() {
  local file="$1"
  local key="$2"
  local count
  count="$(awk -F= -v key="${key}" '$1 == key {count += 1} END {
      print count + 0
    }' "${file}")"
  if [[ "${count}" -gt 1 ]]; then
    echo "环境文件中的 ${key} 重复。" >&2
    return 1
  fi
  if [[ "${count}" == "1" ]]; then
    awk -F= -v key="${key}" '$1 == key {
        sub(/^[^=]*=/, "")
        print
      }' "${file}"
  fi
}

image_manifest_value() {
  local archive="$1"
  local field="$2"
  awk -F '\t' -v archive="${archive}" -v field="${field}" '
    $1 == archive {
      count += 1
      value = $field
    }
    END {
      if (count == 1) {
        print value
      }
    }
  ' "${release_dir}/IMAGE_ARCHIVES.tsv"
}

container_exists() {
  docker container inspect "$1" >/dev/null 2>&1
}

container_image() {
  docker container inspect --format '{{.Image}}' "$1"
}

container_running() {
  local state
  state="$(docker container inspect --format '{{.State.Running}}' "$1")"
  if [[ "${state}" != "true" && "${state}" != "false" ]]; then
    echo "容器运行状态无效：$1" >&2
    return 1
  fi
  printf '%s\n' "${state}"
}

image_id() {
  docker image inspect --format '{{.Id}}' "$1"
}

inspect_loaded_image() {
  local image="$1"
  local expected_id="$2"
  local expected_provenance="$3"
  local actual_id
  local actual_revision
  local architecture
  local operating_system
  architecture="$(docker image inspect --format '{{.Architecture}}' "${image}")"
  operating_system="$(docker image inspect --format '{{.Os}}' "${image}")"
  actual_id="$(image_id "${image}")"
  if [[ "${architecture}/${operating_system}" != "amd64/linux" \
    || "${actual_id}" != "${expected_id}" \
    || ! "${actual_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "镜像平台或本地 image ID 与 runtime 清单不一致。" >&2
    return 1
  fi
  if [[ "${expected_provenance}" =~ ^[0-9a-f]{40}$ ]]; then
    actual_revision="$(docker image inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "${image}")"
    if [[ "${actual_revision}" != "${expected_provenance}" ]]; then
      echo "镜像 revision 与 runtime 包不一致。" >&2
      return 1
    fi
  elif [[ ! "${expected_provenance}" \
    =~ ^qdrant/qdrant@sha256:[0-9a-f]{64}$ ]]; then
    echo "镜像 provenance 记录无效。" >&2
    return 1
  fi
}

verify_container_target() {
  local container="$1"
  local expected_image="$2"
  local expected_running="$3"
  if ! container_exists "${container}" \
    || [[ "$(container_image "${container}")" != "${expected_image}" ]] \
    || [[ "$(container_running "${container}")" != "${expected_running}" ]]; then
    echo "容器未恢复到目标镜像或运行状态：${container}" >&2
    return 1
  fi
}

wait_for_container_health() {
  local container="$1"
  local attempt
  local status
  local max_attempts=30
  for ((attempt = 1; attempt <= max_attempts; attempt += 1)); do
    if ! container_exists "${container}"; then
      echo "健康检查时容器不存在：${container}" >&2
      return 1
    fi
    status="$(docker container inspect \
      --format '{{.State.Health.Status}}' "${container}")"
    case "${status}" in
      healthy)
        return 0
        ;;
      unhealthy)
        echo "容器 health 为 unhealthy：${container}" >&2
        return 1
        ;;
      starting)
        if ((attempt < max_attempts)); then
          sleep 1
        fi
        ;;
      *)
        echo "容器 health 字段缺失或无效：${container}" >&2
        return 1
        ;;
    esac
  done
  echo "容器 health 在固定期限内未达到 healthy：${container}" >&2
  return 1
}

wait_for_app_live() {
  local port="$1"
  local attempt
  local max_attempts=30
  for ((attempt = 1; attempt <= max_attempts; attempt += 1)); do
    if curl -fsS --connect-timeout 2 --max-time 5 \
      "http://127.0.0.1:${port}/live" >/dev/null; then
      return 0
    fi
    if ((attempt < max_attempts)); then
      sleep 1
    fi
  done
  echo "rag-app /live 在固定期限内未返回 200。" >&2
  return 1
}

wait_for_runtime_health() {
  local port="$1"
  wait_for_container_health "rag-qdrant" || return 1
  wait_for_container_health "rag-ocr" || return 1
  wait_for_container_health "rag-app" || return 1
  wait_for_app_live "${port}"
}

if [[ -z "${candidate_env}" || "${candidate_env}" != /* ]]; then
  fail "必须显式提供候选环境文件。"
fi
if [[ "${release_dir}" != "${project_root}/releases/"* ]]; then
  fail "runtime release 必须位于固定 releases 目录。"
fi
bash "${release_dir}/verify-offline.sh"
release_id="$(cat "${release_dir}/RELEASE_ID")"
source_revision="$(cat "${release_dir}/SOURCE_REVISION")"
if [[ "$(basename "${release_dir}")" != "${release_id}" \
  || ! "${source_revision}" =~ ^[0-9a-f]{40}$ ]]; then
  fail "release ID 或 SOURCE_REVISION 无效。"
fi
require_regular_0600 "${candidate_env}" "候选环境文件"
candidate_env="$(realpath -e "${candidate_env}")"
if [[ "${candidate_env}" != "${candidate_dir}/${release_id}.env" \
  || "${candidate_env}" == "${active_env}" ]]; then
  fail "候选环境文件必须位于固定 candidates 目录并匹配 release ID。"
fi
if grep -Eq 'REPLACE_' "${candidate_env}"; then
  fail "候选环境文件仍含占位符。"
fi
candidate_revision="$(exact_env_value \
  "${candidate_env}" RAG_RELEASE_REVISION)"
if [[ "${candidate_revision}" != "${source_revision}" ]]; then
  fail "候选 revision 必须等于 release SOURCE_REVISION。"
fi

state_path="$(exact_env_value "${candidate_env}" RAG_STATE_PATH)"
qdrant_path="$(exact_env_value "${candidate_env}" RAG_QDRANT_PATH)"
docs_path="$(exact_env_value "${candidate_env}" RAG_DOCS_PATH)"
if [[ "${state_path}" != "${project_root}/data/state" \
  || "${qdrant_path}" != "${project_root}/data/qdrant" \
  || "${docs_path}" != "${project_root}/shared/corpora/"*/docs ]]; then
  fail "状态、Qdrant 或 DOCX 路径未固定在项目根。"
fi
if [[ ! -d "${state_path}" || -L "${state_path}" \
  || ! -d "${qdrant_path}" || -L "${qdrant_path}" \
  || ! -d "${docs_path}" || -L "${docs_path}" ]]; then
  fail "state、Qdrant 与 docs 必须是预先创建的真实目录。"
fi
if [[ "$(stat -c '%u' "${state_path}")" != "10001" \
  || "$(stat -c '%a' "${state_path}")" != "700" ]]; then
  fail "state 目录必须归 UID 10001 且权限为 0700。"
fi
if find "${docs_path}" -xdev ! -uid 10001 -print -quit | grep -q .; then
  fail "语料目录必须全部归 UID 10001 所有。"
fi
port="$(optional_env_value "${candidate_env}" RAG_PORT)"
port="${port:-8088}"
if [[ ! "${port}" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
  fail "RAG_PORT 无效。"
fi

app_image="$(exact_env_value "${candidate_env}" RAG_APP_IMAGE)"
ocr_image="$(exact_env_value "${candidate_env}" RAG_OCR_IMAGE)"
qdrant_image="$(exact_env_value "${candidate_env}" RAG_QDRANT_IMAGE)"
app_archive="images/docx-rag-linux-amd64.tar"
ocr_archive="images/docx-rag-ocr-linux-amd64.tar"
qdrant_archive="images/qdrant-linux-amd64.tar"
if [[ "${app_image}" != "$(image_manifest_value "${app_archive}" 2)" \
  || "${ocr_image}" != "$(image_manifest_value "${ocr_archive}" 2)" \
  || "${qdrant_image}" != "$(image_manifest_value "${qdrant_archive}" 2)" ]]; then
  fail "候选镜像引用与 runtime 白名单不一致。"
fi
new_app_id="$(image_manifest_value "${app_archive}" 3)"
new_ocr_id="$(image_manifest_value "${ocr_archive}" 3)"
new_qdrant_id="$(image_manifest_value "${qdrant_archive}" 3)"
app_provenance="$(image_manifest_value "${app_archive}" 4)"
ocr_provenance="$(image_manifest_value "${ocr_archive}" 4)"
qdrant_provenance="$(image_manifest_value "${qdrant_archive}" 4)"
if [[ "${app_provenance}" != "${source_revision}" \
  || "${ocr_provenance}" != "${source_revision}" ]]; then
  fail "app/OCR provenance 必须等于 release revision。"
fi

container_names="$(docker ps -a --format '{{.Names}}')"
existing_count="$(printf '%s\n' "${container_names}" | awk '
  $0 == "rag-app" || $0 == "rag-ocr" || $0 == "rag-qdrant" {
    count += 1
  }
  END {print count + 0}
')"
if [[ "${existing_count}" != "0" && "${existing_count}" != "3" ]]; then
  fail "已有 rag 核心容器不完整，拒绝覆盖部署。"
fi

worker_exists=false
worker_running=false
worker_image="-"
if container_exists rag-worker; then
  worker_exists=true
  worker_running="$(container_running rag-worker)"
  worker_image="$(container_image rag-worker)"
fi
old_runtime=false
old_active_exists=false
old_release=""
old_revision=""
old_app_image=""
old_ocr_image=""
old_qdrant_image=""
old_app_running=false
old_ocr_running=false
old_qdrant_running=false
old_current_exists=false
old_env_sha=""
if [[ -f "${active_env}" && ! -L "${active_env}" ]]; then
  require_regular_0600 "${active_env}" "活动环境文件"
  old_active_exists=true
  old_env_snapshot="$(mktemp \
    "${shared_env_dir}/.rag.env.deploy-old.XXXXXXXX")"
  cp -- "${active_env}" "${old_env_snapshot}"
  chmod 0600 "${old_env_snapshot}"
  old_env_sha="$(sha256sum "${old_env_snapshot}" | awk '{print $1}')"
elif [[ -e "${active_env}" || -L "${active_env}" ]]; then
  fail "活动环境路径不是安全普通文件。"
fi
if [[ -L "${current_link}" ]]; then
  old_current_exists=true
  old_release="$(readlink -f "${current_link}")"
fi
if [[ "${existing_count}" == "3" ]]; then
  old_runtime=true
  if [[ "${old_active_exists}" != "true" \
    || "${old_current_exists}" != "true" \
    || "${old_release}" != "${project_root}/releases/"* \
    || ! -f "${old_release}/SOURCE_REVISION" \
    || ! -f "${old_release}/compose.yaml" ]]; then
    fail "升级要求安全 active env 与 current release。"
  fi
  old_revision="$(cat "${old_release}/SOURCE_REVISION")"
  if [[ "$(exact_env_value "${active_env}" RAG_RELEASE_REVISION)" \
    != "${old_revision}" ]]; then
    fail "active env 不是 current release 的实际配置。"
  fi
  old_app_image="$(container_image rag-app)"
  old_ocr_image="$(container_image rag-ocr)"
  old_qdrant_image="$(container_image rag-qdrant)"
  old_app_running="$(container_running rag-app)"
  old_ocr_running="$(container_running rag-ocr)"
  old_qdrant_running="$(container_running rag-qdrant)"
  if [[ "$(image_id "$(exact_env_value "${active_env}" RAG_APP_IMAGE)")" \
      != "${old_app_image}" \
    || "$(image_id "$(exact_env_value "${active_env}" RAG_OCR_IMAGE)")" \
      != "${old_ocr_image}" \
    || "$(image_id "$(exact_env_value "${active_env}" RAG_QDRANT_IMAGE)")" \
      != "${old_qdrant_image}" ]]; then
    fail "active env 镜像与当前容器不一致。"
  fi
fi
if [[ "${worker_exists}" == "true" \
  && "${old_active_exists}" != "true" ]]; then
  fail "既有 worker 要求可恢复的 active env。"
fi

commit_candidate_env() {
  active_new="$(mktemp "${shared_env_dir}/.rag.env.active-new.XXXXXXXX")" \
    || return 1
  cp -- "${candidate_env}" "${active_new}" || return 1
  chmod 0600 "${active_new}" || return 1
  if [[ "$(sha256sum "${active_new}" | awk '{print $1}')" \
    != "$(sha256sum "${candidate_env}" | awk '{print $1}')" ]]; then
    return 1
  fi
  mv -T "${active_new}" "${active_env}" || return 1
  active_new=""
}

publish_rollback_state() {
  if [[ "${old_runtime}" != "true" ]]; then
    return 0
  fi
  rollback_new="$(mktemp \
    "${shared_env_dir}/.rollback-images.env.new.XXXXXXXX")" \
    || return 1
  {
    printf 'ROLLBACK_SCHEMA_VERSION=2\n'
    printf 'ROLLBACK_RELEASE_DIR=%s\n' "${old_release}"
    printf 'ROLLBACK_APP_IMAGE=%s\n' "${old_app_image}"
    printf 'ROLLBACK_OCR_IMAGE=%s\n' "${old_ocr_image}"
    printf 'ROLLBACK_QDRANT_IMAGE=%s\n' "${old_qdrant_image}"
    printf 'ROLLBACK_WORKER_EXISTS=%s\n' "${worker_exists}"
    printf 'ROLLBACK_WORKER_WAS_RUNNING=%s\n' "${worker_running}"
    printf 'ROLLBACK_WORKER_IMAGE=%s\n' "${worker_image}"
    printf 'ROLLBACK_SOURCE_REVISION=%s\n' "${old_revision}"
    printf 'ROLLBACK_ENV_SHA256=%s\n' "${old_env_sha}"
    printf 'ROLLBACK_ENV_BASE64=%s\n' "$(
      base64 -w 0 "${old_env_snapshot}"
    )"
  } > "${rollback_new}" || return 1
  chmod 0600 "${rollback_new}" || return 1
  mv -T "${rollback_new}" "${rollback_file}" || return 1
  rollback_new=""
}

verify_final_state() {
  if [[ "$(readlink -f "${current_link}")" != "${release_dir}" \
    || "$(sha256sum "${active_env}" | awk '{print $1}')" \
      != "$(sha256sum "${candidate_env}" | awk '{print $1}')" ]]; then
    return 1
  fi
  verify_container_target rag-app "${new_app_id}" true || return 1
  verify_container_target rag-ocr "${new_ocr_id}" true || return 1
  verify_container_target rag-qdrant "${new_qdrant_id}" true || return 1
  docker compose --env-file "${active_env}" \
    -f "${current_link}/compose.yaml" config -q
}

perform_deploy() {
  if [[ "${worker_running}" == "true" ]]; then
    docker compose --profile index \
      --env-file "${active_env}" \
      -f "${old_release}/compose.yaml" stop rag-worker \
      || return 1
  fi
  docker load --input "${release_dir}/${app_archive}" || return 1
  docker load --input "${release_dir}/${ocr_archive}" || return 1
  docker load --input "${release_dir}/${qdrant_archive}" || return 1
  inspect_loaded_image \
    "${app_image}" "${new_app_id}" "${app_provenance}" || return 1
  inspect_loaded_image \
    "${ocr_image}" "${new_ocr_id}" "${ocr_provenance}" || return 1
  inspect_loaded_image \
    "${qdrant_image}" "${new_qdrant_id}" "${qdrant_provenance}" || return 1
  docker compose --env-file "${candidate_env}" \
    -f "${compose_file}" config -q || return 1
  docker compose --env-file "${candidate_env}" -f "${compose_file}" \
    up -d --no-build --pull never || return 1
  verify_container_target rag-app "${new_app_id}" true || return 1
  verify_container_target rag-ocr "${new_ocr_id}" true || return 1
  verify_container_target rag-qdrant "${new_qdrant_id}" true || return 1
  wait_for_runtime_health "${port}" || return 1
  docker compose --env-file "${candidate_env}" \
    -f "${compose_file}" ps || return 1
  if container_exists rag-worker \
    && [[ "$(container_running rag-worker)" != "false" ]]; then
    echo "新核心健康后 worker 不得继续运行。" >&2
    return 1
  fi
  commit_candidate_env || return 1
  ln -s "${release_dir}" "${current_new}" || return 1
  mv -T "${current_new}" "${current_link}" || return 1
  verify_final_state || return 1
  publish_rollback_state || return 1
}

restore_original_runtime() {
  local service
  if [[ "${old_runtime}" == "true" ]]; then
    docker compose --env-file "${old_env_snapshot}" \
      -f "${old_release}/compose.yaml" \
      up -d --no-build --pull never rag-qdrant rag-ocr rag-app \
      || return 1
    for service in rag-app rag-ocr rag-qdrant; do
      case "${service}" in
        rag-app) expected="${old_app_running}" ;;
        rag-ocr) expected="${old_ocr_running}" ;;
        rag-qdrant) expected="${old_qdrant_running}" ;;
      esac
      if [[ "${expected}" == "false" ]]; then
        docker compose --env-file "${old_env_snapshot}" \
          -f "${old_release}/compose.yaml" stop "${service}" || return 1
      fi
    done
    verify_container_target \
      rag-app "${old_app_image}" "${old_app_running}" || return 1
    verify_container_target \
      rag-ocr "${old_ocr_image}" "${old_ocr_running}" || return 1
    verify_container_target \
      rag-qdrant "${old_qdrant_image}" "${old_qdrant_running}" || return 1
    if [[ "${old_app_running}" == "true" \
      && "${old_ocr_running}" == "true" \
      && "${old_qdrant_running}" == "true" ]]; then
      old_port="$(optional_env_value "${old_env_snapshot}" RAG_PORT)"
      wait_for_runtime_health "${old_port:-8088}" || return 1
    fi
  else
    for service in rag-app rag-ocr rag-qdrant; do
      if container_exists "${service}"; then
        docker container rm -f "${service}" || return 1
      fi
    done
  fi
  if [[ "${worker_exists}" == "true" ]]; then
    docker compose --profile index \
      --env-file "${old_env_snapshot}" \
      -f "${old_release}/compose.yaml" \
      up -d --no-deps --no-build --pull never rag-worker \
      || return 1
    if [[ "${worker_running}" == "false" ]]; then
      docker compose --profile index \
        --env-file "${old_env_snapshot}" \
        -f "${old_release}/compose.yaml" stop rag-worker || return 1
    fi
    verify_container_target \
      rag-worker "${worker_image}" "${worker_running}" || return 1
  elif container_exists rag-worker; then
    docker container rm -f rag-worker || return 1
  fi
}

restore_original_metadata() {
  if [[ "${old_active_exists}" == "true" ]]; then
    active_new="$(mktemp "${shared_env_dir}/.rag.env.restore.XXXXXXXX")" \
      || return 1
    cp -- "${old_env_snapshot}" "${active_new}" || return 1
    chmod 0600 "${active_new}" || return 1
    mv -T "${active_new}" "${active_env}" || return 1
    active_new=""
  else
    rm -f -- "${active_env}"
  fi
  if [[ "${old_current_exists}" == "true" ]]; then
    ln -s "${old_release}" "${current_restore}" || return 1
    mv -T "${current_restore}" "${current_link}" || return 1
  else
    rm -f -- "${current_link}"
  fi
  if [[ "${old_active_exists}" == "true" \
    && "$(sha256sum "${active_env}" | awk '{print $1}')" \
      != "${old_env_sha}" ]]; then
    return 1
  fi
}

if ! perform_deploy; then
  runtime_recovered=false
  metadata_recovered=false
  if restore_original_runtime; then
    runtime_recovered=true
  fi
  if restore_original_metadata; then
    metadata_recovered=true
  fi
  if [[ "${runtime_recovered}" == "true" \
    && "${metadata_recovered}" == "true" ]]; then
    echo "DEPLOY_FAILED_RECOVERED" >&2
    exit 1
  fi
  echo "DEPLOY_FAILED_RECOVERY_FAILED" >&2
  exit 70
fi

echo "release ${release_id} 已健康提交；仅 rag-app 暴露端口 ${port}。"
