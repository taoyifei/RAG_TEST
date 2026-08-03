#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=deployment/qdrant-policy.sh
source "${repo_root}/deployment/qdrant-policy.sh"
artifact_root="${repo_root}/artifacts"
release_parent="${artifact_root}/releases"
git_revision="$(git -C "${repo_root}" rev-parse HEAD)"
release_id="${RELEASE_ID:-${git_revision:0:12}}"
release_tier="${RELEASE_TIER:-}"
corpus_manifest_input="${CORPUS_MANIFEST:-}"
pipeline_config="${repo_root}/deployment/config/pipeline.json"
retrieval_config="${repo_root}/deployment/config/retrieval.json"
corpus_policy_config="${repo_root}/deployment/config/corpus-policy.json"
freeze_decision="${repo_root}/deployment/config/FREEZE_DECISION.json"
app_image="${RAG_APP_IMAGE:-docx-rag:${release_id}}"
ocr_image="${RAG_OCR_IMAGE:-docx-rag-ocr:${release_id}}"
qdrant_image="${RAG_QDRANT_IMAGE:-${RAG_APPROVED_QDRANT_SOURCE_IMAGE}}"
qdrant_runtime_image="rag-qdrant:${release_id}"
stage=""

fail() {
  echo "$1" >&2
  exit 1
}

cleanup() {
  if [[ -n "${stage}" && -d "${stage}" \
    && "${stage}" == "${release_parent}"/.* ]]; then
    find -P "${stage}" -depth -delete
  fi
}
trap cleanup EXIT

