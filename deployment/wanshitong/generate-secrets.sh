#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
env_file="${1:-${script_dir}/.env}"

fail() {
  echo "secret-init=failed reason=$1" >&2
  exit 1
}

[[ -f "${env_file}" && ! -L "${env_file}" ]] || fail "env-file-invalid"
set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a

[[ "${WANSHITONG_ROOT:-}" = "/data/tyf/wanshitong" ]] || \
  fail "unexpected-root"
secret_dir="${WANSHITONG_ROOT}/secrets"
[[ -d "${secret_dir}" && ! -L "${secret_dir}" ]] || \
  fail "secret-directory-invalid"
chmod 0700 -- "${secret_dir}"

files=(master-key admin-bootstrap-token qdrant-api-key qdrant.yaml)
existing=0
for name in "${files[@]}"; do
  path="${secret_dir}/${name}"
  if [[ -e "${path}" || -L "${path}" ]]; then
    ((existing += 1))
  fi
done

if ((existing != 0 && existing != ${#files[@]})); then
  fail "partial-secret-bundle"
fi

if ((existing == 0)); then
  docker run --rm --user 0:0 \
    --volume "${secret_dir}:/run/rag-secrets" \
    --entrypoint sh "${RAG_APP_IMAGE:?required}" \
    -c 'rag-app init-secrets --directory /run/rag-secrets >/tmp/secret-report.json && chown -R 10001:10001 /run/rag-secrets && chmod 0700 /run/rag-secrets && chmod 0600 /run/rag-secrets/*'
fi

for name in "${files[@]}"; do
  path="${secret_dir}/${name}"
  [[ -f "${path}" && ! -L "${path}" ]] || fail "secret-file-invalid"
  [[ "$(stat -c '%a' -- "${path}")" = "600" ]] || \
    fail "secret-file-mode-invalid"
done
echo "secret-init=passed directory=${secret_dir} files=${#files[@]}"
