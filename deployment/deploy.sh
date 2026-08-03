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
QDRANT_HEALTH_TIMEOUT_SECONDS=60
QDRANT_READY_TIMEOUT_SECONDS=60
APP_HEALTH_TIMEOUT_SECONDS=60
APP_LIVE_TIMEOUT_SECONDS=60
OCR_HEALTH_TIMEOUT_SECONDS=240
HEALTH_POLL_INTERVAL_SECONDS=1

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

validate_model_endpoint_array() {
  local raw_value="$1"
  local error_category
  if error_category="$(
    printf '%s' "${raw_value}" | python3 -c '
import json
import sys
from urllib.parse import urlsplit


def reject(category):
    print(category)
    raise SystemExit(1)


try:
    endpoints = json.load(sys.stdin)
except (UnicodeDecodeError, json.JSONDecodeError):
    reject("MODEL_ENDPOINTS_INVALID_JSON")
if not isinstance(endpoints, list):
    reject("MODEL_ENDPOINTS_NOT_ARRAY")
if not endpoints:
    reject("MODEL_ENDPOINTS_EMPTY")
if any(
    not isinstance(endpoint, str) or not endpoint
    for endpoint in endpoints
):
    reject("MODEL_ENDPOINT_ITEM_INVALID")
if len(endpoints) != len(set(endpoints)):
    reject("MODEL_ENDPOINTS_DUPLICATE")
for endpoint in endpoints:
    if "REPLACE_" in endpoint:
        reject("MODEL_ENDPOINT_PLACEHOLDER_FORBIDDEN")
    if "?" in endpoint:
        reject("MODEL_ENDPOINT_QUERY_FORBIDDEN")
    if "#" in endpoint:
        reject("MODEL_ENDPOINT_FRAGMENT_FORBIDDEN")
    if "\\" in endpoint or any(
        character.isspace() or ord(character) < 32
        for character in endpoint
    ):
        reject("MODEL_ENDPOINT_URL_INVALID")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        reject("MODEL_ENDPOINT_URL_INVALID")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
    ):
        reject("MODEL_ENDPOINT_URL_INVALID")
    if parsed.username is not None or parsed.password is not None:
        reject("MODEL_ENDPOINT_CREDENTIALS_FORBIDDEN")
    normalized_hostname = hostname.rstrip(".").casefold()
    if (
        normalized_hostname == "invalid"
        or normalized_hostname.endswith(".invalid")
    ):
        reject("MODEL_ENDPOINT_HOST_FORBIDDEN")
' 2>/dev/null
  )"; then
    return 0
  fi
  if [[ -z "${error_category}" ]]; then
    error_category="MODEL_ENDPOINT_VALIDATION_FAILED"
  fi
  echo "${error_category}" >&2
  return 1
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

image_descriptor_digest() {
  local descriptor_json
  descriptor_json="$(docker image inspect \
    --format '{{json .Descriptor}}' "$1")"
  python3 -c '
import json
import re
import sys

payload = json.load(sys.stdin)
digest = payload.get("digest") if isinstance(payload, dict) else None
if not isinstance(digest, str) or re.fullmatch(
    r"sha256:[0-9a-f]{64}", digest
) is None:
    raise SystemExit(1)
print(digest)
' <<< "${descriptor_json}"
}

verify_containerd_image_store() {
  local driver_status
  driver_status="$(docker info --format '{{json .DriverStatus}}')"
  python3 -c '
import json
import sys

payload = json.load(sys.stdin)
expected = ["driver-type", "io.containerd.snapshotter.v1"]
raise SystemExit(0 if expected in payload else 1)
' <<< "${driver_status}"
}

