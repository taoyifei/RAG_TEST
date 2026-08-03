#!/usr/bin/env bash
set -euo pipefail

release_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "${release_dir}"
sha256sum -c MANIFEST.sha256

if find . -type f -name '*Zone.Identifier*' -print -quit | grep -q .; then
  echo "runtime 包含 Zone.Identifier，拒绝继续。" >&2
  exit 1
fi

required_common_files=(
  "RELEASE_ID"
  "RELEASE_METADATA.json"
  "SOURCE_REVISION"
  "QDRANT_SOURCE_IMAGE"
  "IMAGE_ARCHIVES.tsv"
  "README.md"
  "compose.yaml"
  ".env.example"
  "deploy.sh"
  "rollback.sh"
  "server-preflight.sh"
  "verify-offline.sh"
  "images/docx-rag-linux-amd64.tar"
  "images/docx-rag-ocr-linux-amd64.tar"
  "images/qdrant-linux-amd64.tar"
  "images/docx-rag.inspect.json"
  "images/docx-rag-ocr.inspect.json"
  "images/qdrant.inspect.json"
  "licenses/NVIDIA-CONTAINER-LICENSE"
  "licenses/PaddleOCR-LICENSE.txt"
  "licenses/THIRD_PARTY_NOTICES.md"
  "provenance/ocr/ASSET_SOURCES.json"
  "provenance/ocr/BASE_RUNTIME.json"
  "provenance/ocr/WHEELS.sha256"
  "provenance/ocr/MODELS.sha256"
  "provenance/ocr/requirements.lock"
  "provenance/ocr/pipeline.yaml"
  "evaluation/runtime/scripts/build_model_deployment_manifest.py"
  "evaluation/runtime/scripts/verify_model_contracts.py"
  "config/pipeline.json"
  "config/retrieval.json"
  "config/corpus-policy.json"
  "offline_bundle.py"
  "freeze_corpus_manifest.py"
  "qdrant-policy.sh"
  "scripts/docker_archive_identity.py"
  "scripts/docker_archive_loaded_identity.py"
  "scripts/docker_archive_reader.py"
  "backup.sh"
  "bootstrap.sh"
  "install.sh"
)
for path in "${required_common_files[@]}"; do
  if [[ ! -f "${path}" || ! -s "${path}" || -L "${path}" ]]; then
    echo "runtime 缺少普通文件：${path}" >&2
    exit 1
  fi
done

if [[ ! -x bootstrap.sh ]]; then
  echo "runtime bootstrap.sh 不可执行。" >&2
  exit 1
fi

if ! release_tier="$(python3 - \
  "${release_dir}/RELEASE_METADATA.json" \
  "${release_dir}/config/retrieval.json" \
  "${release_dir}/config/FREEZE_DECISION.json" \
  "${release_dir}/SOURCE_REVISION" <<'PY'
import hashlib
import json
import sys
from pathlib import Path


def load_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} 不是有效 UTF-8 JSON。") from error
    if type(value) is not dict:
        raise ValueError(f"{label} 必须是 JSON object。")
    return value


try:
    metadata = load_object(Path(sys.argv[1]), "release metadata")
    retrieval_path = Path(sys.argv[2])
    decision_path = Path(sys.argv[3])
    source_revision = Path(sys.argv[4]).read_text(encoding="ascii").strip()
    retrieval = load_object(retrieval_path, "retrieval 配置")
    status = retrieval.get("status")
    if status not in {"provisional", "frozen"}:
        raise ValueError(
            "retrieval status 必须是 provisional 或 frozen。"
        )
    if set(metadata) != {
        "configuration_status",
        "release_tier",
        "schema_version",
        "source_revision",
    }:
        raise ValueError("release metadata 字段集合无效。")
    release_tier = metadata.get("release_tier")
    if (
        metadata.get("schema_version") != "1"
        or release_tier not in {"smoke", "production"}
        or metadata.get("configuration_status") != status
        or metadata.get("source_revision") != source_revision
    ):
        raise ValueError("release metadata 与 runtime 配置不一致。")
    if release_tier == "production" and status != "frozen":
        raise ValueError("production release 必须使用 frozen 配置。")
    decision_exists = decision_path.exists() or decision_path.is_symlink()
    if decision_exists and (
        decision_path.is_symlink() or not decision_path.is_file()
    ):
        raise ValueError("FREEZE_DECISION.json 必须是普通文件。")
    if status == "frozen":
        if not decision_exists:
            raise ValueError(
                "frozen runtime 缺少普通 FREEZE_DECISION.json。"
            )
        decision = load_object(decision_path, "freeze decision")
        canonical = json.dumps(
            decision,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        decision_sha256 = (
            "sha256:" + hashlib.sha256(canonical).hexdigest()
        )
        model_revisions = decision.get("model_revisions")
        if (
            retrieval.get("freeze_decision_sha256")
            != decision_sha256
            or decision.get("schema_version") != "1"
            or type(model_revisions) is not dict
            or not isinstance(
                model_revisions.get("calibration_source_revision"), str
            )
            or len(model_revisions["calibration_source_revision"]) != 40
            or any(
                character not in "0123456789abcdef"
                for character in model_revisions[
                    "calibration_source_revision"
                ]
            )
        ):
            raise ValueError(
                "freeze decision 摘要、schema 或 calibration revision 无效。"
            )
        for key in ("index_fingerprint", "serving_fingerprint"):
            value = decision.get(key)
            if (
                type(value) is not str
                or len(value) != 71
                or not value.startswith("sha256:")
                or any(
                    character not in "0123456789abcdef"
                    for character in value[7:]
                )
            ):
                raise ValueError("freeze decision 指纹无效。")
    print(release_tier)
except ValueError as error:
    print(f"release metadata 离线校验失败：{error}", file=sys.stderr)
    raise SystemExit(1) from None
PY
)"; then
  exit 1
fi

if [[ "${release_tier}" == "production" ]]; then
  required_production_files=(
    "acceptance.sh"
    "config/FREEZE_DECISION.json"
    "evaluation/runtime/evaluation/__init__.py"
    "evaluation/runtime/evaluation/active_state.py"
    "evaluation/runtime/evaluation/chunking_ablation.py"
    "evaluation/runtime/evaluation/chunking_experiment.py"
    "evaluation/runtime/evaluation/dataset.py"
    "evaluation/runtime/evaluation/evaluate.py"
    "evaluation/runtime/evaluation/freeze_release.py"
    "evaluation/runtime/evaluation/legacy_chunking.py"
    "evaluation/runtime/evaluation/metrics.py"
    "evaluation/runtime/evaluation/validate_dataset.py"
    "evaluation/runtime/scripts/benchmark_qdrant.py"
    "evaluation/runtime/scripts/load_test_chat.py"
    "evaluation/runtime/scripts/verify_model_fleet.py"
    "sbom/docx-rag.cdx.json"
    "sbom/docx-rag-ocr.cdx.json"
    "sbom/qdrant.cdx.json"
  )
  for path in "${required_production_files[@]}"; do
    if [[ ! -f "${path}" || ! -s "${path}" || -L "${path}" ]]; then
      echo "production runtime 缺少普通文件：${path}" >&2
      exit 1
    fi
  done
  if [[ ! -x acceptance.sh ]]; then
    echo "production runtime acceptance.sh 不可执行。" >&2
    exit 1
  fi
fi

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

echo "${release_tier} runtime 包逐文件摘要、OCI 镜像身份与来源校验通过。"
