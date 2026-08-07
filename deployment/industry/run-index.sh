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
compose=(
  docker compose
  -p rag-industry
  --env-file "${env_file}"
  -f "${compose_file}"
)
backup_path="$(exact_env_value "${env_file}" RAG_BACKUP_PATH)"
mkdir -p -- "${backup_path}"
export RAG_RUNTIME_CHECK_TOKEN="${admin_token}"
job_id="$(python3 "${release_dir}/runtime_check.py" create-job \
  "${base_url}" "industry-full-${revision}-${corpus_sha:0:16}")" \
  || industry_fail "FULL_INDEX_JOB_CREATE_FAILED"
unset admin_token
printf 'job_id=%s\n' "${job_id}"
[[ "${job_id}" =~ ^[A-Za-z0-9_-]+$ ]] \
  || industry_fail "INDEX_JOB_ID_UNSAFE"
snapshot_name="manifest-before-${job_id}.sqlite3"
capture_report="$("${compose[@]}" --profile index run --rm --no-deps \
  --user 0:0 \
  --entrypoint python \
  --env "RAG_ROLLBACK_OWNER_UID=$(id -u)" \
  --env "RAG_ROLLBACK_OWNER_GID=$(id -g)" \
  --volume "${release_dir}/runtime_check.py:/runtime_check.py:ro" \
  --volume "${backup_path}:/backup" \
  rag-industry-worker \
  /runtime_check.py capture-index-rollback "/backup/${snapshot_name}")" \
  || industry_fail "INDEX_ROLLBACK_CAPTURE_FAILED"

"${compose[@]}" --profile index run --rm --no-deps \
  rag-industry-worker worker --once \
  || industry_fail "INDUSTRY_WORKER_ONCE_FAILED"
job_report="$(python3 "${release_dir}/runtime_check.py" wait-job \
  "${base_url}" "${job_id}")" \
  || industry_fail "FULL_INDEX_JOB_FAILED"
unset RAG_RUNTIME_CHECK_TOKEN

index_report="$("${compose[@]}" --profile index run --rm --no-deps \
  --entrypoint python \
  --volume "${release_dir}/runtime_check.py:/runtime_check.py:ro" \
  --volume "${release_dir}/validation/expected-corpus.json:/expected-corpus.json:ro" \
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
printf 'RAG_INDUSTRY_FULL_INDEX_OK report=%s\n' "${report_path}"
