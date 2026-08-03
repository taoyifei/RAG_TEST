#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=deployment/qdrant-policy.sh
source "${script_dir}/qdrant-policy.sh"
project_root="/data/tyf/RAG"
shared_env_dir="${project_root}/shared/env"
active_env="${shared_env_dir}/rag.env"
rollback_file="${shared_env_dir}/rollback-images.env"
releases_dir="${project_root}/releases"
current_link="${project_root}/current"
requested_env="${1:-${active_env}}"
target_env=""
original_env=""
active_new=""
current_new="${current_link}.rollback-new"
current_restore="${current_link}.rollback-restore"
QDRANT_HEALTH_TIMEOUT_SECONDS=60
QDRANT_READY_TIMEOUT_SECONDS=60
APP_HEALTH_TIMEOUT_SECONDS=60
APP_LIVE_TIMEOUT_SECONDS=60
OCR_HEALTH_TIMEOUT_SECONDS=240
HEALTH_POLL_INTERVAL_SECONDS=1
ORIGINAL_APP_EXISTS=false
ORIGINAL_APP_RUNNING=false
ORIGINAL_APP_IMAGE="-"
ORIGINAL_OCR_EXISTS=false
ORIGINAL_OCR_RUNNING=false
ORIGINAL_OCR_IMAGE="-"
ORIGINAL_QDRANT_EXISTS=false
ORIGINAL_QDRANT_RUNNING=false
ORIGINAL_QDRANT_IMAGE="-"
ORIGINAL_WORKER_EXISTS=false
ORIGINAL_WORKER_RUNNING=false
ORIGINAL_WORKER_IMAGE="-"

fail() {
  echo "$1" >&2
  exit 1
}

cleanup() {
  for path in "${target_env}" "${original_env}" "${active_new}"; do
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

exact_value() {
  local file="$1"
  local key="$2"
  local count
  count="$(awk -F= -v key="${key}" '$1 == key {count += 1} END {
      print count + 0
    }' "${file}")"
  if [[ "${count}" != "1" ]]; then
    echo "${key} 必须恰好出现一次。" >&2
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
    return 1
  fi
  if [[ "${count}" == "1" ]]; then
    exact_value "${file}" "${key}"
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
  ' "${rollback_release}/IMAGE_ARCHIVES.tsv"
}

container_exists() {
  docker container inspect "$1" >/dev/null 2>&1
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

container_image() {
  docker container inspect --format '{{.Image}}' "$1"
}

capture_container_state() {
  local container="$1"
  local prefix="$2"
  if container_exists "${container}"; then
    printf -v "ORIGINAL_${prefix}_EXISTS" '%s' true
    printf -v "ORIGINAL_${prefix}_RUNNING" '%s' "$(
      container_running "${container}"
    )"
    printf -v "ORIGINAL_${prefix}_IMAGE" '%s' "$(
      container_image "${container}"
    )"
  fi
}

verify_container_state() {
  local container="$1"
  local expected_exists="$2"
  local expected_running="$3"
  local expected_image="$4"
  if [[ "${expected_exists}" == "false" ]]; then
    if container_exists "${container}"; then
      echo "容器原本不存在但补偿后仍存在：${container}" >&2
      return 1
    fi
    return 0
  fi
  if ! container_exists "${container}" \
    || [[ "$(container_running "${container}")" != "${expected_running}" ]] \
    || [[ "$(container_image "${container}")" != "${expected_image}" ]]; then
    echo "容器未恢复到调用前精确状态：${container}" >&2
    return 1
  fi
}

image_id() {
  docker image inspect --format '{{.Id}}' "$1"
}

