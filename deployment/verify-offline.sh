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
  "offline_bundle.py"
  "freeze_corpus_manifest.py"
  "backup.sh"
  "install.sh"
)
for path in "${required_files[@]}"; do
  if [[ ! -s "${path}" || -L "${path}" ]]; then
    echo "runtime 缺少普通文件：${path}" >&2
    exit 1
  fi
done

if [[ "$(wc -l < IMAGE_ARCHIVES.tsv)" -ne 3 ]]; then
  echo "IMAGE_ARCHIVES.tsv 必须恰有三行。" >&2
  exit 1
fi
if awk -F '\t' 'NF != 4 {exit 1}' IMAGE_ARCHIVES.tsv; then
  :
else
  echo "IMAGE_ARCHIVES.tsv 每行必须恰有四列。" >&2
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
if ! awk -F '\t' -v revision="${source_revision}" '
  NR <= 2 {
    if ($3 !~ /^sha256:[0-9a-f]{64}$/ || $4 != revision) {
      exit 1
    }
  }
  NR == 3 {
    if ($3 !~ /^sha256:[0-9a-f]{64}$/ \
      || $4 !~ /^qdrant\/qdrant@sha256:[0-9a-f]{64}$/) {
      exit 1
    }
  }
' IMAGE_ARCHIVES.tsv; then
  echo "镜像 image ID 或 provenance 无效。" >&2
  exit 1
fi

echo "runtime 包逐文件摘要、固定镜像白名单、来源与 SBOM 校验通过。"
