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
state_path="$(env_value "${env_file}" RAG_STATE_PATH)" \
  || fail "STATE_PATH_INVALID"
target_revision="$(manifest_value revision)" || fail "TARGET_REVISION_INVALID"
runtime_archive_sha="$(manifest_value runtime.archive_sha256)" \
  || fail "RUNTIME_SHA_INVALID"
target_image="$(manifest_value image.ref)" || fail "TARGET_IMAGE_INVALID"
target_image_id="$(manifest_value image.id)" || fail "TARGET_IMAGE_ID_INVALID"
target_platform="$(manifest_value image.platform)" \
  || fail "TARGET_PLATFORM_INVALID"
target_index="$(manifest_value index_fingerprint.target)" \
  || fail "TARGET_INDEX_INVALID"
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
    or source.get("trace_v2_read_compatible") is not True
    or source.get("required_index_fingerprint") != target_index
):
    raise SystemExit("SOURCE_COMPATIBILITY_INVALID")
if old_revision != target_revision and old_revision not in compatible:
    raise SystemExit("SOURCE_REVISION_NOT_COMPATIBLE")
PY
worker_state="$(docker container inspect --format '{{.State.Running}}' \
  rag-industry-worker 2>/dev/null || true)"
[[ "${worker_state}" != "true" ]] || fail "WORKER_RUNNING"

transaction="${backup_path}/serving-updates/${update_id}"
if [[ -d "${transaction}" ]]; then
  current_revision="$(exact_env_value "${env_file}" RAG_RELEASE_REVISION)"
  current_compose="$(exact_env_value "${env_file}" RAG_INDUSTRY_COMPOSE_FILE)"
  if [[ "${current_revision}" == "${target_revision}" \
    && "${current_compose}" == "${runtime_dir}/compose.yaml" ]]; then
    bash "${runtime_dir}/verify-app-update.sh" "${env_file}" "${transaction}" \
      || fail "IDEMPOTENT_VERIFY_FAILED"
    printf 'reindex_required=false\n'
    printf 'RAG_INDUSTRY_SERVING_UPDATE_ALREADY_CURRENT\n'
    exit 0
  fi
  fail "UPDATE_TRANSACTION_ALREADY_EXISTS"
fi
mkdir -m 700 -- "${transaction}"
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

python3 - "${env_file}" "${old_compose}" "${old_config}" \
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
config_path = pathlib.Path(sys.argv[3])
output_path = pathlib.Path(sys.argv[4])
old_image = sys.argv[5]
old_revision = sys.argv[6]
backup_path = pathlib.Path(sys.argv[7])
sha_pattern = re.compile(r"[0-9a-f]{64}")
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
    or not config_path.is_dir()
    or config_path.is_symlink()
    or revision_pattern.fullmatch(old_revision) is None
):
    raise SystemExit("PRE_UPDATE_FILE_IDENTITY_INVALID")
config_names = {
    "corpus-policy.json",
    "intent-router-calibration.json",
    "intent-router.json",
    "pipeline.json",
    "retrieval.json",
}
actual_config = {
    path.name
    for path in config_path.iterdir()
    if path.is_file() and not path.is_symlink()
}
if actual_config != config_names:
    raise SystemExit("PRE_UPDATE_CONFIG_EXACT_SET_INVALID")
config_files = {
    name: sha256(config_path / name) for name in sorted(config_names)
}
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
    "config": {"files": config_files, "path": str(config_path)},
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
}
if any(sha_pattern.fullmatch(value) is None for value in config_files.values()):
    raise SystemExit("PRE_UPDATE_CONFIG_SHA_INVALID")
descriptor = os.open(
    output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
)
with os.fdopen(descriptor, "w", encoding="utf-8") as output:
    json.dump(payload, output, separators=(",", ":"), sort_keys=True)
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())
PY

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

trace_backup="${transaction}/traces-before.sqlite3"
trace_report="$(python3 "${runtime_dir}/runtime_check.py" \
  backup-trace-database "${state_path}/traces.sqlite3" \
  "${trace_backup}" "${target_revision}")" \
  || fail "TRACE_BACKUP_FAILED"
python3 - "${trace_report}" "${transaction}/trace-backup.json" \
  "${target_revision}" <<'PY'
import json
import os
import pathlib
import re
import sys

