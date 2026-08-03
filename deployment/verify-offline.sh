#!/usr/bin/env bash
set -euo pipefail

release_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "${release_dir}"
sha256sum -c MANIFEST.sha256

if find . -type f -name '*Zone.Identifier*' -print -quit | grep -q .; then
  echo "runtime 包含 Zone.Identifier，拒绝继续。" >&2
  exit 1
fi

required_files=(
  "RELEASE_ID"
  "SOURCE_REVISION"
  "QDRANT_SOURCE_IMAGE"
  "IMAGE_ARCHIVES.tsv"
  "images/docx-rag-linux-amd64.tar"
  "images/docx-rag-ocr-linux-amd64.tar"
  "images/qdrant-linux-amd64.tar"
  "images/docx-rag.inspect.json"
  "images/docx-rag-ocr.inspect.json"
  "images/qdrant.inspect.json"
  "sbom/docx-rag.cdx.json"
  "sbom/docx-rag-ocr.cdx.json"
  "sbom/qdrant.cdx.json"
  "licenses/NVIDIA-CONTAINER-LICENSE"
  "licenses/PaddleOCR-LICENSE.txt"
  "licenses/THIRD_PARTY_NOTICES.md"
  "provenance/ocr/ASSET_SOURCES.json"
  "provenance/ocr/BASE_RUNTIME.json"
  "provenance/ocr/WHEELS.sha256"
  "provenance/ocr/MODELS.sha256"
  "provenance/ocr/requirements.lock"
  "provenance/ocr/pipeline.yaml"
  "evaluation/runtime/evaluation/evaluate.py"
  "evaluation/runtime/evaluation/metrics.py"
  "evaluation/runtime/scripts/load_test_chat.py"
  "evaluation/runtime/scripts/verify_model_contracts.py"
  "offline_bundle.py"
  "freeze_corpus_manifest.py"
  "qdrant-policy.sh"
  "scripts/docker_archive_identity.py"
  "scripts/docker_archive_reader.py"
  "backup.sh"
  "install.sh"
)
for path in "${required_files[@]}"; do
  if [[ ! -s "${path}" || -L "${path}" ]]; then
    echo "runtime 缺少普通文件：${path}" >&2
    exit 1
  fi
done

# shellcheck source=deployment/qdrant-policy.sh
source "${release_dir}/qdrant-policy.sh"
if [[ "$(cat QDRANT_SOURCE_IMAGE)" \
  != "${RAG_APPROVED_QDRANT_SOURCE_IMAGE}" ]]; then
  echo "Qdrant 来源镜像不在批准白名单。" >&2
  exit 1
fi

if [[ "$(wc -l < IMAGE_ARCHIVES.tsv)" -ne 3 ]]; then
  echo "IMAGE_ARCHIVES.tsv 必须恰有三行。" >&2
  exit 1
fi
if awk -F '\t' 'NF != 6 {exit 1}' IMAGE_ARCHIVES.tsv; then
  :
else
  echo "IMAGE_ARCHIVES.tsv 每行必须恰有六列。" >&2
  exit 1
fi
if [[ "$(cut -f1 IMAGE_ARCHIVES.tsv | paste -sd ',')" \
  != "images/docx-rag-linux-amd64.tar,images/docx-rag-ocr-linux-amd64.tar,images/qdrant-linux-amd64.tar" ]]; then
  echo "镜像归档白名单或顺序无效。" >&2
  exit 1
fi
if [[ ! "$(cat SOURCE_REVISION)" =~ ^[0-9a-f]{40}$ ]]; then
  echo "SOURCE_REVISION 无效。" >&2
  exit 1
fi
source_revision="$(cat SOURCE_REVISION)"
if ! awk -F '\t' \
  -v revision="${source_revision}" \
  -v approved_qdrant="${RAG_APPROVED_QDRANT_REPO_DIGEST}" '
  function is_sha256(value) {
    return length(value) == 71 && value ~ /^sha256:[0-9a-f]+$/
  }
  NR <= 2 {
    if (!is_sha256($3) \
      || $4 != revision \
      || !is_sha256($5) \
      || $6 != "linux/amd64") {
      exit 1
    }
  }
  NR == 3 {
    if (!is_sha256($3) \
      || $4 != approved_qdrant \
      || !is_sha256($5) \
      || $6 != "linux/amd64") {
      exit 1
    }
  }
' IMAGE_ARCHIVES.tsv; then
  echo "镜像 manifest/config digest、provenance 或平台无效。" >&2
  exit 1
fi

line_number=0
while IFS=$'\t' read -r \
  archive_path image manifest_digest provenance config_digest platform; do
  line_number="$((line_number + 1))"
  identity_arguments=(
    python3 -m scripts.docker_archive_identity
    "${archive_path}"
    --tag "${image}"
    --platform "${platform}"
  )
  if ((line_number <= 2)); then
    identity_arguments+=(--expected-revision "${source_revision}")
  fi
  if ! actual_identity="$("${identity_arguments[@]}")"; then
    echo "镜像归档语义身份校验失败：${archive_path}" >&2
    exit 1
  fi
  expected_identity="${manifest_digest}"$'\t'"${config_digest}"$'\t'"${platform}"
  if [[ "${actual_identity}" != "${expected_identity}" ]]; then
    echo "镜像归档身份与 IMAGE_ARCHIVES.tsv 不一致：${archive_path}" >&2
    exit 1
  fi
done < IMAGE_ARCHIVES.tsv

echo "runtime 包逐文件摘要、OCI 镜像身份、来源与 SBOM 校验通过。"
