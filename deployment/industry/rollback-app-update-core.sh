#!/usr/bin/env bash
set -Eeuo pipefail

rollback_fail() {
  printf 'RAG_INDUSTRY_APP_ROLLBACK_FAILED: %s\n' "$*" >&2
  exit 70
}

[[ "$#" -eq 3 ]] \
  || rollback_fail \
    "用法: rollback-app-update-core.sh --automatic-failure|--manual-verified /absolute/env /absolute/transaction"
mode="$1"
env_file="$2"
transaction="$3"
[[ "${mode}" == "--automatic-failure" \
  || "${mode}" == "--manual-verified" ]] \
  || rollback_fail "ROLLBACK_MODE_INVALID"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"
require_industry_env "${env_file}"
[[ "${transaction}" == /* && -d "${transaction}" \
  && ! -L "${transaction}" ]] \
  || rollback_fail "ROLLBACK_PATH_INVALID"
old_env="${transaction}/old-rag-industry.env"
pre_index="${transaction}/pre-index.json"
container_identity="${transaction}/container-identity.json"
pre_snapshot="${transaction}/pre-update-snapshot.json"
source_checkpoint="${transaction}/source-checkpoint.json"
source_state="${transaction}/pre-update-source-state.json"
update_manifest="${transaction}/UPDATE_MANIFEST.json"
candidate_env="${transaction}/candidate-rag-industry.env"
verified_state="${transaction}/verified-state.json"
target_contract="${transaction}/target-contract.json"
target_image_identity="${transaction}/target-image-identity.json"
manual_precheck="${transaction}/manual-rollback-precheck.json"
transaction_state="${transaction}/transaction-state.json"
for path in "${old_env}" "${pre_index}" "${container_identity}" \
  "${pre_snapshot}" "${source_checkpoint}" "${source_state}" \
  "${update_manifest}" "${transaction_state}"; do
  [[ -f "${path}" && ! -L "${path}" ]] \
    || rollback_fail "ROLLBACK_EVIDENCE_MISSING"
done

state_identity="$(python3 - "${transaction_state}" "${update_manifest}" <<'PY'
import json
import pathlib
import re
import stat
import sys

path = pathlib.Path(sys.argv[1])
if stat.S_IMODE(path.stat().st_mode) != 0o600:
    raise SystemExit("ROLLBACK_TRANSACTION_STATE_MODE_INVALID")
value = json.loads(path.read_bytes())
manifest = json.loads(pathlib.Path(sys.argv[2]).read_bytes())
revision = manifest.get("revision")
runtime = manifest.get("runtime")
archive_sha = runtime.get("archive_sha256") if isinstance(runtime, dict) else None
source = manifest.get("source_compatibility")
expected_update_id = (
    f"{revision[:12]}-{archive_sha[:12]}"
    if isinstance(revision, str) and isinstance(archive_sha, str)
    else None
)
allowed = {
    "activated",
    "activating",
    "prechecking",
    "rollback_failed",
    "rolled_back",
    "rolling_back",
    "validated",
    "verified",
    "verifying",
}
if (
    value.get("schema_version") != "2"
    or value.get("state") not in allowed
    or re.fullmatch(
        r"[0-9a-f]{12}-[0-9a-f]{12}", str(value.get("update_id", ""))
    )
    is None
    or value.get("update_id") != expected_update_id
    or not isinstance(source, dict)
    or source.get("trace_v2_read_compatible") is not True
):
    raise SystemExit("ROLLBACK_TRANSACTION_STATE_INVALID")
print(f'{value["state"]}|{value["update_id"]}')
PY
)" || rollback_fail "ROLLBACK_TRANSACTION_STATE_INVALID"
current_state="${state_identity%%|*}"
update_id="${state_identity#*|}"
if [[ "${mode}" == "--manual-verified" ]]; then
  [[ "${current_state}" == "verified" ]] \
    || rollback_fail "MANUAL_ROLLBACK_REQUIRES_VERIFIED"
else
  [[ "${current_state}" =~ ^(activating|activated|verifying)$ ]] \
    || rollback_fail "AUTOMATIC_ROLLBACK_STATE_INVALID"
fi

rollback_abort() {
  local code="$1"
  set +e
  write_industry_serving_transaction_state \
    "${transaction_state}" rollback_failed "${update_id}" \
    rollback "${code}" >/dev/null 2>&1
  rollback_fail "${code}"
}

manual_precheck_fail() {
  printf 'RAG_INDUSTRY_MANUAL_ROLLBACK_PRECHECK_FAILED: %s\n' "$1" >&2
  exit 70
}

verify_dependency_identity() {
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
}

verify_index_identity() {
  local check_env="$1"
  local check_compose="$2"
  local current_index
  current_index="$(run_industry_compose "${check_env}" "${check_compose}" \
    run --rm --no-deps --entrypoint python \
    --volume "${script_dir}/runtime_check.py:/update/runtime_check.py:ro" \
    rag-industry-app /update/runtime_check.py pre-update-index-state)" \
    || return 1
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
}

verify_source_container_contract() {
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
}

verify_target_control_plane() {
  local expected_contract
  local expected_image_ref
  local expected_image_id
  local expected_platform
  local expected_revision
  local actual_image_id
  local actual_platform
  local actual_revision
  local actual_entrypoint
  expected_contract="$(python3 - "${update_manifest}" "${candidate_env}" \
    "${target_contract}" "${target_image_identity}" "${verified_state}" \
    "${pre_index}" <<'PY'
import json
import pathlib
import re
import stat
import sys


def load(path):
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise SystemExit("MANUAL_ROLLBACK_JSON_INVALID")
    return value


def parse_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise SystemExit("MANUAL_ROLLBACK_ENV_DUPLICATE_KEY")
        values[key] = value.strip("\"'")
    return values


manifest_path, env_path, contract_path, image_path, verified_path, index_path = (
    pathlib.Path(item) for item in sys.argv[1:]
)
for path in (
    manifest_path,
    env_path,
    contract_path,
    image_path,
    verified_path,
    index_path,
):
    if (
        not path.is_file()
        or path.is_symlink()
        or stat.S_IMODE(path.stat().st_mode) != 0o600
    ):
        raise SystemExit("MANUAL_ROLLBACK_EVIDENCE_INVALID")
manifest = load(manifest_path)
contract = load(contract_path)
image_identity = load(image_path)
verified = load(verified_path)
pre_index = load(index_path)
image = manifest.get("image")
target = manifest.get("target")
revision = manifest.get("revision")
if (
    manifest.get("schema_version") != "2"
    or manifest.get("package_contract_revision")
    != "industry-serving-update-v2"
    or not isinstance(image, dict)
    or not isinstance(target, dict)
    or not isinstance(revision, str)
    or re.fullmatch(r"[0-9a-f]{40}", revision) is None
    or image.get("ref") != f"docx-rag:{revision[:12]}"
    or image.get("revision") != revision
    or image.get("platform") != "linux/amd64"
    or re.fullmatch(r"sha256:[0-9a-f]{64}", str(image.get("id"))) is None
    or target
    != {
        "alias": "rag-industry-active",
        "project": "rag-industry",
        "service": "rag-industry-app",
    }
):
    raise SystemExit("MANUAL_ROLLBACK_MANIFEST_INVALID")
env = parse_env(env_path)
if (
    env.get("RAG_APP_IMAGE") != image["ref"]
    or env.get("RAG_RELEASE_REVISION") != revision
):
    raise SystemExit("MANUAL_ROLLBACK_CANDIDATE_ENV_INVALID")
expected_contract = {
    "index_fingerprint": manifest["index_fingerprint"]["target"],
    "revision": revision,
    "serving_fingerprint": manifest["serving_fingerprint"]["target"],
    "trace": manifest["trace"],
    "ui": manifest["ui"],
}
if contract != expected_contract:
    raise SystemExit("MANUAL_ROLLBACK_TARGET_CONTRACT_INVALID")
expected_build_info = {
    "expected_revision": revision,
    "installed_revision": revision,
    "matches": True,
}
if image_identity != {
    "build_info": expected_build_info,
    "entrypoint": ["rag-app"],
    "image_id": image["id"],
    "image_ref": image["ref"],
    "oci_revision": revision,
    "platform": image["platform"],
    "revision": revision,
    "schema_version": "1",
}:
    raise SystemExit("MANUAL_ROLLBACK_TARGET_IMAGE_EVIDENCE_INVALID")
if (
    verified.get("schema_version") != "2"
    or verified.get("stage") != "last_good"
    or verified.get("update_kind") != "serving_app_update"
    or verified.get("revision") != revision
    or verified.get("index")
    != {
        key: pre_index[key]
        for key in (
            "active_collection",
            "alias",
            "index_fingerprint",
            "manifest_sha256",
            "point_count",
        )
    }
):
    raise SystemExit("MANUAL_ROLLBACK_VERIFIED_STATE_INVALID")
print(image["ref"])
print(image["id"])
print(image["platform"])
print(revision)
PY
  )" || return 1
  mapfile -t expected_values <<<"${expected_contract}"
  [[ "${#expected_values[@]}" -eq 4 ]] || return 1
  expected_image_ref="${expected_values[0]}"
  expected_image_id="${expected_values[1]}"
  expected_platform="${expected_values[2]}"
  expected_revision="${expected_values[3]}"
  actual_image_id="$(docker image inspect --format '{{.Id}}' \
    "${expected_image_ref}")" || return 1
  actual_platform="$(docker image inspect --format \
    '{{.Os}}/{{.Architecture}}' "${expected_image_ref}")" || return 1
  actual_revision="$(docker image inspect --format \
    '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "${expected_image_ref}")" || return 1
  actual_entrypoint="$(docker image inspect --format \
    '{{json .Config.Entrypoint}}' "${expected_image_ref}")" || return 1
  python3 - "${actual_image_id}" "${actual_platform}" \
    "${actual_revision}" "${actual_entrypoint}" "${expected_image_id}" \
    "${expected_platform}" "${expected_revision}" <<'PY'
import json
import sys

if (
    sys.argv[1] != sys.argv[5]
    or sys.argv[2] != sys.argv[6]
    or sys.argv[3] != sys.argv[7]
    or json.loads(sys.argv[4]) != ["rag-app"]
):
    raise SystemExit("MANUAL_ROLLBACK_TARGET_IMAGE_CHANGED")
PY
}

manual_target_container_state=""
manual_target_runtime_checked="false"

classify_manual_target() {
  local container_id
  local listed
  local running
  local health
  local port
  local target_compose
  local runtime_candidate
  if ! container_id="$(docker container inspect --format '{{.Id}}' \
    rag-industry-app 2>/dev/null)"; then
    listed="$(docker container ls -a \
      --filter 'name=^/rag-industry-app$' --format '{{.Names}}')" \
      || return 1
    [[ -z "${listed}" ]] || return 1
    manual_target_container_state="missing"
    return 0
  fi
  [[ -n "${container_id}" ]] || return 1
  verify_industry_app_static_identity "${candidate_env}" || return 1
  running="$(docker container inspect --format '{{.State.Running}}' \
    rag-industry-app)" || return 1
  if [[ "${running}" == "false" ]]; then
    manual_target_container_state="stopped"
    return 0
  fi
  [[ "${running}" == "true" ]] || return 1
  health="$(docker container inspect --format \
    '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    rag-industry-app)" || return 1
  if [[ "${health}" != "healthy" ]]; then
    manual_target_container_state="unhealthy"
    return 0
  fi
  port="$(exact_env_value "${candidate_env}" RAG_PORT)" || return 1
  if ! curl --fail --silent --show-error --max-time 5 \
    "http://127.0.0.1:${port}/ready" >/dev/null 2>&1; then
    manual_target_container_state="unhealthy"
    return 0
  fi
  manual_target_container_state="healthy"
  target_compose="$(industry_compose_file "${candidate_env}")" || return 1
  runtime_candidate="$(mktemp \
    "${transaction}/.manual-rollback-target-runtime.XXXXXX")" || return 1
  if ! run_industry_compose "${candidate_env}" "${target_compose}" \
    exec -T rag-industry-app rag-app runtime-state >"${runtime_candidate}"; then
    rm -f -- "${runtime_candidate}"
    return 0
  fi
  chmod 600 "${runtime_candidate}" || {
    rm -f -- "${runtime_candidate}"
    return 1
  }
  if ! python3 "${script_dir}/runtime_check.py" validate-runtime-state \
    "${pre_index}" "${target_contract}" "${verified_state}" \
    "${update_manifest}" "${runtime_candidate}" >/dev/null; then
    rm -f -- "${runtime_candidate}"
    return 1
  fi
  manual_target_runtime_checked="true"
  rm -f -- "${runtime_candidate}"
}

write_manual_precheck_report() {
  python3 - "${manual_precheck}" "${manual_target_container_state}" \
    "${manual_target_runtime_checked}" \
    "$(exact_env_value "${candidate_env}" RAG_RELEASE_REVISION)" <<'PY'
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timezone

path = pathlib.Path(sys.argv[1])
container_state = sys.argv[2]
runtime_checked = sys.argv[3] == "true"
if container_state not in {"healthy", "missing", "stopped", "unhealthy"}:
    raise SystemExit("MANUAL_ROLLBACK_TARGET_STATE_INVALID")
value = {
    "created_at": datetime.now(timezone.utc).isoformat(),
    "dependency_identity_checked": True,
    "index_identity_checked": True,
    "schema_version": "1",
    "target_container_state": container_state,
    "target_pointer_checked": True,
    "target_revision": sys.argv[4],
    "target_runtime_state_checked": runtime_checked,
    "target_static_identity_checked": True,
    "transaction_state": "verified",
}
descriptor, temporary_name = tempfile.mkstemp(
    prefix=".manual-rollback-precheck.", dir=path.parent
)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, separators=(",", ":"), sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary_name, 0o600)
    os.replace(temporary_name, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY
}

validate_manual_target() {
  for path in "${candidate_env}" "${verified_state}" "${target_contract}" \
    "${target_image_identity}"; do
    [[ -f "${path}" && ! -L "${path}" ]] \
      || manual_precheck_fail "MANUAL_ROLLBACK_EVIDENCE_MISSING"
  done
  cmp -s -- "${candidate_env}" "${env_file}" \
    || manual_precheck_fail "CURRENT_ENV_NOT_CANDIDATE"
  verify_target_control_plane \
    || manual_precheck_fail "TARGET_CONTROL_PLANE_INVALID"
  classify_manual_target \
    || manual_precheck_fail "TARGET_CONTAINER_IDENTITY_INVALID"
  local target_compose
  target_compose="$(industry_compose_file "${candidate_env}")" \
    || manual_precheck_fail "TARGET_COMPOSE_INVALID"
  python3 "${script_dir}/last_good.py" restore-source-pointer \
    "$(exact_env_value "${env_file}" RAG_BACKUP_PATH)" \
    "${source_checkpoint}" "${source_state}" "${old_env}" \
    "${update_manifest}" "${candidate_env}" "${verified_state}" \
    --validate-only >/dev/null \
    || manual_precheck_fail "TARGET_POINTER_OR_SOURCE_SNAPSHOT_INVALID"
  verify_index_identity "${candidate_env}" "${target_compose}" \
    || manual_precheck_fail "INDEX_IDENTITY_INVALID"
  verify_dependency_identity \
    || manual_precheck_fail "DEPENDENCY_IDENTITY_INVALID"
  [[ "$(docker container inspect --format '{{.State.Running}}' \
    rag-industry-worker 2>/dev/null || true)" != "true" ]] \
    || manual_precheck_fail "WORKER_RUNNING"
  write_manual_precheck_report \
    || manual_precheck_fail "PRECHECK_REPORT_WRITE_FAILED"
}

validate_automatic_pointer() {
  python3 - "${source_checkpoint}" \
    "$(exact_env_value "${old_env}" RAG_BACKUP_PATH)" \
    "${script_dir}/last_good.py" <<'PY'
import json
import pathlib
import subprocess
import sys

checkpoint = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
backup = pathlib.Path(sys.argv[2])
expected = checkpoint.get("pointer_after")
pointer = backup / "last-good-pointer.json"
if expected == {"state": "absent"}:
    if pointer.exists() or pointer.is_symlink():
        raise SystemExit("AUTOMATIC_ROLLBACK_POINTER_DRIFT")
elif isinstance(expected, dict) and expected.get("state") == "pointer":
    output = subprocess.run(
        [sys.executable, sys.argv[3], "resolve", str(backup)],
        check=True,
        capture_output=True,
        text=True,
    )
    actual = json.loads(output.stdout)
    source = checkpoint.get("source_snapshot")
    if (
        not isinstance(source, dict)
        or actual.get("revision") != checkpoint.get("revision")
        or actual.get("snapshot_id") != source.get("snapshot_id")
    ):
        raise SystemExit("AUTOMATIC_ROLLBACK_POINTER_DRIFT")
else:
    raise SystemExit("AUTOMATIC_ROLLBACK_POINTER_EVIDENCE_INVALID")
PY
}

if [[ "${mode}" == "--manual-verified" ]]; then
  validate_manual_target
fi
if ! write_industry_serving_transaction_state \
  "${transaction_state}" rolling_back "${update_id}"; then
  if [[ "${mode}" == "--manual-verified" ]]; then
    manual_precheck_fail "ROLLBACK_STATE_WRITE_FAILED"
  fi
  rollback_abort "ROLLBACK_STATE_WRITE_FAILED"
fi
replace_industry_private_env "${old_env}" "${env_file}" \
  || rollback_abort "OLD_ENV_RESTORE_FAILED"
old_compose="$(industry_compose_file "${env_file}")" \
  || rollback_abort "OLD_COMPOSE_INVALID"
validate_industry_compose "${env_file}" "${old_compose}" \
  || rollback_abort "OLD_COMPOSE_CANONICAL_INVALID"
run_industry_compose "${env_file}" "${old_compose}" \
  up -d --no-deps --no-build --pull never --force-recreate \
  rag-industry-app \
  || rollback_abort "OLD_APP_RECREATE_FAILED"
wait_industry_health rag-industry-app 180 \
  || rollback_abort "OLD_APP_UNHEALTHY"
verify_industry_app_identity "${env_file}" true \
  || rollback_abort "OLD_APP_IDENTITY_INVALID"
verify_source_container_contract \
  || rollback_abort "ROLLBACK_APP_MOUNT_OR_PORT_DRIFT"
verify_index_identity "${env_file}" "${old_compose}" \
  || rollback_abort "ROLLBACK_INDEX_IDENTITY_MISMATCH"
verify_dependency_identity \
  || rollback_abort "ROLLBACK_DEPENDENCY_IDENTITY_DRIFT"

if [[ "${mode}" == "--manual-verified" ]]; then
  python3 "${script_dir}/last_good.py" restore-source-pointer \
    "$(exact_env_value "${env_file}" RAG_BACKUP_PATH)" \
    "${source_checkpoint}" "${source_state}" "${old_env}" \
    "${update_manifest}" "${candidate_env}" "${verified_state}" \
    >/dev/null || rollback_abort "SOURCE_POINTER_RESTORE_FAILED"
  failure_stage="post_verified_manual_rollback"
  result_code="MANUAL_VERIFIED_ROLLBACK"
else
  validate_automatic_pointer \
    || rollback_abort "AUTOMATIC_ROLLBACK_POINTER_DRIFT"
  failure_stage="automatic_failure_rollback"
  result_code="AUTOMATIC_FAILURE_ROLLBACK"
fi
write_industry_serving_transaction_state \
  "${transaction_state}" rolled_back "${update_id}" \
  "${failure_stage}" "${result_code}" \
  || rollback_abort "ROLLBACK_STATE_WRITE_FAILED"

printf 'RAG_INDUSTRY_APP_ROLLBACK_OK mode=%s\n' "${mode#--}"
