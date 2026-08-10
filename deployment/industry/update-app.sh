#!/usr/bin/env bash
set -euo pipefail

fail() {
  printf 'RAG_INDUSTRY_APP_UPDATE_FAILED: %s\n' "$*" >&2
  exit 1
}

rollback_fail() {
  printf 'RAG_INDUSTRY_APP_ROLLBACK_FAILED: %s\n' "$*" >&2
  exit 2
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
      if (count != 1 || value == "") exit 2
      print value
    }
  ' "${env_file}"
}

run_compose_clean() {
  local env_file="$1"
  local compose_file="$2"
  shift 2
  local -a clean=(env -i "PATH=${PATH}" "HOME=${HOME:-/}")
  local name
  for name in \
    DOCKER_HOST DOCKER_CONFIG XDG_RUNTIME_DIR SSL_CERT_FILE SSL_CERT_DIR; do
    if [[ -n "${!name:-}" ]]; then
      clean+=("${name}=${!name}")
    fi
  done
  "${clean[@]}" docker compose -p rag-industry \
    --env-file "${env_file}" -f "${compose_file}" "$@"
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
      if (image_count != 1 || revision_count != 1) exit 2
    }
  ' "${source}" >"${destination}"
  chmod --reference="${source}" "${destination}"
}

image_fingerprint() {
  local image="$1"
  local report
  report="$(docker run --rm --network none "${image}" asset-selfcheck)" \
    || return 1
  python3 -c '
import json
import re
import sys

value = json.load(sys.stdin)
fingerprint = value.get("pipeline_fingerprint")
if (
    not isinstance(fingerprint, str)
    or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
):
    raise SystemExit("PIPELINE_FINGERPRINT_INVALID")
print(fingerprint)
' <<<"${report}"
}

container_env_value() {
  local key="$1"
  docker container inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
    rag-industry-app | exact_env_value /dev/stdin "${key}"
}

other_container_identity() {
  local name
  for name in rag-industry-worker rag-industry-ocr rag-industry-qdrant; do
    if docker container inspect "${name}" >/dev/null 2>&1; then
      docker container inspect --format \
        '{{.Name}}|{{.Id}}|{{.State.StartedAt}}' "${name}"
    else
      printf '/%s|absent|absent\n' "${name}"
    fi
  done
}

app_mount_identity() {
  docker container inspect --format '{{json .Mounts}}' rag-industry-app \
    | sha256sum | awk '{print $1}'
}

runtime_state() {
  docker exec rag-industry-app rag-app runtime-state
}

index_identity() {
  python3 -c '
import json
import re
import sys

value = json.load(sys.stdin)
active_collection = value.get("active_collection")
alias = value.get("alias")
index_fingerprint = value.get("index_fingerprint")
manifest_sha256 = value.get("manifest_sha256")
point_count = value.get("point_count")
if not isinstance(active_collection, str) or not active_collection:
    raise SystemExit("ACTIVE_COLLECTION_INVALID")
if alias != "rag-industry-active":
    raise SystemExit("ACTIVE_ALIAS_INVALID")
if (
    not isinstance(index_fingerprint, str)
    or re.fullmatch(r"sha256:[0-9a-f]{64}", index_fingerprint) is None
):
    raise SystemExit("INDEX_FINGERPRINT_INVALID")
if (
    not isinstance(manifest_sha256, str)
    or re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None
):
    raise SystemExit("MANIFEST_SHA256_INVALID")
if not isinstance(point_count, int) or isinstance(point_count, bool) or point_count <= 0:
    raise SystemExit("POINT_COUNT_INVALID")
print(json.dumps({
    "active_collection": active_collection,
    "alias": alias,
    "index_fingerprint": index_fingerprint,
    "manifest_sha256": manifest_sha256,
    "point_count": point_count,
}, separators=(",", ":"), sort_keys=True))
'
}

