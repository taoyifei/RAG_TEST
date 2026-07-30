#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
artifact_root="${repo_root}/artifacts"
release_parent="${artifact_root}/releases"
git_revision="$(git -C "${repo_root}" rev-parse HEAD)"
release_id="${RELEASE_ID:-${git_revision:0:12}}"
corpus_manifest_input="${CORPUS_MANIFEST:-}"
app_image="${RAG_APP_IMAGE:-docx-rag:${release_id}}"
ocr_image="${RAG_OCR_IMAGE:-docx-rag-ocr:${release_id}}"
approved_qdrant_image="qdrant/qdrant:v1.18.3@sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286"
approved_qdrant_repo_digest="qdrant/qdrant@sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286"
qdrant_image="${RAG_QDRANT_IMAGE:-${approved_qdrant_image}}"
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

if [[ -z "${corpus_manifest_input}" \
  || "${corpus_manifest_input}" != /* \
  || ! -f "${corpus_manifest_input}" \
  || -L "${corpus_manifest_input}" ]]; then
  fail "必须通过 CORPUS_MANIFEST 提供绝对路径的普通 manifest 文件。"
fi
corpus_manifest_input="$(realpath -e "${corpus_manifest_input}")"
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
if [[ "${qdrant_image}" != "${approved_qdrant_image}" ]]; then
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
app_image_id="$(docker image inspect --format '{{.Id}}' "${app_image}")"
ocr_image_id="$(docker image inspect --format '{{.Id}}' "${ocr_image}")"
qdrant_image_id="$(docker image inspect --format '{{.Id}}' "${qdrant_image}")"
qdrant_repo_digests="$(docker image inspect \
  --format '{{range .RepoDigests}}{{println .}}{{end}}' \
  "${qdrant_image}")"
if ! grep -Fxq -- \
  "${approved_qdrant_repo_digest}" <<< "${qdrant_repo_digests}"; then
  fail "Qdrant RepoDigests 不包含批准的 canonical digest。"
fi
docker sbom --help >/dev/null
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
  "${runtime_root}/evaluation/runtime/evaluation" \
  "${runtime_root}/evaluation/runtime/scripts" \
  "${runtime_root}/images" \
  "${runtime_root}/licenses" \
  "${runtime_root}/provenance/ocr" \
  "${runtime_root}/sbom" \
  "${corpus_root}/evaluation"

printf '%s\n' "${release_id}" > "${runtime_root}/RELEASE_ID"
printf '%s\n' "${git_revision}" > "${runtime_root}/SOURCE_REVISION"
printf '%s\n' "${qdrant_image}" > "${runtime_root}/QDRANT_SOURCE_IMAGE"
cp "${repo_root}/deployment/compose.yaml" "${runtime_root}/compose.yaml"
cp "${repo_root}/deployment/.env.example" "${runtime_root}/.env.example"
cp "${repo_root}/deployment/deploy.sh" "${runtime_root}/deploy.sh"
cp "${repo_root}/deployment/rollback.sh" "${runtime_root}/rollback.sh"
cp "${repo_root}/deployment/backup.sh" "${runtime_root}/backup.sh"
cp "${repo_root}/deployment/install.sh" "${runtime_root}/install.sh"
cp "${repo_root}/deployment/verify-offline.sh" \
  "${runtime_root}/verify-offline.sh"
cp "${repo_root}/design/public/offline-build-and-server-deployment.md" \
  "${runtime_root}/README.md"
cp "${repo_root}/scripts/offline_bundle.py" \
  "${runtime_root}/offline_bundle.py"
cp "${repo_root}/evaluation/"*.py \
  "${runtime_root}/evaluation/runtime/evaluation/"
cp "${repo_root}/scripts/load_test_chat.py" \
  "${runtime_root}/evaluation/runtime/scripts/"
cp "${repo_root}/scripts/benchmark_qdrant.py" \
  "${runtime_root}/evaluation/runtime/scripts/"
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
printf '%s\t%s\t%s\t%s\n' \
  'images/docx-rag-linux-amd64.tar' \
  "${app_image}" "${app_image_id}" "${git_revision}" \
  'images/docx-rag-ocr-linux-amd64.tar' \
  "${ocr_image}" "${ocr_image_id}" "${git_revision}" \
  'images/qdrant-linux-amd64.tar' \
  "${qdrant_runtime_image}" "${qdrant_image_id}" \
  "${approved_qdrant_repo_digest}" \
  > "${runtime_root}/IMAGE_ARCHIVES.tsv"

docker sbom --format cyclonedx-json "${app_image}" \
  > "${runtime_root}/sbom/docx-rag.cdx.json"
docker sbom --format cyclonedx-json "${ocr_image}" \
  > "${runtime_root}/sbom/docx-rag-ocr.cdx.json"
docker sbom --format cyclonedx-json "${qdrant_image}" \
  > "${runtime_root}/sbom/qdrant.cdx.json"
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
cp "${repo_root}/evaluation/frozen/dataset.json" \
  "${corpus_root}/evaluation/dataset.json"
cp "${repo_root}/evaluation/frozen/MANIFEST.sha256" \
  "${corpus_root}/evaluation/FROZEN_MANIFEST.sha256"

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

printf 'release_dir=%s\nruntime=%s\ncorpus=%s\nunpacker=%s\n' \
  "${final_release}" \
  "${final_release}/$(basename "${runtime_archive}")" \
  "${final_release}/$(basename "${corpus_archive}")" \
  "${final_release}/$(basename "${unpacker}")"
