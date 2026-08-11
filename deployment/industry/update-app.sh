#!/usr/bin/env bash
set -Eeuo pipefail

fail() {
  printf 'RAG_INDUSTRY_SERVING_UPDATE_FAILED: %s\n' "$*" >&2
  exit 1
}

[[ "$#" -eq 1 ]] \
  || fail "用法: update-app.sh /absolute/rag-industry.env"
package_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
env_file="$1"
[[ "${env_file}" == /* && -f "${env_file}" && ! -L "${env_file}" ]] \
  || fail "ENV_FILE_INVALID"
env_file="$(realpath "${env_file}")"

env_value() {
  python3 - "$1" "$2" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
matches = []
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith(key + "="):
        matches.append(line.split("=", 1)[1].strip("\"'"))
if len(matches) != 1 or not matches[0] or "\n" in matches[0]:
    raise SystemExit(f"{key}_INVALID")
print(matches[0])
PY
}

manifest_value() {
  python3 - "${package_dir}/UPDATE_MANIFEST.json" "$1" <<'PY'
import json
import pathlib
import sys

value = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
for part in sys.argv[2].split("."):
    if not isinstance(value, dict) or part not in value:
        raise SystemExit("MANIFEST_FIELD_MISSING")
    value = value[part]
if not isinstance(value, str) or not value:
    raise SystemExit("MANIFEST_FIELD_INVALID")
print(value)
PY
}

python3 "${package_dir}/package_selfcheck.py" verify "${package_dir}" \
  >/dev/null || fail "PACKAGE_SELFCHECK_FAILED"
release_root="$(env_value "${env_file}" RAG_RELEASE_ROOT)" \
  || fail "RELEASE_ROOT_INVALID"
backup_path="$(env_value "${env_file}" RAG_BACKUP_PATH)" \
  || fail "BACKUP_PATH_INVALID"
target_revision="$(manifest_value revision)" || fail "TARGET_REVISION_INVALID"
runtime_archive_sha="$(manifest_value runtime.archive_sha256)" \
  || fail "RUNTIME_SHA_INVALID"
target_image="$(manifest_value image.ref)" || fail "TARGET_IMAGE_INVALID"
target_image_id="$(manifest_value image.id)" || fail "TARGET_IMAGE_ID_INVALID"
target_platform="$(manifest_value image.platform)" \
  || fail "TARGET_PLATFORM_INVALID"
target_index="$(manifest_value index_fingerprint.target)" \
  || fail "TARGET_INDEX_INVALID"
source_config_profile="$(manifest_value source_compatibility.config_profile)" \
  || fail "SOURCE_CONFIG_PROFILE_INVALID"
target_config_profile="$(manifest_value target_config_profile)" \
  || fail "TARGET_CONFIG_PROFILE_INVALID"
[[ -d "${backup_path}" && ! -L "${backup_path}" ]] \
  || fail "BACKUP_PATH_INVALID"
command -v flock >/dev/null 2>&1 || fail "FLOCK_NOT_FOUND"
update_id="${target_revision:0:12}-${runtime_archive_sha:0:12}"
runtime_parent="${release_root}/serving-updates"
runtime_dir="${runtime_parent}/${update_id}"
mkdir -p -- "${runtime_parent}" "${backup_path}/serving-updates"

temporary_extract=""
old_config_json=""
new_config_json=""
cleanup() {
  [[ -z "${temporary_extract}" ]] || rm -rf -- "${temporary_extract}"
  [[ -z "${old_config_json}" ]] || rm -f -- "${old_config_json}"
  [[ -z "${new_config_json}" ]] || rm -f -- "${new_config_json}"
}
trap cleanup EXIT

if [[ -e "${runtime_dir}" ]]; then
  [[ -d "${runtime_dir}" && ! -L "${runtime_dir}" ]] \
    || fail "RUNTIME_DESTINATION_INVALID"
  python3 - "${runtime_dir}" "${package_dir}/UPDATE_MANIFEST.json" <<'PY'
import hashlib
import json
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1])
manifest = json.loads(pathlib.Path(sys.argv[2]).read_bytes())
expected = manifest.get("runtime", {}).get("files")
if not isinstance(expected, dict):
    raise SystemExit("RUNTIME_FILES_INVALID")
expected_names = set(expected)
expected_directories = set()
for name in expected_names:
    parent = pathlib.PurePosixPath(name).parent
    while str(parent) not in {".", ""}:
        expected_directories.add(str(parent))
        parent = parent.parent
actual_files = set()
actual_directories = set()
for path in root.rglob("*"):
    if path.is_symlink():
        raise SystemExit("RUNTIME_REUSE_SYMLINK")
    relative = str(path.relative_to(root))
    if path.is_file():
        actual_files.add(relative)
    elif path.is_dir():
        actual_directories.add(relative)
    else:
        raise SystemExit("RUNTIME_REUSE_SPECIAL_FILE")
if actual_files != expected_names or actual_directories != expected_directories:
    raise SystemExit("RUNTIME_REUSE_EXACT_SET_MISMATCH")
for name, digest in expected.items():
    path = root / name
    expected_mode = 0o755 if name.endswith(".sh") or name in {
        "compose_check.py",
        "last_good.py",
        "runtime_check.py",
        "ui_contract_check.py",
        "validation_check.py",
    } else 0o644
    if (
        hashlib.sha256(path.read_bytes()).hexdigest() != digest
        or stat.S_IMODE(path.stat().st_mode) != expected_mode
    ):
        raise SystemExit("RUNTIME_REUSE_SHA256_MISMATCH")
if stat.S_IMODE(root.stat().st_mode) != 0o755:
    raise SystemExit("RUNTIME_REUSE_ROOT_MODE_INVALID")
PY
else
  temporary_extract="$(mktemp -d "${runtime_parent}/.${update_id}.XXXXXX")"
  python3 "${package_dir}/package_selfcheck.py" extract \
    "${package_dir}" "${temporary_extract}" >/dev/null \
    || fail "RUNTIME_SAFE_EXTRACTION_FAILED"
  extracted_runtime="${temporary_extract}/serving-runtime/${target_revision:0:12}"
  [[ -d "${extracted_runtime}" && ! -L "${extracted_runtime}" ]] \
    || fail "EXTRACTED_RUNTIME_INVALID"
  mv -- "${extracted_runtime}" "${runtime_dir}" \
    || fail "RUNTIME_ATOMIC_PUBLISH_FAILED"
fi

# shellcheck source=lib.sh
source "${runtime_dir}/lib.sh"
update_lock_fd=""
acquire_industry_serving_update_lock "${backup_path}" update_lock_fd \
  || fail "${INDUSTRY_SERVING_LOCK_ERROR:-SERVING_UPDATE_LOCK_FAILED}"
require_industry_env "${env_file}"
old_compose="$(industry_compose_file "${env_file}")"
old_revision="$(exact_env_value "${env_file}" RAG_RELEASE_REVISION)"
old_image="$(exact_env_value "${env_file}" RAG_APP_IMAGE)"
old_config="$(exact_env_value "${env_file}" RAG_CONFIG_PATH)"
python3 - "${package_dir}/UPDATE_MANIFEST.json" \
  "${old_revision}" "${target_revision}" "${target_index}" <<'PY'
import json
import pathlib
import re
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
old_revision, target_revision, target_index = sys.argv[2:]
source = manifest.get("source_compatibility")
if not isinstance(source, dict):
    raise SystemExit("SOURCE_COMPATIBILITY_INVALID")
compatible = source.get("compatible_revisions")
source_config = source.get("config_files")
trusted_last_good = source.get("trusted_last_good_revisions")
trace_compatibility = source.get("trace_compatibility")
target_config = manifest.get("config_files")
if (
    re.fullmatch(r"[0-9a-f]{40}", old_revision) is None
    or re.fullmatch(r"[0-9a-f]{40}", target_revision) is None
    or not isinstance(compatible, list)
    or not all(
        isinstance(item, str)
        and re.fullmatch(r"[0-9a-f]{40}", item) is not None
        for item in compatible
    )
    or source.get("old_app_runtime_state_required") is not False
    or trace_compatibility
    != {
        "accepted_user_versions": [0, 1, 2],
        "legacy_v0_profile": "industry-trace-2c4-v0",
        "target_schema_version": 2,
    }
    or source.get("required_index_fingerprint") != target_index
    or not isinstance(trusted_last_good, list)
    or not trusted_last_good
    or not compatible
    or trusted_last_good[0] != compatible[0]
    or len(set(trusted_last_good)) != len(trusted_last_good)
    or any(
        not isinstance(item, str)
        or re.fullmatch(r"[0-9a-f]{40}", item) is None
        for item in trusted_last_good
    )
    or source.get("config_profile")
    not in {
        "first-deploy-private-v1",
        "serving-runtime-public-config-v1",
    }
    or manifest.get("target_config_profile")
    != "serving-runtime-public-config-v1"
    or re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        str(source.get("serving_fingerprint")),
    )
    is None
    or not isinstance(source_config, dict)
    or not isinstance(target_config, dict)
    or set(source_config) != set(target_config)
    or set(source_config)
    != {
        "corpus-policy.json",
        "intent-router-calibration.json",
        "intent-router.json",
        "pipeline.json",
        "retrieval.json",
    }
    or any(
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for digest in [*source_config.values(), *target_config.values()]
    )
):
    raise SystemExit("SOURCE_COMPATIBILITY_INVALID")
if old_revision != target_revision and old_revision not in compatible:
    raise SystemExit("SOURCE_REVISION_NOT_COMPATIBLE")
PY
worker_state="$(docker container inspect --format '{{.State.Running}}' \
  rag-industry-worker 2>/dev/null || true)"
[[ "${worker_state}" != "true" ]] || fail "WORKER_RUNNING"

write_transaction_state() {
  write_industry_serving_transaction_state \
    "$1" "$2" "${update_id}" "${3:-}" "${4:-}"
}

validate_target_runtime_checkpoint() {
  local transaction_path="$1"
  local candidate
  local published="${transaction_path}/runtime-state-before-finalize.json"
  candidate="$(mktemp \
    "${transaction_path}/.runtime-state-before-finalize.XXXXXX")" \
    || return 1
  chmod 600 "${candidate}" || return 1
  if ! run_industry_compose \
    "${env_file}" "${runtime_dir}/compose.yaml" \
    exec -T rag-industry-app rag-app runtime-state >"${candidate}"; then
    rm -f -- "${candidate}"
    return 1
  fi
  if ! python3 "${runtime_dir}/runtime_check.py" \
    validate-runtime-state \
    "${transaction_path}/pre-index.json" \
    "${transaction_path}/target-contract.json" \
    "${transaction_path}/verified-state.json" \
    "${transaction_path}/UPDATE_MANIFEST.json" "${candidate}" \
    >/dev/null; then
    rm -f -- "${candidate}"
    return 1
  fi
  mv -f -- "${candidate}" "${published}" || return 1
  chmod 600 "${published}" || return 1
}

validate_activation_intent() {
  local transaction_path="$1"
  python3 - "${transaction_path}/activation-intent.json" \
    "${transaction_path}" "${update_id}" "${target_revision}" \
    "${runtime_dir}/compose.yaml" <<'PY'
import hashlib
import json
import pathlib
import re
import stat
import sys

intent_path = pathlib.Path(sys.argv[1])
transaction = pathlib.Path(sys.argv[2])
update_id = sys.argv[3]
target_revision = sys.argv[4]
target_compose = pathlib.Path(sys.argv[5])
if (
    not intent_path.is_file()
    or intent_path.is_symlink()
    or stat.S_IMODE(intent_path.stat().st_mode) != 0o600
):
    raise SystemExit("ACTIVATION_INTENT_FILE_INVALID")
value = json.loads(intent_path.read_bytes())
expected_fields = {
    "attempt",
    "candidate_env_sha256",
    "created_at",
    "schema_version",
    "source_checkpoint",
    "source_compose_sha256",
    "source_config",
    "source_env_sha256",
    "source_image",
    "source_revision",
    "target_compose_sha256",
    "target_config",
    "target_image",
    "target_revision",
    "update_id",
}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


state = json.loads((transaction / "transaction-state.json").read_bytes())
manifest = json.loads((transaction / "UPDATE_MANIFEST.json").read_bytes())
snapshot = json.loads((transaction / "pre-update-snapshot.json").read_bytes())
filesystem = json.loads((transaction / "pre-filesystem.json").read_bytes())
checkpoint = json.loads((transaction / "source-checkpoint.json").read_bytes())
source_image = snapshot.get("app")
source_compose = snapshot.get("compose")
manifest_image = manifest.get("image")
expected_source_image = {
    "id": source_image.get("image_id") if isinstance(source_image, dict) else None,
    "ref": source_image.get("image_ref") if isinstance(source_image, dict) else None,
    "revision": (
        source_image.get("oci_revision") if isinstance(source_image, dict) else None
    ),
}
expected_target_image = {
    "id": manifest_image.get("id") if isinstance(manifest_image, dict) else None,
    "ref": manifest_image.get("ref") if isinstance(manifest_image, dict) else None,
    "revision": target_revision,
}
source_compose_path = (
    pathlib.Path(str(source_compose.get("path", "")))
    if isinstance(source_compose, dict)
    else pathlib.Path("")
)
if (
    set(value) != expected_fields
    or value.get("schema_version") != "1"
    or value.get("update_id") != update_id
    or value.get("attempt") != state.get("attempt")
    or value.get("source_revision") != checkpoint.get("revision")
    or value.get("target_revision") != target_revision
    or value.get("source_env_sha256")
    != digest(transaction / "old-rag-industry.env")
    or value.get("candidate_env_sha256")
    != digest(transaction / "candidate-rag-industry.env")
    or value.get("source_compose_sha256") != digest(source_compose_path)
    or value.get("target_compose_sha256") != digest(target_compose)
    or value.get("source_image") != expected_source_image
    or value.get("target_image") != expected_target_image
    or value.get("source_config") != filesystem.get("config")
    or value.get("target_config")
    != {
        "files": manifest.get("config_files"),
        "profile": manifest.get("target_config_profile"),
    }
    or value.get("source_checkpoint") != checkpoint.get("source_snapshot")
    or not isinstance(value.get("created_at"), str)
    or any(
        re.fullmatch(r"[0-9a-f]{64}", str(value.get(field, ""))) is None
        for field in (
            "candidate_env_sha256",
            "source_compose_sha256",
            "source_env_sha256",
            "target_compose_sha256",
        )
    )
):
    raise SystemExit("ACTIVATION_INTENT_INVALID")
PY
}

write_activation_intent() {
  local transaction_path="$1"
  python3 - "${transaction_path}" "${update_id}" "${target_revision}" \
    "${runtime_dir}/compose.yaml" <<'PY'
import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timezone

transaction = pathlib.Path(sys.argv[1])
update_id = sys.argv[2]
target_revision = sys.argv[3]
target_compose = pathlib.Path(sys.argv[4])


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


state = json.loads((transaction / "transaction-state.json").read_bytes())
manifest = json.loads((transaction / "UPDATE_MANIFEST.json").read_bytes())
snapshot = json.loads((transaction / "pre-update-snapshot.json").read_bytes())
filesystem = json.loads((transaction / "pre-filesystem.json").read_bytes())
checkpoint = json.loads((transaction / "source-checkpoint.json").read_bytes())
source_image = snapshot["app"]
source_compose = pathlib.Path(snapshot["compose"]["path"])
value = {
    "attempt": state["attempt"],
    "candidate_env_sha256": digest(
        transaction / "candidate-rag-industry.env"
    ),
    "created_at": datetime.now(timezone.utc).isoformat(),
    "schema_version": "1",
    "source_checkpoint": checkpoint["source_snapshot"],
    "source_compose_sha256": digest(source_compose),
    "source_config": filesystem["config"],
    "source_env_sha256": digest(transaction / "old-rag-industry.env"),
    "source_image": {
        "id": source_image["image_id"],
        "ref": source_image["image_ref"],
        "revision": source_image["oci_revision"],
    },
    "source_revision": checkpoint["revision"],
    "target_compose_sha256": digest(target_compose),
    "target_config": {
        "files": manifest["config_files"],
        "profile": manifest["target_config_profile"],
    },
    "target_image": {
        "id": manifest["image"]["id"],
        "ref": manifest["image"]["ref"],
        "revision": target_revision,
    },
    "target_revision": target_revision,
    "update_id": update_id,
}
path = transaction / "activation-intent.json"
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    json.dump(value, output, separators=(",", ":"), sort_keys=True)
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())
directory = os.open(transaction, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
  validate_activation_intent "${transaction_path}"
}

activation_env_identity() {
  python3 - "$1/activation-intent.json" "${env_file}" <<'PY'
import hashlib
import json
import pathlib
import sys

intent = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
actual = hashlib.sha256(pathlib.Path(sys.argv[2]).read_bytes()).hexdigest()
if actual == intent.get("source_env_sha256"):
    print("source")
elif actual == intent.get("candidate_env_sha256"):
    print("target")
else:
    print("unknown")
PY
}

write_recovery_failure() {
  local transaction_path="$1"
  local code="$2"
  write_transaction_state \
    "${transaction_path}/transaction-state.json" rollback_failed \
    activation_recovery "${code}" \
    || fail "RECOVERY_FAILURE_STATE_WRITE_FAILED"
  fail "${code}"
}

rollback_recovery_attempt() {
  local transaction_path="$1"
  local code="$2"
  if ! bash "${runtime_dir}/rollback-app-update-core.sh" \
    --automatic-failure "${env_file}" "${transaction_path}"; then
    write_recovery_failure "${transaction_path}" \
      "ACTIVATION_RECOVERY_ROLLBACK_FAILED"
  fi
  fail "${code}"
}

complete_recovered_target() {
  local transaction_path="$1"
  local state="$2"
  if [[ "${state}" == "verified" ]]; then
    bash "${runtime_dir}/verify-app-update.sh" \
      "${env_file}" "${transaction_path}" \
      || fail "IDEMPOTENT_VERIFY_FAILED"
  elif [[ "${state}" == "verifying" ]]; then
    if [[ -f "${transaction_path}/verified-state.json" \
      && ! -L "${transaction_path}/verified-state.json" ]]; then
      validate_target_runtime_checkpoint "${transaction_path}" \
        || rollback_recovery_attempt \
          "${transaction_path}" "VERIFYING_RECOVERY_RUNTIME_MISMATCH"
    else
      bash "${runtime_dir}/verify-app-update.sh" \
        "${env_file}" "${transaction_path}" \
        || rollback_recovery_attempt \
          "${transaction_path}" "VERIFYING_RECOVERY_VERIFY_FAILED"
    fi
    write_transaction_state \
      "${transaction_path}/transaction-state.json" validated \
      || write_recovery_failure \
        "${transaction_path}" "VERIFYING_RECOVERY_STATE_WRITE_FAILED"
    state=validated
  fi
  validate_target_runtime_checkpoint "${transaction_path}" \
    || fail "RECOVERY_RUNTIME_STATE_MISMATCH"
  bash "${runtime_dir}/finalize-app-update.sh" \
    "${env_file}" "${transaction_path}" "${target_revision}" reconcile \
    || fail "IDEMPOTENT_LAST_GOOD_RECONCILIATION_FAILED"
  write_transaction_state \
    "${transaction_path}/transaction-state.json" verified \
    || write_recovery_failure \
      "${transaction_path}" "IDEMPOTENT_TRANSACTION_STATE_WRITE_FAILED"
  printf 'reindex_required=false\n'
  printf 'RAG_INDUSTRY_SERVING_UPDATE_ALREADY_CURRENT\n'
  exit 0
}

update_root="${backup_path}/serving-updates/${update_id}"
if [[ -e "${update_root}" ]]; then
  [[ -d "${update_root}" && ! -L "${update_root}" ]] \
    || fail "UPDATE_AUDIT_ROOT_INVALID"
else
  mkdir -m 700 -- "${update_root}"
fi
current_revision="$(exact_env_value "${env_file}" RAG_RELEASE_REVISION)"
current_compose="$(exact_env_value "${env_file}" RAG_INDUSTRY_COMPOSE_FILE)"
recovery="$(python3 - "${update_root}" "${update_id}" <<'PY'
import json
import pathlib
import re
import stat
import sys

root = pathlib.Path(sys.argv[1])
allowed_states = {
    "activated",
    "activating",
    "precheck_failed",
    "prechecking",
    "prepared",
    "rollback_failed",
    "rolled_back",
    "rolling_back",
    "validated",
    "verified",
    "verifying",
}
recoverable_states = {
    "activated",
    "activating",
    "prechecking",
    "rollback_failed",
    "rolling_back",
    "validated",
    "verified",
    "verifying",
}
attempts = []
for path in root.iterdir():
    match = re.fullmatch(r"attempt-([0-9]{4})", path.name)
    if not path.is_dir() or path.is_symlink() or match is None:
        raise SystemExit("UPDATE_ATTEMPT_ENTRY_INVALID")
    state_path = path / "transaction-state.json"
    if (
        not state_path.is_file()
        or state_path.is_symlink()
        or stat.S_IMODE(state_path.stat().st_mode) != 0o600
    ):
        raise SystemExit("UPDATE_ATTEMPT_STATE_INVALID")
    value = json.loads(state_path.read_bytes())
    if (
        set(value)
        != {
            "attempt",
            "error_code",
            "failure_stage",
            "schema_version",
            "state",
            "update_id",
            "updated_at",
        }
        or value.get("attempt") != int(match.group(1))
        or value.get("schema_version") != "2"
        or value.get("state") not in allowed_states
        or value.get("update_id") != sys.argv[2]
        or not isinstance(value.get("updated_at"), str)
        or (
            value.get("state")
            in {"precheck_failed", "rollback_failed", "rolled_back"}
            and (
                not isinstance(value.get("failure_stage"), str)
                or not isinstance(value.get("error_code"), str)
            )
        )
        or (
            value.get("state")
            not in {"precheck_failed", "rollback_failed", "rolled_back"}
            and (
                value.get("failure_stage") is not None
                or value.get("error_code") is not None
            )
        )
    ):
        raise SystemExit("UPDATE_ATTEMPT_ID_INVALID")
    attempts.append((int(match.group(1)), path, str(value.get("state"))))
attempts.sort()
if [number for number, _, _ in attempts] != list(range(1, len(attempts) + 1)):
    raise SystemExit("UPDATE_ATTEMPT_SEQUENCE_INVALID")
current = [item for item in attempts if item[2] in recoverable_states]
if len(current) > 1 or (current and current[0] != attempts[-1]):
    raise SystemExit("CURRENT_ATTEMPT_INVALID")
value = (
    {"path": str(current[0][1]), "state": current[0][2]}
    if current
    else {}
)
print(json.dumps(value, separators=(",", ":"), sort_keys=True))
PY
)" || fail "RECOVERY_TRANSACTION_INVALID"
recovery_state="$(python3 - "${recovery}" <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
print(value.get("state", ""))
PY
)" || fail "RECOVERY_TRANSACTION_INVALID"
if [[ -n "${recovery_state}" ]]; then
  transaction="$(python3 - "${recovery}" <<'PY'
import json
import sys

print(json.loads(sys.argv[1])["path"])
PY
)" || fail "RECOVERY_TRANSACTION_INVALID"
  if [[ "${recovery_state}" == "rollback_failed" \
    || "${recovery_state}" == "rolling_back" ]]; then
    fail "RECOVERY_REQUIRES_MANUAL_INTERVENTION"
  fi
  if [[ "${recovery_state}" == "prechecking" \
    && ! -e "${transaction}/activation-intent.json" \
    && ! -L "${transaction}/activation-intent.json" ]]; then
    if [[ "${current_revision}" != "${old_revision}" \
      || ! -f "${transaction}/old-rag-industry.env" ]] \
      || ! cmp -s -- "${transaction}/old-rag-industry.env" "${env_file}" \
      || ! verify_industry_app_identity "${env_file}" true; then
      write_recovery_failure "${transaction}" \
        "PRECHECKING_SOURCE_IDENTITY_UNKNOWN"
    fi
    write_transaction_state \
      "${transaction}/transaction-state.json" precheck_failed \
      activation_recovery PRECHECKING_INTERRUPTED_BEFORE_ACTIVATION \
      || fail "PRECHECKING_RECOVERY_STATE_WRITE_FAILED"
    recovery_state=""
  elif [[ "${recovery_state}" =~ ^(prechecking|activating|activated)$ ]]; then
    validate_activation_intent "${transaction}" \
      || write_recovery_failure "${transaction}" "ACTIVATION_INTENT_INVALID"
    if [[ "${recovery_state}" == "prechecking" ]]; then
      write_transaction_state \
        "${transaction}/transaction-state.json" activating \
        || write_recovery_failure \
          "${transaction}" "ACTIVATING_RECOVERY_STATE_WRITE_FAILED"
      recovery_state=activating
    fi
    env_identity="$(activation_env_identity "${transaction}")" \
      || write_recovery_failure \
        "${transaction}" "ACTIVATION_ENV_IDENTITY_INVALID"
    app_identity="$(classify_industry_app_identity \
      "${transaction}/old-rag-industry.env" \
      "${transaction}/candidate-rag-industry.env")"
    needs_recreate=false
    if [[ "${env_identity}" == "source" \
      && "${app_identity}" == "source" ]]; then
      write_transaction_state \
        "${transaction}/transaction-state.json" activating \
        || write_recovery_failure \
          "${transaction}" "ACTIVATING_RECOVERY_STATE_WRITE_FAILED"
      replace_industry_private_env \
        "${transaction}/candidate-rag-industry.env" "${env_file}" \
        || rollback_recovery_attempt \
          "${transaction}" "ACTIVATING_ENV_SWAP_FAILED"
      [[ "$(activation_env_identity "${transaction}")" == "target" ]] \
        || rollback_recovery_attempt \
          "${transaction}" "ACTIVATING_ENV_VERIFY_FAILED"
      write_transaction_state \
        "${transaction}/transaction-state.json" activated \
        || rollback_recovery_attempt \
          "${transaction}" "ACTIVATED_RECOVERY_STATE_WRITE_FAILED"
      needs_recreate=true
    elif [[ "${env_identity}" == "target" \
      && "${app_identity}" == "source" ]]; then
      write_transaction_state \
        "${transaction}/transaction-state.json" activated \
        || rollback_recovery_attempt \
          "${transaction}" "ACTIVATED_RECOVERY_STATE_WRITE_FAILED"
      needs_recreate=true
    elif [[ "${env_identity}" == "target" \
      && "${app_identity}" == "target" ]]; then
      needs_recreate=false
    elif [[ "${env_identity}" == "target" \
      && "${app_identity}" == "target_unhealthy" ]]; then
      write_transaction_state \
        "${transaction}/transaction-state.json" activated \
        || rollback_recovery_attempt \
          "${transaction}" "ACTIVATED_RECOVERY_STATE_WRITE_FAILED"
      needs_recreate=true
    elif [[ "${env_identity}" == "source" \
      && "${app_identity}" == "target" ]]; then
      rollback_recovery_attempt \
        "${transaction}" "ACTIVATION_MIXED_IDENTITY_ROLLED_BACK"
    else
      write_recovery_failure "${transaction}" \
        "ACTIVATION_IDENTITY_UNKNOWN"
    fi
    if [[ "${needs_recreate}" == "true" ]]; then
      run_industry_compose "${env_file}" "${runtime_dir}/compose.yaml" \
        up -d --no-deps --no-build --pull never --force-recreate \
        rag-industry-app \
        || rollback_recovery_attempt \
          "${transaction}" "ACTIVATION_RECOVERY_RECREATE_FAILED"
      wait_industry_health rag-industry-app 180 \
        || rollback_recovery_attempt \
          "${transaction}" "ACTIVATION_RECOVERY_APP_UNHEALTHY"
      verify_industry_app_identity "${env_file}" true \
        || rollback_recovery_attempt \
          "${transaction}" "ACTIVATION_RECOVERY_APP_IDENTITY_INVALID"
    fi
    write_transaction_state \
      "${transaction}/transaction-state.json" verifying \
      || rollback_recovery_attempt \
        "${transaction}" "VERIFYING_RECOVERY_STATE_WRITE_FAILED"
    complete_recovered_target "${transaction}" verifying
  else
    [[ "${current_revision}" == "${target_revision}" \
      && "${current_compose}" == "${runtime_dir}/compose.yaml" ]] \
      || write_recovery_failure \
        "${transaction}" "RECOVERY_ENV_IDENTITY_INVALID"
    validate_industry_compose "${env_file}" "${runtime_dir}/compose.yaml" \
      || write_recovery_failure "${transaction}" "RECOVERY_COMPOSE_INVALID"
    verify_industry_app_identity "${env_file}" true \
      || write_recovery_failure \
        "${transaction}" "RECOVERY_APP_IDENTITY_INVALID"
    complete_recovered_target "${transaction}" "${recovery_state}"
  fi
fi
transaction="$(python3 - "${update_root}" "${update_id}" <<'PY'
import json
import pathlib
import re
import stat
import sys

root = pathlib.Path(sys.argv[1])
attempts = []
allowed_states = {
    "activated",
    "activating",
    "precheck_failed",
    "prechecking",
    "prepared",
    "rollback_failed",
    "rolled_back",
    "rolling_back",
    "validated",
    "verified",
    "verifying",
}
for path in root.iterdir():
    match = re.fullmatch(r"attempt-([0-9]{4})", path.name)
    if not path.is_dir() or path.is_symlink() or match is None:
        raise SystemExit("UPDATE_ATTEMPT_ENTRY_INVALID")
    state_path = path / "transaction-state.json"
    if (
        not state_path.is_file()
        or state_path.is_symlink()
        or stat.S_IMODE(state_path.stat().st_mode) != 0o600
    ):
        raise SystemExit("UPDATE_ATTEMPT_STATE_MISSING")
    value = json.loads(state_path.read_bytes())
    attempt_number = int(match.group(1))
    if (
        set(value)
        != {
            "attempt",
            "error_code",
            "failure_stage",
            "schema_version",
            "state",
            "update_id",
            "updated_at",
        }
        or value.get("attempt") != attempt_number
        or value.get("schema_version") != "2"
        or value.get("state") not in allowed_states
        or value.get("update_id") != sys.argv[2]
        or not isinstance(value.get("updated_at"), str)
        or (
            value.get("state")
            in {"precheck_failed", "rollback_failed", "rolled_back"}
            and (
                not isinstance(value.get("failure_stage"), str)
                or not isinstance(value.get("error_code"), str)
            )
        )
        or (
            value.get("state")
            not in {"precheck_failed", "rollback_failed", "rolled_back"}
            and (
                value.get("failure_stage") is not None
                or value.get("error_code") is not None
            )
        )
    ):
        raise SystemExit("UPDATE_ATTEMPT_ID_INVALID")
    attempts.append((attempt_number, str(value.get("state"))))
attempts.sort()
if [number for number, _ in attempts] != list(
    range(1, len(attempts) + 1)
):
    raise SystemExit("UPDATE_ATTEMPT_SEQUENCE_INVALID")
if attempts:
    last_state = attempts[-1][1]
    if last_state == "rollback_failed":
        raise SystemExit("UPDATE_ROLLBACK_FAILED_REQUIRES_INTERVENTION")
    if last_state not in {"precheck_failed", "rolled_back"}:
        raise SystemExit("UPDATE_ATTEMPT_NOT_RETRYABLE")
next_attempt = len(attempts) + 1
print(root / f"attempt-{next_attempt:04d}")
PY
)" || fail "UPDATE_TRANSACTION_NOT_RETRYABLE"
mkdir -m 700 -- "${transaction}"
write_transaction_state "${transaction}/transaction-state.json" prepared \
  || fail "TRANSACTION_STATE_WRITE_FAILED"
activated=false
validated_checkpoint=false
transaction_terminal=false
failure_stage="transaction_prepare"
failure_code="TRANSACTION_PREPARE_FAILED"
transaction_exit() {
  local exit_code="$?"
  trap - EXIT
  set +e
  if [[ "${exit_code}" -ne 0 && "${transaction_terminal}" != "true" ]]; then
    if [[ "${validated_checkpoint}" == "true" ]]; then
      printf '%s\n' \
        'RAG_INDUSTRY_SERVING_UPDATE_VALIDATED_RECOVERY_REQUIRED' >&2
    elif [[ "${activated}" == "true" ]]; then
      if ! bash "${runtime_dir}/rollback-app-update-core.sh" \
        --automatic-failure "${env_file}" "${transaction}"; then
        if ! write_transaction_state \
          "${transaction}/transaction-state.json" rollback_failed \
          rollback ROLLBACK_FAILED; then
          printf '%s\n' \
            'RAG_INDUSTRY_SERVING_UPDATE_ROLLBACK_STATE_WRITE_FAILED' >&2
        fi
        cleanup
        exit 70
      fi
      if ! write_transaction_state \
        "${transaction}/transaction-state.json" rolled_back \
        "${failure_stage}" "${failure_code}"; then
        printf '%s\n' \
          'RAG_INDUSTRY_SERVING_UPDATE_ROLLBACK_STATE_WRITE_FAILED' >&2
        cleanup
        exit 70
      fi
      printf 'RAG_INDUSTRY_SERVING_UPDATE_ROLLED_BACK\n' >&2
    else
      if ! write_transaction_state \
        "${transaction}/transaction-state.json" precheck_failed \
        "${failure_stage}" "${failure_code}"; then
        printf '%s\n' \
          'RAG_INDUSTRY_SERVING_UPDATE_PRECHECK_STATE_WRITE_FAILED' >&2
        cleanup
        exit 70
      fi
      printf 'RAG_INDUSTRY_SERVING_UPDATE_PRECHECK_FAILED stage=%s code=%s\n' \
        "${failure_stage}" "${failure_code}" >&2
    fi
  fi
  cleanup
  exit "${exit_code}"
}
trap transaction_exit EXIT
write_transaction_state "${transaction}/transaction-state.json" prechecking \
  || fail "TRANSACTION_STATE_WRITE_FAILED"
cp -- "${env_file}" "${transaction}/old-rag-industry.env"
chmod 600 "${transaction}/old-rag-industry.env"
cp -- "${package_dir}/UPDATE_MANIFEST.json" \
  "${transaction}/UPDATE_MANIFEST.json"
chmod 600 "${transaction}/UPDATE_MANIFEST.json"

python3 - "${package_dir}/UPDATE_MANIFEST.json" \
  "${transaction}/target-contract.json" <<'PY'
import json
import os
import pathlib
import sys

source = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
target = {
    "index_fingerprint": source["index_fingerprint"]["target"],
    "revision": source["revision"],
    "serving_fingerprint": source["serving_fingerprint"]["target"],
    "trace": source["trace"],
    "ui": source["ui"],
}
path = pathlib.Path(sys.argv[2])
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    json.dump(target, output, separators=(",", ":"), sort_keys=True)
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())
PY

python3 - "${transaction}/container-identity.json" <<'PY'
import json
import os
import pathlib
import subprocess
import sys

value = {}
for name in (
    "rag-industry-app",
    "rag-industry-ocr",
    "rag-industry-qdrant",
    "rag-industry-worker",
):
    result = subprocess.run(
        [
            "docker",
            "container",
            "inspect",
            "--format",
            "{{.Id}}|{{.State.StartedAt}}",
            name,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    value[name] = result.stdout.strip() if result.returncode == 0 else None
path = pathlib.Path(sys.argv[1])
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    json.dump(value, output, separators=(",", ":"), sort_keys=True)
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())
PY

failure_stage="last_good_precheck"
failure_code="LAST_GOOD_PRECHECK_FAILED"
python3 "${runtime_dir}/last_good.py" inspect "${backup_path}" \
  >"${transaction}/pre-last-good.json" \
  || fail "LAST_GOOD_INSPECTION_FAILED"
chmod 600 "${transaction}/pre-last-good.json"

failure_stage="config_filesystem_precheck"
failure_code="PRE_UPDATE_FILESYSTEM_IDENTITY_FAILED"
pre_filesystem_json="$(run_industry_compose "${env_file}" "${old_compose}" \
  run --rm --no-deps --entrypoint python \
  --volume "${runtime_dir}/runtime_check.py:/update/runtime_check.py:ro" \
  rag-industry-app /update/runtime_check.py pre-update-filesystem-state \
  /config /state/traces.sqlite3 "${source_config_profile}")" \
  || fail "PRE_UPDATE_FILESYSTEM_IDENTITY_FAILED"
python3 - "${pre_filesystem_json}" \
  "${transaction}/pre-filesystem.json" \
  "${package_dir}/UPDATE_MANIFEST.json" "${source_config_profile}" <<'PY'
import json
import os
import pathlib
import re
import sys

value = json.loads(sys.argv[1])
manifest = json.loads(pathlib.Path(sys.argv[3]).read_bytes())
config = value.get("config") if isinstance(value, dict) else None
files = config.get("files") if isinstance(config, dict) else None
trace = value.get("trace") if isinstance(value, dict) else None
source = manifest.get("source_compatibility")
expected_files = source.get("config_files") if isinstance(source, dict) else None
trace_compatibility = (
    source.get("trace_compatibility") if isinstance(source, dict) else None
)
expected = {
    "corpus-policy.json",
    "intent-router-calibration.json",
    "intent-router.json",
    "pipeline.json",
    "retrieval.json",
}
if (
    not isinstance(files, dict)
    or set(files) != expected
    or any(
        not isinstance(identity, dict)
        or set(identity) != {"gid", "mode", "sha256", "uid"}
        or not isinstance(identity.get("uid"), int)
        or isinstance(identity.get("uid"), bool)
        or not isinstance(identity.get("gid"), int)
        or isinstance(identity.get("gid"), bool)
        or identity.get("mode")
        != {
            "first-deploy-private-v1": "0600",
            "serving-runtime-public-config-v1": "0644",
        }.get(sys.argv[4])
        or not isinstance(identity.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", identity["sha256"]) is None
        for identity in files.values()
    )
    or config.get("profile") != sys.argv[4]
    or not isinstance(expected_files, dict)
    or any(
        files[name]["sha256"] != expected_files.get(name)
        for name in expected
    )
    or not isinstance(trace, dict)
    or set(trace)
    != {
        "filename",
        "has_question_columns",
        "mode",
        "quick_check",
        "schema_profile",
        "sqlite_user_version",
        "trace_count",
    }
    or trace.get("filename") != "traces.sqlite3"
    or trace.get("mode") != "0600"
    or trace.get("quick_check") != "ok"
    or not isinstance(trace.get("trace_count"), int)
    or isinstance(trace.get("trace_count"), bool)
    or trace["trace_count"] < 0
    or not isinstance(trace_compatibility, dict)
    or trace.get("sqlite_user_version")
    not in trace_compatibility.get("accepted_user_versions", [])
    or (
        trace.get("sqlite_user_version") == 0
        and (
            trace.get("schema_profile")
            != trace_compatibility.get("legacy_v0_profile")
            or trace.get("has_question_columns") is not False
        )
    )
    or (
        trace.get("sqlite_user_version") == 1
        and trace.get("schema_profile") != "trace-v1"
    )
    or (
        trace.get("sqlite_user_version") == 2
        and (
            trace.get("schema_profile") != "trace-v2"
            or trace.get("has_question_columns") is not True
        )
    )
):
    raise SystemExit("PRE_UPDATE_FILESYSTEM_IDENTITY_INVALID")
path = pathlib.Path(sys.argv[2])
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    json.dump(value, output, separators=(",", ":"), sort_keys=True)
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())
PY

failure_stage="source_runtime_identity"
failure_code="PRE_UPDATE_SOURCE_IDENTITY_FAILED"
python3 - "${env_file}" "${old_compose}" \
  "${transaction}/pre-filesystem.json" \
  "${transaction}/pre-update-snapshot.json" "${old_image}" \
  "${old_revision}" "${backup_path}" <<'PY'
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys

env_path = pathlib.Path(sys.argv[1])
compose_path = pathlib.Path(sys.argv[2])
filesystem_path = pathlib.Path(sys.argv[3])
output_path = pathlib.Path(sys.argv[4])
old_image = sys.argv[5]
old_revision = sys.argv[6]
backup_path = pathlib.Path(sys.argv[7])
revision_pattern = re.compile(r"[0-9a-f]{40}")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(*arguments):
    completed = subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def exact_env():
    values = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise SystemExit("PRIVATE_ENV_DUPLICATE_KEY")
        values[key] = value.strip("\"'")
    return values


if (
    not env_path.is_file()
    or env_path.is_symlink()
    or stat.S_IMODE(env_path.stat().st_mode) != 0o600
    or not compose_path.is_file()
    or compose_path.is_symlink()
    or not filesystem_path.is_file()
    or filesystem_path.is_symlink()
    or revision_pattern.fullmatch(old_revision) is None
):
    raise SystemExit("PRE_UPDATE_FILE_IDENTITY_INVALID")
filesystem = json.loads(filesystem_path.read_bytes())
image_id = run("docker", "image", "inspect", "--format", "{{.Id}}", old_image)
image_revision = run(
    "docker",
    "image",
    "inspect",
    "--format",
    '{{index .Config.Labels "org.opencontainers.image.revision"}}',
    old_image,
)
if (
    re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
    or image_revision != old_revision
):
    raise SystemExit("PRE_UPDATE_IMAGE_IDENTITY_INVALID")
build_info = json.loads(
    run(
        "docker",
        "exec",
        "rag-industry-app",
        "rag-app",
        "build-info",
        "--expected-revision",
        old_revision,
    )
)
if build_info != {
    "expected_revision": old_revision,
    "installed_revision": old_revision,
    "matches": True,
}:
    raise SystemExit("PRE_UPDATE_WHEEL_IDENTITY_INVALID")
mounts = json.loads(
    run(
        "docker",
        "container",
        "inspect",
        "--format",
        "{{json .Mounts}}",
        "rag-industry-app",
    )
)
ports = json.loads(
    run(
        "docker",
        "container",
        "inspect",
        "--format",
        "{{json .NetworkSettings.Ports}}",
        "rag-industry-app",
    )
)
if not isinstance(mounts, list) or not isinstance(ports, dict):
    raise SystemExit("PRE_UPDATE_CONTAINER_JSON_INVALID")
last_good_pointer = backup_path / "last-good-pointer.json"
last_good = None
if last_good_pointer.exists():
    if not last_good_pointer.is_file() or last_good_pointer.is_symlink():
        raise SystemExit("PRE_UPDATE_LAST_GOOD_INVALID")
    last_good = {
        "pointer": json.loads(last_good_pointer.read_bytes()),
        "pointer_sha256": sha256(last_good_pointer),
    }
env = exact_env()
payload = {
    "app": {
        "build_info": build_info,
        "container_id": run(
            "docker", "container", "inspect", "--format", "{{.Id}}",
            "rag-industry-app",
        ),
        "image_id": image_id,
        "image_ref": old_image,
        "mounts": mounts,
        "oci_revision": image_revision,
        "ports": ports,
        "started_at": run(
            "docker", "container", "inspect", "--format",
            "{{.State.StartedAt}}", "rag-industry-app",
        ),
    },
    "compose": {"path": str(compose_path), "sha256": sha256(compose_path)},
    "config": filesystem["config"],
    "last_good": last_good,
    "private_env": {
        "mode": "0600",
        "sha256": sha256(env_path),
    },
    "release_revision": old_revision,
    "schema_version": "1",
    "serving_modes": {
        "trace_question_capture": env.get(
            "RAG_TRACE_QUESTION_CAPTURE", "hash_only"
        ),
        "trace_question_retention_seconds": env.get(
            "RAG_TRACE_QUESTION_RETENTION_SECONDS"
        ),
        "ui_allow_insecure_http": env.get(
            "RAG_UI_ALLOW_INSECURE_HTTP", "false"
        ),
        "ui_cookie_secure": env.get("RAG_UI_COOKIE_SECURE", "true"),
        "ui_query_auth_mode": env.get(
            "RAG_UI_QUERY_AUTH_MODE", "browser_bearer"
        ),
        "ui_session_ttl_seconds": env.get("RAG_UI_SESSION_TTL_SECONDS"),
    },
    "trace": filesystem["trace"],
}
descriptor = os.open(
    output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    json.dump(payload, output, separators=(",", ":"), sort_keys=True)
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())
PY

failure_stage="index_identity_precheck"
failure_code="PRE_UPDATE_INDEX_IDENTITY_FAILED"
pre_index_json="$(run_industry_compose "${env_file}" "${old_compose}" \
  run --rm --no-deps --entrypoint python \
  --volume "${runtime_dir}/runtime_check.py:/update/runtime_check.py:ro" \
  rag-industry-app /update/runtime_check.py pre-update-index-state)" \
  || fail "PRE_UPDATE_INDEX_IDENTITY_FAILED"
python3 - "${pre_index_json}" "${transaction}/pre-index.json" \
  "${target_index}" "${old_revision}" <<'PY'
import json
import os
import pathlib
import re
import sys

value = json.loads(sys.argv[1])
required = {
    "active_collection",
    "alias",
    "index_fingerprint",
    "manifest_sha256",
    "payload_schema",
    "point_count",
    "release_revision",
    "source_count",
}
if not isinstance(value, dict) or set(value) != required:
    raise SystemExit("PRE_UPDATE_INDEX_FIELDS_INVALID")
if (
    value.get("index_fingerprint") != sys.argv[3]
    or value.get("release_revision") != sys.argv[4]
    or value.get("alias") != "rag-industry-active"
    or not isinstance(value.get("point_count"), int)
    or isinstance(value.get("point_count"), bool)
    or value["point_count"] <= 0
    or re.fullmatch(r"[0-9a-f]{64}", str(value.get("manifest_sha256"))) is None
):
    raise SystemExit("PRE_UPDATE_INDEX_IDENTITY_INVALID")
path = pathlib.Path(sys.argv[2])
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    json.dump(value, output, separators=(",", ":"), sort_keys=True)
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())
PY

failure_stage="source_checkpoint"
failure_code="SOURCE_CHECKPOINT_FAILED"
validate_industry_compose "${env_file}" "${old_compose}" \
  || fail "SOURCE_COMPOSE_CANONICAL_INVALID"
verify_industry_app_identity "${env_file}" true \
  || fail "SOURCE_APP_IDENTITY_INVALID"
source_port="$(exact_env_value "${env_file}" RAG_PORT)"
wait_industry_http "http://127.0.0.1:${source_port}/live" 60 \
  || fail "SOURCE_LIVE_FAILED"
wait_industry_http "http://127.0.0.1:${source_port}/ready" 60 \
  || fail "SOURCE_READY_FAILED"
python3 - "${transaction}/pre-update-snapshot.json" \
  "${transaction}/pre-index.json" \
  "${transaction}/pre-update-source-state.json" "${old_revision}" <<'PY'
import json
import os
import pathlib
import sys

snapshot = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
index = json.loads(pathlib.Path(sys.argv[2]).read_bytes())
app = snapshot.get("app") if isinstance(snapshot, dict) else None
config = snapshot.get("config") if isinstance(snapshot, dict) else None
private_env = (
    snapshot.get("private_env") if isinstance(snapshot, dict) else None
)
compose = snapshot.get("compose") if isinstance(snapshot, dict) else None
if not all(
    isinstance(value, dict)
    for value in (app, config, private_env, compose, index)
):
    raise SystemExit("SOURCE_CHECKPOINT_INPUT_INVALID")
created_at = app.get("started_at")
if not isinstance(created_at, str) or not created_at:
    raise SystemExit("SOURCE_CHECKPOINT_CREATED_AT_INVALID")
value = {
    "app": app,
    "compose": compose,
    "config": config,
    "created_at": created_at,
    "index": index,
    "private_env": private_env,
    "revision": sys.argv[4],
    "schema_version": "1",
    "stage": "last_good",
    "update_kind": "pre_update_source_checkpoint",
}
path = pathlib.Path(sys.argv[3])
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
python3 "${runtime_dir}/last_good.py" checkpoint-source \
  "${backup_path}" "${env_file}" \
  "${transaction}/pre-update-source-state.json" "${old_revision}" \
  "${transaction}/pre-last-good.json" \
  "${transaction}/UPDATE_MANIFEST.json" \
  >"${transaction}/source-checkpoint.json" \
  || fail "SOURCE_CHECKPOINT_FAILED"
chmod 600 "${transaction}/source-checkpoint.json"

failure_stage="trace_backup"
failure_code="TRACE_BACKUP_FAILED"
trace_backup="${transaction}/traces-before.sqlite3"
trace_report="$(run_industry_compose "${env_file}" "${old_compose}" \
  run --rm --no-deps --user 0:0 \
  --cap-add DAC_OVERRIDE --cap-add CHOWN --entrypoint python \
  --volume "${runtime_dir}/runtime_check.py:/update/runtime_check.py:ro" \
  --volume "${transaction}:/update-backup" \
  --env "RAG_UPDATE_OWNER_UID=$(id -u)" \
  --env "RAG_UPDATE_OWNER_GID=$(id -g)" \
  rag-industry-app /update/runtime_check.py backup-trace-database \
  /state/traces.sqlite3 /update-backup/traces-before.sqlite3 \
  "${target_revision}")" \
  || fail "TRACE_BACKUP_FAILED"
python3 - "${trace_report}" "${transaction}/trace-backup.json" \
  "${target_revision}" "${transaction}/pre-filesystem.json" <<'PY'
import json
import os
import pathlib
import re
import sys

value = json.loads(sys.argv[1])
pre_filesystem = json.loads(pathlib.Path(sys.argv[4]).read_bytes())
pre_trace = pre_filesystem.get("trace")
expected_fields = {
    "backup_filename",
    "bytes",
    "created_at",
    "mode",
    "owner",
    "page_count",
    "schema_version",
    "sha256",
    "source_changed_during_backup",
    "source_database_identity",
    "source_database_observation",
    "source_filename",
    "sqlite_user_version",
    "target_revision",
    "trace_count",
}
stable = value.get("source_database_identity")
observation = value.get("source_database_observation")
before = observation.get("before") if isinstance(observation, dict) else None
after = observation.get("after") if isinstance(observation, dict) else None
if (
    not isinstance(value, dict)
    or set(value) != expected_fields
    or value.get("schema_version") != "2"
    or value.get("backup_filename") != "traces-before.sqlite3"
    or value.get("source_filename") != "traces.sqlite3"
    or value.get("mode") != "0600"
    or value.get("target_revision") != sys.argv[3]
    or value.get("owner")
    != {"uid": os.getuid(), "gid": os.getgid()}
    or not isinstance(value.get("page_count"), int)
    or isinstance(value.get("page_count"), bool)
    or value["page_count"] <= 0
    or not isinstance(value.get("bytes"), int)
    or isinstance(value.get("bytes"), bool)
    or value["bytes"] <= 0
    or value.get("sqlite_user_version") not in {0, 1, 2}
    or not isinstance(value.get("trace_count"), int)
    or isinstance(value.get("trace_count"), bool)
    or value["trace_count"] < 0
    or not isinstance(pre_trace, dict)
    or not isinstance(pre_trace.get("trace_count"), int)
    or isinstance(pre_trace.get("trace_count"), bool)
    or value["trace_count"] < pre_trace["trace_count"]
    or not isinstance(value.get("source_changed_during_backup"), bool)
    or not isinstance(stable, dict)
    or set(stable)
    != {"device", "file_type", "gid", "inode", "mode", "uid"}
    or stable.get("file_type") != "regular"
    or stable.get("mode") != "0600"
    or any(
        not isinstance(stable.get(key), int)
        or isinstance(stable.get(key), bool)
        for key in ("device", "gid", "inode", "uid")
    )
    or not isinstance(observation, dict)
    or set(observation) != {"after", "before"}
    or any(
        not isinstance(item, dict)
        or set(item) != {"bytes", "mtime_ns", "wal_bytes"}
        or not isinstance(item.get("bytes"), int)
        or isinstance(item.get("bytes"), bool)
        or not isinstance(item.get("mtime_ns"), int)
        or isinstance(item.get("mtime_ns"), bool)
        or (
            item.get("wal_bytes") is not None
            and (
                not isinstance(item.get("wal_bytes"), int)
                or isinstance(item.get("wal_bytes"), bool)
            )
        )
        for item in (before, after)
    )
    or re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256"))) is None
):
    raise SystemExit("TRACE_BACKUP_IDENTITY_INVALID")
path = pathlib.Path(sys.argv[2])
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    json.dump(value, output, separators=(",", ":"), sort_keys=True)
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())
PY

failure_stage="candidate_environment"
failure_code="CANDIDATE_ENV_INVALID"
candidate_env="${transaction}/candidate-rag-industry.env"
python3 - "${env_file}" "${candidate_env}" \
  "RAG_APP_IMAGE=${target_image}" \
  "RAG_RELEASE_REVISION=${target_revision}" \
  "RAG_INDUSTRY_COMPOSE_FILE=${runtime_dir}/compose.yaml" \
  "RAG_CONFIG_PATH=${runtime_dir}/config" \
  "RAG_TRACE_QUESTION_CAPTURE=plaintext" \
  "RAG_TRACE_QUESTION_RETENTION_SECONDS=604800" \
  "RAG_UI_QUERY_AUTH_MODE=same_origin_session" \
  "RAG_UI_COOKIE_SECURE=false" \
  "RAG_UI_ALLOW_INSECURE_HTTP=true" \
  "RAG_UI_SESSION_TTL_SECONDS=1800" <<'PY'
import os
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
updates = dict(item.split("=", 1) for item in sys.argv[3:])
lines = source.read_text(encoding="utf-8").splitlines()
seen = {key: 0 for key in updates}
output = []
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else None
    if key in updates:
        seen[key] += 1
        output.append(f"{key}={updates[key]}")
    else:
        output.append(line)
if any(count > 1 for count in seen.values()):
    raise SystemExit("CANDIDATE_ENV_DUPLICATE_KEY")
for key, count in seen.items():
    if count == 0:
        output.append(f"{key}={updates[key]}")
descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
    stream.write("\n".join(output) + "\n")
    stream.flush()
    os.fsync(stream.fileno())
PY

python3 - "${env_file}" "${candidate_env}" \
  "${target_image}" "${target_revision}" \
  "${runtime_dir}/compose.yaml" "${runtime_dir}/config" <<'PY'
import pathlib
import stat
import sys


def parse(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if key in values:
            raise SystemExit("CANDIDATE_ENV_DUPLICATE_KEY")
        values[key] = value.strip("\"'")
    return values


old_path, new_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
if stat.S_IMODE(new_path.stat().st_mode) != 0o600:
    raise SystemExit("CANDIDATE_ENV_MODE_INVALID")
old, new = parse(old_path), parse(new_path)
expected = {
    "RAG_APP_IMAGE": sys.argv[3],
    "RAG_RELEASE_REVISION": sys.argv[4],
    "RAG_INDUSTRY_COMPOSE_FILE": sys.argv[5],
    "RAG_CONFIG_PATH": sys.argv[6],
    "RAG_TRACE_QUESTION_CAPTURE": "plaintext",
    "RAG_TRACE_QUESTION_RETENTION_SECONDS": "604800",
    "RAG_UI_QUERY_AUTH_MODE": "same_origin_session",
    "RAG_UI_COOKIE_SECURE": "false",
    "RAG_UI_ALLOW_INSECURE_HTTP": "true",
    "RAG_UI_SESSION_TTL_SECONDS": "1800",
}
if any(new.get(key) != value for key, value in expected.items()):
    raise SystemExit("CANDIDATE_ENV_TARGET_INVALID")
for key in set(old) | set(new):
    if key not in expected and old.get(key) != new.get(key):
        raise SystemExit("CANDIDATE_ENV_IMMUTABLE_FIELD_CHANGED")
if set(new) != set(old) | (set(expected) - set(old)):
    raise SystemExit("CANDIDATE_ENV_EXACT_KEYS_INVALID")
PY

failure_stage="compose_contract"
failure_code="COMPOSE_CONTRACT_CHANGED"
validate_industry_compose "${candidate_env}" "${runtime_dir}/compose.yaml" \
  || fail "CANDIDATE_COMPOSE_INVALID"
old_config_json="$(mktemp "${transaction}/.old-compose.XXXXXX")"
new_config_json="$(mktemp "${transaction}/.new-compose.XXXXXX")"
chmod 600 "${old_config_json}" "${new_config_json}"
run_industry_compose "${env_file}" "${old_compose}" \
  --profile index --profile dedicated-ocr config --format json \
  >"${old_config_json}" || fail "OLD_COMPOSE_RENDER_FAILED"
run_industry_compose "${candidate_env}" "${runtime_dir}/compose.yaml" \
  --profile index --profile dedicated-ocr config --format json \
  >"${new_config_json}" || fail "NEW_COMPOSE_RENDER_FAILED"
python3 "${runtime_dir}/compose_check.py" \
  "${old_config_json}" "${new_config_json}" \
  "${old_config}" "${old_image}" "${old_revision}" \
  "${runtime_dir}/config" "${target_image}" "${target_revision}" \
  >"${transaction}/compose-contract.json" \
  || fail "COMPOSE_CONTRACT_CHANGED"
chmod 600 "${transaction}/compose-contract.json"
rm -f -- "${old_config_json}" "${new_config_json}"
old_config_json=""
new_config_json=""

failure_stage="image_load"
failure_code="APP_IMAGE_LOAD_FAILED"
gzip -dc -- "${package_dir}/app-image.tar.gz" | docker image load >/dev/null \
  || fail "APP_IMAGE_LOAD_FAILED"
failure_stage="image_identity"
failure_code="APP_IMAGE_IDENTITY_FAILED"
actual_image_id="$(docker image inspect --format '{{.Id}}' "${target_image}")"
actual_platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' \
  "${target_image}")"
actual_revision="$(docker image inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "${target_image}")"
actual_entrypoint="$(docker image inspect --format \
  '{{json .Config.Entrypoint}}' "${target_image}")"
python3 - "${actual_image_id}" "${actual_platform}" \
  "${actual_revision}" "${actual_entrypoint}" "${target_image_id}" \
  "${target_platform}" "${target_revision}" <<'PY'
import json
import re
import sys

actual_id, actual_platform, actual_revision = sys.argv[1:4]
entrypoint = json.loads(sys.argv[4])
expected_id, expected_platform, expected_revision = sys.argv[5:8]
if (
    actual_id != expected_id
    or re.fullmatch(r"sha256:[0-9a-f]{64}", actual_id) is None
    or actual_platform != expected_platform
    or actual_platform != "linux/amd64"
    or actual_revision != expected_revision
    or entrypoint != ["rag-app"]
):
    raise SystemExit("APP_IMAGE_IDENTITY_MISMATCH")
PY
failure_stage="asset_selfcheck"
failure_code="IMAGE_ASSET_SELFCHECK_FAILED"
build_report="$(docker run --rm --network none "${target_image}" \
  build-info --expected-revision "${target_revision}")" \
  || fail "IMAGE_BUILD_INFO_FAILED"
python3 - "${build_report}" "${target_revision}" \
  "${transaction}/target-image-identity.json" "${target_image}" \
  "${actual_image_id}" "${actual_platform}" "${actual_revision}" \
  "${actual_entrypoint}" <<'PY'
import json
import os
import pathlib
import sys

value = json.loads(sys.argv[1])
expected = {
    "expected_revision": sys.argv[2],
    "installed_revision": sys.argv[2],
    "matches": True,
}
if value != expected:
    raise SystemExit("IMAGE_BUILD_INFO_INVALID")
entrypoint = json.loads(sys.argv[8])
if entrypoint != ["rag-app"]:
    raise SystemExit("IMAGE_ENTRYPOINT_INVALID")
identity = {
    "build_info": value,
    "entrypoint": entrypoint,
    "image_id": sys.argv[5],
    "image_ref": sys.argv[4],
    "oci_revision": sys.argv[7],
    "platform": sys.argv[6],
    "revision": sys.argv[2],
    "schema_version": "1",
}
path = pathlib.Path(sys.argv[3])
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    json.dump(identity, output, separators=(",", ":"), sort_keys=True)
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())
directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
asset_report="$(docker run --rm --network none "${target_image}" \
  asset-selfcheck)" || fail "IMAGE_ASSET_SELFCHECK_FAILED"
python3 - "${asset_report}" "${target_index}" <<'PY'
import json
import re
import sys

value = json.loads(sys.argv[1])
fingerprint = value.get("pipeline_fingerprint")
if (
    not isinstance(fingerprint, str)
    or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
    or fingerprint != sys.argv[2]
):
    raise SystemExit("IMAGE_INDEX_FINGERPRINT_MISMATCH")
PY

failure_stage="activation"
failure_code="TARGET_ACTIVATION_FAILED"
write_activation_intent "${transaction}" \
  || fail "ACTIVATION_INTENT_WRITE_FAILED"
activated=true
write_transaction_state "${transaction}/transaction-state.json" activating \
  || fail "TRANSACTION_STATE_WRITE_FAILED"
replace_industry_private_env "${candidate_env}" "${env_file}" \
  || fail "TARGET_ENV_ACTIVATION_FAILED"
[[ "$(activation_env_identity "${transaction}")" == "target" ]] \
  || fail "TARGET_ENV_ACTIVATION_IDENTITY_FAILED"
write_transaction_state "${transaction}/transaction-state.json" activated

run_industry_compose "${env_file}" "${runtime_dir}/compose.yaml" \
  up -d --no-deps --no-build --pull never --force-recreate \
  rag-industry-app
wait_industry_health rag-industry-app 180
write_transaction_state "${transaction}/transaction-state.json" verifying
failure_stage="verification"
failure_code="TARGET_VERIFICATION_FAILED"
bash "${runtime_dir}/verify-app-update.sh" "${env_file}" "${transaction}"
write_transaction_state "${transaction}/transaction-state.json" validated \
  || fail "TRANSACTION_STATE_WRITE_FAILED"
validated_checkpoint=true
failure_stage="last_good_promotion"
failure_code="LAST_GOOD_PROMOTION_FAILED"
validate_target_runtime_checkpoint "${transaction}" \
  || fail "PRE_PROMOTION_RUNTIME_STATE_MISMATCH"
bash "${runtime_dir}/finalize-app-update.sh" \
  "${env_file}" "${transaction}" "${target_revision}" promote \
  || fail "LAST_GOOD_PROMOTION_FAILED"
write_transaction_state "${transaction}/transaction-state.json" verified \
  || fail "TRANSACTION_STATE_WRITE_FAILED"
transaction_terminal=true

printf 'reindex_required=false\n'
printf 'RAG_INDUSTRY_SERVING_UPDATE_OK image=%s revision=%s worker_restarted=false\n' \
  "${target_image}" "${target_revision}"
