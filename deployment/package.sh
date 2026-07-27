#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
artifact_root="${repo_root}/artifacts"
build_id="$(date -u +%Y%m%dT%H%M%SZ)"
bundle_name="rag-docx-offline-0.1.0-linux-amd64-${build_id}"
bundle_dir="${artifact_root}/${bundle_name}"
archive_path="${artifact_root}/${bundle_name}.tar"
app_image="${RAG_APP_IMAGE:-docx-rag:0.1.0}"
ocr_image="${RAG_OCR_IMAGE:-docx-rag-ocr:0.1.0}"
qdrant_image="${RAG_QDRANT_IMAGE:-qdrant/qdrant:v1.18.3}"

if [[ -e "${bundle_dir}" || -e "${archive_path}" ]]; then
  echo "输出路径已存在，拒绝覆盖：${bundle_name}" >&2
  exit 1
fi

for image in "${app_image}" "${ocr_image}" "${qdrant_image}"; do
  architecture="$(docker image inspect \
    --format '{{.Architecture}}' "${image}")"
  operating_system="$(docker image inspect \
    --format '{{.Os}}' "${image}")"
  if [[ "${architecture}/${operating_system}" != "amd64/linux" ]]; then
    echo "镜像不是 linux/amd64：${image}" >&2
    exit 1
  fi
done

(
  cd "${repo_root}/deployment/ocr/assets/wheelhouse"
  sha256sum --check "${repo_root}/deployment/ocr/WHEELS.sha256"
)
(
  cd "${repo_root}/deployment/ocr/assets"
  sha256sum --check MANIFEST.sha256
)

mkdir -p \
  "${bundle_dir}/evaluation/runtime/evaluation" \
  "${bundle_dir}/evaluation/runtime/scripts" \
  "${bundle_dir}/images" \
  "${bundle_dir}/input" \
  "${bundle_dir}/licenses" \
  "${bundle_dir}/provenance/ocr" \
  "${bundle_dir}/sbom"

cp "${repo_root}/deployment/compose.yaml" "${bundle_dir}/compose.yaml"
cp "${repo_root}/deployment/.env.example" "${bundle_dir}/.env.example"
cp "${repo_root}/deployment/deploy.sh" "${bundle_dir}/deploy.sh"
cp "${repo_root}/deployment/rollback.sh" "${bundle_dir}/rollback.sh"
cp "${repo_root}/deployment/verify-offline.sh" \
  "${bundle_dir}/verify-offline.sh"
cp "${repo_root}/deployment/README.md" "${bundle_dir}/README.md"
cp "${repo_root}/evaluation/frozen/dataset.json" \
  "${bundle_dir}/evaluation/dataset.json"
cp "${repo_root}/evaluation/frozen/MANIFEST.sha256" \
  "${bundle_dir}/evaluation/MANIFEST.sha256"
cp "${repo_root}/evaluation/"*.py \
  "${bundle_dir}/evaluation/runtime/evaluation/"
cp "${repo_root}/scripts/load_test_chat.py" \
  "${bundle_dir}/evaluation/runtime/scripts/"
cp "${repo_root}/scripts/benchmark_qdrant.py" \
  "${bundle_dir}/evaluation/runtime/scripts/"
cp "${repo_root}/deployment/ocr/assets/licenses/PaddleOCR-LICENSE.txt" \
  "${bundle_dir}/licenses/"
cp "${repo_root}/deployment/ocr/THIRD_PARTY_NOTICES.md" \
  "${bundle_dir}/licenses/"
cp "${repo_root}/deployment/ocr/ASSET_SOURCES.json" \
  "${bundle_dir}/provenance/ocr/"
cp "${repo_root}/deployment/ocr/BASE_RUNTIME.json" \
  "${bundle_dir}/provenance/ocr/"
cp "${repo_root}/deployment/ocr/WHEELS.sha256" \
  "${bundle_dir}/provenance/ocr/"
cp "${repo_root}/deployment/ocr/requirements.lock" \
  "${bundle_dir}/provenance/ocr/"
cp "${repo_root}/deployment/ocr/pipeline.yaml" \
  "${bundle_dir}/provenance/ocr/"
docker run \
  --rm \
  --network none \
  --entrypoint cat \
  "${ocr_image}" \
  /opt/rag-ocr/licenses/NVIDIA-CONTAINER-LICENSE \
  > "${bundle_dir}/licenses/NVIDIA-CONTAINER-LICENSE"

mapfile -d '' docx_files < <(
  find "${repo_root}/docs" \
    -type f \
    -name '*.docx' \
    -print0 \
    | sort -z
)
if [[ "${#docx_files[@]}" -ne 6 ]]; then
  echo "冻结输入不是 6 个 DOCX，拒绝打包。" >&2
  exit 1
fi
for source in "${docx_files[@]}"; do
  relative="${source#"${repo_root}/docs/"}"
  mkdir -p "${bundle_dir}/input/$(dirname "${relative}")"
  cp "${source}" "${bundle_dir}/input/${relative}"
done
input_bytes="$(find "${bundle_dir}/input" \
  -type f \
  -name '*.docx' \
  -printf '%s\n' \
  | awk '{total += $1} END {print total + 0}')"
if [[ "${input_bytes}" != "22358173" ]]; then
  echo "冻结输入字节数不一致：${input_bytes}" >&2
  exit 1
fi
(
  cd "${bundle_dir}/input"
  find . \
    -type f \
    -name '*.docx' \
    -print0 \
    | sort -z \
    | xargs -0 sha256sum \
    > DOCS.sha256
)

docker image save \
  --platform linux/amd64 \
  --output "${bundle_dir}/images/rag-images-linux-amd64.tar" \
  "${app_image}" \
  "${ocr_image}" \
  "${qdrant_image}"
docker sbom --format cyclonedx-json "${app_image}" \
  > "${bundle_dir}/sbom/docx-rag.cdx.json"
docker sbom --format cyclonedx-json "${ocr_image}" \
  > "${bundle_dir}/sbom/docx-rag-ocr.cdx.json"
docker sbom --format cyclonedx-json "${qdrant_image}" \
  > "${bundle_dir}/sbom/qdrant.cdx.json"
docker image inspect "${app_image}" \
  > "${bundle_dir}/images/docx-rag.inspect.json"
docker image inspect "${ocr_image}" \
  > "${bundle_dir}/images/docx-rag-ocr.inspect.json"
docker image inspect "${qdrant_image}" \
  > "${bundle_dir}/images/qdrant.inspect.json"

(
  cd "${bundle_dir}"
  find . \
    -type f \
    ! -path './MANIFEST.sha256' \
    -print0 \
    | sort -z \
    | xargs -0 sha256sum \
    > MANIFEST.sha256
)
tar --format=posix -C "${artifact_root}" -cf "${archive_path}" \
  "${bundle_name}"

archive_sha256="$(sha256sum "${archive_path}" | awk '{print $1}')"
printf 'archive=%s\nsha256=%s\nbundle=%s\n' \
  "${archive_path}" \
  "${archive_sha256}" \
  "${bundle_dir}"
