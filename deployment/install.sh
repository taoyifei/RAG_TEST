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
  if [[ -d "${install_lock}" ]]; then
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
bash "${runtime_source}/verify-offline.sh"
(
  cd "${corpus_source}"
  sha256sum --check MANIFEST.sha256
)
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
if [[ -e "${release_target}" || -L "${release_target}" \
  || -e "${corpus_target}" || -L "${corpus_target}" ]]; then
  fail "目标 release 或 corpus 已存在，拒绝覆盖。"
fi
if ! mkdir "${install_lock}"; then
  fail "另一个安装事务正在运行。"
fi

release_stage="$(mktemp -d "${releases_dir}/.${release_id}.install.XXXXXXXX")"
corpus_stage="$(mktemp -d "${corpora_dir}/.${corpus_id}.install.XXXXXXXX")"
cp -a "${runtime_source}/." "${release_stage}/"
cp -a "${corpus_source}/." "${corpus_stage}/"
find -P "${release_stage}" -type d -exec chmod 0555 {} +
find -P "${release_stage}" -type f -exec chmod 0444 {} +
find -P "${release_stage}" -type f -name '*.sh' -exec chmod 0555 {} +
find -P "${corpus_stage}" -type d -exec chmod 0700 {} +
find -P "${corpus_stage}" -type f -exec chmod 0400 {} +
if find -P "${release_stage}" -type f \
  \( -name '.env' -o -name 'rag.env' \
    -o \( -name '*.env' ! -name '.env.example' \) \) \
  -print -quit | grep -q .; then
  fail "安装 staging 不能包含 secret env。"
fi
if [[ "$(stat -c '%a' "${release_stage}")" != "555" \
  || "$(stat -c '%a' "${release_stage}/deploy.sh")" != "555" \
  || "$(stat -c '%a' "${release_stage}/compose.yaml")" != "444" ]]; then
  fail "release 不可变权限复核失败。"
fi
python3 "${release_stage}/offline_bundle.py" publish \
  "${corpus_stage}" "${corpus_target}"
corpus_stage=""
corpus_published=true
python3 "${release_stage}/offline_bundle.py" publish \
  "${release_stage}" "${release_target}"
release_stage=""
corpus_published=false

echo "installed_release=${release_target}"
echo "installed_corpus=${corpus_target}"
