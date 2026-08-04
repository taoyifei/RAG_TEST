#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'SIMPLE_DEPLOY_FAILED: %s\n' "$*" >&2
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
    }
    END {
      if (count != 1 || value == "") {
        exit 2
      }
      print value
    }
  ' "${env_file}"
}

require_absolute_directory_value() {
  local value="$1"
  local label="$2"
  [[ "${value}" == /* && "${value}" != *$'\n'* ]] \
    || fail "${label} 必须是非空绝对路径。"
}

wait_healthy() {
  local container="$1"
  local deadline=$((SECONDS + 300))
  local state
  while ((SECONDS < deadline)); do
    state="$(docker inspect \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "${container}" 2>/dev/null || true)"
    case "${state}" in
      healthy)
        return 0
        ;;
      unhealthy|exited|dead)
        fail "${container} 状态为 ${state}。"
        ;;
    esac
    sleep 2
  done
  fail "${container} 未在 300 秒内进入 healthy。"
}

wait_live() {
  local port="$1"
  local deadline=$((SECONDS + 60))
  while ((SECONDS < deadline)); do
    if curl --fail --silent --show-error \
      "http://127.0.0.1:${port}/live" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  fail "rag-app /live 未在 60 秒内返回 200。"
}

wait_demo_ready() {
  local port="$1"
  local deadline=$((SECONDS + 180))
  local payload
  while ((SECONDS < deadline)); do
    if payload="$(curl --fail --silent --show-error \
        "http://127.0.0.1:${port}/ready" 2>/dev/null)" \
      && grep -Eq '"ready"[[:space:]]*:[[:space:]]*true' \
        <<<"${payload}" \
      && grep -Eq '"run_mode"[[:space:]]*:[[:space:]]*"demo"' \
        <<<"${payload}" \
      && grep -Eq '"production_ready"[[:space:]]*:[[:space:]]*false' \
        <<<"${payload}"; then
      printf '%s\n' "${payload}"
      return 0
    fi
    sleep 2
  done
  fail "demo /ready 未在 180 秒内建立活动索引并返回 200。"
}

[[ "$#" -eq 2 ]] \
  || fail "用法: bash deploy.sh /absolute/path/rag.env /absolute/path/package-dir"
[[ "$1" == /* && "$2" == /* ]] || fail "两个参数都必须是绝对路径。"
[[ -f "$1" && ! -L "$1" ]] || fail "env 必须是普通文件。"
[[ -d "$2" && ! -L "$2" ]] || fail "package-dir 必须是真实目录。"

env_file="$(realpath "$1")"
package_dir="$(realpath "$2")"
compose_file="${package_dir}/compose.yaml"
[[ -f "${compose_file}" && ! -L "${compose_file}" ]] \
  || fail "package-dir 缺少 compose.yaml。"

expected_compose="$(exact_env_value "${env_file}" RAG_SIMPLE_COMPOSE_FILE)" \
  || fail "env 缺少唯一 RAG_SIMPLE_COMPOSE_FILE。"
[[ "${expected_compose}" == "${compose_file}" ]] \
  || fail "RAG_SIMPLE_COMPOSE_FILE 必须指向当前 package-dir/compose.yaml。"

archives=(
  app-image.tar.gz
  ocr-image.tar.gz
  qdrant-image.tar.gz
  corpus.tar.gz
)
for archive in "${archives[@]}"; do
  [[ -f "${package_dir}/${archive}" \
    && -f "${package_dir}/${archive}.sha256" ]] \
    || fail "部署包缺少 ${archive} 或 sidecar。"
  (
    cd "${package_dir}"
    sha256sum --check "${archive}.sha256"
  ) || fail "${archive} SHA256 校验失败。"
done

for archive in app-image.tar.gz ocr-image.tar.gz qdrant-image.tar.gz; do
  gzip -dc -- "${package_dir}/${archive}" | docker load \
    || fail "docker load 失败：${archive}"
done

app_image="$(exact_env_value "${env_file}" RAG_APP_IMAGE)" \
  || fail "env 缺少唯一 RAG_APP_IMAGE。"
ocr_image="$(exact_env_value "${env_file}" RAG_OCR_IMAGE)" \
  || fail "env 缺少唯一 RAG_OCR_IMAGE。"
qdrant_image="$(exact_env_value "${env_file}" RAG_QDRANT_IMAGE)" \
  || fail "env 缺少唯一 RAG_QDRANT_IMAGE。"
for image in "${app_image}" "${ocr_image}" "${qdrant_image}"; do
  docker image inspect "${image}" >/dev/null \
    || fail "docker load 后缺少镜像 tag：${image}"
done

state_path="$(exact_env_value "${env_file}" RAG_STATE_PATH)" \
  || fail "env 缺少唯一 RAG_STATE_PATH。"
qdrant_path="$(exact_env_value "${env_file}" RAG_QDRANT_PATH)" \
  || fail "env 缺少唯一 RAG_QDRANT_PATH。"
docs_path="$(exact_env_value "${env_file}" RAG_DOCS_PATH)" \
  || fail "env 缺少唯一 RAG_DOCS_PATH。"
logs_path="$(exact_env_value "${env_file}" RAG_LOGS_PATH)" \
  || fail "env 缺少唯一 RAG_LOGS_PATH。"
for entry in \
  "${state_path}:RAG_STATE_PATH" \
  "${qdrant_path}:RAG_QDRANT_PATH" \
  "${docs_path}:RAG_DOCS_PATH" \
  "${logs_path}:RAG_LOGS_PATH"; do
  require_absolute_directory_value "${entry%%:*}" "${entry#*:}"
done
mkdir -p -- "${state_path}" "${qdrant_path}" "${docs_path}" "${logs_path}"

if find "${docs_path}" -mindepth 1 -print -quit | grep -q .; then
  fail "docs 目录非空，拒绝覆盖 corpus。"
fi
tar -xzf "${package_dir}/corpus.tar.gz" -C "${docs_path}"
for writable_path in "${state_path}" "${docs_path}" "${logs_path}"; do
  docker run --rm --network none --user 0:0 --entrypoint chown \
    --volume "${writable_path}:/target" \
    "${app_image}" -R 10001:10001 /target \
    || fail "无法为 app UID 10001 设置目录所有权：${writable_path}"
done

compose=(
  docker compose
  --env-file "${env_file}"
  -f "${compose_file}"
)
"${compose[@]}" up -d --no-build --pull never \
  rag-qdrant rag-ocr rag-app

wait_healthy rag-qdrant
wait_healthy rag-ocr
wait_healthy rag-app
port="$(exact_env_value "${env_file}" RAG_PORT)" \
  || fail "env 缺少唯一 RAG_PORT。"
wait_live "${port}"

revision="$(exact_env_value "${env_file}" RAG_RELEASE_REVISION)" \
  || fail "env 缺少唯一 RAG_RELEASE_REVISION。"
"${compose[@]}" --profile index run --rm --no-deps rag-worker \
  index full --idempotency-key "simple-full-${revision}"

ready_payload="$(wait_demo_ready "${port}")"
printf 'demo_ready=%s\n' "${ready_payload}"
"${compose[@]}" ps rag-qdrant rag-ocr rag-app
printf 'frontend=http://SERVER_IP:%s/\n' "${port}"
printf 'app_update=bash update-app.sh app-image.tar.gz app-image.tar.gz.sha256 %s\n' \
  "${env_file}"
