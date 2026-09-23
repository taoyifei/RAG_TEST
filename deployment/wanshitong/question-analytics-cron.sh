#!/usr/bin/env bash
set -euo pipefail
umask 077

# 仅供 60 上的单条 user4a cron 调用；部署回退后自动跳过旧版本。
[[ "$#" -eq 1 ]] || exit 64
candidate_root="$1"
[[ "${candidate_root}" = /* && -d "${candidate_root}" && ! -L "${candidate_root}" ]] || exit 64
revision_file="${candidate_root}/release/SOURCE_REVISION"
[[ -f "${revision_file}" && ! -L "${revision_file}" ]] || exit 64
expected_revision="$(cat -- "${revision_file}")"
[[ "${expected_revision}" =~ ^[0-9a-f]{40}$ ]] || exit 64

container="wanshitong-sso-candidate-app"
if ! running="$(docker inspect -f '{{.State.Running}}' "${container}" 2>/dev/null)"; then
  exit 0
fi
[[ "${running}" = true ]] || exit 0
actual_revision="$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "${container}")"
[[ "${actual_revision}" = "${expected_revision}" ]] || exit 0

exec flock -n "${candidate_root}/question-analytics.cron.lock" \
  docker exec "${container}" python \
  /app/scripts/wanshitong_question_analytics.py --timeout-seconds 300