validate_identifier() {
  local value="$1"
  local label="$2"
  if [[ ! "${value}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    fail "${label} 只允许 1-64 位字母、数字、点、下划线和连字符。"
  fi
}

validate_image() {
  local image="$1"
  local expected_revision="$2"
  local architecture
  local image_id
  local operating_system
  local revision
  architecture="$(docker image inspect --format '{{.Architecture}}' "${image}")"
  operating_system="$(docker image inspect --format '{{.Os}}' "${image}")"
  image_id="$(docker image inspect --format '{{.Id}}' "${image}")"
  if [[ "${architecture}/${operating_system}" != "amd64/linux" ]]; then
    fail "镜像不是 linux/amd64：${image}"
  fi
  if [[ ! "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    fail "镜像 ID 无效：${image}"
  fi
  if [[ "${expected_revision}" != "-" ]]; then
    revision="$(docker image inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "${image}")"
    if [[ "${revision}" != "${expected_revision}" ]]; then
      fail "镜像源码 revision 不一致：${image}"
    fi
  fi
}

write_manifest() {
  local root="$1"
  (
    cd "${root}"
    find . -type f ! -name MANIFEST.sha256 -print0 \
      | sort -z \
      | xargs -0 sha256sum \
      > MANIFEST.sha256
  )
}

write_sidecar() {
  local archive="$1"
  (
    cd "$(dirname "${archive}")"
    sha256sum "$(basename "${archive}")" \
      > "$(basename "${archive}").sha256"
  )
}

if [[ "${release_tier}" != "smoke" \
  && "${release_tier}" != "production" ]]; then
  fail "RELEASE_TIER 必须显式设置为 smoke 或 production。"
fi

if [[ -z "${corpus_manifest_input}" \
  || "${corpus_manifest_input}" != /* \
  || ! -f "${corpus_manifest_input}" \
  || -L "${corpus_manifest_input}" ]]; then
  fail "必须通过 CORPUS_MANIFEST 提供绝对路径的普通 manifest 文件。"
fi
corpus_manifest_input="$(realpath -e "${corpus_manifest_input}")"
for config_path in \
  "${pipeline_config}" \
  "${retrieval_config}" \
  "${corpus_policy_config}"; do
  if [[ ! -f "${config_path}" || ! -s "${config_path}" \
    || -L "${config_path}" ]]; then
    fail "runtime 配置必须是非空普通文件：${config_path}"
  fi
done
configuration_status="$(python3 - "${retrieval_config}" <<'PY'
import json
import sys
from pathlib import Path

try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise SystemExit("retrieval 配置不是有效 UTF-8 JSON。") from error
if type(value) is not dict or value.get("status") not in {
    "provisional",
    "frozen",
}:
    raise SystemExit("retrieval 配置 status 必须是 provisional 或 frozen。")
print(value["status"])
PY
)"
if [[ "${release_tier}" == "production" \
  && "${configuration_status}" != "frozen" ]]; then
  fail "production release 必须使用 frozen retrieval 配置。"
fi
if [[ -e "${freeze_decision}" || -L "${freeze_decision}" ]]; then
  if [[ ! -f "${freeze_decision}" || ! -s "${freeze_decision}" \
    || -L "${freeze_decision}" ]]; then
    fail "FREEZE_DECISION.json 必须是非空普通文件。"
  fi
  include_freeze_decision="true"
else
  include_freeze_decision="false"
fi
if [[ "${configuration_status}" == "frozen" \
  && "${include_freeze_decision}" != "true" ]]; then
  fail "frozen runtime 必须包含 FREEZE_DECISION.json。"
fi
if [[ "${release_tier}" == "production" ]]; then
  (
    cd "${repo_root}/evaluation/frozen"
    sha256sum --check MANIFEST.sha256
  )
fi
corpus_id="$(
  cd "${repo_root}"
  python3 -m scripts.freeze_corpus_manifest \
    id --manifest "${corpus_manifest_input}"
)"
validate_identifier "${release_id}" "RELEASE_ID"
validate_identifier "${corpus_id}" "corpus ID"
if [[ ! "${git_revision}" =~ ^[0-9a-f]{40}$ ]]; then
  fail "Git revision 无效。"
fi
if [[ -n "$(git -C "${repo_root}" status \
  --porcelain --untracked-files=all)" ]]; then
  fail "源码含未提交改动，无法把镜像可靠绑定到 Git revision。"
fi
if [[ "${qdrant_image}" != "${RAG_APPROVED_QDRANT_SOURCE_IMAGE}" ]]; then
  fail "Qdrant 镜像必须使用已批准的固定 digest。"
fi
final_release="${release_parent}/${release_id}-${corpus_id}"
if [[ -e "${final_release}" || -L "${final_release}" ]]; then
  fail "release 输出已存在，拒绝覆盖。"
fi
(
  cd "${repo_root}"
  python3 -m scripts.freeze_corpus_manifest verify \
    --docs "${repo_root}/docs" \
    --manifest "${corpus_manifest_input}"
) >/dev/null

validate_image "${app_image}" "${git_revision}"
validate_image "${ocr_image}" "${git_revision}"
validate_image "${qdrant_image}" "-"
qdrant_repo_digests="$(docker image inspect \
  --format '{{range .RepoDigests}}{{println .}}{{end}}' \
  "${qdrant_image}")"
if ! grep -Fxq -- \
  "${RAG_APPROVED_QDRANT_REPO_DIGEST}" <<< "${qdrant_repo_digests}"; then
  fail "Qdrant RepoDigests 不包含批准的 canonical digest。"
fi
if [[ "${release_tier}" == "production" ]]; then
  docker sbom --help >/dev/null
fi
(
  cd "${repo_root}/deployment/ocr/assets"
  sha256sum --check MANIFEST.sha256
  sha256sum --check ../MODELS.sha256
)

mkdir -p "${release_parent}"
stage="$(mktemp -d \
  "${release_parent}/.${release_id}-${corpus_id}.XXXXXXXX")"
work="${stage}/.work"
runtime_root="${work}/runtime"
corpus_root="${work}/corpus"
runtime_archive="${stage}/rag-runtime-${release_id}.tar.gz"
corpus_archive="${stage}/rag-corpus-${corpus_id}.tar.gz"
unpacker="${stage}/offline_bundle.py"
mkdir -p \
  "${runtime_root}/config" \
  "${runtime_root}/evaluation/runtime/scripts" \
  "${runtime_root}/images" \
  "${runtime_root}/licenses" \
  "${runtime_root}/provenance/ocr" \
  "${runtime_root}/scripts" \
  "${corpus_root}/evaluation"
if [[ "${release_tier}" == "production" ]]; then
  mkdir -p \
    "${runtime_root}/evaluation/runtime/evaluation" \
    "${runtime_root}/sbom"
fi

printf '%s\n' "${release_id}" > "${runtime_root}/RELEASE_ID"
printf '%s\n' "${git_revision}" > "${runtime_root}/SOURCE_REVISION"
printf '%s\n' "${qdrant_image}" > "${runtime_root}/QDRANT_SOURCE_IMAGE"
python3 - "${runtime_root}/RELEASE_METADATA.json" \
  "${release_tier}" "${configuration_status}" "${git_revision}" <<'PY'
import json
import sys
from pathlib import Path

payload = {
    "configuration_status": sys.argv[3],
    "release_tier": sys.argv[2],
    "schema_version": "1",
    "source_revision": sys.argv[4],
}
Path(sys.argv[1]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
cp "${repo_root}/deployment/compose.yaml" "${runtime_root}/compose.yaml"
cp "${repo_root}/deployment/.env.example" "${runtime_root}/.env.example"
cp "${repo_root}/deployment/deploy.sh" "${runtime_root}/deploy.sh"
cp "${repo_root}/deployment/rollback.sh" "${runtime_root}/rollback.sh"
cp "${repo_root}/deployment/backup.sh" "${runtime_root}/backup.sh"
cp "${repo_root}/deployment/bootstrap.sh" "${runtime_root}/bootstrap.sh"
chmod 0700 "${runtime_root}/bootstrap.sh"
cp "${repo_root}/deployment/install.sh" "${runtime_root}/install.sh"
cp "${repo_root}/deployment/server-preflight.sh" \
  "${runtime_root}/server-preflight.sh"
chmod 0700 "${runtime_root}/server-preflight.sh"
cp "${repo_root}/deployment/verify-offline.sh" \
  "${runtime_root}/verify-offline.sh"
cp "${repo_root}/deployment/qdrant-policy.sh" \
  "${runtime_root}/qdrant-policy.sh"
cp "${pipeline_config}" "${runtime_root}/config/pipeline.json"
cp "${retrieval_config}" "${runtime_root}/config/retrieval.json"
cp "${corpus_policy_config}" \
  "${runtime_root}/config/corpus-policy.json"
if [[ "${include_freeze_decision}" == "true" ]]; then
  cp "${freeze_decision}" \
    "${runtime_root}/config/FREEZE_DECISION.json"
fi
cp "${repo_root}/deployment/README.md" \
  "${runtime_root}/README.md"
cp "${repo_root}/scripts/offline_bundle.py" \
  "${runtime_root}/offline_bundle.py"
cp "${repo_root}/scripts/freeze_corpus_manifest.py" \
  "${runtime_root}/freeze_corpus_manifest.py"
cp "${repo_root}/scripts/docker_archive_identity.py" \
  "${runtime_root}/scripts/docker_archive_identity.py"
cp "${repo_root}/scripts/docker_archive_reader.py" \
  "${runtime_root}/scripts/docker_archive_reader.py"
cp "${repo_root}/scripts/docker_archive_loaded_identity.py" \
  "${runtime_root}/scripts/docker_archive_loaded_identity.py"
cp "${repo_root}/scripts/build_model_deployment_manifest.py" \
  "${runtime_root}/evaluation/runtime/scripts/"
cp "${repo_root}/scripts/verify_model_contracts.py" \
  "${runtime_root}/evaluation/runtime/scripts/"
if [[ "${release_tier}" == "production" ]]; then
  cp "${repo_root}/deployment/acceptance.sh" \
    "${runtime_root}/acceptance.sh"
  chmod 0700 "${runtime_root}/acceptance.sh"
  cp "${repo_root}/evaluation/"*.py \
    "${runtime_root}/evaluation/runtime/evaluation/"
  cp "${repo_root}/scripts/load_test_chat.py" \
    "${runtime_root}/evaluation/runtime/scripts/"
  cp "${repo_root}/scripts/benchmark_qdrant.py" \
    "${runtime_root}/evaluation/runtime/scripts/"
  cp "${repo_root}/scripts/verify_model_fleet.py" \
    "${runtime_root}/evaluation/runtime/scripts/"
fi
cp "${repo_root}/deployment/ocr/assets/licenses/PaddleOCR-LICENSE.txt" \
  "${runtime_root}/licenses/"
cp "${repo_root}/deployment/ocr/THIRD_PARTY_NOTICES.md" \
  "${runtime_root}/licenses/"
for name in \
  ASSET_SOURCES.json \
  BASE_RUNTIME.json \
  WHEELS.sha256 \
  MODELS.sha256 \
  requirements.lock \
  pipeline.yaml; do
  cp "${repo_root}/deployment/ocr/${name}" \
    "${runtime_root}/provenance/ocr/${name}"
done

docker image tag "${qdrant_image}" "${qdrant_runtime_image}"
docker run --rm --network none --entrypoint cat "${ocr_image}" \
  /opt/rag-ocr/licenses/NVIDIA-CONTAINER-LICENSE \
  > "${runtime_root}/licenses/NVIDIA-CONTAINER-LICENSE"
docker image save --platform linux/amd64 \
  --output "${runtime_root}/images/docx-rag-linux-amd64.tar" \
  "${app_image}"
docker image save --platform linux/amd64 \
  --output "${runtime_root}/images/docx-rag-ocr-linux-amd64.tar" \
  "${ocr_image}"
docker image save --platform linux/amd64 \
  --output "${runtime_root}/images/qdrant-linux-amd64.tar" \
  "${qdrant_runtime_image}"

inspect_archive() {
  local archive="$1"
  local image="$2"
  local expected_revision="$3"
  local arguments=(
    python3 -m scripts.docker_archive_identity
    "${archive}"
    --tag "${image}"
    --platform linux/amd64
  )
  if [[ "${expected_revision}" != "-" ]]; then
    arguments+=(--expected-revision "${expected_revision}")
  fi
  (
    cd "${repo_root}"
    "${arguments[@]}"
  )
}

app_identity="$(inspect_archive \
  "${runtime_root}/images/docx-rag-linux-amd64.tar" \
  "${app_image}" "${git_revision}")"
ocr_identity="$(inspect_archive \
  "${runtime_root}/images/docx-rag-ocr-linux-amd64.tar" \
  "${ocr_image}" "${git_revision}")"
qdrant_identity="$(inspect_archive \
  "${runtime_root}/images/qdrant-linux-amd64.tar" \
  "${qdrant_runtime_image}" -)"
IFS=$'\t' read -r app_manifest_id app_config_id app_platform \
  <<< "${app_identity}"
IFS=$'\t' read -r ocr_manifest_id ocr_config_id ocr_platform \
  <<< "${ocr_identity}"
IFS=$'\t' read -r qdrant_manifest_id qdrant_config_id qdrant_platform \
  <<< "${qdrant_identity}"

printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
  'images/docx-rag-linux-amd64.tar' \
  "${app_image}" "${app_manifest_id}" "${git_revision}" \
  "${app_config_id}" "${app_platform}" \
  'images/docx-rag-ocr-linux-amd64.tar' \
  "${ocr_image}" "${ocr_manifest_id}" "${git_revision}" \
  "${ocr_config_id}" "${ocr_platform}" \
  'images/qdrant-linux-amd64.tar' \
  "${qdrant_runtime_image}" "${qdrant_manifest_id}" \
  "${RAG_APPROVED_QDRANT_REPO_DIGEST}" \
  "${qdrant_config_id}" "${qdrant_platform}" \
  > "${runtime_root}/IMAGE_ARCHIVES.tsv"

if [[ "${release_tier}" == "production" ]]; then
  docker sbom --format cyclonedx-json "${app_image}" \
    > "${runtime_root}/sbom/docx-rag.cdx.json"
  docker sbom --format cyclonedx-json "${ocr_image}" \
    > "${runtime_root}/sbom/docx-rag-ocr.cdx.json"
  docker sbom --format cyclonedx-json "${qdrant_image}" \
    > "${runtime_root}/sbom/qdrant.cdx.json"
fi
docker image inspect "${app_image}" \
  > "${runtime_root}/images/docx-rag.inspect.json"
docker image inspect "${ocr_image}" \
  > "${runtime_root}/images/docx-rag-ocr.inspect.json"
docker image inspect "${qdrant_image}" \
  > "${runtime_root}/images/qdrant.inspect.json"

printf '%s\n' "${corpus_id}" > "${corpus_root}/CORPUS_ID"
cp "${corpus_manifest_input}" "${corpus_root}/CORPUS_MANIFEST.json"
(
  cd "${repo_root}"
  python3 -m scripts.freeze_corpus_manifest stage \
    --docs "${repo_root}/docs" \
    --manifest "${corpus_manifest_input}" \
    --destination "${corpus_root}/docs"
) >/dev/null
if [[ "${release_tier}" == "production" ]]; then
  cp "${repo_root}/evaluation/frozen/dataset.json" \
    "${corpus_root}/evaluation/dataset.json"
  cp "${repo_root}/evaluation/frozen/MANIFEST.sha256" \
    "${corpus_root}/evaluation/FROZEN_MANIFEST.sha256"
fi

write_manifest "${runtime_root}"
write_manifest "${corpus_root}"
(
  cd "${runtime_root}"
  sha256sum --check MANIFEST.sha256
)
(
  cd "${corpus_root}"
  sha256sum --check MANIFEST.sha256
)
tar --format=posix -C "${work}" -czf "${runtime_archive}" runtime
tar --format=posix -C "${work}" -czf "${corpus_archive}" corpus
write_sidecar "${runtime_archive}"
write_sidecar "${corpus_archive}"
cp "${repo_root}/scripts/offline_bundle.py" "${unpacker}"
write_sidecar "${unpacker}"
(
  cd "${stage}"
  sha256sum --check "$(basename "${runtime_archive}").sha256"
  sha256sum --check "$(basename "${corpus_archive}").sha256"
  sha256sum --check "$(basename "${unpacker}").sha256"
)
mkdir -p "${work}/verified-runtime" "${work}/verified-corpus"
(
  cd "${repo_root}"
  python3 -m scripts.offline_bundle \
    "${runtime_archive}" "${runtime_archive}.sha256" \
    "${work}/verified-runtime" --top-level runtime
  python3 -m scripts.offline_bundle \
    "${corpus_archive}" "${corpus_archive}.sha256" \
    "${work}/verified-corpus" --top-level corpus
) >/dev/null
find -P "${work}" -depth -delete
(
  cd "${stage}"
  find . -maxdepth 1 -type f ! -name RELEASE_MANIFEST.sha256 -print0 \
    | sort -z \
    | xargs -0 sha256sum \
    > RELEASE_MANIFEST.sha256
  sha256sum --check RELEASE_MANIFEST.sha256
)
(
  cd "${repo_root}"
  python3 -m scripts.offline_bundle \
    publish "${stage}" "${final_release}"
)
stage=""

printf 'release_tier=%s\nrelease_dir=%s\nruntime=%s\ncorpus=%s\nunpacker=%s\n' \
  "${release_tier}" \
  "${final_release}" \
  "${final_release}/$(basename "${runtime_archive}")" \
  "${final_release}/$(basename "${corpus_archive}")" \
  "${final_release}/$(basename "${unpacker}")"
