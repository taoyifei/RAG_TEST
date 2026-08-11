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
for name in \
  pre-index.json target-contract.json container-identity.json \
  UPDATE_MANIFEST.json; do
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
python3 "${script_dir}/runtime_check.py" validate-runtime-state \
  "${transaction}/pre-index.json" \
  "${transaction}/target-contract.json" - \
  "${transaction}/UPDATE_MANIFEST.json" "${runtime_state}" >/dev/null \
  || verify_fail "RUNTIME_STATE_CONTRACT_MISMATCH"

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

export RAG_RUNTIME_ADMIN_TOKEN
RAG_RUNTIME_ADMIN_TOKEN="$(exact_env_value "${env_file}" RAG_ADMIN_TOKEN)"
log_since="$(python3 - <<'PY'
from datetime import datetime, timedelta, timezone

print((datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat())
PY
)" || verify_fail "APP_LOG_START_TIME_FAILED"
ui_report="${transaction}/ui-contract.json"
python3 "${script_dir}/ui_contract_check.py" verify-ui-trace \
  "${base_url}" >"${ui_report}" \
  || verify_fail "UI_TRACE_CONTRACT_FAILED"
chmod 600 "${ui_report}"
log_path="${transaction}/target-app.log"
docker logs --since "${log_since}" rag-industry-app >"${log_path}" 2>&1 \
  || verify_fail "APP_LOG_CAPTURE_FAILED"
chmod 600 "${log_path}"
python3 "${script_dir}/ui_contract_check.py" verify-log \
  --log-path "${log_path}" >/dev/null \
  || verify_fail "APP_LOG_REDACTION_FAILED"
unset RAG_RUNTIME_ADMIN_TOKEN RAG_RUNTIME_CHECK_TOKEN

trace_schema="$(run_industry_compose "${env_file}" "${compose_file}" \
  run --rm --no-deps --entrypoint python \
  --volume "${script_dir}/runtime_check.py:/update/runtime_check.py:ro" \
  rag-industry-app /update/runtime_check.py trace-schema \
  /state/traces.sqlite3)" \
  || verify_fail "TRACE_SCHEMA_CHECK_FAILED"
python3 - "${trace_schema}" "${transaction}/trace-backup.json" <<'PY'
import json
import pathlib
import sys

value = json.loads(sys.argv[1])
backup = json.loads(pathlib.Path(sys.argv[2]).read_bytes())
expected_fields = {
    "has_question_columns",
    "quick_check",
    "schema_profile",
    "sqlite_user_version",
    "trace_count",
}
if (
    not isinstance(value, dict)
    or set(value) != expected_fields
    or value.get("has_question_columns") is not True
    or value.get("quick_check") != "ok"
    or value.get("schema_profile") != "trace-v2"
    or value.get("sqlite_user_version") != 2
    or not isinstance(value.get("trace_count"), int)
    or isinstance(value.get("trace_count"), bool)
    or not isinstance(backup.get("trace_count"), int)
    or isinstance(backup.get("trace_count"), bool)
    or value["trace_count"] < backup["trace_count"]
):
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
directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY

python3 "${script_dir}/runtime_check.py" validate-runtime-state \
  "${transaction}/pre-index.json" \
  "${transaction}/target-contract.json" "${verified_state}" \
  "${transaction}/UPDATE_MANIFEST.json" "${runtime_state}" >/dev/null \
  || verify_fail "VERIFIED_RUNTIME_STATE_MISMATCH"

printf '%s\n' "${smoke_report}"
printf 'RAG_INDUSTRY_APP_UPDATE_VALIDATED\n'
