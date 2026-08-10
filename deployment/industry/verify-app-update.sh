#!/usr/bin/env bash
set -euo pipefail

verify_fail() {
  printf 'RAG_INDUSTRY_APP_UPDATE_VERIFY_FAILED: %s\n' "$*" >&2
  exit 1
}

[[ "$#" -eq 2 ]] \
  || verify_fail "用法: verify-app-update.sh /absolute/env /absolute/transaction"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"
env_file="$1"
transaction="$2"
require_industry_env "${env_file}"
[[ "${transaction}" == /* && -d "${transaction}" && ! -L "${transaction}" ]] \
  || verify_fail "TRANSACTION_PATH_INVALID"
for name in pre-index.json target-contract.json container-identity.json; do
  [[ -f "${transaction}/${name}" && ! -L "${transaction}/${name}" ]] \
    || verify_fail "TRANSACTION_EVIDENCE_MISSING"
done

compose_file="$(industry_compose_file "${env_file}")"
validate_industry_compose "${env_file}" "${compose_file}" \
  || verify_fail "TARGET_COMPOSE_CANONICAL_INVALID"
verify_industry_app_identity "${env_file}" true \
  || verify_fail "TARGET_APP_IDENTITY_INVALID"
port="$(exact_env_value "${env_file}" RAG_PORT)"
base_url="http://127.0.0.1:${port}"
wait_industry_http "${base_url}/live" 60 \
  || verify_fail "TARGET_LIVE_FAILED"
wait_industry_http "${base_url}/ready" 60 \
  || verify_fail "TARGET_READY_FAILED"

worker_state="$(docker container inspect --format '{{.State.Running}}' \
  rag-industry-worker 2>/dev/null || true)"
[[ "${worker_state}" != "true" ]] || verify_fail "WORKER_RUNNING"

runtime_state="${transaction}/runtime-state.json"
docker exec rag-industry-app rag-app runtime-state >"${runtime_state}" \
  || verify_fail "RUNTIME_STATE_COMMAND_FAILED"
chmod 600 "${runtime_state}"
python3 - "${transaction}/pre-index.json" \
  "${transaction}/target-contract.json" "${runtime_state}" <<'PY'
import json
import pathlib
import re
import sys

pre = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
target = json.loads(pathlib.Path(sys.argv[2]).read_bytes())
actual = json.loads(pathlib.Path(sys.argv[3]).read_bytes())
expected_fields = {
    "active_collection",
    "alias",
    "index_fingerprint",
    "installed_revision",
    "manifest_sha256",
    "point_count",
    "production_ready",
    "release_matches",
    "release_revision",
    "run_mode",
    "schema_version",
    "serving_fingerprint",
    "trace_question_capture",
    "trace_question_retention_seconds",
    "trace_schema_version",
    "ui_cookie_secure",
    "ui_query_auth_mode",
}
if not isinstance(actual, dict) or set(actual) != expected_fields:
    raise SystemExit("RUNTIME_STATE_FIELDS_INVALID")
for key in (
    "active_collection",
    "alias",
    "index_fingerprint",
    "manifest_sha256",
    "point_count",
):
    if actual.get(key) != pre.get(key):
        raise SystemExit("INDEX_IDENTITY_DRIFT")
if (
    actual.get("schema_version") != "2"
    or actual.get("release_revision") != target.get("revision")
    or actual.get("installed_revision") != target.get("revision")
    or actual.get("release_matches") is not True
    or actual.get("serving_fingerprint") != target.get("serving_fingerprint")
    or actual.get("ui_query_auth_mode") != "same_origin_session"
    or actual.get("ui_cookie_secure") is not False
    or actual.get("trace_question_capture") != "plaintext"
    or actual.get("trace_question_retention_seconds") != 604800
    or actual.get("trace_schema_version") != 2
    or actual.get("run_mode") != "demo"
    or actual.get("production_ready") is not False
):
    raise SystemExit("RUNTIME_SERVING_CONTRACT_MISMATCH")
fingerprint = actual.get("serving_fingerprint")
if not isinstance(fingerprint, str) or re.fullmatch(
    r"sha256:[0-9a-f]{64}", fingerprint
) is None:
    raise SystemExit("SERVING_FINGERPRINT_INVALID")
PY

index_report="$(run_industry_compose "${env_file}" "${compose_file}" \
  run --rm --no-deps --entrypoint python \
  --volume "${script_dir}/validation_check.py:/update/validation_check.py:ro" \
  --volume "${script_dir}/validation/expected-corpus.json:/update/expected-corpus.json:ro" \
  rag-industry-app /update/validation_check.py index-state \
  /update/expected-corpus.json)" \
  || verify_fail "INDEX_SOURCE_VALIDATION_FAILED"
python3 - "${transaction}/pre-index.json" "${index_report}" <<'PY'
import json
import pathlib
import sys

pre = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
report = json.loads(sys.argv[2])
if (
    report.get("active_source_count") != pre.get("source_count")
    or report.get("point_count") != pre.get("point_count")
):
    raise SystemExit("INDEX_SOURCE_OR_POINT_DRIFT")
PY

export RAG_RUNTIME_CHECK_TOKEN
RAG_RUNTIME_CHECK_TOKEN="$(exact_env_value "${env_file}" RAG_QUERY_TOKEN)"
smoke_report="$(python3 "${script_dir}/validation_check.py" smoke \
  "${base_url}" "${script_dir}/validation/industry-smoke.jsonl")" \
  || verify_fail "INDUSTRY_SMOKE_FAILED"
python3 - "${smoke_report}" <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
if (
    value.get("passed") != 20
    or value.get("positive", 0) + value.get("negative", 0) != 20
):
    raise SystemExit("INDUSTRY_SMOKE_COUNT_INVALID")
PY

log_path="${transaction}/target-app.log"
docker logs rag-industry-app >"${log_path}" 2>&1 \
  || verify_fail "APP_LOG_CAPTURE_FAILED"
chmod 600 "${log_path}"
export RAG_RUNTIME_ADMIN_TOKEN
RAG_RUNTIME_ADMIN_TOKEN="$(exact_env_value "${env_file}" RAG_ADMIN_TOKEN)"
python3 "${script_dir}/ui_contract_check.py" \
  "${base_url}" --log-path "${log_path}" >/dev/null \
  || verify_fail "UI_TRACE_CONTRACT_FAILED"
unset RAG_RUNTIME_ADMIN_TOKEN RAG_RUNTIME_CHECK_TOKEN

state_path="$(exact_env_value "${env_file}" RAG_STATE_PATH)"
trace_schema="$(python3 "${script_dir}/runtime_check.py" trace-schema \
  "${state_path}/traces.sqlite3")" \
  || verify_fail "TRACE_SCHEMA_CHECK_FAILED"
python3 - "${trace_schema}" <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
if value != {"has_question_columns": True, "sqlite_user_version": 2}:
    raise SystemExit("TRACE_SCHEMA_NOT_V2")
PY

python3 - "${transaction}/container-identity.json" <<'PY'
import json
import pathlib
import subprocess
import sys

expected = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
for name in (
    "rag-industry-ocr",
    "rag-industry-qdrant",
    "rag-industry-worker",
):
    item = expected.get(name)
    if item is None:
        continue
    actual = subprocess.run(
        [
            "docker",
            "container",
            "inspect",
            "--format",
            "{{.Id}}|{{.State.StartedAt}}",
            name,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual != item:
        raise SystemExit("DEPENDENCY_CONTAINER_RESTARTED")
PY

revision="$(exact_env_value "${env_file}" RAG_RELEASE_REVISION)"
verified_state="${transaction}/verified-state.json"
python3 - "${runtime_state}" "${verified_state}" "${revision}" <<'PY'
import json
import os
import pathlib
import sys

runtime = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
path = pathlib.Path(sys.argv[2])
value = {
    "index": {
        key: runtime[key]
        for key in (
            "active_collection",
            "alias",
            "index_fingerprint",
            "manifest_sha256",
            "point_count",
        )
    },
    "revision": sys.argv[3],
    "schema_version": "2",
    "stage": "last_good",
    "update_kind": "serving_app_update",
}
if path.exists():
    if json.loads(path.read_bytes()) != value:
        raise SystemExit("VERIFIED_STATE_IDEMPOTENCY_MISMATCH")
    raise SystemExit(0)
descriptor = os.open(
    path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    json.dump(value, output, separators=(",", ":"), sort_keys=True)
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())
PY
backup_path="$(exact_env_value "${env_file}" RAG_BACKUP_PATH)"
resolved_last_good="$(python3 "${script_dir}/last_good.py" resolve \
  "${backup_path}" 2>/dev/null || true)"
if [[ -n "${resolved_last_good}" ]]; then
  resolved_revision="$(python3 - "${resolved_last_good}" <<'PY'
import json
import sys

resolved = json.loads(sys.argv[1])
revision = resolved.get("revision")
if not isinstance(revision, str):
    raise SystemExit("LAST_GOOD_REVISION_INVALID")
print(revision)
PY
  )" || verify_fail "LAST_GOOD_RESOLVE_INVALID"
else
  resolved_revision=""
fi
if [[ "${resolved_revision}" == "${revision}" ]]; then
  python3 - "${resolved_last_good}" "${env_file}" \
    "${verified_state}" <<'PY'
import json
import pathlib
import sys

resolved = json.loads(sys.argv[1])
for key, expected in (
    ("env_path", pathlib.Path(sys.argv[2])),
    ("state_path", pathlib.Path(sys.argv[3])),
):
    actual = pathlib.Path(str(resolved.get(key, "")))
    if actual.read_bytes() != expected.read_bytes():
        raise SystemExit("LAST_GOOD_CONTENT_MISMATCH")
PY
else
  python3 "${script_dir}/last_good.py" promote \
    "${backup_path}" "${env_file}" "${verified_state}" "${revision}" \
    >/dev/null || verify_fail "LAST_GOOD_PROMOTION_FAILED"
fi

printf '%s\n' "${smoke_report}"
printf 'RAG_INDUSTRY_APP_UPDATE_VERIFY_OK\n'