wait_url() {
  local url="$1"
  local deadline=$((SECONDS + 90))
  while ((SECONDS < deadline)); do
    if curl --fail --silent --show-error "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

validate_compose() {
  local env_file="$1"
  local compose_file="$2"
  local expected_image="$3"
  local config
  config="$(run_compose_clean "${env_file}" "${compose_file}" \
    --profile index --profile dedicated-ocr config --format json)" \
    || return 1
  python3 - "${expected_image}" \
    "$(exact_env_value "${env_file}" RAG_DOCS_PATH)" \
    "$(exact_env_value "${env_file}" RAG_CONFIG_PATH)" \
    "$(exact_env_value "${env_file}" RAG_STATE_PATH)" \
    3<<<"${config}" <<'PY'
import json
import os
import sys

value = json.load(os.fdopen(3))
if value.get("name") != "rag-industry":
    raise SystemExit("PROJECT_INVALID")
services = value.get("services", {})
app = services.get("rag-industry-app", {})
if app.get("image") != sys.argv[1]:
    raise SystemExit("APP_IMAGE_INVALID")
environment = app.get("environment", {})
if environment.get("RAG_QDRANT_ALIAS") != "rag-industry-active":
    raise SystemExit("ALIAS_INVALID")
ports = app.get("ports", [])
if len(ports) != 1 or str(ports[0].get("published")) != "8188":
    raise SystemExit("PORT_INVALID")
if str(ports[0].get("target")) != "8088":
    raise SystemExit("TARGET_PORT_INVALID")
mounts = {item.get("target"): item.get("source") for item in app.get("volumes", [])}
expected = {"/data/docs": sys.argv[2], "/config": sys.argv[3], "/state": sys.argv[4]}
if any(mounts.get(target) != source for target, source in expected.items()):
    raise SystemExit("MOUNT_INVALID")
PY
}

validate_observed() {
  local expected_image="$1"
  local expected_revision="$2"
  local expected_image_id="$3"
  local port="$4"
  local configured image_id project service revision
  configured="$(docker container inspect --format '{{.Config.Image}}' \
    rag-industry-app)" || return 1
  image_id="$(docker container inspect --format '{{.Image}}' \
    rag-industry-app)" || return 1
  project="$(docker container inspect --format \
    '{{index .Config.Labels "com.docker.compose.project"}}' \
    rag-industry-app)" || return 1
  service="$(docker container inspect --format \
    '{{index .Config.Labels "com.docker.compose.service"}}' \
    rag-industry-app)" || return 1
  revision="$(container_env_value RAG_RELEASE_REVISION)" || return 1
  [[ "${configured}" == "${expected_image}" \
    && "${image_id}" == "${expected_image_id}" \
    && "${project}" == "rag-industry" \
    && "${service}" == "rag-industry-app" \
    && "${revision}" == "${expected_revision}" ]] || return 1
  docker container inspect --format '{{json .NetworkSettings.Ports}}' \
    rag-industry-app | python3 -c '
import json, sys
ports = json.load(sys.stdin)
bindings = ports.get("8088/tcp")
if not isinstance(bindings, list) or len(bindings) != 1:
    raise SystemExit(1)
if bindings[0].get("HostPort") != "8188":
    raise SystemExit(1)
' || return 1
  docker exec rag-industry-app rag-app build-info \
    --expected-revision "${expected_revision}" >/dev/null || return 1
  wait_url "http://127.0.0.1:${port}/live" || return 1
  wait_url "http://127.0.0.1:${port}/ready" || return 1
}

write_candidate_state() {
  local path="$1"
  local base_image="$2"
  local base_revision="$3"
  local target_image="$4"
  local target_revision="$5"
  local index="$6"
  local temporary
  temporary="$(mktemp "$(dirname "${path}")/.app-candidate.XXXXXX")"
  python3 - "${temporary}" "${base_image}" "${base_revision}" \
    "${target_image}" "${target_revision}" "${index}" <<'PY'
import json
import pathlib
import sys

payload = {
    "base": {"image": sys.argv[2], "revision": sys.argv[3]},
    "index": json.loads(sys.argv[6]),
    "schema_version": "1",
    "stage": "candidate",
    "target": {"image": sys.argv[4], "revision": sys.argv[5]},
    "update_kind": "app_only",
}
path = pathlib.Path(sys.argv[1])
path.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
path.chmod(0o600)
PY
  mv -f -- "${temporary}" "${path}"
}

[[ "$#" -eq 4 ]] || fail \
  "用法: update-app.sh app-image.tar.gz app-image.tar.gz.sha256 UPDATE_MANIFEST.json /absolute/rag-industry.env"
for path in "$1" "$2" "$3" "$4"; do
  [[ "${path}" == /* && -f "${path}" && ! -L "${path}" ]] \
    || fail "所有输入必须是绝对路径下的普通文件。"
done
archive="$(realpath "$1")"
sidecar="$(realpath "$2")"
manifest="$(realpath "$3")"
env_file="$(realpath "$4")"
package_dir="$(dirname "${archive}")"
[[ "$(dirname "${sidecar}")" == "${package_dir}" \
  && "$(dirname "${manifest}")" == "${package_dir}" \
  && "$(realpath "${BASH_SOURCE[0]}")" == "${package_dir}/update-app.sh" ]] \
  || fail "更新包四个文件必须位于同一目录。"
mapfile -t package_files < <(find "${package_dir}" -maxdepth 1 -type f \
  -printf '%f\n' | LC_ALL=C sort)
[[ "${package_files[*]}" == \
  "UPDATE_MANIFEST.json app-image.tar.gz app-image.tar.gz.sha256 update-app.sh" ]] \
  || fail "Industry app update exact set 无效。"
(
  cd "${package_dir}"
  sha256sum --check "$(basename "${sidecar}")"
) >/dev/null || fail "app image SHA256 校验失败。"

manifest_row="$(python3 - "${manifest}" "${package_dir}" <<'PY'
import hashlib
import json
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
root = pathlib.Path(sys.argv[2])
value = json.loads(path.read_bytes())
if not isinstance(value, dict):
    raise SystemExit("MANIFEST_TYPE_INVALID")
if value.get("schema_version") != "1" or value.get("branch") != "Industry":
    raise SystemExit("MANIFEST_IDENTITY_INVALID")
if value.get("target") != {
    "alias": "rag-industry-active",
    "project": "rag-industry",
    "service": "rag-industry-app",
}:
    raise SystemExit("MANIFEST_TARGET_INVALID")
image = value.get("image", {})
revision = value.get("revision")
fingerprint = value.get("index_fingerprint", {})
serving_fingerprint = value.get("serving_fingerprint")
if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
    raise SystemExit("REVISION_INVALID")
if not isinstance(image, dict):
    raise SystemExit("IMAGE_TYPE_INVALID")
if (
    image.get("revision") != revision
    or image.get("platform") != "linux/amd64"
    or image.get("ref") != f"docx-rag:{revision[:12]}"
    or not isinstance(image.get("id"), str)
    or re.fullmatch(r"sha256:[0-9a-f]{64}", image["id"]) is None
):
    raise SystemExit("IMAGE_IDENTITY_INVALID")
if not isinstance(fingerprint, dict):
    raise SystemExit("INDEX_FINGERPRINT_TYPE_INVALID")
if (
    fingerprint.get("reindex_required") is not False
    or not isinstance(fingerprint.get("target"), str)
    or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint["target"]) is None
):
    raise SystemExit("REINDEX_REQUIRED")
if (
    not isinstance(serving_fingerprint, str)
    or re.fullmatch(r"sha256:[0-9a-f]{64}", serving_fingerprint) is None
):
    raise SystemExit("SERVING_FINGERPRINT_INVALID")
files = value.get("files", {})
if not isinstance(files, dict) or set(files) != {
    "app-image.tar.gz",
    "app-image.tar.gz.sha256",
    "update-app.sh",
}:
    raise SystemExit("FILES_INVALID")
for name in ("app-image.tar.gz", "app-image.tar.gz.sha256", "update-app.sh"):
    digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
    expected_digest = files.get(name)
    if (
        not isinstance(expected_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        or expected_digest != digest
    ):
        raise SystemExit("FILE_SHA256_INVALID")
print("\t".join((image["ref"], image["id"], revision, fingerprint["target"])))
PY
)" || fail "UPDATE_MANIFEST.json 校验失败。"
IFS=$'\t' read -r new_image expected_new_image_id new_revision \
  manifest_index_fingerprint <<<"${manifest_row}"

old_image="$(exact_env_value "${env_file}" RAG_APP_IMAGE)" \
  || fail "env 缺少唯一 RAG_APP_IMAGE。"
old_revision="$(exact_env_value "${env_file}" RAG_RELEASE_REVISION)" \
  || fail "env 缺少唯一 RAG_RELEASE_REVISION。"
compose_file="$(exact_env_value "${env_file}" RAG_INDUSTRY_COMPOSE_FILE)" \
  || fail "env 缺少唯一 RAG_INDUSTRY_COMPOSE_FILE。"
port="$(exact_env_value "${env_file}" RAG_PORT)" \
  || fail "env 缺少唯一 RAG_PORT。"
alias="$(exact_env_value "${env_file}" RAG_QDRANT_ALIAS)" \
  || fail "env 缺少唯一 RAG_QDRANT_ALIAS。"
backup_path="$(exact_env_value "${env_file}" RAG_BACKUP_PATH)" \
  || fail "env 缺少唯一 RAG_BACKUP_PATH。"
[[ -f "${compose_file}" && ! -L "${compose_file}" \
  && "${port}" == "8188" && "${alias}" == "rag-industry-active" ]] \
  || fail "Industry compose、port 或 alias 身份无效。"
mkdir -p -- "${backup_path}"
validate_compose "${env_file}" "${compose_file}" "${old_image}" \
  || fail "更新前 Compose canonical config 无效。"
docker image inspect "${old_image}" >/dev/null \
  || fail "旧 app image 不存在。"
old_image_id="$(docker image inspect --format '{{.Id}}' "${old_image}")"
old_image_revision="$(docker image inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "${old_image}")"
[[ "${old_image_revision}" == "${old_revision}" ]] \
  || fail "旧 app image revision 与 env 不一致。"
validate_observed "${old_image}" "${old_revision}" "${old_image_id}" "${port}" \
  || fail "更新前 running app identity 无效。"
before_runtime="$(runtime_state)" || fail "更新前 runtime-state 不可用。"
before_index="$(index_identity <<<"${before_runtime}")" \
  || fail "更新前 index identity 无效。"
before_mounts="$(app_mount_identity)"
before_others="$(other_container_identity)"
old_fingerprint="$(image_fingerprint "${old_image}")" \
  || fail "旧 app asset-selfcheck 失败。"
[[ "${old_fingerprint}" == "${manifest_index_fingerprint}" ]] \
  || fail "旧 app index fingerprint 与更新包不一致。"

load_output="$(gzip -dc -- "${archive}" | docker image load)" \
  || fail "docker load 新 app image 失败。"
loaded_image="$(awk -F': ' '/^Loaded image: / {value=$2} END {print value}' \
  <<<"${load_output}")"
[[ "${loaded_image}" == "${new_image}" ]] \
  || fail "docker load image tag 与 manifest 不一致。"
actual_new_image_id="$(docker image inspect --format '{{.Id}}' "${new_image}")"
actual_new_platform="$(docker image inspect --format \
  '{{.Os}}/{{.Architecture}}' "${new_image}")"
actual_new_revision="$(docker image inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "${new_image}")"
[[ "${actual_new_image_id}" == "${expected_new_image_id}" \
  && "${actual_new_platform}" == "linux/amd64" \
  && "${actual_new_revision}" == "${new_revision}" ]] \
  || fail "已加载 app image 身份与 manifest 不一致。"
docker run --rm --network none "${new_image}" build-info \
  --expected-revision "${new_revision}" >/dev/null \
  || fail "新 app wheel revision 自检失败。"
new_fingerprint="$(image_fingerprint "${new_image}")" \
  || fail "新 app asset-selfcheck 失败。"
[[ "${new_fingerprint}" == "${old_fingerprint}" \
  && "${new_fingerprint}" == "${manifest_index_fingerprint}" ]] \
  || fail "index fingerprint 已变化，拒绝 app-only 更新。"

env_dir="$(dirname "${env_file}")"
backup="$(mktemp "${env_dir}/.industry-env.backup.XXXXXX")"
candidate="$(mktemp "${env_dir}/.industry-env.candidate.XXXXXX")"
restore_candidate=""
trap 'rm -f -- "${backup}" "${candidate}" ${restore_candidate:+"${restore_candidate}"}' EXIT
cp --preserve=mode,ownership,timestamps -- "${env_file}" "${backup}"
write_env_candidate "${env_file}" "${candidate}" "${new_image}" "${new_revision}" \
  || fail "env 中 app image/revision 必须各出现一次。"
write_candidate_state \
  "${backup_path}/app-candidate.json" \
  "${old_image}" "${old_revision}" "${new_image}" "${new_revision}" \
  "${before_index}" || fail "app candidate 状态写入失败。"
mv -f -- "${candidate}" "${env_file}"
validate_compose "${env_file}" "${compose_file}" "${new_image}" \
  || fail "更新后 Compose canonical config 无效。"

rollback_update() {
  restore_candidate="$(mktemp "${env_dir}/.industry-env.restore.XXXXXX")"
  cp --preserve=mode,ownership,timestamps -- \
    "${backup}" "${restore_candidate}" || return 1
  mv -f -- "${restore_candidate}" "${env_file}" || return 1
  restore_candidate=""
  validate_compose "${env_file}" "${compose_file}" "${old_image}" \
    || return 1
  run_compose_clean "${env_file}" "${compose_file}" up -d \
    --no-deps --no-build --pull never --force-recreate rag-industry-app \
    || return 1
  validate_observed "${old_image}" "${old_revision}" \
    "${old_image_id}" "${port}" || return 1
  [[ "$(index_identity <<<"$(runtime_state)")" == "${before_index}" \
    && "$(app_mount_identity)" == "${before_mounts}" \
    && "$(other_container_identity)" == "${before_others}" ]] || return 1
}

update_ok=true
run_compose_clean "${env_file}" "${compose_file}" up -d \
  --no-deps --no-build --pull never --force-recreate rag-industry-app \
  || update_ok=false
if [[ "${update_ok}" == true ]]; then
  validate_observed "${new_image}" "${new_revision}" \
    "${expected_new_image_id}" "${port}" || update_ok=false
fi
if [[ "${update_ok}" == true ]]; then
  after_index="$(index_identity <<<"$(runtime_state)")" || update_ok=false
  [[ "${after_index:-}" == "${before_index}" \
    && "$(app_mount_identity)" == "${before_mounts}" \
    && "$(other_container_identity)" == "${before_others}" ]] \
    || update_ok=false
fi
if [[ "${update_ok}" != true ]]; then
  rollback_update || rollback_fail \
    "新 app 失败，旧 env/image/identity 未完整恢复。"
  fail "新 app 未通过终态校验；旧 app 已完整恢复。"
fi

rm -f -- "${backup}"
printf 'reindex_required=false\n'
printf 'RAG_INDUSTRY_APP_UPDATE_OK image=%s revision=%s candidate=%s\n' \
  "${new_image}" "${new_revision}" "${backup_path}/app-candidate.json"
printf 'next=bash %s/verify.sh %s\n' "$(dirname "${compose_file}")" "${env_file}"
