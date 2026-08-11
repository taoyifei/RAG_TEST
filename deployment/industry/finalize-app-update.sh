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
inspection="${transaction}/pre-last-good.json"
source_state="${transaction}/pre-update-source-state.json"
source_checkpoint="${transaction}/source-checkpoint.json"
for required in "${inspection}" "${source_state}" "${source_checkpoint}"; do
  [[ -f "${required}" && ! -L "${required}" ]] \
    || finalize_fail "FINALIZE_EVIDENCE_MISSING"
done
python3 "${script_dir}/last_good.py" finalize-target \
  "${backup_path}" "${env_file}" "${verified_state}" \
  "${target_revision}" "${inspection}" "${source_state}" \
  "${source_checkpoint}" >/dev/null \
  || finalize_fail "LAST_GOOD_TARGET_FINALIZE_FAILED"

printf 'RAG_INDUSTRY_APP_UPDATE_LAST_GOOD_OK\n'
