#!/usr/bin/env bash
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repository_root="$(cd -- "${script_dir}/../.." && pwd -P)"
compose_file="${script_dir}/compose.candidate.yaml"
guard="${repository_root}/scripts/wb08r_prep_runtime_guard.py"
project="wanshitong-candidate"
candidate_container="wanshitong-prep-candidate-app"

fail() {
  echo "candidate_deploy=failed reason=$1" >&2
  exit 1
}

usage() {
  echo "usage: $0 <render|create|start|rollback> ENV BASELINE_INSPECT CANDIDATE_ROOT EVIDENCE_DIR" >&2
  exit 64
}

[[ "$#" -eq 5 ]] || usage
action="$1"
env_file="$2"
baseline_inspect="$3"
candidate_root="$4"
evidence_dir="$5"

[[ "${env_file}" = /* ]] || fail "env-file-not-absolute"
[[ "${baseline_inspect}" = /* ]] || fail "baseline-inspect-not-absolute"
[[ "${candidate_root}" = /* ]] || fail "candidate-root-not-absolute"
[[ "${evidence_dir}" = /* ]] || fail "evidence-dir-not-absolute"
[[ -f "${env_file}" && ! -L "${env_file}" ]] || fail "env-file-invalid"
[[ -f "${baseline_inspect}" && ! -L "${baseline_inspect}" ]] || \
  fail "baseline-inspect-invalid"
[[ -d "${candidate_root}" && ! -L "${candidate_root}" ]] || \
  fail "candidate-root-invalid"
[[ -d "${evidence_dir}" && ! -L "${evidence_dir}" ]] || \
  fail "evidence-dir-invalid"
[[ "$(stat -c '%a' -- "${env_file}")" = "600" ]] || \
  fail "env-file-mode-invalid"
[[ "$(stat -c '%a' -- "${evidence_dir}")" = "700" ]] || \
  fail "evidence-dir-mode-invalid"

command -v docker >/dev/null || fail "docker-missing"
command -v python3 >/dev/null || fail "python-missing"
docker compose version >/dev/null || fail "docker-compose-missing"

mapfile -t interpolation_keys < <(
  grep -oE '\$\{[A-Z_][A-Z0-9_]*' "${compose_file}" |
    cut -c3- |
    sort -u
)
conflicts=()
for key in "${interpolation_keys[@]}"; do
  if [[ -v "${key}" ]]; then
    conflicts+=("${key}")
  fi
done
if ((${#conflicts[@]})); then
  printf 'candidate_deploy=failed reason=shell-environment-conflict keys=%s\n' \
    "$(IFS=,; echo "${conflicts[*]}")" >&2
  exit 1
fi

compose=(
  docker compose
  --project-directory "${script_dir}"
  --env-file "${env_file}"
  -f "${compose_file}"
  -p "${project}"
)
rendered="${evidence_dir}/candidate.rendered.private.json"
image_inspect="${evidence_dir}/candidate.image.private.json"
rendered_guard="${evidence_dir}/candidate.rendered.guard.safe.json"
created_inspect="${evidence_dir}/candidate.created.private.json"
created_guard="${evidence_dir}/candidate.created.guard.safe.json"

report_ready() {
  python3 - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    report = json.load(stream)
raise SystemExit(0 if report.get("ready") is True else 1)
PY
}

case "${action}" in
  render)
    [[ ! -e "${rendered}" && ! -e "${image_inspect}" ]] || \
      fail "render-evidence-already-exists"
    "${compose[@]}" config --format json >"${rendered}"
    image="$({ python3 - "${rendered}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)["services"]["app"]["image"])
PY
    } )"
    docker image inspect "${image}" >"${image_inspect}"
    chmod 0600 -- "${rendered}" "${image_inspect}"
    python3 "${guard}" compare \
      --stage rendered_spec \
      --baseline-inspect "${baseline_inspect}" \
      --candidate-input "${rendered}" \
      --target-image-inspect "${image_inspect}" \
      --candidate-root "${candidate_root}" \
      --forbidden-root /data/tyf/wanshitong \
      --output "${rendered_guard}"
    ;;
  create)
    [[ -f "${rendered_guard}" ]] || fail "rendered-guard-missing"
    report_ready "${rendered_guard}" || fail "rendered-guard-not-ready"
    if docker container inspect "${candidate_container}" >/dev/null 2>&1; then
      fail "candidate-container-already-exists"
    fi
    "${compose[@]}" create --no-build --pull never app
    docker inspect "${candidate_container}" >"${created_inspect}"
    chmod 0600 -- "${created_inspect}"
    python3 "${guard}" compare \
      --stage created_container \
      --baseline-inspect "${baseline_inspect}" \
      --candidate-input "${created_inspect}" \
      --target-image-inspect "${image_inspect}" \
      --candidate-root "${candidate_root}" \
      --forbidden-root /data/tyf/wanshitong \
      --output "${created_guard}"
    ;;
  start)
    [[ -f "${created_guard}" ]] || fail "created-guard-missing"
    report_ready "${created_guard}" || fail "created-guard-not-ready"
    baseline_id="$({ python3 - "${baseline_inspect}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)[0]["Id"])
PY
    } )"
    current_id="$(
      docker inspect -f '{{.Id}}' wanshitong-wb08r01-app 2>/dev/null || true
    )"
    [[ "${current_id}" = "${baseline_id}" ]] || \
      fail "baseline-container-identity-changed"
    mapfile -t port_owners < <(
      docker ps --no-trunc --filter publish=8289 --format '{{.ID}}'
    )
    [[ "${#port_owners[@]}" -eq 1 ]] || fail "candidate-port-owner-count"
    [[ "${port_owners[0]}" = "${baseline_id}" ]] || \
      fail "candidate-port-owned-by-unexpected-container"

    rollback_required=true
    rollback_on_error() {
      if [[ "${rollback_required}" = true ]]; then
        docker stop --time 20 "${candidate_container}" >/dev/null 2>&1 || true
        docker start "${baseline_id}" >/dev/null
      fi
    }
    abort_start() {
      local reason="$1"
      rollback_on_error
      rollback_required=false
      trap - ERR INT TERM
      fail "${reason}"
    }
    trap rollback_on_error ERR INT TERM
    docker stop --time 30 "${baseline_id}" >/dev/null
    "${compose[@]}" start app
    ready=false
    for _ in $(seq 1 36); do
      if curl -fsS --max-time 3 http://127.0.0.1:8289/live >/dev/null &&
        curl -fsS --max-time 3 http://127.0.0.1:8289/ready >/dev/null &&
        [[ "$(docker inspect -f '{{.State.Health.Status}}' "${candidate_container}")" = "healthy" ]]; then
        ready=true
        break
      fi
      sleep 5
    done
    [[ "${ready}" = true ]] || abort_start "candidate-health-timeout"
    rollback_required=false
    trap - ERR INT TERM
    echo "candidate_deploy=started container=${candidate_container} port=8289"
    ;;
  rollback)
    baseline_id="$({ python3 - "${baseline_inspect}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    print(json.load(stream)[0]["Id"])
PY
    } )"
    docker stop --time 20 "${candidate_container}" >/dev/null 2>&1 || true
    docker start "${baseline_id}" >/dev/null
    for _ in $(seq 1 36); do
      if curl -fsS --max-time 3 http://127.0.0.1:8289/live >/dev/null &&
        curl -fsS --max-time 3 http://127.0.0.1:8289/ready >/dev/null; then
        echo "candidate_deploy=rolled-back container=${baseline_id}"
        exit 0
      fi
      sleep 5
    done
    fail "rollback-health-timeout"
    ;;
  *)
    usage
    ;;
esac