inspect_loaded_image() {
  local image="$1"
  local expected_manifest_id="$2"
  local expected_config_id="$3"
  local expected_platform="$4"
  local expected_revision="$5"
  local arguments=(
    python3
    "${script_dir}/scripts/docker_archive_loaded_identity.py"
    --manifest-digest "${expected_manifest_id}"
    --config-digest "${expected_config_id}"
    --platform "${expected_platform}"
  )
  if [[ "${expected_revision}" =~ ^[0-9a-f]{40}$ ]]; then
    arguments+=(--expected-revision "${expected_revision}")
  fi
  if ! docker image inspect "${image}" | "${arguments[@]}" >/dev/null; then
    echo "rollback 镜像离线身份不一致。" >&2
    return 1
  fi
}

wait_for_container_health() {
  local container="$1"
  local timeout_seconds="$2"
  local deadline
  local now
  local remaining
  local sleep_seconds
  local status
  deadline="$(($(date +%s) + timeout_seconds))"
  while true; do
    now="$(date +%s)"
    if ((now >= deadline)); then
      echo "容器 health 在 ${timeout_seconds} 秒内未达到 healthy：${container}" >&2
      return 1
    fi
    if ! container_exists "${container}"; then
      echo "健康检查时容器不存在：${container}" >&2
      return 1
    fi
    if ! status="$(docker container inspect \
      --format '{{.State.Health.Status}}' "${container}")"; then
      echo "容器 health 字段缺失或无效：${container}" >&2
      return 1
    fi
    case "${status}" in
      healthy)
        return 0
        ;;
      unhealthy)
        echo "容器 health 为 unhealthy：${container}" >&2
        return 1
        ;;
      starting) ;;
      *)
        echo "容器 health 字段缺失或无效：${container}" >&2
        return 1
        ;;
    esac
    now="$(date +%s)"
    remaining="$((deadline - now))"
    if ((remaining <= 0)); then
      echo "容器 health 在 ${timeout_seconds} 秒内未达到 healthy：${container}" >&2
      return 1
    fi
    sleep_seconds="${HEALTH_POLL_INTERVAL_SECONDS}"
    if ((sleep_seconds > remaining)); then
      sleep_seconds="${remaining}"
    fi
    sleep "${sleep_seconds}"
  done
}

wait_for_app_live() {
  local port="$1"
  local timeout_seconds="$2"
  local deadline
  local now
  local remaining
  local request_timeout
  local sleep_seconds
  deadline="$(($(date +%s) + timeout_seconds))"
  while true; do
    now="$(date +%s)"
    remaining="$((deadline - now))"
    if ((remaining <= 0)); then
      echo "rag-app /live 在 ${timeout_seconds} 秒内未返回 200。" >&2
      return 1
    fi
    request_timeout=5
    if ((request_timeout > remaining)); then
      request_timeout="${remaining}"
    fi
    if curl -fsS --connect-timeout 2 --max-time "${request_timeout}" \
      "http://127.0.0.1:${port}/live" >/dev/null; then
      return 0
    fi
    now="$(date +%s)"
    remaining="$((deadline - now))"
    if ((remaining <= 0)); then
      echo "rag-app /live 在 ${timeout_seconds} 秒内未返回 200。" >&2
      return 1
    fi
    sleep_seconds="${HEALTH_POLL_INTERVAL_SECONDS}"
    if ((sleep_seconds > remaining)); then
      sleep_seconds="${remaining}"
    fi
    sleep "${sleep_seconds}"
  done
}

