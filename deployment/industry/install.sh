#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"

[[ "$#" -eq 2 ]] \
  || industry_fail "用法: install.sh /absolute/rag-industry.env /absolute/release-dir"
require_industry_env "$1"
require_release_directory "$2"
env_file="$(realpath "$1")"
release_dir="$(realpath "$2")"
compose_file="$(industry_compose_file "${env_file}")"
[[ "${compose_file}" == "${release_dir}/compose.yaml" ]] \
  || industry_fail "env compose path 必须指向当前 release。"

python3 "${release_dir}/package_selfcheck.py" release "${release_dir}" \
  >/dev/null || industry_fail "PACKAGE_SELFCHECK_FAILED"

mapfile -t image_rows < <(
  python3 - "${release_dir}/RELEASE_MANIFEST.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
for name in ("app", "ocr", "qdrant"):
    image = manifest["images"][name]
    print("\t".join((
        name,
        image["delivery"],
        image["ref"],
        image["id"],
        image["platform"],
        image.get("revision") or "-",
        image.get("archive_name") or "-",
    )))
PY
)
[[ "${#image_rows[@]}" -eq 3 ]] || industry_fail "IMAGE_MANIFEST_INVALID"

for row in "${image_rows[@]}"; do
  IFS=$'\t' read -r name delivery image_ref expected_id \
    expected_platform expected_revision archive <<<"${row}"
  case "${delivery}" in
    archive)
      [[ "${archive}" != "-" ]] || industry_fail "IMAGE_ARCHIVE_MISSING"
      gzip -dc -- "${release_dir}/${archive}" | docker image load \
        || industry_fail "docker load 失败：${archive}"
      ;;
    server-existing)
      [[ "${archive}" == "-" ]] || industry_fail "IMAGE_DELIVERY_INVALID"
      ;;
    *)
      industry_fail "IMAGE_DELIVERY_INVALID"
      ;;
  esac
done

app_image=""
for row in "${image_rows[@]}"; do
  IFS=$'\t' read -r name delivery image_ref expected_id \
    expected_platform expected_revision archive <<<"${row}"
  actual_id="$(docker image inspect --format '{{.Id}}' "${image_ref}")" \
    || industry_fail "镜像 tag 未加载：${image_ref}"
  actual_platform="$(docker image inspect --format \
    '{{.Os}}/{{.Architecture}}' "${image_ref}")"
  [[ "${actual_id}" == "${expected_id}" \
    && "${actual_platform}" == "${expected_platform}" \
    && "${actual_platform}" == "linux/amd64" ]] \
    || industry_fail "镜像 ID 或 platform 与 manifest 不一致。"
  if [[ "${expected_revision}" != "-" ]]; then
    actual_revision="$(docker image inspect --format \
      '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "${image_ref}")"
    [[ "${actual_revision}" == "${expected_revision}" ]] \
      || industry_fail "镜像 revision 与 manifest 不一致。"
  fi
  if [[ "${name}" == "app" ]]; then
    app_image="${image_ref}"
  fi
done
[[ -n "${app_image}" ]] || industry_fail "APP_IMAGE_IDENTITY_MISSING"

revision="$(exact_env_value "${env_file}" RAG_RELEASE_REVISION)" \
  || industry_fail "env 缺少唯一 RAG_RELEASE_REVISION。"
image_revision="$(docker image inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "${app_image}")"
[[ "${image_revision}" == "${revision}" ]] \
  || industry_fail "app image revision 与 env 不一致。"
docker run --rm --network none "${app_image}" \
  build-info --expected-revision "${revision}" >/dev/null \
  || industry_fail "app wheel SOURCE_REVISION 与 release 不一致。"
docker run --rm --network none "${app_image}" asset-selfcheck >/dev/null \
  || industry_fail "app asset-selfcheck 失败。"

docs_path="$(exact_env_value "${env_file}" RAG_DOCS_PATH)"
reference_path="$(exact_env_value "${env_file}" RAG_REFERENCE_PATH)"
config_path="$(exact_env_value "${env_file}" RAG_CONFIG_PATH)"
data_revision_dir="$(dirname "${docs_path}")"
data_parent="$(dirname "${data_revision_dir}")"
[[ "$(dirname "${reference_path}")" == "${data_revision_dir}" \
  && "$(dirname "${config_path}")" == "${data_revision_dir}" \
  && "$(basename "${docs_path}")" == "docs" \
  && "$(basename "${reference_path}")" == "reference" \
  && "$(basename "${config_path}")" == "config" ]] \
  || industry_fail "docs/reference/config 必须属于同一 release data 目录。"
[[ ! -e "${data_revision_dir}" && ! -L "${data_revision_dir}" ]] \
  || industry_fail "release data 已存在，拒绝覆盖。"
mkdir -p -- "${data_parent}"
stage="$(mktemp -d "${data_parent}/.industry-install.XXXXXX")"
trap 'rm -rf --one-file-system -- "${stage}"' EXIT

python3 "${release_dir}/package_selfcheck.py" extract-corpus \
  "${release_dir}/corpus.tar.gz" \
  "${release_dir}/corpus.tar.gz.sha256" \
  "${stage}/corpus" >/dev/null \
  || industry_fail "corpus 安全解包或 manifest 校验失败。"
mv -- "${stage}/corpus/docs" "${stage}/docs"
mv -- "${stage}/corpus/reference" "${stage}/reference"
mv -- "${stage}/corpus/industry-corpus-manifest.json" "${stage}/"
mv -- "${stage}/corpus/industry-corpus-audit.json" "${stage}/"
rmdir -- "${stage}/corpus"
mkdir -- "${stage}/config"
cp -- "${release_dir}/config/"*.json "${stage}/config/"
find "${stage}" -type d -exec chmod 700 {} +
find "${stage}" -type f -exec chmod 600 {} +

python3 "${release_dir}/package_selfcheck.py" publish \
  "${stage}" "${data_revision_dir}" >/dev/null \
  || industry_fail "release data 原子发布失败。"
stage="${data_parent}/.industry-install.already-published"

for key in RAG_STATE_PATH RAG_QDRANT_PATH RAG_LOGS_PATH RAG_BACKUP_PATH; do
  value="$(exact_env_value "${env_file}" "${key}")"
  require_absolute_path "${value}" "${key}"
  [[ ! -L "${value}" ]] || industry_fail "${key} 不能是 symlink。"
  mkdir -p -- "${value}"
done

for writable_path in \
  "${data_revision_dir}" \
  "$(exact_env_value "${env_file}" RAG_STATE_PATH)" \
  "$(exact_env_value "${env_file}" RAG_LOGS_PATH)"; do
  docker run --rm --network none --user 0:0 --entrypoint chown \
    --volume "${writable_path}:/target" \
    "${app_image}" -R 10001:10001 /target \
    || industry_fail "无法设置 app UID 10001 所有权。"
done

printf 'RAG_INDUSTRY_INSTALL_OK release=%s\n' "$(basename "${release_dir}")"
