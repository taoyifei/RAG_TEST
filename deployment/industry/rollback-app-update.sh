#!/usr/bin/env bash
set -euo pipefail

rollback_fail() {
  printf 'RAG_INDUSTRY_APP_ROLLBACK_FAILED: %s\n' "$*" >&2
  exit 70
}

[[ "$#" -eq 2 ]] \
  || rollback_fail "用法: rollback-app-update.sh /absolute/env /absolute/transaction"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"
env_file="$1"
transaction="$2"
[[ "${env_file}" == /* && "${transaction}" == /* \
  && -d "${transaction}" && ! -L "${transaction}" ]] \
  || rollback_fail "ROLLBACK_PATH_INVALID"
old_env="${transaction}/old-rag-industry.env"
pre_index="${transaction}/pre-index.json"
container_identity="${transaction}/container-identity.json"
pre_snapshot="${transaction}/pre-update-snapshot.json"
for path in "${old_env}" "${pre_index}" "${container_identity}" \
  "${pre_snapshot}"; do
  [[ -f "${path}" && ! -L "${path}" ]] \
    || rollback_fail "ROLLBACK_EVIDENCE_MISSING"
done

python3 - "${old_env}" "${env_file}" <<'PY'
import os
import pathlib
import shutil
import sys
import tempfile

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{target.name}.rollback.", dir=target.parent
)
try:
    with source.open("rb") as input_stream, os.fdopen(
        descriptor, "wb"
    ) as output_stream:
        shutil.copyfileobj(input_stream, output_stream)
        output_stream.flush()
        os.fsync(output_stream.fileno())
    os.chmod(temporary_name, 0o600)
    os.replace(temporary_name, target)
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY

old_compose="$(industry_compose_file "${env_file}")" \
  || rollback_fail "OLD_COMPOSE_INVALID"
validate_industry_compose "${env_file}" "${old_compose}" \
  || rollback_fail "OLD_COMPOSE_CANONICAL_INVALID"
run_industry_compose "${env_file}" "${old_compose}" \
  up -d --no-deps --no-build --pull never --force-recreate \
  rag-industry-app \
  || rollback_fail "OLD_APP_RECREATE_FAILED"
wait_industry_health rag-industry-app 180 \
  || rollback_fail "OLD_APP_UNHEALTHY"
verify_industry_app_identity "${env_file}" true \
  || rollback_fail "OLD_APP_IDENTITY_INVALID"
python3 - "${pre_snapshot}" <<'PY'
import json
import pathlib
import subprocess
import sys

snapshot = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
app = snapshot.get("app")
if not isinstance(app, dict):
    raise SystemExit("ROLLBACK_APP_SNAPSHOT_INVALID")


def inspect(template):
    output = subprocess.run(
        [
            "docker",
            "container",
            "inspect",
            "--format",
            template,
            "rag-industry-app",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(output)


if (
    inspect("{{json .Mounts}}") != app.get("mounts")
    or inspect("{{json .NetworkSettings.Ports}}") != app.get("ports")
):
    raise SystemExit("ROLLBACK_APP_MOUNT_OR_PORT_DRIFT")
PY

current_index="$(run_industry_compose "${env_file}" "${old_compose}" \
  run --rm --no-deps --entrypoint python \
  --volume "${script_dir}/runtime_check.py:/update/runtime_check.py:ro" \
  rag-industry-app /update/runtime_check.py pre-update-index-state)" \
  || rollback_fail "OLD_INDEX_RECHECK_FAILED"
python3 - "${pre_index}" "${current_index}" <<'PY'
import json
import pathlib
import sys

expected = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
actual = json.loads(sys.argv[2])
keys = {
    "active_collection",
    "alias",
    "index_fingerprint",
    "manifest_sha256",
    "point_count",
    "source_count",
}
if not isinstance(actual, dict) or any(
    expected.get(key) != actual.get(key) for key in keys
):
    raise SystemExit("ROLLBACK_INDEX_IDENTITY_MISMATCH")
PY

python3 - "${container_identity}" <<'PY'
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
    output = subprocess.run(
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
    if output != item:
        raise SystemExit("ROLLBACK_DEPENDENCY_IDENTITY_DRIFT")
PY

printf 'RAG_INDUSTRY_APP_ROLLBACK_OK\n'
