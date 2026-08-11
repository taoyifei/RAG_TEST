#!/usr/bin/env bash
set -Eeuo pipefail

rollback_fail() {
  printf 'RAG_INDUSTRY_APP_ROLLBACK_FAILED: %s\n' "$*" >&2
  exit 70
}

[[ "$#" -eq 2 ]] \
  || rollback_fail \
    "用法: rollback-app-update.sh /absolute/env /absolute/verified-transaction"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib.sh
source "${script_dir}/lib.sh"
env_file="$1"
transaction="$2"
require_industry_env "${env_file}"
[[ "${transaction}" == /* && -d "${transaction}" \
  && ! -L "${transaction}" ]] \
  || rollback_fail "ROLLBACK_PATH_INVALID"
backup_path="$(exact_env_value "${env_file}" RAG_BACKUP_PATH)" \
  || rollback_fail "BACKUP_PATH_INVALID"
rollback_lock_fd=""
acquire_industry_serving_update_lock "${backup_path}" rollback_lock_fd \
  || rollback_fail \
    "${INDUSTRY_SERVING_LOCK_ERROR:-SERVING_UPDATE_LOCK_FAILED}"
bash "${script_dir}/rollback-app-update-core.sh" \
  --manual-verified "${env_file}" "${transaction}"
