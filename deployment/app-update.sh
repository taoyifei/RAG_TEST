#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root="/data/tyf/RAG"
current_link="${project_root}/current"
active_env="${project_root}/shared/env/rag.env"
update_root="${project_root}/shared/app-update"
state_file="${update_root}/state.json"
override_file="${update_root}/app-update.override.yaml"
command="${1:-}"
update_argument="${2:-}"
APP_HEALTH_TIMEOUT_SECONDS=60
APP_LIVE_TIMEOUT_SECONDS=60
HEALTH_POLL_INTERVAL_SECONDS=1

fail() {
  echo "$1" >&2
  exit 1
}

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
    exact_env_value "${file}" "${key}"
  fi
}

container_exists() {
  docker container inspect "$1" >/dev/null 2>&1
}

container_running() {
  docker container inspect --format '{{.State.Running}}' "$1"
}

container_image() {
  docker container inspect --format '{{.Image}}' "$1"
}

image_id() {
  docker image inspect --format '{{.Id}}' "$1"
}

wait_for_app_health() {
  local deadline
  local status
  deadline="$(($(date +%s) + APP_HEALTH_TIMEOUT_SECONDS))"
  while (($(date +%s) < deadline)); do
    if ! container_exists rag-app; then
      echo "rag-app 容器不存在。" >&2
      return 1
    fi
    status="$(docker container inspect \
      --format '{{.State.Health.Status}}' rag-app)"
    case "${status}" in
      healthy) return 0 ;;
      unhealthy)
        echo "rag-app health 为 unhealthy。" >&2
        return 1
        ;;
      starting) ;;
      *)
        echo "rag-app health 字段无效。" >&2
        return 1
        ;;
    esac
    sleep "${HEALTH_POLL_INTERVAL_SECONDS}"
  done
  echo "rag-app health 超时。" >&2
  return 1
}

wait_for_app_live() {
  local port="$1"
  local deadline
  deadline="$(($(date +%s) + APP_LIVE_TIMEOUT_SECONDS))"
  while (($(date +%s) < deadline)); do
    if curl -fsS --connect-timeout 2 --max-time 5 \
      "http://127.0.0.1:${port}/live" >/dev/null; then
      return 0
    fi
    sleep "${HEALTH_POLL_INTERVAL_SECONDS}"
  done
  echo "rag-app /live 未在限时内返回 200。" >&2
  return 1
}

validate_update_directory() {
  local directory="$1"
  python3 - "${directory}" <<'PY'
import hashlib
import json
import re
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
sha_line = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)$")
full_revision = re.compile(r"^[0-9a-f]{40}$")
digest = re.compile(r"^sha256:[0-9a-f]{64}$")
allowed_categories = {
    "app_assets",
    "app_build",
    "app_dependencies",
    "app_python",
    "app_serving_config",
    "frontend",
    "verification_only",
}


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


entries = list(root.iterdir())
if len(entries) != 4 or any(
    not stat.S_ISREG(path.lstat().st_mode) or path.is_symlink()
    for path in entries
):
    raise SystemExit("app update 目录必须恰有四个普通文件。")
metadata_path = root / "APP_UPDATE.json"
manifest_path = root / "APP_UPDATE_MANIFEST.sha256"
try:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit("APP_UPDATE.json 不是有效 UTF-8 JSON。") from error
expected_keys = {
    "archive",
    "base_revision",
    "change_categories",
    "changed_path_count",
    "config_digest",
    "image_tag",
    "index_fingerprint",
    "manifest_digest",
    "platform",
    "reindex_required",
    "schema_version",
    "serving_fingerprint",
    "target_revision",
}
if type(metadata) is not dict or set(metadata) != expected_keys:
    raise SystemExit("APP_UPDATE.json 字段集合无效。")
base = metadata["base_revision"]
target = metadata["target_revision"]
archive = metadata["archive"]
tag = metadata["image_tag"]
categories = metadata["change_categories"]
path_count = metadata["changed_path_count"]
if (
    metadata["schema_version"] != "1"
    or type(base) is not str
    or full_revision.fullmatch(base) is None
    or type(target) is not str
    or full_revision.fullmatch(target) is None
    or type(archive) is not str
    or archive != f"docx-rag-app-{target[:12]}.tar.gz"
    or type(tag) is not str
    or tag != f"docx-rag:{target[:12]}"
    or metadata["platform"] != "linux/amd64"
    or type(path_count) is not int
    or type(path_count) is bool
    or path_count < 1
    or type(categories) is not list
    or not categories
    or categories != sorted(set(categories))
    or any(category not in allowed_categories for category in categories)
    or type(metadata["reindex_required"]) is not bool
    or type(metadata["manifest_digest"]) is not str
    or digest.fullmatch(metadata["manifest_digest"]) is None
    or type(metadata["config_digest"]) is not str
    or digest.fullmatch(metadata["config_digest"]) is None
):
    raise SystemExit("APP_UPDATE.json revision、platform 或身份字段无效。")
