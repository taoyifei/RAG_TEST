#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"

[[ "$#" -eq 2 ]] \
  || industry_fail "用法: generate-secrets.sh /absolute/.env.example /absolute/rag-industry.env"
[[ "$1" == /* && "$2" == /* ]] \
  || industry_fail "模板与输出都必须使用绝对路径。"
[[ -f "$1" && ! -L "$1" ]] || industry_fail "env 模板不是普通文件。"
[[ ! -e "$2" && ! -L "$2" ]] || industry_fail "env 输出已存在，拒绝覆盖。"
command -v openssl >/dev/null || industry_fail "缺少 openssl。"

template="$(realpath "$1")"
destination="$2"
temporary="$(mktemp "$(dirname "${destination}")/.industry-env.XXXXXX")"
trap 'rm -f -- "${temporary}"' EXIT

query_token="$(openssl rand -hex 32)"
admin_token="$(openssl rand -hex 32)"
qdrant_key="$(openssl rand -hex 32)"
ocr_token="$(openssl rand -hex 32)"
[[ "${query_token}" != "${admin_token}" \
  && "${query_token}" != "${qdrant_key}" \
  && "${query_token}" != "${ocr_token}" \
  && "${admin_token}" != "${qdrant_key}" \
  && "${admin_token}" != "${ocr_token}" \
  && "${qdrant_key}" != "${ocr_token}" ]] \
  || industry_fail "随机 secret 意外重复。"

awk -F= \
  -v query="${query_token}" \
  -v admin="${admin_token}" \
  -v qdrant="${qdrant_key}" \
  -v ocr="${ocr_token}" '
    $1 == "RAG_QUERY_TOKEN" { print $1 "=" query; next }
    $1 == "RAG_ADMIN_TOKEN" { print $1 "=" admin; next }
    $1 == "RAG_QDRANT_API_KEY" { print $1 "=" qdrant; next }
    $1 == "RAG_OCR_API_TOKEN" { print $1 "=" ocr; next }
    { print }
  ' "${template}" >"${temporary}"
chmod 600 "${temporary}"
mv -- "${temporary}" "${destination}"
printf 'RAG_INDUSTRY_ENV_CREATED=%s\n' "${destination}"
