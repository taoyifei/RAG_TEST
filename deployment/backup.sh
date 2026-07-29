#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root="/data/tyf/RAG"
shared_env_dir="${project_root}/shared/env"
default_env_file="${shared_env_dir}/rag.env"
data_root="${project_root}/data"
state_path="${project_root}/data/state"
qdrant_path="${project_root}/data/qdrant"
backup_root="${project_root}/backups"
releases_dir="${project_root}/releases"
current_link="${project_root}/current"
exit_validation=64
exit_restore=70

if [[ "$#" -gt 2 ]]; then
  echo "用法：backup.sh [backup-id] [shared-env-file]" >&2
  exit "${exit_validation}"
fi
backup_id="${1:-$(date -u +%Y%m%dT%H%M%SZ)}"
env_file="${2:-${default_env_file}}"

fail() {
  echo "$1" >&2
  exit "${exit_validation}"
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

canonical_project_dir() {
  local path="$1"
  local label="$2"
  local resolved
  if [[ ! -d "${path}" || -L "${path}" ]]; then
    fail "${label} 必须是已存在的真实目录：${path}"
  fi
  assert_no_symlink_ancestors "${path}"
  resolved="$(realpath -e "${path}")"
  if [[ "${resolved}" != "${path}" \
    || ("${resolved}" != "${project_root}" \
      && "${resolved}" != "${project_root}/"*) ]]; then
    fail "${label} 不在固定项目根内：${path}"
  fi
}

container_running_state() {
  local container="$1"
  local state
  if state="$(docker container inspect \
    --format '{{.State.Running}}' "${container}" 2>/dev/null)"; then
    if [[ "${state}" != "true" && "${state}" != "false" ]]; then
      echo "容器状态无效：${container}" >&2
      return 1
    fi
    printf '%s\n' "${state}"
  else
    printf 'false\n'
  fi
}

env_optional_value() {
  local key="$1"
  local count
  count="$(awk -F= -v key="${key}" '$1 == key {count += 1} END {
      print count + 0
    }' "${env_file}")"
  if [[ "${count}" -gt 1 ]]; then
    echo "环境文件中的 ${key} 重复。" >&2
    return 1
  fi
  if [[ "${count}" == "1" ]]; then
    awk -F= -v key="${key}" '$1 == key {
        sub(/^[^=]*=/, "")
        print
      }' "${env_file}"
  fi
}

