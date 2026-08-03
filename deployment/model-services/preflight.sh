#!/usr/bin/env bash
set -euo pipefail

readonly EMBEDDING_IMAGE="ghcr.m.daocloud.io/huggingface/text-embeddings-inference:1.9"
readonly RERANKER_IMAGE="covlink-rerank-api:server"
readonly EMBEDDING_MODEL="Qwen3-Embedding-0.6B"
readonly RERANKER_MODEL="Qwen3-Reranker-0.6B"

fail() {
  printf 'MODEL_SERVICES_PREFLIGHT_FAILED: %s\n' "$1" >&2
  exit 1
}

read_env_value() {
  local source_file="$1"
  local name="$2"
  local count
  local line

  count="$(grep -cE "^${name}=" "${source_file}" || true)"
  [[ "${count}" == "1" ]] || \
    fail "${name} must appear exactly once in ${source_file}"
  line="$(grep -E "^${name}=" "${source_file}")"
  printf '%s' "${line#*=}"
}

require_file() {
  local path="$1"

  [[ -f "${path}" && ! -L "${path}" ]] || \
    fail "required regular file is missing: ${path}"
}

require_port() {
  local name="$1"
  local value="$2"

  [[ "${value}" =~ ^[0-9]+$ ]] || fail "${name} must be an integer"
  ((value >= 1024 && value <= 65535)) || \
    fail "${name} must be between 1024 and 65535"
}

require_gpu() {
  local name="$1"
  local value="$2"

  [[ "${value}" =~ ^[0-9]+$ ]] || \
    fail "${name} must be a non-negative integer"
  grep -Fxq -- "${value}" <<<"${available_gpus}" || \
    fail "${name}=${value} is not present on this host"
}

verify_image() {
  local image="$1"
  local platform

  docker image inspect "${image}" >/dev/null 2>&1 || \
    fail "required image is not loaded: ${image}"
  platform="$(docker image inspect \
    --format '{{.Os}}/{{.Architecture}}' "${image}")"
  [[ "${platform}" == "linux/amd64" ]] || \
    fail "${image} has unsupported platform ${platform}"
}

[[ "$#" == "1" ]] || \
  fail "usage: bash preflight.sh /absolute/path/to/model-services.env"

readonly env_file="$1"
readonly script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly compose_file="${script_dir}/compose.yaml"

require_file "${env_file}"
require_file "${compose_file}"

asset_root="$(read_env_value "${env_file}" RAG_MODEL_ASSET_ROOT)"
bind_address="$(read_env_value "${env_file}" RAG_MODEL_BIND_ADDRESS)"
embedding_port="$(read_env_value "${env_file}" RAG_EMBEDDING_PORT)"
reranker_port="$(read_env_value "${env_file}" RAG_RERANKER_PORT)"
embedding_gpu="$(read_env_value \
  "${env_file}" RAG_EMBEDDING_GPU_DEVICE_ID)"
reranker_gpu="$(read_env_value \
  "${env_file}" RAG_RERANKER_GPU_DEVICE_ID)"

[[ "${asset_root}" == /* ]] || \
  fail "RAG_MODEL_ASSET_ROOT must be an absolute path"
[[ -d "${asset_root}" && ! -L "${asset_root}" ]] || \
  fail "RAG_MODEL_ASSET_ROOT must be a real directory"
asset_root="$(realpath -e -- "${asset_root}")"
[[ "${bind_address}" =~ ^[A-Za-z0-9.-]+$ ]] || \
  fail "RAG_MODEL_BIND_ADDRESS contains unsupported characters"

require_port RAG_EMBEDDING_PORT "${embedding_port}"
require_port RAG_RERANKER_PORT "${reranker_port}"
[[ "${embedding_port}" != "${reranker_port}" ]] || \
  fail "embedding and reranker host ports must differ"
[[ "${embedding_gpu}" != "${reranker_gpu}" ]] || \
  fail "embedding and reranker must use different host GPUs"

for path in \
  "models/${EMBEDDING_MODEL}/config.json" \
  "models/${EMBEDDING_MODEL}/model.safetensors" \
  "models/${EMBEDDING_MODEL}/tokenizer.json" \
  "models/${RERANKER_MODEL}/config.json" \
  "models/${RERANKER_MODEL}/model.safetensors" \
  "models/${RERANKER_MODEL}/tokenizer.json" \
  "images/ghcr.m.daocloud.io_huggingface_text-embeddings-inference_1.9.tar" \
  "images/covlink-rerank-api_server.tar" \
  "manifests/${EMBEDDING_MODEL}.sha256" \
  "manifests/${RERANKER_MODEL}.sha256" \
  "manifests/ghcr.m.daocloud.io_huggingface_text-embeddings-inference_1.9.tar.sha256" \
  "manifests/covlink-rerank-api_server.tar.sha256" \
  MODEL_REVISIONS.env \
  MANIFEST.sha256; do
  require_file "${asset_root}/${path}"
done

command -v docker >/dev/null 2>&1 || fail "docker is not installed"
docker compose version >/dev/null 2>&1 || \
  fail "docker compose plugin is not available"
verify_image "${EMBEDDING_IMAGE}"
verify_image "${RERANKER_IMAGE}"

command -v nvidia-smi >/dev/null 2>&1 || fail "nvidia-smi is not installed"
available_gpus="$(nvidia-smi --query-gpu=index --format=csv,noheader,nounits)"
[[ -n "${available_gpus}" ]] || fail "no NVIDIA GPU was detected"
require_gpu RAG_EMBEDDING_GPU_DEVICE_ID "${embedding_gpu}"
require_gpu RAG_RERANKER_GPU_DEVICE_ID "${reranker_gpu}"

(
  cd -- "${asset_root}"
  sha256sum -c MANIFEST.sha256
  sha256sum -c "manifests/${EMBEDDING_MODEL}.sha256"
  sha256sum -c "manifests/${RERANKER_MODEL}.sha256"
  sha256sum -c \
    manifests/ghcr.m.daocloud.io_huggingface_text-embeddings-inference_1.9.tar.sha256
  sha256sum -c manifests/covlink-rerank-api_server.tar.sha256
)

embedding_revision="$(read_env_value \
  "${asset_root}/MODEL_REVISIONS.env" EMBEDDING_REVISION)"
reranker_revision="$(read_env_value \
  "${asset_root}/MODEL_REVISIONS.env" RERANKER_REVISION)"
[[ "${embedding_revision}" == \
  "sha256:$(sha256sum "${asset_root}/manifests/${EMBEDDING_MODEL}.sha256" | cut -d' ' -f1)" ]] || \
  fail "embedding revision does not match its model manifest"
[[ "${reranker_revision}" == \
  "sha256:$(sha256sum "${asset_root}/manifests/${RERANKER_MODEL}.sha256" | cut -d' ' -f1)" ]] || \
  fail "reranker revision does not match its model manifest"

docker compose \
  --env-file "${env_file}" \
  -f "${compose_file}" \
  config --quiet

printf '%s\n' "RAG_MODEL_SERVICES_PREFLIGHT_OK"
