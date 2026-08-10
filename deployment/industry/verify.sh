#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"

if [[ "$#" -eq 1 && "$1" == "--package-only" ]]; then
  command -v python3 >/dev/null || industry_fail "PYTHON3_NOT_FOUND"
  python3 "${script_dir}/package_selfcheck.py" release "${script_dir}" \
    >/dev/null || industry_fail "PACKAGE_SELFCHECK_FAILED"
  python3 - "${script_dir}" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
expected = json.loads((root / "validation/expected-corpus.json").read_bytes())
smoke = [
    json.loads(line)
    for line in (root / "validation/industry-smoke.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
]
if len(expected["active_documents"]) != 10:
    raise SystemExit("EXPECTED_CORPUS_COUNT_INVALID")
if expected["reference_documents"] != [] or len(smoke) != 20:
    raise SystemExit("PACKAGE_VALIDATION_DATASET_INVALID")
if json.loads((root / "config/intent-router.json").read_bytes())["mode"] != "shadow":
    raise SystemExit("INTENT_MODE_NOT_SHADOW")
if json.loads((root / "config/intent-router-calibration.json").read_bytes())["status"] != "unverified":
    raise SystemExit("CALIBRATION_NOT_UNVERIFIED")
if json.loads((root / "config/retrieval.json").read_bytes())["status"] != "provisional":
    raise SystemExit("RETRIEVAL_NOT_PROVISIONAL")
PY
  printf 'RAG_INDUSTRY_VERIFY_PACKAGE_OK\n'
  exit 0
fi

[[ "$#" -eq 1 ]] \
  || industry_fail "用法: verify.sh /absolute/rag-industry.env"
require_industry_env "$1"
env_file="$(realpath "$1")"
compose_file="$(industry_compose_file "${env_file}")"
release_dir="$(dirname "${compose_file}")"
require_release_directory "${release_dir}"
port="$(exact_env_value "${env_file}" RAG_PORT)"
base_url="http://127.0.0.1:${port}"

wait_industry_http "${base_url}/live" 30 \
  || industry_fail "Industry /live 不可用。"
wait_industry_http "${base_url}/ready" 30 \
  || industry_fail "Industry /ready 不可用。"
validate_industry_compose "${env_file}" "${compose_file}" \
  || industry_fail "INDUSTRY_COMPOSE_CANONICAL_CONFIG_INVALID"
verify_industry_app_identity "${env_file}" true \
  || industry_fail "INDUSTRY_APP_IDENTITY_INVALID"
index_report="$(run_industry_compose "${env_file}" "${compose_file}" \
  --profile index run --rm --no-deps \
  --user 0:0 \
  --entrypoint python \
  --volume "${release_dir}/runtime_check.py:/runtime_check.py:ro" \
  --volume "${release_dir}/validation/expected-corpus.json:/expected-corpus.json:ro" \
  rag-industry-worker \
  /runtime_check.py index-state /expected-corpus.json)" \
  || industry_fail "Industry alias/manifest/source/point 验证失败。"

export RAG_RUNTIME_CHECK_TOKEN
RAG_RUNTIME_CHECK_TOKEN="$(exact_env_value "${env_file}" RAG_QUERY_TOKEN)"
smoke_report="$(python3 "${release_dir}/runtime_check.py" smoke \
  "${base_url}" "${release_dir}/validation/industry-smoke.jsonl")" \
  || industry_fail "Industry smoke 或 training 负向隔离失败。"
unset RAG_RUNTIME_CHECK_TOKEN

python3 - "${index_report}" "${smoke_report}" \
  "$(exact_env_value "${env_file}" RAG_CONFIG_PATH)" <<'PY'
import json
import pathlib
import sys

index = json.loads(sys.argv[1])
smoke = json.loads(sys.argv[2])
config = pathlib.Path(sys.argv[3])
if json.loads((config / "intent-router.json").read_bytes())["mode"] != "shadow":
    raise SystemExit("INTENT_MODE_NOT_SHADOW")
print(
    json.dumps(
        {
            "active_source_count": index["active_source_count"],
            "point_count": index["point_count"],
            "smoke_passed": smoke["passed"],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
)
PY
runtime_index="$(industry_runtime_index_identity)" \
  || industry_fail "INDUSTRY_RUNTIME_INDEX_IDENTITY_INVALID"
promote_industry_last_good "${env_file}" "${runtime_index}" \
  || industry_fail "INDUSTRY_LAST_GOOD_PROMOTION_FAILED"
printf 'RAG_INDUSTRY_VERIFY_OK\n'
