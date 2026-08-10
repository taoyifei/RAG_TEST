#!/usr/bin/env bash
set -euo pipefail

finalize_fail() {
  printf 'RAG_INDUSTRY_APP_UPDATE_FINALIZE_FAILED: %s\n' "$*" >&2
  exit 1
}

[[ "$#" -eq 4 ]] \
  || finalize_fail \
    "用法: finalize-app-update.sh /absolute/env /absolute/transaction revision promote|reconcile"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"
env_file="$1"
transaction="$2"
target_revision="$3"
mode="$4"
require_industry_env "${env_file}"
[[ "${transaction}" == /* && -d "${transaction}" && ! -L "${transaction}" ]] \
  || finalize_fail "TRANSACTION_PATH_INVALID"
[[ "${target_revision}" =~ ^[0-9a-f]{40}$ ]] \
  || finalize_fail "TARGET_REVISION_INVALID"
[[ "${mode}" == "promote" || "${mode}" == "reconcile" ]] \
  || finalize_fail "FINALIZE_MODE_INVALID"
[[ "$(exact_env_value "${env_file}" RAG_RELEASE_REVISION)" \
  == "${target_revision}" ]] || finalize_fail "ENV_REVISION_MISMATCH"
verified_state="${transaction}/verified-state.json"
[[ -f "${verified_state}" && ! -L "${verified_state}" ]] \
  || finalize_fail "VERIFIED_STATE_MISSING"
backup_path="$(exact_env_value "${env_file}" RAG_BACKUP_PATH)"
pointer="${backup_path}/last-good-pointer.json"

resolved=""
if [[ -e "${pointer}" || -L "${pointer}" ]]; then
  resolved="$(python3 "${script_dir}/last_good.py" resolve "${backup_path}")" \
    || finalize_fail "LAST_GOOD_RESOLVE_FAILED"
fi
if [[ "${mode}" == "reconcile" && -n "${resolved}" ]]; then
  python3 - "${resolved}" "${target_revision}" <<'PY' \
    || finalize_fail "LAST_GOOD_POINTER_MISMATCH"
import json
import sys

value = json.loads(sys.argv[1])
if value.get("revision") != sys.argv[2]:
    raise SystemExit("LAST_GOOD_POINTER_MISMATCH")
PY
fi
if [[ -z "${resolved}" || "${mode}" == "promote" ]]; then
  python3 "${script_dir}/last_good.py" promote \
    "${backup_path}" "${env_file}" "${verified_state}" \
    "${target_revision}" >/dev/null \
    || finalize_fail "LAST_GOOD_PROMOTION_FAILED"
fi
resolved="$(python3 "${script_dir}/last_good.py" resolve "${backup_path}")" \
  || finalize_fail "LAST_GOOD_POST_PROMOTION_RESOLVE_FAILED"
python3 - "${resolved}" "${env_file}" "${verified_state}" \
  "${target_revision}" <<'PY' \
  || finalize_fail "LAST_GOOD_CONTENT_MISMATCH"
import json
import pathlib
import sys

resolved = json.loads(sys.argv[1])
if resolved.get("revision") != sys.argv[4]:
    raise SystemExit("LAST_GOOD_REVISION_MISMATCH")
for key, expected in (
    ("env_path", pathlib.Path(sys.argv[2])),
    ("state_path", pathlib.Path(sys.argv[3])),
):
    actual = pathlib.Path(str(resolved.get(key, "")))
    if actual.read_bytes() != expected.read_bytes():
        raise SystemExit("LAST_GOOD_CONTENT_MISMATCH")
PY

printf 'RAG_INDUSTRY_APP_UPDATE_LAST_GOOD_OK\n'