value = json.loads(sys.argv[1])
if (
    not isinstance(value, dict)
    or value.get("mode") != "0600"
    or value.get("target_revision") != sys.argv[3]
    or not isinstance(value.get("page_count"), int)
    or isinstance(value.get("page_count"), bool)
    or value["page_count"] <= 0
    or not isinstance(value.get("source_database_identity"), dict)
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
python3 - "${old_config_json}" "${new_config_json}" \
  "${runtime_dir}/config" "${target_image}" "${old_config}" <<'PY'
import json
import pathlib
import sys

old = json.loads(pathlib.Path(sys.argv[1]).read_bytes())
new = json.loads(pathlib.Path(sys.argv[2]).read_bytes())
if old.get("name") != "rag-industry" or new.get("name") != "rag-industry":
    raise SystemExit("COMPOSE_PROJECT_CHANGED")
for service in ("rag-industry-qdrant", "rag-industry-ocr"):
    if old.get("services", {}).get(service) != new.get("services", {}).get(service):
        raise SystemExit("DEPENDENCY_SERVICE_CHANGED")
if old.get("networks") != new.get("networks"):
    raise SystemExit("COMPOSE_NETWORKS_CHANGED")
app = new.get("services", {}).get("rag-industry-app", {})
old_app = old.get("services", {}).get("rag-industry-app", {})
if not isinstance(app, dict) or not isinstance(old_app, dict):
    raise SystemExit("APP_SERVICE_INVALID")
if app.get("image") != sys.argv[4]:
    raise SystemExit("TARGET_APP_IMAGE_INVALID")
if app.get("ports") != old_app.get("ports") or app.get("ports") != [
    {"published": "8188", "target": 8088}
]:
    raise SystemExit("APP_PORT_CHANGED")
mounts = {
    item.get("target"): item.get("source")
    for item in app.get("volumes", [])
    if isinstance(item, dict)
}
old_mounts = {
    item.get("target"): item.get("source")
    for item in old_app.get("volumes", [])
    if isinstance(item, dict)
}
if mounts.get("/config") != sys.argv[3]:
    raise SystemExit("TARGET_CONFIG_MOUNT_INVALID")
if old_mounts.get("/config") != sys.argv[5]:
    raise SystemExit("SOURCE_CONFIG_MOUNT_INVALID")
for target in ("/data/docs", "/state", "/logs"):
    if mounts.get(target) != old_mounts.get(target) or mounts.get(target) is None:
        raise SystemExit("APP_VOLUME_CHANGED")
environment = app.get("environment", {})
expected = {
    "RAG_RUN_MODE": "demo",
    "RAG_TRACE_QUESTION_CAPTURE": "plaintext",
    "RAG_TRACE_QUESTION_RETENTION_SECONDS": "604800",
    "RAG_UI_QUERY_AUTH_MODE": "same_origin_session",
    "RAG_UI_COOKIE_SECURE": "false",
    "RAG_UI_ALLOW_INSECURE_HTTP": "true",
    "RAG_UI_SESSION_TTL_SECONDS": "1800",
}
if any(str(environment.get(key)).lower() != value for key, value in expected.items()):
    raise SystemExit("TARGET_SERVING_ENV_INVALID")
allowed_environment_changes = set(expected)
old_environment = old_app.get("environment", {})
if not isinstance(old_environment, dict) or not isinstance(environment, dict):
    raise SystemExit("APP_ENVIRONMENT_INVALID")
for key in set(old_environment) | set(environment):
    if (
        key not in allowed_environment_changes
        and old_environment.get(key) != environment.get(key)
    ):
        raise SystemExit("APP_ENVIRONMENT_CHANGED")
for service_name in ("rag-industry-app", "rag-industry-worker"):
    old_service = old.get("services", {}).get(service_name)
    new_service = new.get("services", {}).get(service_name)
    if old_service is None and new_service is None:
        continue
    if not isinstance(old_service, dict) or not isinstance(new_service, dict):
        raise SystemExit("APP_WORKER_SERVICE_CHANGED")
    for key in set(old_service) | set(new_service):
        if key not in {"environment", "image", "volumes"} and (
            old_service.get(key) != new_service.get(key)
        ):
            raise SystemExit("APP_WORKER_STRUCTURE_CHANGED")
    if new_service.get("image") != sys.argv[4]:
        raise SystemExit("APP_WORKER_IMAGE_INVALID")
PY
rm -f -- "${old_config_json}" "${new_config_json}"
old_config_json=""
new_config_json=""

gzip -dc -- "${package_dir}/app-image.tar.gz" | docker image load >/dev/null \
  || fail "APP_IMAGE_LOAD_FAILED"
actual_image_id="$(docker image inspect --format '{{.Id}}' "${target_image}")"
actual_platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' \
  "${target_image}")"
actual_revision="$(docker image inspect --format \
  '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  "${target_image}")"
[[ "${actual_image_id}" == "${target_image_id}" \
  && "${actual_platform}" == "${target_platform}" \
  && "${actual_revision}" == "${target_revision}" ]] \
  || fail "APP_IMAGE_IDENTITY_MISMATCH"
asset_report="$(docker run --rm --network none "${target_image}" \
  rag-app asset-selfcheck)" || fail "IMAGE_ASSET_SELFCHECK_FAILED"
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

python3 - "${candidate_env}" "${env_file}" <<'PY'
import os
import pathlib
import shutil
import sys
import tempfile

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{target.name}.candidate.", dir=target.parent
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

activated=true
rollback_on_error() {
  local exit_code="$?"
  trap - ERR
  if [[ "${activated}" == "true" ]]; then
    if ! bash "${runtime_dir}/rollback-app-update.sh" \
      "${env_file}" "${transaction}"; then
      printf 'RAG_INDUSTRY_SERVING_UPDATE_ROLLBACK_FAILED\n' >&2
      exit 70
    fi
  fi
  printf 'RAG_INDUSTRY_SERVING_UPDATE_ROLLED_BACK\n' >&2
  exit "${exit_code}"
}
trap rollback_on_error ERR

run_industry_compose "${env_file}" "${runtime_dir}/compose.yaml" \
  up -d --no-deps --no-build --pull never --force-recreate \
  rag-industry-app
wait_industry_health rag-industry-app 180
bash "${runtime_dir}/verify-app-update.sh" "${env_file}" "${transaction}"
trap - ERR

printf 'reindex_required=false\n'
printf 'RAG_INDUSTRY_SERVING_UPDATE_OK image=%s revision=%s worker_restarted=false\n' \
  "${target_image}" "${target_revision}"
