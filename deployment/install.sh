#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root="/data/tyf/RAG"
runtime_source="${1:-}"
corpus_source="${2:-}"
releases_dir="${project_root}/releases"
corpora_dir="${project_root}/shared/corpora"
shared_env="${project_root}/shared/env/rag.env"
release_stage=""
corpus_stage=""
corpus_published=false
install_lock="${project_root}/.install.lock"
lock_acquired=false

fail() {
  echo "$1" >&2
  exit 1
}

cleanup_directory() {
  local path="$1"
  local parent="$2"
  if [[ -n "${path}" && -d "${path}" && "${path}" == "${parent}"/.* ]]; then
    find -P "${path}" -type d -exec chmod u+w {} +
    find -P "${path}" -depth -delete
  fi
}

cleanup() {
  cleanup_directory "${release_stage}" "${releases_dir}"
  cleanup_directory "${corpus_stage}" "${corpora_dir}"
  if [[ "${corpus_published}" == "true" \
    && -n "${corpus_target:-}" \
    && "${corpus_target}" == "${corpora_dir}/"* \
    && -d "${corpus_target}" ]]; then
    find -P "${corpus_target}" -depth -delete
  fi
  if [[ "${lock_acquired}" == "true" && -d "${install_lock}" ]]; then
    rmdir "${install_lock}"
  fi
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

require_real_directory() {
  local path="$1"
  local label="$2"
  if [[ ! -d "${path}" || -L "${path}" ]]; then
    fail "${label} 必须是真实目录。"
  fi
  assert_no_symlink_ancestors "${path}"
  if find -P "${path}" -type l -print -quit | grep -q .; then
    fail "${label} 不能含符号链接。"
  fi
}

require_same_file_set() {
  local left="$1"
  local right="$2"
  if ! cmp -s \
    <(cd "${left}" && find -P . -type f -printf '%P\0' | LC_ALL=C sort -z) \
    <(cd "${right}" && find -P . -type f -printf '%P\0' | LC_ALL=C sort -z); then
    fail "复用目标与输入的文件集合不一致。"
  fi
}

verify_runtime_directory() {
  local directory="$1"
  require_real_directory "${directory}" "runtime"
  if [[ ! -f "${directory}/SOURCE_REVISION" \
    || -L "${directory}/SOURCE_REVISION" \
    || ! "$(cat "${directory}/SOURCE_REVISION")" =~ ^[0-9a-f]{40}$ ]]; then
    fail "runtime SOURCE_REVISION 无效。"
  fi
  bash "${directory}/verify-offline.sh"
}

verify_release_permissions() {
  local directory="$1"
  if find -P "${directory}" ! -type d ! -type f \
    -print -quit | grep -q .; then
    fail "release 只能包含目录和普通文件。"
  fi
  if find -P "${directory}" -type d \
    \( ! -perm 0555 -o ! -uid 0 -o ! -gid 0 \) \
    -print -quit | grep -q .; then
    fail "release owner 或权限无效：目录必须为 root:root/0555。"
  fi
  if find -P "${directory}" -type f -name '*.sh' \
    \( ! -perm 0555 -o ! -uid 0 -o ! -gid 0 \) \
    -print -quit | grep -q .; then
    fail "release owner 或权限无效：Shell 必须为 root:root/0555。"
  fi
  if find -P "${directory}" -type f ! -name '*.sh' \
    \( ! -perm 0444 -o ! -uid 0 -o ! -gid 0 \) \
    -print -quit | grep -q .; then
    fail "release owner 或权限无效：普通文件必须为 root:root/0444。"
  fi
}

verify_corpus_directory() {
  local directory="$1"
  local validator="$2"
  local manifest_id
  require_real_directory "${directory}" "corpus"
  (
    cd "${directory}"
    sha256sum --check MANIFEST.sha256
  )
  manifest_id="$(
    python3 "${validator}" id \
      --manifest "${directory}/CORPUS_MANIFEST.json"
  )"
  if [[ "${manifest_id}" != "$(cat "${directory}/CORPUS_ID")" ]]; then
    fail "CORPUS_ID 与 CORPUS_MANIFEST 不一致。"
  fi
  python3 "${validator}" verify \
    --docs "${directory}/docs" \
    --manifest "${directory}/CORPUS_MANIFEST.json" \
    >/dev/null
}

verify_runtime_reuse() {
  local source="$1"
  local target="$2"
  verify_runtime_directory "${target}"
  if ! cmp -s "${source}/SOURCE_REVISION" "${target}/SOURCE_REVISION" \
    || ! cmp -s "${source}/MANIFEST.sha256" \
      "${target}/MANIFEST.sha256"; then
    fail "既有 release 身份或 MANIFEST 与输入不一致。"
  fi
  require_same_file_set "${source}" "${target}"
  verify_release_permissions "${target}"
}

verify_corpus_permissions() {
  local directory="$1"
  if find -P "${directory}" -type d \
    \( ! -perm 0700 -o ! -uid 10001 -o ! -gid 10001 \) \
    -print -quit | grep -q .; then
    fail "corpus 目录 owner 或 0700 权限无效。"
  fi
  if find -P "${directory}" -type f \
    \( ! -perm 0400 -o ! -uid 10001 -o ! -gid 10001 \) \
    -print -quit | grep -q .; then
    fail "corpus 文件 owner 或 0400 权限无效。"
  fi
}