wait_for_qdrant_ready() {
  local timeout_seconds="$1"
  local deadline
  local now
  local remaining
  local request_timeout
  local sleep_seconds
  deadline="$(($(date +%s) + timeout_seconds))"
  while true; do
    now="$(date +%s)"
    remaining="$((deadline - now))"
    if ((remaining <= 0)); then
      echo "Qdrant /readyz 在 ${timeout_seconds} 秒内未返回 200。" >&2
      return 1
    fi
    if ! container_exists rag-app || ! container_exists rag-qdrant; then
      echo "Qdrant /readyz 检查时核心容器不存在。" >&2
      return 1
    fi
    request_timeout=3
    if ((request_timeout > remaining)); then
      request_timeout="${remaining}"
    fi
    if docker exec rag-app python -c '
import os
import sys
import urllib.request

base_url = os.environ["RAG_QDRANT_URL"].rstrip("/")
request = urllib.request.Request(
    f"{base_url}/readyz",
    headers={"api-key": os.environ["RAG_QDRANT_API_KEY"]},
)
response = urllib.request.urlopen(request, timeout=float(sys.argv[1]))
status = response.status
response.close()
raise SystemExit(0 if status == 200 else 1)
' "${request_timeout}" >/dev/null 2>&1; then
      return 0
    fi
    if ! container_exists rag-app || ! container_exists rag-qdrant; then
      echo "Qdrant /readyz 检查时核心容器不存在。" >&2
      return 1
    fi
    now="$(date +%s)"
    remaining="$((deadline - now))"
    if ((remaining <= 0)); then
      echo "Qdrant /readyz 在 ${timeout_seconds} 秒内未返回 200。" >&2
      return 1
    fi
    sleep_seconds="${HEALTH_POLL_INTERVAL_SECONDS}"
    if ((sleep_seconds > remaining)); then
      sleep_seconds="${remaining}"
    fi
    sleep "${sleep_seconds}"
  done
}

wait_for_runtime_health() {
  local port="$1"
  wait_for_container_health \
    "rag-qdrant" "${QDRANT_HEALTH_TIMEOUT_SECONDS}" || return 1
  wait_for_container_health \
    "rag-ocr" "${OCR_HEALTH_TIMEOUT_SECONDS}" || return 1
  wait_for_container_health \
    "rag-app" "${APP_HEALTH_TIMEOUT_SECONDS}" || return 1
  wait_for_qdrant_ready "${QDRANT_READY_TIMEOUT_SECONDS}" || return 1
  wait_for_app_live "${port}" "${APP_LIVE_TIMEOUT_SECONDS}"
}

if [[ "${requested_env}" != "${active_env}" ]]; then
  fail "rollback 只允许固定 active env。"
fi
require_regular_0600 "${active_env}" "活动环境文件"
require_regular_0600 "${rollback_file}" "rollback state"
if [[ "$(exact_value "${rollback_file}" ROLLBACK_SCHEMA_VERSION)" != "2" ]]; then
  fail "rollback state schema 不受支持。"
fi
rollback_release="$(exact_value \
  "${rollback_file}" ROLLBACK_RELEASE_DIR)"
rollback_revision="$(exact_value \
  "${rollback_file}" ROLLBACK_SOURCE_REVISION)"
rollback_app_image="$(exact_value \
  "${rollback_file}" ROLLBACK_APP_IMAGE)"
rollback_ocr_image="$(exact_value \
  "${rollback_file}" ROLLBACK_OCR_IMAGE)"
rollback_qdrant_image="$(exact_value \
  "${rollback_file}" ROLLBACK_QDRANT_IMAGE)"
rollback_worker_exists="$(exact_value \
  "${rollback_file}" ROLLBACK_WORKER_EXISTS)"
rollback_worker_running="$(exact_value \
  "${rollback_file}" ROLLBACK_WORKER_WAS_RUNNING)"
rollback_worker_image="$(exact_value \
  "${rollback_file}" ROLLBACK_WORKER_IMAGE)"
rollback_env_sha="$(exact_value \
  "${rollback_file}" ROLLBACK_ENV_SHA256)"
rollback_env_base64="$(exact_value \
  "${rollback_file}" ROLLBACK_ENV_BASE64)"
if [[ "${rollback_release}" != "${releases_dir}/"* \
  || ! -d "${rollback_release}" \
  || -L "${rollback_release}" \
  || "$(dirname "${rollback_release}")" != "${releases_dir}" \
  || ! "${rollback_revision}" =~ ^[0-9a-f]{40}$ \
  || ! "${rollback_env_sha}" =~ ^[0-9a-f]{64}$ ]]; then
  fail "rollback state 的 release、revision 或 env SHA 无效。"
