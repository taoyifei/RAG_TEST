#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"

if [[ "$#" -eq 1 && "$1" == "--package-only" ]]; then
  command -v docker >/dev/null || industry_fail "DOCKER_NOT_FOUND"
  docker info >/dev/null 2>&1 || industry_fail "DOCKER_RUNTIME_UNAVAILABLE"
  docker compose version >/dev/null 2>&1 \
    || industry_fail "COMPOSE_PLUGIN_UNAVAILABLE"
  command -v python3 >/dev/null || industry_fail "PYTHON3_NOT_FOUND"
  python3 "${script_dir}/package_selfcheck.py" release "${script_dir}" \
    >/dev/null || industry_fail "PACKAGE_SELFCHECK_FAILED"
  docker compose -p rag-industry \
    --env-file "${script_dir}/.env.example" \
    -f "${script_dir}/compose.yaml" \
    --profile index --profile dedicated-ocr config -q \
    || industry_fail "INDUSTRY_COMPOSE_CONFIG_FAILED"
  printf 'RAG_INDUSTRY_PREFLIGHT_PACKAGE_OK\n'
  exit 0
fi

[[ "$#" -eq 2 ]] \
  || industry_fail "用法: preflight.sh /absolute/rag-industry.env /absolute/release-dir"
require_industry_env "$1"
require_release_directory "$2"
env_file="$(realpath "$1")"
release_dir="$(realpath "$2")"
compose_file="$(industry_compose_file "${env_file}")"
[[ "${compose_file}" == "${release_dir}/compose.yaml" ]] \
  || industry_fail "env compose path 必须指向当前 release。"

[[ "$(uname -m)" == "x86_64" ]] || industry_fail "ARCH_NOT_X86_64"
command -v docker >/dev/null || industry_fail "DOCKER_NOT_FOUND"
docker info >/dev/null 2>&1 || industry_fail "DOCKER_RUNTIME_UNAVAILABLE"
docker compose version >/dev/null 2>&1 || industry_fail "COMPOSE_PLUGIN_UNAVAILABLE"
command -v python3 >/dev/null || industry_fail "PYTHON3_NOT_FOUND"
command -v curl >/dev/null || industry_fail "CURL_NOT_FOUND"

python3 "${release_dir}/package_selfcheck.py" release "${release_dir}" \
  >/dev/null || industry_fail "PACKAGE_SELFCHECK_FAILED"

mapfile -t existing_image_rows < <(
  python3 - "${release_dir}/RELEASE_MANIFEST.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
for name in ("ocr", "qdrant"):
    image = manifest["images"][name]
    if image["delivery"] == "server-existing":
        print("\t".join((
            image["ref"],
            image["id"],
            image["platform"],
            image.get("revision") or "-",
        )))
PY
)
for row in "${existing_image_rows[@]}"; do
  IFS=$'\t' read -r image_ref expected_id expected_platform \
    expected_revision <<<"${row}"
  actual_id="$(docker image inspect --format '{{.Id}}' "${image_ref}")" \
    || industry_fail "SERVER_IMAGE_NOT_FOUND: ${image_ref}"
  actual_platform="$(docker image inspect --format \
    '{{.Os}}/{{.Architecture}}' "${image_ref}")"
  [[ "${actual_id}" == "${expected_id}" \
    && "${actual_platform}" == "${expected_platform}" \
    && "${actual_platform}" == "linux/amd64" ]] \
    || industry_fail "SERVER_IMAGE_IDENTITY_MISMATCH: ${image_ref}"
  if [[ "${expected_revision}" != "-" ]]; then
    actual_revision="$(docker image inspect --format \
      '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "${image_ref}")"
    [[ "${actual_revision}" == "${expected_revision}" ]] \
      || industry_fail "SERVER_IMAGE_REVISION_MISMATCH: ${image_ref}"
  fi
done