verify_corpus_reuse() {
  local source="$1"
  local target="$2"
  local validator="$3"
  verify_corpus_directory "${target}" "${validator}"
  if ! cmp -s "${source}/MANIFEST.sha256" \
      "${target}/MANIFEST.sha256" \
    || ! cmp -s "${source}/CORPUS_MANIFEST.json" \
      "${target}/CORPUS_MANIFEST.json"; then
    fail "既有 corpus MANIFEST 与输入不一致。"
  fi
  require_same_file_set "${source}" "${target}"
  verify_corpus_permissions "${target}"
}

if [[ "$(/usr/bin/id -u)" -ne 0 ]]; then
  fail "install.sh 必须由 root 执行。"
fi
if [[ "$#" -ne 2 || "${runtime_source}" != /* \
  || "${corpus_source}" != /* ]]; then
  fail "用法：install.sh <安全解出的 runtime 绝对路径> <corpus 绝对路径>"
fi
require_real_directory "${runtime_source}" "runtime 输入"
require_real_directory "${corpus_source}" "corpus 输入"
runtime_source="$(realpath -e "${runtime_source}")"
corpus_source="$(realpath -e "${corpus_source}")"
if find -P "${runtime_source}" -type f \
  \( -name '.env' -o -name 'rag.env' \
    -o \( -name '*.env' ! -name '.env.example' \) \) \
  -print -quit | grep -q .; then
  fail "release 输入不能包含 secret env。"
fi
verify_runtime_directory "${runtime_source}"
corpus_validator="${runtime_source}/freeze_corpus_manifest.py"
if [[ ! -s "${corpus_validator}" || -L "${corpus_validator}" ]]; then
  fail "runtime 缺少 corpus manifest verifier。"
fi
verify_corpus_directory "${corpus_source}" "${corpus_validator}"
release_id="$(cat "${runtime_source}/RELEASE_ID")"
corpus_id="$(cat "${corpus_source}/CORPUS_ID")"
for value in "${release_id}" "${corpus_id}"; do
  if [[ ! "${value}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    fail "release 或 corpus ID 无效。"
  fi
done

for directory in \
  "${project_root}" \
  "${releases_dir}" \
  "${corpora_dir}" \
  "${project_root}/shared/env"; do
  require_real_directory "${directory}" "安装父目录"
done
if [[ -e "${shared_env}" || -L "${shared_env}" ]]; then
  if [[ ! -f "${shared_env}" || -L "${shared_env}" \
    || "$(stat -c '%a' "${shared_env}")" != "600" ]]; then
    fail "shared/env/rag.env 必须是 release 外的 0600 普通文件。"
  fi
fi
release_target="${releases_dir}/${release_id}"
corpus_target="${corpora_dir}/${corpus_id}"
if ! mkdir "${install_lock}"; then
  fail "另一个安装事务正在运行。"
fi
lock_acquired=true

release_reused=false
corpus_reused=false
if [[ -e "${release_target}" || -L "${release_target}" ]]; then
  require_real_directory "${release_target}" "既有 release"
  verify_runtime_reuse "${runtime_source}" "${release_target}"
  release_reused=true
fi
if [[ -e "${corpus_target}" || -L "${corpus_target}" ]]; then
  require_real_directory "${corpus_target}" "既有 corpus"
  verify_corpus_reuse \
    "${corpus_source}" "${corpus_target}" "${corpus_validator}"
  corpus_reused=true
fi

if [[ "${release_reused}" != "true" ]]; then
  release_stage="$(
    mktemp -d "${releases_dir}/.${release_id}.install.XXXXXXXX"
  )"
  cp -a "${runtime_source}/." "${release_stage}/"
  verify_runtime_directory "${release_stage}"
  require_same_file_set "${runtime_source}" "${release_stage}"
  if find -P "${release_stage}" -type f \
    \( -name '.env' -o -name 'rag.env' \
      -o \( -name '*.env' ! -name '.env.example' \) \) \
    -print -quit | grep -q .; then
    fail "安装 staging 不能包含 secret env。"
  fi
  chown -R root:root "${release_stage}"
  find -P "${release_stage}" -type d -exec chmod 0555 {} +
  find -P "${release_stage}" -type f -exec chmod 0444 {} +
  find -P "${release_stage}" -type f -name '*.sh' -exec chmod 0555 {} +
  verify_release_permissions "${release_stage}"
fi

if [[ "${corpus_reused}" != "true" ]]; then
  corpus_stage="$(
    mktemp -d "${corpora_dir}/.${corpus_id}.install.XXXXXXXX"
  )"
  cp -a "${corpus_source}/." "${corpus_stage}/"
  verify_corpus_directory "${corpus_stage}" "${corpus_validator}"
  require_same_file_set "${corpus_source}" "${corpus_stage}"
  chown -R 10001:10001 "${corpus_stage}"
  find -P "${corpus_stage}" -type d -exec chmod 0700 {} +
  find -P "${corpus_stage}" -type f -exec chmod 0400 {} +
  verify_corpus_permissions "${corpus_stage}"
fi

publisher="${runtime_source}/offline_bundle.py"
if [[ "${release_reused}" == "true" ]]; then
  publisher="${release_target}/offline_bundle.py"
fi
if [[ "${corpus_reused}" != "true" ]]; then
  python3 "${publisher}" publish "${corpus_stage}" "${corpus_target}"
  corpus_stage=""
  corpus_published=true
fi
if [[ "${release_reused}" != "true" ]]; then
  python3 "${publisher}" publish "${release_stage}" "${release_target}"
  release_stage=""
fi
corpus_published=false

echo "installed_release=${release_target}"
echo "installed_corpus=${corpus_target}"