fi
for value in "${rollback_worker_exists}" "${rollback_worker_running}"; do
  if [[ "${value}" != "true" && "${value}" != "false" ]]; then
    fail "rollback worker 状态必须为布尔值。"
  fi
done
for image in \
  "${rollback_app_image}" \
  "${rollback_ocr_image}" \
  "${rollback_qdrant_image}"; do
  if [[ ! "${image}" =~ ^sha256:[0-9a-f]{64}$ \
    || "$(image_id "${image}")" != "${image}" ]]; then
    fail "rollback image ID 不存在或不精确。"
  fi
done
if [[ "${rollback_worker_exists}" == "true" \
  && ! "${rollback_worker_image}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  fail "rollback worker image ID 无效。"
fi
if [[ "${rollback_worker_exists}" == "true" \
  && "${rollback_worker_image}" != "${rollback_app_image}" ]]; then
  fail "rollback worker image 必须等于 rollback app image。"
fi

bash "${rollback_release}/verify-offline.sh"
compose_file="${rollback_release}/compose.yaml"
if [[ "$(cat "${rollback_release}/SOURCE_REVISION")" \
  != "${rollback_revision}" ]]; then
  fail "rollback source revision 与 release 不一致。"
fi
qdrant_source_image="$(cat "${rollback_release}/QDRANT_SOURCE_IMAGE")"
manifest_qdrant_provenance="$(image_manifest_value \
  images/qdrant-linux-amd64.tar 4)"
if [[ "${qdrant_source_image}" \
    != "${RAG_APPROVED_QDRANT_SOURCE_IMAGE}" \
  || "${manifest_qdrant_provenance}" \
    != "${RAG_APPROVED_QDRANT_REPO_DIGEST}" ]]; then
  fail "rollback Qdrant provenance 不在批准白名单。"
fi
app_manifest_id="$(image_manifest_value images/docx-rag-linux-amd64.tar 3)"
ocr_manifest_id="$(image_manifest_value images/docx-rag-ocr-linux-amd64.tar 3)"
qdrant_manifest_id="$(image_manifest_value images/qdrant-linux-amd64.tar 3)"
app_config_id="$(image_manifest_value images/docx-rag-linux-amd64.tar 5)"
ocr_config_id="$(image_manifest_value images/docx-rag-ocr-linux-amd64.tar 5)"
qdrant_config_id="$(image_manifest_value images/qdrant-linux-amd64.tar 5)"
if [[ "${rollback_app_image}" != "${app_manifest_id}" \
    && "${rollback_app_image}" != "${app_config_id}" \
  || "${rollback_ocr_image}" != "${ocr_manifest_id}" \
    && "${rollback_ocr_image}" != "${ocr_config_id}" \
  || "${rollback_qdrant_image}" != "${qdrant_manifest_id}" \
    && "${rollback_qdrant_image}" != "${qdrant_config_id}" \
  || "$(image_manifest_value \
      images/docx-rag-linux-amd64.tar 4)" != "${rollback_revision}" \
  || "$(image_manifest_value \
      images/docx-rag-ocr-linux-amd64.tar 4)" != "${rollback_revision}" \
  ]]; then
  fail "rollback release IMAGE_ARCHIVES.tsv 身份不一致。"
fi
inspect_loaded_image \
  "${rollback_app_image}" "${app_manifest_id}" "${app_config_id}" \
  linux/amd64 "${rollback_revision}" \
  || fail "rollback app 镜像身份无效。"
inspect_loaded_image \
  "${rollback_ocr_image}" "${ocr_manifest_id}" "${ocr_config_id}" \
  linux/amd64 "${rollback_revision}" \
  || fail "rollback OCR 镜像身份无效。"
inspect_loaded_image \
  "${rollback_qdrant_image}" "${qdrant_manifest_id}" \
  "${qdrant_config_id}" linux/amd64 - \
  || fail "rollback Qdrant 镜像身份无效。"
target_env="$(mktemp "${shared_env_dir}/.rag.env.rollback-target.XXXXXXXX")"
if ! printf '%s' "${rollback_env_base64}" | base64 -d > "${target_env}"; then
  fail "rollback env 快照无法解码。"
fi
chmod 0600 "${target_env}"
if [[ "$(sha256sum "${target_env}" | awk '{print $1}')" \
  != "${rollback_env_sha}" ]]; then
  fail "rollback env 快照 SHA256 不一致。"
fi
if [[ "$(image_id "$(exact_value "${target_env}" RAG_APP_IMAGE)")" \
    != "${rollback_app_image}" \
  || "$(image_id "$(exact_value "${target_env}" RAG_OCR_IMAGE)")" \
    != "${rollback_ocr_image}" \
  || "$(image_id "$(exact_value "${target_env}" RAG_QDRANT_IMAGE)")" \
    != "${rollback_qdrant_image}" \
  || "$(exact_value "${target_env}" RAG_RELEASE_REVISION)" \
    != "${rollback_revision}" ]]; then
  fail "rollback env 与记录的镜像/revision 不一致。"
fi
docker compose --env-file "${target_env}" \
  -f "${compose_file}" config -q

if [[ ! -L "${current_link}" ]]; then
  fail "current 必须是 release 符号链接。"
fi
original_release="$(readlink -f "${current_link}")"
if [[ "${original_release}" != "${releases_dir}/"* \
  || ! -f "${original_release}/compose.yaml" \
  || ! -f "${original_release}/verify-offline.sh" ]]; then
  fail "当前 release 无效。"
fi
bash "${original_release}/verify-offline.sh"
original_env="$(mktemp "${shared_env_dir}/.rag.env.rollback-original.XXXXXXXX")"
cp -- "${active_env}" "${original_env}"
chmod 0600 "${original_env}"
original_env_sha="$(sha256sum "${original_env}" | awk '{print $1}')"
original_compose="${original_release}/compose.yaml"
original_revision="$(cat "${original_release}/SOURCE_REVISION")"
if [[ ! "${original_revision}" =~ ^[0-9a-f]{40}$ \
  || "$(exact_value "${active_env}" RAG_RELEASE_REVISION)" \
    != "${original_revision}" ]]; then
  fail "当前 active env revision 与 current release 不一致。"
fi
original_app_ref="$(exact_value "${active_env}" RAG_APP_IMAGE)"
original_ocr_ref="$(exact_value "${active_env}" RAG_OCR_IMAGE)"
original_qdrant_ref="$(exact_value "${active_env}" RAG_QDRANT_IMAGE)"
for image in \
  "${original_app_ref}" \
  "${original_ocr_ref}" \
  "${original_qdrant_ref}"; do
  image_id "${image}" >/dev/null
done
capture_container_state rag-app APP
capture_container_state rag-ocr OCR
capture_container_state rag-qdrant QDRANT
capture_container_state rag-worker WORKER
if [[ "${ORIGINAL_APP_EXISTS}" == "true" \
  && "$(image_id "${original_app_ref}")" != "${ORIGINAL_APP_IMAGE}" ]]; then
  fail "当前 app 容器与 active env 不一致。"
fi
if [[ "${ORIGINAL_OCR_EXISTS}" == "true" \
  && "$(image_id "${original_ocr_ref}")" != "${ORIGINAL_OCR_IMAGE}" ]]; then
  fail "当前 OCR 容器与 active env 不一致。"
fi
if [[ "${ORIGINAL_QDRANT_EXISTS}" == "true" \
  && "$(image_id "${original_qdrant_ref}")" \
    != "${ORIGINAL_QDRANT_IMAGE}" ]]; then
  fail "当前 Qdrant 容器与 active env 不一致。"
fi

rollback_port="$(optional_value "${target_env}" RAG_PORT)"
rollback_port="${rollback_port:-8088}"
original_port="$(optional_value "${original_env}" RAG_PORT)"
original_port="${original_port:-8088}"
runtime_mutated=false

verify_rollback_target() {
  verify_container_state \
    rag-app true true "${rollback_app_image}" || return 1
  verify_container_state \
    rag-ocr true true "${rollback_ocr_image}" || return 1
  verify_container_state \
    rag-qdrant true true "${rollback_qdrant_image}" || return 1
  if [[ "${rollback_worker_exists}" == "true" ]]; then
    verify_container_state \
      rag-worker true "${rollback_worker_running}" \
      "${rollback_worker_image}" || return 1
  else
    verify_container_state rag-worker false false "-" || return 1
  fi
}

commit_target_metadata() {
  active_new="$(mktemp "${shared_env_dir}/.rag.env.rollback-new.XXXXXXXX")" \
    || return 1
  cp -- "${target_env}" "${active_new}" || return 1
  chmod 0600 "${active_new}" || return 1
  mv -T "${active_new}" "${active_env}" || return 1
  active_new=""
  ln -s "${rollback_release}" "${current_new}" || return 1
  mv -T "${current_new}" "${current_link}" || return 1
  if [[ "$(sha256sum "${active_env}" | awk '{print $1}')" \
      != "${rollback_env_sha}" \
    || "$(readlink -f "${current_link}")" != "${rollback_release}" ]]; then
    return 1
  fi
}

perform_rollback() {
  local command=(
    docker compose
    --env-file "${target_env}"
    -f "${compose_file}"
  )
  local services=(rag-qdrant rag-ocr rag-app)
  if [[ "${rollback_worker_exists}" == "true" ]]; then
    command+=(--profile index)
    services+=(rag-worker)
  fi
  runtime_mutated=true
  "${command[@]}" up -d --no-build --pull never "${services[@]}" \
    || return 1
  if [[ "${rollback_worker_exists}" == "true" \
    && "${rollback_worker_running}" == "false" ]]; then
    docker compose --profile index \
      --env-file "${target_env}" -f "${compose_file}" \
      stop rag-worker || return 1
  elif [[ "${rollback_worker_exists}" == "false" ]] \
    && container_exists rag-worker; then
    docker container rm -f rag-worker || return 1
  fi
  verify_rollback_target || return 1
  wait_for_runtime_health "${rollback_port}" || return 1
  commit_target_metadata || return 1
  verify_rollback_target
}

restore_original_runtime() {
  local command=(
    docker compose
    --env-file "${original_env}"
    -f "${original_compose}"
  )
  local services=()
  local service
  bash "${original_release}/verify-offline.sh" || return 1
  if [[ "${ORIGINAL_APP_EXISTS}" == "true" ]]; then
    services+=(rag-app)
  fi
  if [[ "${ORIGINAL_OCR_EXISTS}" == "true" ]]; then
    services+=(rag-ocr)
  fi
  if [[ "${ORIGINAL_QDRANT_EXISTS}" == "true" ]]; then
    services+=(rag-qdrant)
  fi
  if [[ "${ORIGINAL_WORKER_EXISTS}" == "true" ]]; then
    command+=(--profile index)
    services+=(rag-worker)
  fi
  if ((${#services[@]} > 0)); then
    "${command[@]}" up -d --no-build --pull never "${services[@]}" \
      || return 1
  fi
  for service in APP OCR QDRANT WORKER; do
    eval "exists=\${ORIGINAL_${service}_EXISTS}"
    eval "running=\${ORIGINAL_${service}_RUNNING}"
    case "${service}" in
      APP) container=rag-app ;;
      OCR) container=rag-ocr ;;
      QDRANT) container=rag-qdrant ;;
      WORKER) container=rag-worker ;;
    esac
    if [[ "${exists}" == "false" ]] && container_exists "${container}"; then
      docker container rm -f "${container}" || return 1
    elif [[ "${exists}" == "true" && "${running}" == "false" ]]; then
      docker compose --profile index \
        --env-file "${original_env}" -f "${original_compose}" \
        stop "${container}" || return 1
    fi
  done
  verify_container_state \
    rag-app "${ORIGINAL_APP_EXISTS}" "${ORIGINAL_APP_RUNNING}" \
    "${ORIGINAL_APP_IMAGE}" || return 1
  verify_container_state \
    rag-ocr "${ORIGINAL_OCR_EXISTS}" "${ORIGINAL_OCR_RUNNING}" \
    "${ORIGINAL_OCR_IMAGE}" || return 1
  verify_container_state \
    rag-qdrant "${ORIGINAL_QDRANT_EXISTS}" "${ORIGINAL_QDRANT_RUNNING}" \
    "${ORIGINAL_QDRANT_IMAGE}" || return 1
  verify_container_state \
    rag-worker "${ORIGINAL_WORKER_EXISTS}" "${ORIGINAL_WORKER_RUNNING}" \
    "${ORIGINAL_WORKER_IMAGE}" || return 1
  for service in rag-qdrant rag-ocr rag-app; do
    case "${service}" in
      rag-app) running="${ORIGINAL_APP_RUNNING}" ;;
      rag-ocr) running="${ORIGINAL_OCR_RUNNING}" ;;
      rag-qdrant) running="${ORIGINAL_QDRANT_RUNNING}" ;;
    esac
    if [[ "${running}" == "true" ]]; then
      case "${service}" in
        rag-app) timeout_seconds="${APP_HEALTH_TIMEOUT_SECONDS}" ;;
        rag-ocr) timeout_seconds="${OCR_HEALTH_TIMEOUT_SECONDS}" ;;
        rag-qdrant) timeout_seconds="${QDRANT_HEALTH_TIMEOUT_SECONDS}" ;;
      esac
      wait_for_container_health \
        "${service}" "${timeout_seconds}" || return 1
    fi
  done
  if [[ "${ORIGINAL_APP_RUNNING}" == "true" \
    && "${ORIGINAL_QDRANT_RUNNING}" == "true" ]]; then
    wait_for_qdrant_ready "${QDRANT_READY_TIMEOUT_SECONDS}" || return 1
  fi
  if [[ "${ORIGINAL_APP_RUNNING}" == "true" ]]; then
    wait_for_app_live \
      "${original_port}" "${APP_LIVE_TIMEOUT_SECONDS}" || return 1
  fi
}

