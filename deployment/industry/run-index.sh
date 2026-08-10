#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"

[[ "$#" -eq 1 ]] \
  || industry_fail "用法: run-index.sh /absolute/rag-industry.env"
require_industry_env "$1"
env_file="$(realpath "$1")"
compose_file="$(industry_compose_file "${env_file}")"
release_dir="$(dirname "${compose_file}")"
require_release_directory "${release_dir}"
require_release_directory "${script_dir}"
runtime_check="${script_dir}/runtime_check.py"
expected_corpus="${script_dir}/validation/expected-corpus.json"
port="$(exact_env_value "${env_file}" RAG_PORT)"
base_url="http://127.0.0.1:${port}"
wait_industry_http "${base_url}/live" 30 \
  || industry_fail "Industry app /live 不可用。"

admin_token="$(exact_env_value "${env_file}" RAG_ADMIN_TOKEN)"
revision="$(exact_env_value "${env_file}" RAG_RELEASE_REVISION)"
corpus_sha="$(python3 - "${release_dir}/RELEASE_MANIFEST.json" <<'PY'
import json
import pathlib
import sys

print(json.loads(pathlib.Path(sys.argv[1]).read_bytes())["corpus"]["sha256"])
PY
)"
script_corpus_sha="$(python3 - "${script_dir}/RELEASE_MANIFEST.json" <<'PY'
import json
import pathlib
import sys

print(json.loads(pathlib.Path(sys.argv[1]).read_bytes())["corpus"]["sha256"])
PY
)"
[[ "${script_corpus_sha}" == "${corpus_sha}" ]] \
  || industry_fail "INDEX_RESUME_CORPUS_MISMATCH"
validate_industry_compose "${env_file}" "${compose_file}" \
  || industry_fail "INDUSTRY_COMPOSE_CANONICAL_CONFIG_INVALID"
backup_path="$(exact_env_value "${env_file}" RAG_BACKUP_PATH)"
mkdir -p -- "${backup_path}"
export RAG_RUNTIME_CHECK_TOKEN="${admin_token}"
job_id="$(python3 "${runtime_check}" create-job \
  "${base_url}" "industry-full-${revision}-${corpus_sha:0:16}")" \
  || industry_fail "FULL_INDEX_JOB_CREATE_FAILED"
unset admin_token
printf 'job_id=%s\n' "${job_id}"
[[ "${job_id}" =~ ^[A-Za-z0-9_-]+$ ]] \
  || industry_fail "INDEX_JOB_ID_UNSAFE"
snapshot_name="manifest-before-${job_id}.sqlite3"
snapshot_path="${backup_path}/${snapshot_name}"
rollback_runtime=(
  --user 0:0
  --cap-add DAC_OVERRIDE
  --cap-add CHOWN
  --entrypoint python
  --volume "${runtime_check}:/runtime_check.py:ro"
  --volume "${backup_path}:/backup"
)
job_state="$(python3 "${runtime_check}" job-state \
  "${base_url}" "${job_id}")" \
  || industry_fail "INDEX_JOB_STATE_INVALID"
if [[ -e "${snapshot_path}" ]]; then
  capture_report="$(run_industry_compose "${env_file}" "${compose_file}" \
    --profile index run --rm --no-deps \
    "${rollback_runtime[@]}" \
    rag-industry-worker \
    /runtime_check.py describe-index-rollback \
    "/backup/${snapshot_name}")" \
    || industry_fail "INDEX_ROLLBACK_SNAPSHOT_INVALID"
elif [[ "${job_state}" == "pending" ]]; then
  capture_report="$(run_industry_compose "${env_file}" "${compose_file}" \
    --profile index run --rm --no-deps \
    "${rollback_runtime[@]}" \
    --env "RAG_ROLLBACK_OWNER_UID=$(id -u)" \
    --env "RAG_ROLLBACK_OWNER_GID=$(id -g)" \
    rag-industry-worker \
    /runtime_check.py capture-index-rollback "/backup/${snapshot_name}")" \
    || industry_fail "INDEX_ROLLBACK_CAPTURE_FAILED"
else
  industry_fail "INDEX_ROLLBACK_SNAPSHOT_MISSING"
fi

if [[ "${job_state}" == "pending" ]]; then
  run_industry_compose "${env_file}" "${compose_file}" \
    --profile index run --rm --no-deps \
    rag-industry-worker worker --once \
    || industry_fail "INDUSTRY_WORKER_ONCE_FAILED"
elif [[ "${job_state}" != "succeeded" ]]; then
  industry_fail "INDEX_JOB_NOT_RESUMABLE: ${job_state}"
fi
job_report="$(python3 "${runtime_check}" wait-job \
  "${base_url}" "${job_id}")" \
  || industry_fail "FULL_INDEX_JOB_FAILED"
unset RAG_RUNTIME_CHECK_TOKEN

index_report="$(run_industry_compose "${env_file}" "${compose_file}" \
  --profile index run --rm --no-deps \
  --user 0:0 \
  --entrypoint python \
  --volume "${runtime_check}:/runtime_check.py:ro" \
  --volume "${expected_corpus}:/expected-corpus.json:ro" \
  rag-industry-worker \
  /runtime_check.py index-state /expected-corpus.json)" \
  || industry_fail "INDUSTRY_INDEX_STATE_INVALID"

report_path="${backup_path}/index-${job_id}.json"
descriptor_path="${backup_path}/index-rollback-${job_id}.json"
python3 - "${report_path}" "${job_report}" "${index_report}" \
  "${corpus_sha}" "${capture_report}" "${revision}" \
  "${descriptor_path}" <<'PY'
import json
import pathlib
import sys

payload = {
    "corpus_sha256": sys.argv[4],
    "index": json.loads(sys.argv[3]),
    "job": json.loads(sys.argv[2]),
    "schema_version": "1",
}
path = pathlib.Path(sys.argv[1])
with path.open("x", encoding="utf-8") as output:
    output.write(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    output.write("\n")
path.chmod(0o600)
rollback = json.loads(sys.argv[5])
rollback["current_revision"] = sys.argv[6]
descriptor = pathlib.Path(sys.argv[7])
with descriptor.open("x", encoding="utf-8") as output:
    output.write(json.dumps(rollback, separators=(",", ":"), sort_keys=True))
    output.write("\n")
descriptor.chmod(0o600)
PY
last_rollback="${backup_path}/last-index-rollback.json"
rollback_temp="$(mktemp "${backup_path}/.last-index-rollback.XXXXXX")"
trap 'rm -f -- "${rollback_temp}"' EXIT
cp -- "${descriptor_path}" "${rollback_temp}"
chmod 600 "${rollback_temp}"
mv -f -- "${rollback_temp}" "${last_rollback}"
trap - EXIT
write_industry_release_state "${env_file}" indexed "${index_report}" \
  || industry_fail "INDUSTRY_INDEXED_STATE_FAILED"
printf 'RAG_INDUSTRY_FULL_INDEX_OK report=%s\n' "${report_path}"