for key in ("index_fingerprint", "serving_fingerprint"):
    value = metadata[key]
    if (
        type(value) is not dict
        or set(value) != {"base", "target"}
        or any(type(item) is not str or digest.fullmatch(item) is None
               for item in value.values())
    ):
        raise SystemExit(f"APP_UPDATE.json {key} 无效。")
if metadata["reindex_required"] != (
    metadata["index_fingerprint"]["base"]
    != metadata["index_fingerprint"]["target"]
):
    raise SystemExit("reindex_required 与 index fingerprint 不一致。")
expected = {
    "APP_UPDATE.json",
    "APP_UPDATE_MANIFEST.sha256",
    archive,
    f"{archive}.sha256",
}
if {path.name for path in entries} != expected:
    raise SystemExit("app update 四文件集合无效。")
manifest_entries = {}
for line in manifest_path.read_text(encoding="ascii").splitlines():
    matched = sha_line.fullmatch(line)
    if matched is None or matched.group(2) in manifest_entries:
        raise SystemExit("APP_UPDATE_MANIFEST.sha256 格式无效。")
    manifest_entries[matched.group(2)] = matched.group(1)
if set(manifest_entries) != expected - {manifest_path.name}:
    raise SystemExit("APP_UPDATE_MANIFEST.sha256 文件集合无效。")
for name, expected_sha in manifest_entries.items():
    if sha256(root / name) != expected_sha:
        raise SystemExit(f"app update SHA256 不一致：{name}")
sidecar_lines = (root / f"{archive}.sha256").read_text(
    encoding="ascii"
).splitlines()
if len(sidecar_lines) != 1:
    raise SystemExit("app archive SHA256 sidecar 格式无效。")
matched = sha_line.fullmatch(sidecar_lines[0])
if matched is None or matched.group(2) != archive:
    raise SystemExit("app archive SHA256 sidecar 未绑定归档。")
if matched.group(1) != sha256(root / archive):
    raise SystemExit("app archive SHA256 不一致。")
print("\t".join((
    archive,
    base,
    target,
    tag,
    metadata["manifest_digest"],
    metadata["config_digest"],
    metadata["platform"],
    "true" if metadata["reindex_required"] else "false",
)))
PY
}

