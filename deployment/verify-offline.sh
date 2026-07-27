#!/usr/bin/env bash
set -euo pipefail

bundle_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cd "${bundle_dir}"
sha256sum -c MANIFEST.sha256
(
  cd input
  sha256sum -c DOCS.sha256
)
(
  cd evaluation
  sha256sum -c MANIFEST.sha256
)

if find . -type f -name '*Zone.Identifier*' -print -quit | grep -q .; then
  echo "离线包包含 Zone.Identifier，拒绝继续。" >&2
  exit 1
fi
if [[ "$(find input -type f -name '*.docx' | wc -l)" -ne 6 ]]; then
  echo "离线包必须恰有 6 个 DOCX。" >&2
  exit 1
fi

required_files=(
  "images/rag-images-linux-amd64.tar"
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
  "provenance/ocr/requirements.lock"
  "provenance/ocr/pipeline.yaml"
  "evaluation/runtime/evaluation/evaluate.py"
  "evaluation/runtime/evaluation/metrics.py"
  "evaluation/runtime/scripts/load_test_chat.py"
)
for path in "${required_files[@]}"; do
  if [[ ! -s "${path}" ]]; then
    echo "离线包缺少必要文件：${path}" >&2
    exit 1
  fi
done

echo "离线包 checksum、输入、OCR 来源、许可证、SBOM 与评测运行时校验通过。"