if [[ -z "${backup_id}" \
  || "${backup_id}" == *$'\n'* \
  || "${backup_id}" == "." \
  || "${backup_id}" == *".."* \
  || ! "${backup_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
  fail "backup ID 仅允许安全的 1-64 位标识符，且不能包含 ..。"
fi
if [[ -z "${env_file}" \
  || "${env_file}" == *$'\n'* \
  || "${env_file}" == *".."* \
  || "${env_file}" != /* \
  || ! -f "${env_file}" \
  || -L "${env_file}" ]]; then
  fail "共享环境文件路径无效。"
fi

for directory_and_label in \
  "${project_root}|项目根" \
  "${shared_env_dir}|共享环境目录" \
  "${data_root}|数据目录" \
  "${state_path}|SQLite state 目录" \
  "${qdrant_path}|Qdrant 目录" \
  "${backup_root}|备份目录" \
  "${releases_dir}|release 目录"; do
  canonical_project_dir \
    "${directory_and_label%%|*}" \
    "${directory_and_label##*|}"
done
assert_no_symlink_ancestors "${env_file}"
env_file="$(realpath -e "${env_file}")"
if [[ "$(dirname "${env_file}")" != "${shared_env_dir}" ]]; then
  fail "环境文件必须位于 ${shared_env_dir}。"
fi
if [[ ! -L "${current_link}" ]]; then
  fail "current 必须是 release 符号链接。"
fi
active_release="$(readlink -f "${current_link}")"
if [[ "${active_release}" != "${releases_dir}/"* \
  || "$(dirname "${active_release}")" != "${releases_dir}" ]]; then
  fail "current 指向固定 releases 目录之外。"
fi
canonical_project_dir "${active_release}" "当前 release"
compose_file="${active_release}/compose.yaml"
if [[ ! -f "${compose_file}" || -L "${compose_file}" ]]; then
  fail "当前 release 缺少普通 Compose 文件。"
fi

final_dir="${backup_root}/${backup_id}"
if [[ -e "${final_dir}" || -L "${final_dir}" ]]; then
  fail "backup ID 已存在，拒绝覆盖：${backup_id}"
fi
assert_no_symlink_ancestors "${backup_root}"

caller_uid="${SUDO_UID:-$(id -u)}"
caller_gid="${SUDO_GID:-$(id -g)}"
if [[ ! "${caller_uid}" =~ ^[0-9]+$ \
  || ! "${caller_gid}" =~ ^[0-9]+$ ]]; then
  fail "无法确定原调用用户 UID/GID。"
fi
port="$(env_optional_value RAG_PORT)"
port="${port:-8088}"
if [[ ! "${port}" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
  fail "RAG_PORT 无效。"
fi

app_was_running="$(container_running_state rag-app)"
worker_was_running="$(container_running_state rag-worker)"
qdrant_was_running="$(container_running_state rag-qdrant)"
temporary_dir=""
if ! temporary_dir="$(mktemp -d \
  "${backup_root}/.${backup_id}.incomplete.XXXXXXXX")"; then
  fail "无法创建同目录临时备份。"
fi
chmod 0700 "${temporary_dir}"
final_published=false
restore_required=false

start_service() {
  local service="$1"
  local compose_command=(
    docker compose
    --env-file "${env_file}"
    -f "${compose_file}"
  )
  if [[ "${service}" == "rag-worker" ]]; then
    compose_command+=(--profile index)
  fi
  "${compose_command[@]}" up -d --no-deps \
    --no-build --pull never "${service}"
}

wait_for_qdrant_health() {
  local attempt
  local health_status
  local max_attempts=30
  for ((attempt = 1; attempt <= max_attempts; attempt += 1)); do
    if ! health_status="$(docker container inspect \
      --format '{{.State.Health.Status}}' rag-qdrant 2>/dev/null)"; then
      health_status=""
    fi
    case "${health_status}" in
      healthy)
        return 0
        ;;
      unhealthy)
        echo "恢复后的 rag-qdrant health 状态为 unhealthy。" >&2
        return 1
        ;;
      starting|"")
        if ((attempt < max_attempts)); then
          sleep 1
        fi
        ;;
      *)
        echo "恢复后的 rag-qdrant health 状态无效：${health_status}" >&2
        return 1
        ;;
    esac
  done
  echo "恢复后的 rag-qdrant health 在固定期限内未达到 healthy。" >&2
  return 1
}

verify_original_service_set() {
  local actual
  local expected
  local service
  for service in rag-qdrant rag-app rag-worker; do
    case "${service}" in
      rag-app) expected="${app_was_running}" ;;
      rag-worker) expected="${worker_was_running}" ;;
      rag-qdrant) expected="${qdrant_was_running}" ;;
    esac
    if ! actual="$(container_running_state "${service}")" \
      || [[ "${actual}" != "${expected}" ]]; then
      echo "服务未恢复到备份前状态：${service}" >&2
      return 1
    fi
  done
}

restore_services() {
  if [[ "${qdrant_was_running}" == "true" ]]; then
    if ! start_service rag-qdrant; then
      echo "恢复服务失败：rag-qdrant" >&2
      return 1
    fi
    wait_for_qdrant_health || return 1
  fi
  if [[ "${app_was_running}" == "true" ]]; then
    if ! start_service rag-app; then
      echo "恢复服务失败：rag-app" >&2
      return 1
    fi
    if [[ "$(container_running_state rag-app)" != "true" ]] \
      || ! curl -fsS --max-time 10 \
        "http://127.0.0.1:${port}/live" >/dev/null; then
      echo "恢复后的 rag-app 运行状态或 /live 检查失败。" >&2
      return 1
    fi
  fi
  if [[ "${worker_was_running}" == "true" ]]; then
    if ! start_service rag-worker; then
      echo "恢复服务失败：rag-worker" >&2
      return 1
    fi
    if [[ "$(container_running_state rag-worker)" != "true" ]]; then
      echo "恢复后的 rag-worker 未运行。" >&2
      return 1
    fi
  fi
  verify_original_service_set
}

on_exit() {
  local status="$?"
  local restore_failed=0
  trap - EXIT
  set +e
  if [[ "${restore_required}" == "true" ]] \
    && ! restore_services; then
    restore_failed=1
  fi
  if [[ "${restore_failed}" == "1" ]]; then
    status="${exit_restore}"
    if [[ "${final_published}" == "true" ]]; then
      echo "备份后的服务恢复失败；已验证的正式备份不会删除。" >&2
    else
      echo "备份失败后的服务恢复也失败；没有发布正式备份。" >&2
    fi
  fi
  if [[ "${final_published}" != "true" \
    && -d "${temporary_dir}" ]]; then
    echo "失败的未发布备份保留在：${temporary_dir}" >&2
  fi
  if [[ "${status}" == "0" ]]; then
    echo "备份已验证发布，服务已恢复到备份前运行集合：${final_dir}"
  fi
  exit "${status}"
}
trap on_exit EXIT

restore_required=true
if ! docker compose --profile index \
  --env-file "${env_file}" \
  -f "${compose_file}" \
  stop rag-worker rag-app rag-qdrant; then
  fail "停止写入服务失败。"
fi
for service in rag-app rag-worker rag-qdrant; do
  if [[ "$(container_running_state "${service}")" != "false" ]]; then
    fail "写入服务未完全停止：${service}"
  fi
done

validate_archive() {
  local archive="$1"
  local expected_top_level="$2"
  local detail
  local member
  local normalized
  local parts
  if [[ ! -s "${archive}" ]]; then
    fail "备份归档为空：$(basename "${archive}")"
  fi
  if ! gzip -t "${archive}"; then
    fail "备份归档 gzip 校验失败：$(basename "${archive}")"
  fi
  if ! tar -tzf "${archive}" >/dev/null; then
    fail "备份归档 tar 列表校验失败：$(basename "${archive}")"
  fi
  mapfile -t archive_members < <(tar -tzf "${archive}")
  if [[ "${#archive_members[@]}" == "0" ]]; then
    fail "备份归档没有成员：$(basename "${archive}")"
  fi
  for member in "${archive_members[@]}"; do
    normalized="${member%/}"
    if [[ -z "${normalized}" \
      || "${member}" == /* \
      || ("${normalized}" != "${expected_top_level}" \
        && "${normalized}" != "${expected_top_level}/"*) ]]; then
      fail "备份归档成员越界：$(basename "${archive}")"
    fi
    IFS="/" read -r -a parts <<< "${normalized}"
    for part in "${parts[@]}"; do
      if [[ "${part}" == ".." ]]; then
        fail "备份归档包含 .. 路径。"
      fi
    done
  done
  mapfile -t archive_details < <(tar -tvzf "${archive}")
  for detail in "${archive_details[@]}"; do
    if [[ "${detail:0:1}" != "-" && "${detail:0:1}" != "d" ]]; then
      fail "备份归档包含链接、设备或 FIFO。"
    fi
  done
}

archive_directory() {
  local directory_name="$1"
  local temporary_archive="${temporary_dir}/${directory_name}.tar.gz.tmp"
  local final_archive="${temporary_dir}/${directory_name}.tar.gz"
  if ! sudo tar --format=posix -C "${data_root}" \
    -cf - "${directory_name}" \
    | gzip > "${temporary_archive}"; then
    fail "读取 ${directory_name} 并生成归档失败。"
  fi
  chmod 0600 "${temporary_archive}"
  validate_archive "${temporary_archive}" "${directory_name}"
  mv "${temporary_archive}" "${final_archive}"
}

archive_directory state
archive_directory qdrant
(
  cd "${temporary_dir}"
  sha256sum state.tar.gz qdrant.tar.gz > MANIFEST.sha256.tmp
  chmod 0600 MANIFEST.sha256.tmp
  mv MANIFEST.sha256.tmp MANIFEST.sha256
  sha256sum -c MANIFEST.sha256
)

for output in \
  "${temporary_dir}/state.tar.gz" \
  "${temporary_dir}/qdrant.tar.gz" \
  "${temporary_dir}/MANIFEST.sha256"; do
  chmod 0600 "${output}"
  if [[ "$(stat -c '%u:%g' "${output}")" \
    != "${caller_uid}:${caller_gid}" ]]; then
    chown "${caller_uid}:${caller_gid}" "${output}"
  fi
done
chmod 0700 "${temporary_dir}"
if [[ "$(stat -c '%u:%g' "${temporary_dir}")" \
  != "${caller_uid}:${caller_gid}" ]]; then
  chown "${caller_uid}:${caller_gid}" "${temporary_dir}"
fi
(
  cd "${temporary_dir}"
  sha256sum -c MANIFEST.sha256
)
if ! mv -Tn "${temporary_dir}" "${final_dir}" \
  || [[ -d "${temporary_dir}" ]]; then
  fail "无法原子发布正式备份目录。"
fi
temporary_dir=""
final_published=true