inspect_loaded_image() {
  local image="$1"
  local expected_manifest_id="$2"
  local expected_config_id="$3"
  local expected_platform="$4"
  local expected_provenance="$5"
  local actual_descriptor_id
  local actual_id
  local actual_revision
  local architecture
  local operating_system
  architecture="$(docker image inspect --format '{{.Architecture}}' "${image}")"
  operating_system="$(docker image inspect --format '{{.Os}}' "${image}")"
  actual_id="$(image_id "${image}")"
  if ! actual_descriptor_id="$(image_descriptor_digest "${image}")"; then
    echo "镜像缺少可信 containerd descriptor：${image}" >&2
    return 1
  fi
  if [[ "${operating_system}/${architecture}" != "${expected_platform}" \
    || "${actual_id}" != "${expected_manifest_id}" \
    || "${actual_descriptor_id}" != "${expected_manifest_id}" \
    || ! "${actual_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    printf '%s\n' \
      "IMAGE_IDENTITY_MISMATCH image=${image} expected_manifest=${expected_manifest_id} actual_id=${actual_id} actual_descriptor=${actual_descriptor_id} expected_config=${expected_config_id} expected_platform=${expected_platform} actual_platform=${operating_system}/${architecture}" \
      >&2
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

if [[ -z "${candidate_env}" || "${candidate_env}" != /* ]]; then
  fail "必须显式提供候选环境文件。"
fi
if [[ "${release_dir}" != "${project_root}/releases/"* ]]; then
  fail "runtime release 必须位于固定 releases 目录。"
fi
bash "${release_dir}/verify-offline.sh"
# shellcheck source=deployment/qdrant-policy.sh
source "${release_dir}/qdrant-policy.sh"
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
  fail "CANDIDATE_PLACEHOLDER_FORBIDDEN：候选环境文件仍含占位符。"
fi
if ! embedding_endpoints="$(
  exact_env_value "${candidate_env}" RAG_EMBEDDING_ENDPOINTS
)"; then
  fail "MODEL_ENDPOINTS_ENV_INVALID"
fi
if ! reranker_endpoints="$(
  exact_env_value "${candidate_env}" RAG_RERANKER_ENDPOINTS
)"; then
  fail "MODEL_ENDPOINTS_ENV_INVALID"
fi
if ! llm_endpoints="$(
  exact_env_value "${candidate_env}" RAG_LLM_ENDPOINTS
)"; then
  fail "MODEL_ENDPOINTS_ENV_INVALID"
fi
validate_model_endpoint_array "${embedding_endpoints}"
validate_model_endpoint_array "${reranker_endpoints}"
validate_model_endpoint_array "${llm_endpoints}"
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
new_app_config_id="$(image_manifest_value "${app_archive}" 5)"
new_ocr_config_id="$(image_manifest_value "${ocr_archive}" 5)"
new_qdrant_config_id="$(image_manifest_value "${qdrant_archive}" 5)"
app_platform="$(image_manifest_value "${app_archive}" 6)"
ocr_platform="$(image_manifest_value "${ocr_archive}" 6)"
qdrant_platform="$(image_manifest_value "${qdrant_archive}" 6)"
if [[ "${app_provenance}" != "${source_revision}" \
  || "${ocr_provenance}" != "${source_revision}" ]]; then
  fail "app/OCR provenance 必须等于 release revision。"
fi
if [[ "${qdrant_provenance}" \
  != "${RAG_APPROVED_QDRANT_REPO_DIGEST}" ]]; then
  fail "Qdrant provenance 不在批准白名单。"
fi
verify_containerd_image_store \
  || fail "DOCKER_CONTAINERD_IMAGE_STORE_REQUIRED"

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
deployment_state=invalid
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
old_rollback_exists=false
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
elif [[ -e "${current_link}" ]]; then
  fail "current 必须是指向 release 的符号链接。"
fi
if [[ -f "${rollback_file}" && ! -L "${rollback_file}" ]]; then
  require_regular_0600 "${rollback_file}" "rollback state"
  old_rollback_exists=true
elif [[ -e "${rollback_file}" || -L "${rollback_file}" ]]; then
  fail "rollback state 路径不是安全普通文件。"
fi

if [[ "${old_active_exists}" == "false" \
  && "${old_current_exists}" == "false" \
  && "${existing_count}" == "0" \
  && "${worker_exists}" == "false" ]]; then
  if [[ "${old_rollback_exists}" == "true" ]]; then
    fail "fresh 部署不允许遗留 rollback state。"
  fi
  deployment_state=fresh
elif [[ "${old_active_exists}" == "true" \
  && "${old_current_exists}" == "true" ]]; then
  if [[ "${existing_count}" == "3" ]]; then
    deployment_state=installed
    old_runtime=true
  elif [[ "${existing_count}" == "0" ]]; then
    deployment_state=degraded
  fi
fi
if [[ "${deployment_state}" == "invalid" ]]; then
  fail "部署前状态不是 fresh、installed 或 degraded。"
fi

if [[ "${deployment_state}" != "fresh" ]]; then
  if [[ "${old_release}" != "${project_root}/releases/"* \
    || "$(dirname "${old_release}")" != "${project_root}/releases" \
    || ! -d "${old_release}" \
    || ! -f "${old_release}/SOURCE_REVISION" \
    || ! -f "${old_release}/compose.yaml" \
    || ! -f "${old_release}/verify-offline.sh" ]]; then
    fail "active env/current 未指向安全的旧 release。"
  fi
  bash "${old_release}/verify-offline.sh"
  old_revision="$(cat "${old_release}/SOURCE_REVISION")"
  if [[ ! "${old_revision}" =~ ^[0-9a-f]{40}$ \
    || "$(exact_env_value "${active_env}" RAG_RELEASE_REVISION)" \
    != "${old_revision}" ]]; then
    fail "active env 不是 current release 的实际配置。"
  fi
  old_app_image="$(image_id "$(
    exact_env_value "${active_env}" RAG_APP_IMAGE
  )")"
  old_ocr_image="$(image_id "$(
    exact_env_value "${active_env}" RAG_OCR_IMAGE
  )")"
  old_qdrant_image="$(image_id "$(
    exact_env_value "${active_env}" RAG_QDRANT_IMAGE
  )")"
  if [[ "${deployment_state}" == "installed" ]]; then
    old_app_running="$(container_running rag-app)"
    old_ocr_running="$(container_running rag-ocr)"
    old_qdrant_running="$(container_running rag-qdrant)"
    if [[ "$(container_image rag-app)" != "${old_app_image}" \
      || "$(container_image rag-ocr)" != "${old_ocr_image}" \
      || "$(container_image rag-qdrant)" != "${old_qdrant_image}" ]]; then
      fail "active env 镜像与当前容器不一致。"
    fi
  fi
  if [[ "${worker_exists}" == "true" \
    && "${worker_image}" != "${old_app_image}" ]]; then
    fail "旧 worker image 必须等于旧 app image。"
  fi
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
  if [[ "${deployment_state}" == "fresh" ]]; then
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
  docker load --platform linux/amd64 \
    --input "${release_dir}/${app_archive}" || return 1
  docker load --platform linux/amd64 \
    --input "${release_dir}/${ocr_archive}" || return 1
  docker load --platform linux/amd64 \
    --input "${release_dir}/${qdrant_archive}" || return 1
  inspect_loaded_image \
    "${app_image}" "${new_app_id}" "${new_app_config_id}" \
    "${app_platform}" "${app_provenance}" || return 1
  inspect_loaded_image \
    "${ocr_image}" "${new_ocr_id}" "${new_ocr_config_id}" \
    "${ocr_platform}" "${ocr_provenance}" || return 1
  inspect_loaded_image \
    "${qdrant_image}" "${new_qdrant_id}" "${new_qdrant_config_id}" \
    "${qdrant_platform}" "${qdrant_provenance}" || return 1
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
  if [[ "${deployment_state}" != "fresh" ]]; then
    bash "${old_release}/verify-offline.sh" || return 1
  fi
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
