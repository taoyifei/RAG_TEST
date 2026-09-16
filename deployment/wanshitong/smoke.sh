#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
env_file="${1:-${script_dir}/.env}"
[[ -f "${env_file}" && ! -L "${env_file}" ]]
set -a
# shellcheck disable=SC1090
source "${env_file}"
set +a

base_url="http://127.0.0.1:${WANSHITONG_PORT:?required}"
cookie_file="$(mktemp)"
response_file="$(mktemp)"
cleanup() {
  rm -f -- "${cookie_file}" "${response_file}"
}
trap cleanup EXIT

curl -fsS --max-time 5 "${base_url}/live" >/dev/null
curl -fsS --max-time 5 "${base_url}/ready" >/dev/null
curl -fsS --max-time 5 "${base_url}/api/public/capabilities" >/dev/null
headers="$(curl -fsS -D - -o "${response_file}" -c "${cookie_file}" \
  -X POST -H 'Content-Type: application/json' --data '{}' \
  "${base_url}/api/public/session")"
grep -qi '^set-cookie: wanshitong_public_session=' <<<"${headers}"
if grep -qi '^set-cookie:.*;[[:space:]]*secure' <<<"${headers}"; then
  echo "smoke=failed reason=http-cookie-is-secure" >&2
  exit 1
fi
python3 -m json.tool "${response_file}" >/dev/null
echo "smoke=passed base_url=${base_url} public_session=passed"
