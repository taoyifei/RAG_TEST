#!/usr/bin/env bash

industry_fail() {
  printf 'RAG_INDUSTRY_FAILED: %s\n' "$*" >&2
  exit 1
}

exact_env_value() {
  local env_file="$1"
  local key="$2"
  awk -F= -v expected="${key}" '
    $1 == expected {
      count += 1
      value = substr($0, index($0, "=") + 1)
      sub(/\r$/, "", value)
      if ((value ~ /^\047.*\047$/) || (value ~ /^".*"$/)) {
        value = substr(value, 2, length(value) - 2)
      }
    }
    END {
      if (count != 1 || value == "") {
        exit 2
      }
      print value
    }
  ' "${env_file}"
}

require_absolute_path() {
  local value="$1"
  local label="$2"
  [[ "${value}" == /* && "${value}" != *$'\n'* ]] \
    || industry_fail "${label} 必须是非空绝对路径。"
}

require_industry_env() {
  local env_file="$1"
  [[ "${env_file}" == /* && -f "${env_file}" && ! -L "${env_file}" ]] \
    || industry_fail "env 必须是绝对路径下的普通文件。"
  local alias
  alias="$(exact_env_value "${env_file}" RAG_QDRANT_ALIAS)" \
    || industry_fail "env 缺少唯一 RAG_QDRANT_ALIAS。"
  [[ "${alias}" == "rag-industry-active" ]] \
    || industry_fail "RAG_QDRANT_ALIAS 必须是 rag-industry-active。"
}

require_release_directory() {
  local release_dir="$1"
  [[ "${release_dir}" == /* && -d "${release_dir}" && ! -L "${release_dir}" ]] \
    || industry_fail "release-dir 必须是绝对路径下的真实目录。"
  [[ -f "${release_dir}/RELEASE_MANIFEST.json" \
    && -f "${release_dir}/SHA256SUMS" ]] \
    || industry_fail "release-dir 缺少 release 身份文件。"
}

industry_compose_file() {
  local env_file="$1"
  local expected
  expected="$(exact_env_value "${env_file}" RAG_INDUSTRY_COMPOSE_FILE)" \
    || industry_fail "env 缺少唯一 RAG_INDUSTRY_COMPOSE_FILE。"
  require_absolute_path "${expected}" RAG_INDUSTRY_COMPOSE_FILE
  [[ -f "${expected}" && ! -L "${expected}" ]] \
    || industry_fail "RAG_INDUSTRY_COMPOSE_FILE 不是普通文件。"
  printf '%s\n' "${expected}"
}

run_docker_compose_clean() {
  local clean=("PATH=${PATH}")
  local name
  for name in HOME DOCKER_HOST DOCKER_CONFIG XDG_RUNTIME_DIR \
    SSL_CERT_FILE SSL_CERT_DIR; do
    if [[ -n "${!name:-}" ]]; then
      clean+=("${name}=${!name}")
    fi
  done
  env -i "${clean[@]}" docker compose "$@"
}

run_industry_compose() {
  local env_file="$1"
  local compose_file="$2"
  shift 2
  run_docker_compose_clean \
    -p rag-industry \
    --env-file "${env_file}" \
    -f "${compose_file}" \
    "$@"
}

validate_industry_compose() {
  local env_file="$1"
  local compose_file="$2"
  local expected_image
  local docs_path
  local config_path
  local state_path
  expected_image="$(exact_env_value "${env_file}" RAG_APP_IMAGE)" || return 1
  docs_path="$(exact_env_value "${env_file}" RAG_DOCS_PATH)" || return 1
  config_path="$(exact_env_value "${env_file}" RAG_CONFIG_PATH)" || return 1
  state_path="$(exact_env_value "${env_file}" RAG_STATE_PATH)" || return 1
  run_industry_compose "${env_file}" "${compose_file}" \
    --profile index --profile dedicated-ocr config --format json \
    | env EXPECTED_IMAGE="${expected_image}" \
      DOCS_PATH="${docs_path}" \
      CONFIG_PATH="${config_path}" \
      STATE_PATH="${state_path}" \
      python3 -c '
import json
import os
import sys

value = json.load(sys.stdin)
service = value.get("services", {}).get("rag-industry-app", {})
if value.get("name") != "rag-industry":
    raise SystemExit("PROJECT_INVALID")
if service.get("image") != os.environ["EXPECTED_IMAGE"]:
    raise SystemExit("APP_IMAGE_INVALID")
if service.get("environment", {}).get("RAG_QDRANT_ALIAS") != "rag-industry-active":
    raise SystemExit("ALIAS_INVALID")
ports = service.get("ports")
if not isinstance(ports, list) or len(ports) != 1:
    raise SystemExit("PORT_INVALID")
port = ports[0]
if str(port.get("published")) != "8188" or int(port.get("target", 0)) != 8088:
    raise SystemExit("PORT_INVALID")
mounts = {
    item.get("target"): item.get("source")
    for item in service.get("volumes", [])
    if isinstance(item, dict)
}
expected = {
    "/config": os.environ["CONFIG_PATH"],
    "/data/docs": os.environ["DOCS_PATH"],
    "/state": os.environ["STATE_PATH"],
}
if any(mounts.get(target) != source for target, source in expected.items()):
    raise SystemExit("MOUNT_INVALID")
'
}

verify_industry_app_identity() {
  local env_file="$1"
  local require_ready="${2:-true}"
  local expected_image
  local expected_revision
  local expected_image_id
  local expected_image_revision
  local configured_image
  local running_image_id
  local project
  local service
  local container_revision
  local port
  expected_image="$(exact_env_value "${env_file}" RAG_APP_IMAGE)" || return 1
  expected_revision="$(exact_env_value "${env_file}" RAG_RELEASE_REVISION)" \
    || return 1
  port="$(exact_env_value "${env_file}" RAG_PORT)" || return 1
  expected_image_id="$(docker image inspect --format '{{.Id}}' \
    "${expected_image}")" || return 1
  expected_image_revision="$(docker image inspect --format \
    '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "${expected_image}")" || return 1
  configured_image="$(docker container inspect --format '{{.Config.Image}}' \
    rag-industry-app)" || return 1
  running_image_id="$(docker container inspect --format '{{.Image}}' \
    rag-industry-app)" || return 1
  project="$(docker container inspect --format \
    '{{index .Config.Labels "com.docker.compose.project"}}' \
    rag-industry-app)" || return 1
  service="$(docker container inspect --format \
    '{{index .Config.Labels "com.docker.compose.service"}}' \
    rag-industry-app)" || return 1
  container_revision="$(docker container inspect --format \
    '{{range .Config.Env}}{{println .}}{{end}}' rag-industry-app \
    | exact_env_value /dev/stdin RAG_RELEASE_REVISION)" || return 1
  [[ "${expected_image_revision}" == "${expected_revision}" \
    && "${configured_image}" == "${expected_image}" \
    && "${running_image_id}" == "${expected_image_id}" \
    && "${project}" == "rag-industry" \
    && "${service}" == "rag-industry-app" \
    && "${container_revision}" == "${expected_revision}" \
    && "${port}" == "8188" ]] || return 1
  docker container inspect --format '{{json .NetworkSettings.Ports}}' \
    rag-industry-app | python3 -c '
import json
import sys

ports = json.load(sys.stdin)
bindings = ports.get("8088/tcp")
if not isinstance(bindings, list) or len(bindings) != 1:
    raise SystemExit("PORT_INVALID")
if bindings[0].get("HostPort") != "8188":
    raise SystemExit("PORT_INVALID")
' || return 1
  docker exec rag-industry-app rag-app build-info \
    --expected-revision "${expected_revision}" >/dev/null || return 1
  wait_industry_http "http://127.0.0.1:${port}/live" 60 || return 1
  if [[ "${require_ready}" == "true" ]]; then
    wait_industry_http "http://127.0.0.1:${port}/ready" 60 || return 1
  fi
}

industry_runtime_index_identity() {
  docker exec rag-industry-app rag-app runtime-state | python3 -c '
import json
import re
import sys

value = json.load(sys.stdin)
identity = {
    "active_collection": value.get("active_collection"),
    "alias": value.get("alias"),
    "index_fingerprint": value.get("index_fingerprint"),
    "manifest_sha256": value.get("manifest_sha256"),
    "point_count": value.get("point_count"),
}
if not isinstance(identity["active_collection"], str) or not identity["active_collection"]:
    raise SystemExit("ACTIVE_COLLECTION_INVALID")
if identity["alias"] != "rag-industry-active":
    raise SystemExit("ACTIVE_ALIAS_INVALID")
if (
    not isinstance(identity["index_fingerprint"], str)
    or re.fullmatch(r"sha256:[0-9a-f]{64}", identity["index_fingerprint"]) is None
):
    raise SystemExit("INDEX_FINGERPRINT_INVALID")
if (
    not isinstance(identity["manifest_sha256"], str)
    or re.fullmatch(r"[0-9a-f]{64}", identity["manifest_sha256"]) is None
):
    raise SystemExit("MANIFEST_SHA256_INVALID")
if (
    not isinstance(identity["point_count"], int)
    or isinstance(identity["point_count"], bool)
    or identity["point_count"] <= 0
):
    raise SystemExit("POINT_COUNT_INVALID")
print(json.dumps(identity, separators=(",", ":"), sort_keys=True))
'
}

write_industry_release_state() {
  local env_file="$1"
  local stage="$2"
  local index_json="${3:-}"
  local backup_path
  local compose_file
  local release_dir
  local app_image
  local revision
  backup_path="$(exact_env_value "${env_file}" RAG_BACKUP_PATH)" || return 1
  compose_file="$(industry_compose_file "${env_file}")" || return 1
  release_dir="$(dirname "${compose_file}")"
  app_image="$(exact_env_value "${env_file}" RAG_APP_IMAGE)" || return 1
  revision="$(exact_env_value "${env_file}" RAG_RELEASE_REVISION)" || return 1
  [[ -n "${index_json}" ]] || index_json='{}'
  mkdir -p -- "${backup_path}"
  python3 - "${backup_path}/deployment-state.json" "${stage}" \
    "${app_image}" "${revision}" \
    "${release_dir}/RELEASE_MANIFEST.json" "${index_json}" <<'PY'
import json
import os
import pathlib
import re
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
stage = sys.argv[2]
image = sys.argv[3]
revision = sys.argv[4]
manifest = json.loads(pathlib.Path(sys.argv[5]).read_bytes())
index = json.loads(sys.argv[6])
if stage not in {"candidate", "deployed", "indexed", "verified", "last_good"}:
    raise SystemExit("STAGE_INVALID")
if re.fullmatch(r"[0-9a-f]{40}", revision) is None or not image:
    raise SystemExit("RELEASE_IDENTITY_INVALID")
corpus_sha256 = manifest.get("corpus", {}).get("sha256")
if not isinstance(corpus_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", corpus_sha256) is None:
    raise SystemExit("CORPUS_IDENTITY_INVALID")
if not isinstance(index, dict):
    raise SystemExit("INDEX_IDENTITY_INVALID")
allowed_previous = {
    "candidate": None,
    "deployed": "candidate",
    "indexed": "deployed",
    "verified": "indexed",
    "last_good": "verified",
}
if path.exists() and stage != "candidate":
    previous = json.loads(path.read_bytes())
    if (
        previous.get("stage") not in {allowed_previous[stage], stage}
        or previous.get("revision") != revision
        or previous.get("image") != image
    ):
        raise SystemExit("STATE_TRANSITION_INVALID")
payload = {
    "corpus_sha256": corpus_sha256,
    "image": image,
    "index": index,
    "revision": revision,
    "schema_version": "1",
    "stage": stage,
    "update_kind": "full_release",
}
descriptor, temporary_name = tempfile.mkstemp(
    prefix=".deployment-state.",
    dir=path.parent,
)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(payload, output, separators=(",", ":"), sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary_name, 0o600)
    os.replace(temporary_name, path)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY
}

promote_industry_last_good() {
  local env_file="$1"
  local index_json="$2"
  local backup_path
  local candidate_path
  local state_path
  local temporary_env
  local temporary_state
  backup_path="$(exact_env_value "${env_file}" RAG_BACKUP_PATH)" || return 1
  candidate_path="${backup_path}/app-candidate.json"
  state_path="${backup_path}/deployment-state.json"
  if [[ -f "${candidate_path}" && ! -L "${candidate_path}" ]]; then
    python3 - "${candidate_path}" "${env_file}" "${index_json}" <<'PY'
import json
import os
import pathlib
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
env_path = pathlib.Path(sys.argv[2])
index = json.loads(sys.argv[3])
candidate = json.loads(path.read_bytes())
env = {}
for line in env_path.read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.startswith("#"):
        key, value = line.split("=", 1)
        env[key] = value.strip("\"'")
target = candidate.get("target", {})
if (
    candidate.get("stage") not in {"candidate", "verified"}
    or target.get("image") != env.get("RAG_APP_IMAGE")
    or target.get("revision") != env.get("RAG_RELEASE_REVISION")
    or candidate.get("index") != index
):
    raise SystemExit("APP_CANDIDATE_INVALID")
candidate["stage"] = "verified"
descriptor, temporary_name = tempfile.mkstemp(prefix=".app-candidate.", dir=path.parent)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(candidate, output, separators=(",", ":"), sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary_name, 0o600)
    os.replace(temporary_name, path)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY
    state_path="${candidate_path}"
  else
    write_industry_release_state "${env_file}" verified "${index_json}" \
      || return 1
  fi
  temporary_env="$(mktemp "${backup_path}/.last-good-env.XXXXXX")"
  temporary_state="$(mktemp "${backup_path}/.last-good-state.XXXXXX")"
  cp --preserve=mode,ownership,timestamps -- "${env_file}" "${temporary_env}" \
    || return 1
  cp -- "${state_path}" "${temporary_state}" || return 1
  chmod 600 "${temporary_env}" "${temporary_state}"
  mv -f -- "${temporary_env}" "${backup_path}/last-good.env"
  mv -f -- "${temporary_state}" "${backup_path}/last-good.json"
  if [[ "${state_path}" == "${candidate_path}" ]]; then
    python3 - "${candidate_path}" <<'PY'
import json
import os
import pathlib
import tempfile
import sys

path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_bytes())
value["stage"] = "last_good"
descriptor, temporary_name = tempfile.mkstemp(prefix=".app-candidate.", dir=path.parent)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, separators=(",", ":"), sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary_name, 0o600)
    os.replace(temporary_name, path)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY
  else
    write_industry_release_state "${env_file}" last_good "${index_json}" \
      || return 1
  fi
}

validate_industry_ocr_gpu_ownership() {
  local env_file="$1"
  local release_dir="$2"
  local ocr_mode
  local gpu_id
  local active_pids
  local expected_row
  local expected_ref
  local expected_id
  local expected_revision
  local project
  local service
  local actual_ref
  local actual_id
  local health
  local actual_revision
  local managed_pids
  ocr_mode="$(exact_env_value "${env_file}" RAG_OCR_MODE)" || return 1
  [[ "${ocr_mode}" == "external" ]] && return 0
  [[ "${ocr_mode}" == "dedicated" ]] \
    || industry_fail "RAG_OCR_MODE 只能是 dedicated 或 external。"
  gpu_id="$(exact_env_value "${env_file}" RAG_INDUSTRY_OCR_GPU_DEVICE_ID)" \
    || industry_fail "独立 OCR 缺少 GPU ID。"
  [[ "${gpu_id}" =~ ^[0-9]+$ ]] || industry_fail "OCR GPU ID 必须是整数。"
  command -v nvidia-smi >/dev/null || industry_fail "NVIDIA_RUNTIME_UNAVAILABLE"
  nvidia-smi -i "${gpu_id}" --query-gpu=index,memory.total \
    --format=csv,noheader >/dev/null || industry_fail "OCR_GPU_UNAVAILABLE"
  active_pids="$(nvidia-smi -i "${gpu_id}" --query-compute-apps=pid \
    --format=csv,noheader,nounits 2>/dev/null || true)"
  [[ -z "${active_pids//[[:space:]]/}" ]] && return 0
  docker container inspect rag-industry-ocr >/dev/null 2>&1 \
    || industry_fail "OCR_GPU_UNKNOWN_PID"
  project="$(docker container inspect --format \
    '{{index .Config.Labels "com.docker.compose.project"}}' \
    rag-industry-ocr)"
  service="$(docker container inspect --format \
    '{{index .Config.Labels "com.docker.compose.service"}}' \
    rag-industry-ocr)"
  [[ "${project}" == "rag-industry" && "${service}" == "rag-industry-ocr" ]] \
    || industry_fail "OCR_GPU_OWNED_BY_OTHER_PROJECT"
  expected_row="$(python3 - "${release_dir}/RELEASE_MANIFEST.json" <<'PY'
import json
import pathlib
import sys

image = json.loads(pathlib.Path(sys.argv[1]).read_bytes())["images"]["ocr"]
print("\t".join((image["ref"], image["id"], image.get("revision") or "-")))
PY
)" || industry_fail "OCR_MANIFEST_IDENTITY_INVALID"
  IFS=$'\t' read -r expected_ref expected_id expected_revision <<<"${expected_row}"
  actual_ref="$(docker container inspect --format '{{.Config.Image}}' \
    rag-industry-ocr)"
  actual_id="$(docker container inspect --format '{{.Image}}' rag-industry-ocr)"
  health="$(docker container inspect --format \
    '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    rag-industry-ocr)"
  actual_revision="$(docker image inspect --format \
    '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "${expected_ref}")"
  [[ "${actual_ref}" == "${expected_ref}" \
    && "${actual_id}" == "${expected_id}" \
    && "${health}" == "healthy" \
    && ( "${expected_revision}" == "-" \
      || "${actual_revision}" == "${expected_revision}" ) ]] \
    || industry_fail "OCR_GPU_MANAGED_OCR_IDENTITY_MISMATCH"
  docker container inspect --format '{{json .HostConfig.DeviceRequests}}' \
    rag-industry-ocr | python3 -c '
import json
import sys

gpu_id = sys.argv[1]
requests = json.load(sys.stdin) or []
device_ids = {
    str(device_id)
    for request in requests
    if isinstance(request, dict)
    for device_id in (request.get("DeviceIDs") or [])
}
if gpu_id not in device_ids:
    raise SystemExit("OCR_GPU_DEVICE_MISMATCH")
' "${gpu_id}" || industry_fail "OCR_GPU_DEVICE_MISMATCH"
  managed_pids="$(docker top rag-industry-ocr -eo pid \
    | awk 'NR > 1 && $1 ~ /^[0-9]+$/ {print $1}')" \
    || industry_fail "OCR_GPU_PID_MAPPING_UNAVAILABLE"
  while read -r pid; do
    [[ -z "${pid}" ]] && continue
    grep -Fxq -- "${pid}" <<<"${managed_pids}" \
      || industry_fail "OCR_GPU_UNKNOWN_PID"
  done <<<"${active_pids}"
}

wait_industry_health() {
  local container="$1"
  local timeout_seconds="${2:-300}"
  local deadline=$((SECONDS + timeout_seconds))
  local state
  while ((SECONDS < deadline)); do
    state="$(docker inspect --format \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "${container}" 2>/dev/null || true)"
    case "${state}" in
      healthy)
        return 0
        ;;
      unhealthy|exited|dead)
        industry_fail "${container} 状态为 ${state}。"
        ;;
    esac
    sleep 2
  done
  industry_fail "${container} 未在时限内进入 healthy。"
}

wait_industry_http() {
  local url="$1"
  local timeout_seconds="$2"
  local deadline=$((SECONDS + timeout_seconds))
  while ((SECONDS < deadline)); do
    if curl --fail --silent --show-error "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}