read_state() {
  python3 - "${state_file}" <<'PY'
import json
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    value = json.loads(path.read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit("app update state 无效。") from error
keys = {
    "base_image_id",
    "base_image_ref",
    "base_release",
    "base_revision",
    "config_digest",
    "manifest_digest",
    "override_sha256",
    "platform",
    "reindex_required",
    "release_tier",
    "schema_version",
    "status",
    "target_image_id",
    "target_image_tag",
    "target_revision",
}
sha = re.compile(r"^sha256:[0-9a-f]{64}$")
revision = re.compile(r"^[0-9a-f]{40}$")
if type(value) is not dict or set(value) != keys:
    raise SystemExit("app update state 字段集合无效。")
if (
    value["schema_version"] != "1"
    or value["status"] != "active"
    or value["platform"] != "linux/amd64"
    or value["release_tier"] not in {"smoke", "production"}
    or type(value["reindex_required"]) is not bool
    or any(revision.fullmatch(value[key]) is None
           for key in ("base_revision", "target_revision"))
    or any(sha.fullmatch(value[key]) is None for key in (
        "base_image_id",
        "target_image_id",
        "config_digest",
        "manifest_digest",
    ))
    or re.fullmatch(r"[0-9a-f]{64}", value["override_sha256"]) is None
):
    raise SystemExit("app update state 身份无效。")
print("\t".join((
    value["base_release"],
    value["base_revision"],
    value["target_revision"],
    value["base_image_ref"],
    value["base_image_id"],
    value["target_image_tag"],
    value["target_image_id"],
    value["manifest_digest"],
    value["config_digest"],
    value["release_tier"],
    "true" if value["reindex_required"] else "false",
    value["override_sha256"],
)))
PY
}

require_current_release() {
  if [[ ! -L "${current_link}" ]]; then
    fail "current 必须是已安装基础 release 的符号链接。"
  fi
  current_release="$(readlink -f "${current_link}")"
  if [[ "${current_release}" != "${project_root}/releases/"* \
    || "$(dirname "${current_release}")" != "${project_root}/releases" \
    || ! -d "${current_release}" \
    || ! -f "${current_release}/SOURCE_REVISION" \
    || ! -f "${current_release}/RELEASE_METADATA.json" \
    || ! -f "${current_release}/compose.yaml" \
    || ! -f "${current_release}/verify-offline.sh" ]]; then
    fail "current 基础 release 无效。"
  fi
  bash "${current_release}/verify-offline.sh"
  base_source_revision="$(cat "${current_release}/SOURCE_REVISION")"
  release_tier="$(python3 - "${current_release}/RELEASE_METADATA.json" <<'PY'
import json
import sys
from pathlib import Path

value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
tier = value.get("release_tier") if isinstance(value, dict) else None
if tier not in {"smoke", "production"}:
    raise SystemExit("基础 release tier 无效。")
print(tier)
PY
)"
  require_regular_0600 "${active_env}" "活动环境文件"
  if [[ "$(exact_env_value "${active_env}" RAG_RELEASE_REVISION)" \
    != "${base_source_revision}" ]]; then
    fail "active env revision 与 current 基础 release 不一致。"
  fi
  base_image_ref="$(exact_env_value "${active_env}" RAG_APP_IMAGE)"
  base_image_id="$(image_id "${base_image_ref}")"
  port="$(optional_env_value "${active_env}" RAG_PORT)"
  port="${port:-8088}"
}

require_worker_stopped() {
  if container_exists rag-worker \
    && [[ "$(container_running rag-worker)" != "false" ]]; then
    fail "rag-worker 必须停止后才能应用或回滚 app update。"
  fi
}

capture_invariants() {
  active_env_sha="$(sha256sum "${active_env}" | awk '{print $1}')"
  current_target="$(readlink -f "${current_link}")"
  if ! container_exists rag-ocr || ! container_exists rag-qdrant; then
    fail "基础 OCR/Qdrant 容器必须存在。"
  fi
  ocr_image="$(container_image rag-ocr)"
  ocr_running="$(container_running rag-ocr)"
  qdrant_image="$(container_image rag-qdrant)"
  qdrant_running="$(container_running rag-qdrant)"
}

verify_invariants() {
  if [[ "$(sha256sum "${active_env}" | awk '{print $1}')" \
      != "${active_env_sha}" \
    || "$(readlink -f "${current_link}")" != "${current_target}" \
    || "$(container_image rag-ocr)" != "${ocr_image}" \
    || "$(container_running rag-ocr)" != "${ocr_running}" \
    || "$(container_image rag-qdrant)" != "${qdrant_image}" \
    || "$(container_running rag-qdrant)" != "${qdrant_running}" ]]; then
    echo "app update 改变了受保护的 env/current/OCR/Qdrant 状态。" >&2
    return 1
  fi
}

restore_base_app() {
  docker compose --env-file "${active_env}" \
    -f "${current_release}/compose.yaml" \
    up -d --no-deps --no-build --pull never rag-app || return 1
  if [[ "$(container_image rag-app)" != "${base_image_id}" ]]; then
    return 1
  fi
  wait_for_app_health || return 1
  wait_for_app_live "${port}" || return 1
  verify_invariants || return 1
  rm -f -- "${override_file}" "${state_file}"
}

