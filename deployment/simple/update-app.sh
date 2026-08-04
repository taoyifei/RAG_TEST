#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'SIMPLE_APP_UPDATE_FAILED: %s\n' "$*" >&2
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

image_fingerprint() {
  local image="$1"
  local report
  report="$(docker run --rm --network none \
    "${image}" asset-selfcheck)" \
    || return 1
  sed -n \
    's/.*"pipeline_fingerprint":"\([^"]*\)".*/\1/p' \
    <<<"${report}"
}

write_env_candidate() {
  local source="$1"
  local destination="$2"
  local image="$3"
  local revision="$4"
  awk -F= -v image="${image}" -v revision="${revision}" '
    $1 == "RAG_APP_IMAGE" {
      image_count += 1
      print "RAG_APP_IMAGE=" image
      next
    }
    $1 == "RAG_RELEASE_REVISION" {
      revision_count += 1
      print "RAG_RELEASE_REVISION=" revision
      next
    }
    { print }
    END {
      if (image_count != 1 || revision_count != 1) {
        exit 2
      }
    }
  ' "${source}" >"${destination}"
  chmod --reference="${source}" "${destination}"
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
  return 1
}

[[ "$#" -eq 3 || "$#" -eq 4 ]] \
  || fail "用法: bash update-app.sh app-image.tar.gz sidecar /path/rag.env [--restart-worker]"
restart_worker=false
if [[ "$#" -eq 4 ]]; then
  [[ "$4" == "--restart-worker" ]] || fail "未知选项：$4"
  restart_worker=true
fi
[[ "$3" == /* ]] || fail "env 必须使用绝对路径。"
[[ -f "$1" && -f "$2" && -f "$3" ]] \
  || fail "归档、sidecar 或 env 不存在。"

archive="$(realpath "$1")"
sidecar="$(realpath "$2")"
env_file="$(realpath "$3")"
[[ "$(dirname "${archive}")" == "$(dirname "${sidecar}")" ]] \
  || fail "app 归档与 sidecar 必须位于同一目录。"
(
  cd "$(dirname "${archive}")"
  sha256sum --check "$(basename "${sidecar}")"
) || fail "app-image.tar.gz SHA256 校验失败。"

old_image="$(exact_env_value "${env_file}" RAG_APP_IMAGE)" \
  || fail "env 缺少唯一 RAG_APP_IMAGE。"
docker image inspect "${old_image}" >/dev/null \
  || fail "旧 app 镜像不存在：${old_image}"
old_fingerprint="$(image_fingerprint "${old_image}")" \
  || fail "旧 app 镜像 asset-selfcheck 失败。"

load_output="$(gzip -dc -- "${archive}" | docker load)" \
  || fail "docker load 新 app 镜像失败。"
new_image="$(awk -F': ' '/^Loaded image: / {value=$2} END {print value}' \
  <<<"${load_output}")"
[[ -n "${new_image}" ]] || fail "docker load 未返回唯一 app image tag。"
docker image inspect "${new_image}" >/dev/null \
  || fail "新 app 镜像 tag 不存在：${new_image}"
new_revision="$(docker image inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "${new_image}")"
[[ "${new_revision}" =~ ^[0-9a-f]{40}$ ]] \
  || fail "新 app 镜像缺少完整 revision label。"
new_fingerprint="$(image_fingerprint "${new_image}")" \
  || fail "新 app 镜像 asset-selfcheck 失败。"

compose_file="$(exact_env_value "${env_file}" RAG_SIMPLE_COMPOSE_FILE)" \
  || fail "env 缺少唯一 RAG_SIMPLE_COMPOSE_FILE。"
[[ -f "${compose_file}" && ! -L "${compose_file}" ]] \
  || fail "RAG_SIMPLE_COMPOSE_FILE 不是普通文件。"
port="$(exact_env_value "${env_file}" RAG_PORT)" \
  || fail "env 缺少唯一 RAG_PORT。"

env_dir="$(dirname "${env_file}")"
backup="$(mktemp "${env_dir}/.rag-env.backup.XXXXXX")"
candidate="$(mktemp "${env_dir}/.rag-env.candidate.XXXXXX")"
restore_candidate=""
trap 'rm -f -- "${backup}" "${candidate}" ${restore_candidate:+"${restore_candidate}"}' EXIT
cp --preserve=mode,ownership,timestamps -- "${env_file}" "${backup}"
write_env_candidate \
  "${env_file}" "${candidate}" "${new_image}" "${new_revision}" \
  || fail "env 中 app image/revision 必须各出现一次。"
mv -f -- "${candidate}" "${env_file}"

compose=(
  docker compose
  --env-file "${env_file}"
  -f "${compose_file}"
)

rollback_update() {
  restore_candidate="$(mktemp "${env_dir}/.rag-env.restore.XXXXXX")"
  cp --preserve=mode,ownership,timestamps -- \
    "${backup}" "${restore_candidate}" || return 1
  mv -f -- "${restore_candidate}" "${env_file}" || return 1
  restore_candidate=""
  "${compose[@]}" up -d --no-deps --no-build --pull never rag-app \
    || return 1
  if [[ "${restart_worker}" == true ]]; then
    "${compose[@]}" --profile index up -d --no-deps \
      --no-build --pull never rag-worker || return 1
  fi
}

update_ok=true
"${compose[@]}" up -d --no-deps --no-build --pull never rag-app \
  || update_ok=false
if [[ "${update_ok}" == true && "${restart_worker}" == true ]]; then
  "${compose[@]}" --profile index up -d --no-deps \
    --no-build --pull never rag-worker || update_ok=false
fi
if [[ "${update_ok}" == true ]]; then
  wait_live "${port}" || update_ok=false
fi

if [[ "${update_ok}" != true ]]; then
  rollback_update || fail "新 app 失败，且旧 env/image 恢复失败。"
  fail "新 app 未通过 /live，已恢复旧 image 和旧 env。"
fi

rm -f -- "${backup}"
if [[ "${old_fingerprint}" != "${new_fingerprint}" ]]; then
  printf '%s\n' \
    'REINDEX_REQUIRED: parser/chunker/embedding/index fingerprint 已变化，请执行 DEPLOYMENT_GUIDE.md 的全量重建索引命令。'
else
  printf '%s\n' 'reindex_required=false'
fi
printf 'app_update_ok image=%s revision=%s worker_restarted=%s\n' \
  "${new_image}" "${new_revision}" "${restart_worker}"
