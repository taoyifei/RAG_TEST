#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
artifact_root="${repo_root}/artifacts"
git_revision="$(cd "${repo_root}" && git rev-parse HEAD)"
release_id="${RELEASE_ID:-${git_revision:0:12}}"
corpus_id="${CORPUS_ID:-frozen-docx-v1}"
app_image="${RAG_APP_IMAGE:-docx-rag:${release_id}}"
ocr_image="${RAG_OCR_IMAGE:-docx-rag-ocr:${release_id}}"
approved_qdrant_image="qdrant/qdrant:v1.18.3@sha256:0bd98fa7977f1e75694779359ca4e212822e5a71334e28421182f72f209d5286"
qdrant_image="${RAG_QDRANT_IMAGE:-${approved_qdrant_image}}"
qdrant_runtime_image="rag-qdrant:${release_id}"
runtime_archive="${artifact_root}/rag-runtime-${release_id}.tar.gz"
corpus_archive="${artifact_root}/rag-corpus-${corpus_id}.tar.gz"
runtime_sidecar="${artifact_root}/rag-runtime-${release_id}.tar.gz.sha256"
corpus_sidecar="${artifact_root}/rag-corpus-${corpus_id}.tar.gz.sha256"

validate_identifier() {
  local value="$1"
  local label="$2"
  if [[ ! "${value}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    echo "${label} 仅允许 1-64 位字母、数字、点、下划线和连字符。" >&2
    exit 1
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
    echo "镜像不是 linux/amd64：${image}" >&2
    exit 1
  fi
  if [[ ! "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "镜像 ID 无效：${image}" >&2
    exit 1
  fi
  if [[ "${expected_revision}" != "-" ]]; then
    revision="$(docker image inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "${image}")"
    if [[ "${revision}" != "${expected_revision}" ]]; then
      echo "镜像源码 revision 不一致：${image}" >&2
      exit 1
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

validate_identifier "${release_id}" "RELEASE_ID"
validate_identifier "${corpus_id}" "CORPUS_ID"
if [[ "${qdrant_image}" != "${approved_qdrant_image}" ]]; then
  echo "Qdrant 镜像必须使用已批准的固定 digest。" >&2
  exit 1
fi
if [[ ! "${git_revision}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Git revision 无效。" >&2
  exit 1
fi
if [[ -n "$(git -C "${repo_root}" status --porcelain)" ]]; then
  echo "源码含未提交改动，无法把镜像可靠绑定到 Git revision。" >&2
  exit 1
fi
if [[ -e "${runtime_archive}" || -e "${runtime_sidecar}" \
  || -e "${corpus_archive}" || -e "${corpus_sidecar}" ]]; then
  echo "双包输出已存在，拒绝覆盖。" >&2
  exit 1
fi

validate_image "${app_image}" "${git_revision}"
validate_image "${ocr_image}" "${git_revision}"
validate_image "${qdrant_image}" "-"
qdrant_image_id="$(docker image inspect --format '{{.Id}}' "${qdrant_image}")"
if [[ "${qdrant_image_id}" != "${qdrant_image##*@}" ]]; then
  echo "Qdrant 镜像 ID 与固定 digest 不一致。" >&2
  exit 1
fi
docker image tag "${qdrant_image}" "${qdrant_runtime_image}"
(
  cd "${repo_root}/deployment/ocr/assets"
  sha256sum --check MANIFEST.sha256
)
(
  cd "${repo_root}/deployment/ocr/assets"
  sha256sum --check ../MODELS.sha256
)

mkdir -p "${artifact_root}"
stage="$(mktemp -d "${artifact_root}/.package-XXXXXXXX")"
trap 'rm -rf -- "${stage}"' EXIT
runtime_root="${stage}/runtime"
corpus_root="${stage}/corpus"
mkdir -p \
  "${runtime_root}/evaluation/runtime/evaluation" \
  "${runtime_root}/evaluation/runtime/scripts" \
  "${runtime_root}/images" \
  "${runtime_root}/licenses" \
  "${runtime_root}/provenance/ocr" \
  "${runtime_root}/sbom" \
  "${corpus_root}/docs" \
  "${corpus_root}/evaluation"

printf '%s\n' "${release_id}" > "${runtime_root}/RELEASE_ID"
printf '%s\n' "${git_revision}" > "${runtime_root}/SOURCE_REVISION"
printf '%s\n' "${qdrant_image}" > "${runtime_root}/QDRANT_SOURCE_IMAGE"
cp "${repo_root}/deployment/compose.yaml" "${runtime_root}/compose.yaml"
cp "${repo_root}/deployment/.env.example" "${runtime_root}/.env.example"
cp "${repo_root}/deployment/deploy.sh" "${runtime_root}/deploy.sh"
cp "${repo_root}/deployment/rollback.sh" "${runtime_root}/rollback.sh"
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
cp "${repo_root}/deployment/ocr/ASSET_SOURCES.json" \
  "${runtime_root}/provenance/ocr/"
cp "${repo_root}/deployment/ocr/BASE_RUNTIME.json" \
  "${runtime_root}/provenance/ocr/"
cp "${repo_root}/deployment/ocr/WHEELS.sha256" \
  "${runtime_root}/provenance/ocr/"
cp "${repo_root}/deployment/ocr/MODELS.sha256" \
  "${runtime_root}/provenance/ocr/"
cp "${repo_root}/deployment/ocr/requirements.lock" \
  "${runtime_root}/provenance/ocr/"
cp "${repo_root}/deployment/ocr/pipeline.yaml" \
  "${runtime_root}/provenance/ocr/"

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
printf '%s\t%s\t%s\n' \
  'images/docx-rag-linux-amd64.tar' "${app_image}" "${git_revision}" \
  'images/docx-rag-ocr-linux-amd64.tar' "${ocr_image}" "${git_revision}" \
  'images/qdrant-linux-amd64.tar' \
  "${qdrant_runtime_image}" "${qdrant_image_id}" \
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
mapfile -d '' docx_files < <(
  find "${repo_root}/docs" -type f -name '*.docx' -print0 | sort -z
)
if [[ "${#docx_files[@]}" -ne 6 ]]; then
  echo "冻结输入不是 6 个 DOCX，拒绝打包。" >&2
  exit 1
fi
for source in "${docx_files[@]}"; do
  relative="${source#"${repo_root}/docs/"}"
  mkdir -p "${corpus_root}/docs/$(dirname "${relative}")"
  cp "${source}" "${corpus_root}/docs/${relative}"
done
input_bytes="$(find "${corpus_root}/docs" -type f -name '*.docx' \
  -printf '%s\n' | awk '{total += $1} END {print total + 0}')"
if [[ "${input_bytes}" != "22358173" ]]; then
  echo "冻结输入字节数不一致：${input_bytes}" >&2
  exit 1
fi
cp "${repo_root}/evaluation/frozen/dataset.json" \
  "${corpus_root}/evaluation/dataset.json"
cp "${repo_root}/evaluation/frozen/MANIFEST.sha256" \
  "${corpus_root}/evaluation/FROZEN_MANIFEST.sha256"

write_manifest "${runtime_root}"
write_manifest "${corpus_root}"
tar --format=posix -C "${stage}" -czf "${runtime_archive}" runtime
tar --format=posix -C "${stage}" -czf "${corpus_archive}" corpus
write_sidecar "${runtime_archive}"
write_sidecar "${corpus_archive}"
cp "${repo_root}/scripts/offline_bundle.py" \
  "${artifact_root}/offline_bundle.py"

printf 'runtime=%s\nruntime_sha=%s\ncorpus=%s\ncorpus_sha=%s\n' \
  "${runtime_archive}" "${runtime_sidecar}" \
  "${corpus_archive}" "${corpus_sidecar}"