apply_update() {
  local update_dir
  local metadata_values
  local raw_archive
  local actual_identity
  local cleanup_succeeded
  local override_new=""
  local state_new=""
  local target_image_id=""
  if [[ -z "${update_argument}" || "${update_argument}" != /* ]]; then
    fail "apply 必须提供 canonical 绝对更新目录。"
  fi
  assert_no_symlink_ancestors "${update_argument}"
  update_dir="$(realpath -e "${update_argument}")"
  if [[ "${update_dir}" != "${update_argument}" \
    || ! -d "${update_dir}" || -L "${update_dir}" ]]; then
    fail "更新目录必须是 canonical 真实目录。"
  fi
  if [[ -e "${state_file}" || -L "${state_file}" \
    || -e "${override_file}" || -L "${override_file}" ]]; then
    fail "已有活动 app update；请先执行 app-update.sh rollback。"
  fi
  require_current_release
  if ! container_exists rag-app \
    || [[ "$(container_running rag-app)" != "true" ]] \
    || [[ "$(container_image rag-app)" != "${base_image_id}" ]]; then
    fail "基础 rag-app 未以 active env 镜像运行。"
  fi
  require_worker_stopped
  capture_invariants
  metadata_values="$(validate_update_directory "${update_dir}")"
  IFS=$'\t' read -r archive base_revision target_revision image_tag \
    manifest_digest config_digest platform reindex_required \
    <<< "${metadata_values}"
  if [[ "${base_revision}" != "${base_source_revision}" ]]; then
    fail "app update base revision 与 current SOURCE_REVISION 不一致。"
  fi
  if [[ "${release_tier}" == "production" \
    && "${reindex_required}" == "true" ]]; then
    fail "production 禁止热切换 reindex_required=true 的 app update。"
  fi
  raw_archive="$(mktemp "${update_root}/.app-image.XXXXXXXX.tar")"
  if ! python3 - "${update_dir}/${archive}" "${raw_archive}" <<'PY'
import gzip
import shutil
import sys
from pathlib import Path

limit = 20 * 1024 * 1024 * 1024
written = 0
with gzip.open(Path(sys.argv[1]), "rb") as source, Path(sys.argv[2]).open(
    "wb"
) as output:
    while block := source.read(1024 * 1024):
        written += len(block)
        if written > limit:
            raise SystemExit("app image 解压上限为 20 GiB。")
        output.write(block)
PY
  then
    rm -f -- "${raw_archive}"
    fail "app image gzip 解包失败。"
  fi
  if ! actual_identity="$(python3 \
    "${current_release}/scripts/docker_archive_identity.py" \
    "${raw_archive}" --tag "${image_tag}" --platform "${platform}" \
    --expected-revision "${target_revision}")"; then
    rm -f -- "${raw_archive}"
    fail "app OCI archive revision 或平台无效。"
  fi
  rm -f -- "${raw_archive}"
  if [[ "${actual_identity}" \
    != "${manifest_digest}"$'\t'"${config_digest}"$'\t'"${platform}" ]]; then
    fail "app OCI manifest/config digest 与 metadata 不一致。"
  fi
  perform_apply() {
    docker load --input "${update_dir}/${archive}" || return 1
    if ! docker image inspect "${image_tag}" | python3 \
      "${current_release}/scripts/docker_archive_loaded_identity.py" \
      --manifest-digest "${manifest_digest}" \
      --config-digest "${config_digest}" \
      --platform "${platform}" \
      --expected-revision "${target_revision}" >/dev/null; then
      echo "docker load 后 app OCI 身份不一致。" >&2
      return 1
    fi
    target_image_id="$(image_id "${image_tag}")" || return 1
    override_new="$(mktemp "${update_root}/.override.XXXXXXXX")" \
      || return 1
    {
      printf 'services:\n'
      printf '  rag-app:\n'
      printf '    image: %s\n' "${image_tag}"
      printf '    environment:\n'
      printf '      RAG_RELEASE_REVISION: %s\n' "${target_revision}"
    } > "${override_new}" || return 1
    chmod 0600 "${override_new}" || return 1
    mv -T "${override_new}" "${override_file}" || return 1
    override_new=""
    docker compose --env-file "${active_env}" \
      -f "${current_release}/compose.yaml" -f "${override_file}" \
      config -q || return 1
    docker compose --env-file "${active_env}" \
      -f "${current_release}/compose.yaml" -f "${override_file}" \
      up -d --no-deps --no-build --pull never rag-app || return 1
    if [[ "$(container_image rag-app)" != "${target_image_id}" ]]; then
      return 1
    fi
    wait_for_app_health || return 1
    wait_for_app_live "${port}" || return 1
    verify_invariants || return 1
    state_new="$(mktemp "${update_root}/.state.XXXXXXXX")" || return 1
    python3 - "${state_new}" "${current_release}" \
      "${base_revision}" "${target_revision}" "${base_image_ref}" \
      "${base_image_id}" "${image_tag}" "${target_image_id}" \
      "${manifest_digest}" "${config_digest}" "${release_tier}" \
      "${reindex_required}" "$(sha256sum "${override_file}" \
        | awk '{print $1}')" <<'PY' || return 1
import json
import sys
from pathlib import Path

keys = (
    "base_release",
    "base_revision",
    "target_revision",
    "base_image_ref",
    "base_image_id",
    "target_image_tag",
    "target_image_id",
    "manifest_digest",
    "config_digest",
    "release_tier",
    "reindex_required",
    "override_sha256",
)
payload = dict(zip(keys, sys.argv[2:], strict=True))
payload.update({
    "platform": "linux/amd64",
    "reindex_required": payload["reindex_required"] == "true",
    "schema_version": "1",
    "status": "active",
})
Path(sys.argv[1]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
    chmod 0600 "${state_new}" || return 1
    mv -T "${state_new}" "${state_file}" || return 1
    state_new=""
  }
  if ! perform_apply; then
    cleanup_succeeded="true"
    if [[ -n "${override_new}" ]] \
      && ! rm -f -- "${override_new}"; then
      cleanup_succeeded="false"
    fi
    if [[ -n "${state_new}" ]] \
      && ! rm -f -- "${state_new}"; then
      cleanup_succeeded="false"
    fi
    if restore_base_app && [[ "${cleanup_succeeded}" == "true" ]]; then
      echo "APP_UPDATE_FAILED_RECOVERED" >&2
      exit 1
    fi
    echo "APP_UPDATE_FAILED_RECOVERY_FAILED" >&2
    exit 70
  fi
  echo "status=active"
  echo "target_revision=${target_revision}"
  echo "reindex_required=${reindex_required}"
}

show_status() {
  if [[ ! -e "${state_file}" && ! -L "${state_file}" \
    && ! -e "${override_file}" && ! -L "${override_file}" ]]; then
    echo "status=inactive"
    return 0
  fi
  require_regular_0600 "${state_file}" "app update state"
  require_regular_0600 "${override_file}" "app update override"
  IFS=$'\t' read -r base_release base_revision target_revision \
    base_image_ref base_image_id target_image_tag target_image_id \
    manifest_digest config_digest release_tier reindex_required \
    override_sha <<< "$(read_state)"
  if [[ "$(sha256sum "${override_file}" | awk '{print $1}')" \
    != "${override_sha}" ]]; then
    fail "app update override SHA256 与 state 不一致。"
  fi
  status=active
  if [[ ! -L "${current_link}" \
    || "$(readlink -f "${current_link}")" != "${base_release}" ]]; then
    status=degraded
  elif ! container_exists rag-app; then
    status=degraded
  elif [[ "$(container_running rag-app)" != "true" \
    || "$(container_image rag-app)" != "${target_image_id}" ]]; then
    status=degraded
  fi
  echo "status=${status}"
  echo "base_revision=${base_revision}"
  echo "target_revision=${target_revision}"
  echo "image_tag=${target_image_tag}"
  echo "reindex_required=${reindex_required}"
}

rollback_update() {
  if [[ ! -e "${state_file}" && ! -L "${state_file}" ]]; then
    if [[ -e "${override_file}" || -L "${override_file}" ]]; then
      fail "app update override 存在但 state 缺失。"
    fi
    echo "status=inactive"
    return 0
  fi
  require_regular_0600 "${state_file}" "app update state"
  IFS=$'\t' read -r state_release state_base_revision target_revision \
    state_base_ref state_base_id target_image_tag target_image_id \
    manifest_digest config_digest state_tier reindex_required \
    override_sha <<< "$(read_state)"
  require_current_release
  require_worker_stopped
  capture_invariants
  if [[ "${current_release}" != "${state_release}" \
    || "${base_source_revision}" != "${state_base_revision}" \
    || "${base_image_ref}" != "${state_base_ref}" \
    || "${base_image_id}" != "${state_base_id}" ]]; then
    fail "current 基础 release 与 app update state 不一致。"
  fi
  if ! restore_base_app; then
    echo "APP_UPDATE_ROLLBACK_FAILED" >&2
    exit 70
  fi
  echo "status=inactive"
}

install -d -m 0700 "${update_root}"
case "${command}" in
  apply) apply_update ;;
  status) show_status ;;
  rollback) rollback_update ;;
  *) fail "用法：app-update.sh apply <update-directory>|status|rollback" ;;
esac