paths=(
  RAG_RELEASE_ROOT
  RAG_BACKUP_PATH
  RAG_STATE_PATH
  RAG_QDRANT_PATH
  RAG_DOCS_PATH
  RAG_REFERENCE_PATH
  RAG_CONFIG_PATH
  RAG_LOGS_PATH
)
declare -A seen_paths=()
for key in "${paths[@]}"; do
  value="$(exact_env_value "${env_file}" "${key}")" \
    || industry_fail "env 缺少唯一 ${key}。"
  require_absolute_path "${value}" "${key}"
  [[ "${value}" != /data/tyf/RAG/* ]] \
    || industry_fail "${key} 指向 training 路径。"
  [[ -z "${seen_paths[${value}]:-}" ]] \
    || industry_fail "Industry bind paths 不能复用。"
  seen_paths["${value}"]="${key}"
  probe="${value}"
  while [[ ! -e "${probe}" && "${probe}" != "/" ]]; do
    probe="$(dirname "${probe}")"
  done
  [[ -d "${probe}" && -w "${probe}" ]] \
    || industry_fail "${key} 没有可写的现存父目录。"
done

port="$(exact_env_value "${env_file}" RAG_PORT)" \
  || industry_fail "env 缺少唯一 RAG_PORT。"
[[ "${port}" =~ ^[0-9]+$ && "${port}" -eq 8188 ]] \
  || industry_fail "Industry host port 必须是 8188。"
if ! python3 - "${port}" <<'PY'
import socket
import sys

with socket.socket() as probe:
    probe.bind(("127.0.0.1", int(sys.argv[1])))
PY
then
  project="$(docker container inspect --format \
    '{{index .Config.Labels "com.docker.compose.project"}}' \
    rag-industry-app 2>/dev/null || true)"
  published="$(docker port rag-industry-app 8088/tcp 2>/dev/null || true)"
  [[ "${project}" == "rag-industry" && "${published}" == *":${port}" ]] \
    || industry_fail "INDUSTRY_PORT_UNAVAILABLE"
fi

secrets=(RAG_QUERY_TOKEN RAG_ADMIN_TOKEN RAG_QDRANT_API_KEY RAG_OCR_API_TOKEN)
declare -A seen_secrets=()
for key in "${secrets[@]}"; do
  value="$(exact_env_value "${env_file}" "${key}")" \
    || industry_fail "env 缺少唯一 ${key}。"
  [[ "${#value}" -ge 32 && "${value}" != *REPLACE_* ]] \
    || industry_fail "${key} 仍是占位符或长度不足。"
  [[ -z "${seen_secrets[${value}]:-}" ]] \
    || industry_fail "Industry secrets 必须互不相同。"
  seen_secrets["${value}"]="${key}"
done

for container in \
  rag-industry-app rag-industry-worker rag-industry-qdrant rag-industry-ocr; do
  if docker container inspect "${container}" >/dev/null 2>&1; then
    project="$(docker container inspect --format \
      '{{index .Config.Labels "com.docker.compose.project"}}' "${container}")"
    [[ "${project}" == "rag-industry" ]] \
      || industry_fail "Industry container name 被无关项目占用。"
  fi
done

ocr_mode="$(exact_env_value "${env_file}" RAG_OCR_MODE)" \
  || industry_fail "env 缺少唯一 RAG_OCR_MODE。"
case "${ocr_mode}" in
  dedicated)
    gpu_id="$(exact_env_value "${env_file}" RAG_INDUSTRY_OCR_GPU_DEVICE_ID)" \
      || industry_fail "独立 OCR 缺少 GPU ID。"
    [[ "${gpu_id}" =~ ^[0-9]+$ ]] || industry_fail "OCR GPU ID 必须是整数。"
    command -v nvidia-smi >/dev/null || industry_fail "NVIDIA_RUNTIME_UNAVAILABLE"
    nvidia-smi -i "${gpu_id}" --query-gpu=index,memory.total \
      --format=csv,noheader >/dev/null \
      || industry_fail "OCR_GPU_UNAVAILABLE"
    active_pids="$(nvidia-smi -i "${gpu_id}" --query-compute-apps=pid \
      --format=csv,noheader,nounits 2>/dev/null || true)"
    [[ -z "${active_pids//[[:space:]]/}" ]] \
      || industry_fail "OCR_GPU_ALREADY_IN_USE"
    ;;
  external)
    ;;
  *)
    industry_fail "RAG_OCR_MODE 只能是 dedicated 或 external。"
    ;;
esac

python3 - "${release_dir}/config" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
expected = {
    "corpus-policy.json",
    "intent-router-calibration.json",
    "intent-router.json",
    "pipeline.json",
    "retrieval.json",
}
if {item.name for item in root.iterdir()} != expected:
    raise SystemExit("CONFIG_EXACT_SET_INVALID")
if json.loads((root / "intent-router.json").read_bytes())["mode"] != "shadow":
    raise SystemExit("INTENT_MODE_NOT_SHADOW")
if json.loads((root / "intent-router-calibration.json").read_bytes())["status"] != "unverified":
    raise SystemExit("CALIBRATION_NOT_UNVERIFIED")
if json.loads((root / "retrieval.json").read_bytes())["status"] != "provisional":
    raise SystemExit("RETRIEVAL_NOT_PROVISIONAL")
if json.loads((root / "retrieval.json").read_bytes()).get("soft_routes") != []:
    raise SystemExit("RETRIEVAL_SOFT_ROUTES_NOT_EMPTY")
PY

for name in \
  RAG_EMBEDDING_ENDPOINTS RAG_RERANKER_ENDPOINTS RAG_LLM_ENDPOINTS \
  RAG_OCR_ENDPOINTS RAG_EMBEDDING_MODEL RAG_RERANKER_MODEL RAG_LLM_MODEL \
  RAG_EMBEDDING_API_TOKEN RAG_RERANKER_API_TOKEN RAG_LLM_API_TOKEN; do
  export "${name}=$(exact_env_value "${env_file}" "${name}" 2>/dev/null || true)"
done
export RAG_OCR_MODE="${ocr_mode}"
export RAG_OCR_API_TOKEN="$(exact_env_value "${env_file}" RAG_OCR_API_TOKEN)"
python3 "${release_dir}/preflight_endpoints.py" >/dev/null \
  || industry_fail "MODEL_OR_OCR_ENDPOINT_PREFLIGHT_FAILED"

docker compose -p rag-industry --env-file "${env_file}" \
  -f "${compose_file}" config -q \
  || industry_fail "INDUSTRY_COMPOSE_CONFIG_FAILED"

for check_path in \
  "${release_dir}" \
  "$(dirname "$(exact_env_value "${env_file}" RAG_QDRANT_PATH)")" \
  "$(dirname "$(exact_env_value "${env_file}" RAG_STATE_PATH)")" \
  "$(dirname "$(exact_env_value "${env_file}" RAG_LOGS_PATH)")" \
  "$(dirname "$(exact_env_value "${env_file}" RAG_BACKUP_PATH)")"; do
  existing="${check_path}"
  while [[ ! -e "${existing}" && "${existing}" != "/" ]]; do
    existing="$(dirname "${existing}")"
  done
  available_kb="$(df -Pk "${existing}" | awk 'NR == 2 {print $4}')"
  [[ "${available_kb}" -ge 5242880 ]] || industry_fail "DISK_SPACE_INSUFFICIENT"
done

printf 'RAG_INDUSTRY_PREFLIGHT_OK\n'