restore_original_metadata() {
  if [[ -n "${active_new}" ]]; then
    rm -f -- "${active_new}"
    active_new=""
  fi
  active_new="$(mktemp "${shared_env_dir}/.rag.env.rollback-restore.XXXXXXXX")" \
    || return 1
  cp -- "${original_env}" "${active_new}" || return 1
  chmod 0600 "${active_new}" || return 1
  mv -T "${active_new}" "${active_env}" || return 1
  active_new=""
  ln -s "${original_release}" "${current_restore}" || return 1
  mv -T "${current_restore}" "${current_link}" || return 1
  if [[ "$(sha256sum "${active_env}" | awk '{print $1}')" \
      != "${original_env_sha}" \
    || "$(readlink -f "${current_link}")" != "${original_release}" ]]; then
    return 1
  fi
}

if ! perform_rollback; then
  runtime_recovered=false
  metadata_recovered=false
  if [[ "${runtime_mutated}" == "true" ]] \
    && restore_original_runtime; then
    runtime_recovered=true
  fi
  if restore_original_metadata; then
    metadata_recovered=true
  fi
  if [[ "${runtime_recovered}" == "true" \
    && "${metadata_recovered}" == "true" ]]; then
    echo "ROLLBACK_FAILED_RECOVERED" >&2
    exit 1
  fi
  echo "ROLLBACK_FAILED_RECOVERY_FAILED" >&2
  exit 70
fi

echo "已健康切回上一版；活动 env/current 已原子提交。"
