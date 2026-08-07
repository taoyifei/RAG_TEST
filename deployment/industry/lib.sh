#!/usr/bin/env bash

industry_fail() {
  printf 'RAG_INDUSTRY_FAILED: %s\n' "$*" >&2
  exit 1
}

exact_env_value() {
  local env_file="$1"
  local key="$2"
  awk -F= -v expected="${key}" '
    $1 == expected {
      count += 1
      value = substr($0, index($0, "=") + 1)
      sub(/\r$/, "", value)
      if ((value ~ /^\047.*\047$/) || (value ~ /^".*"$/)) {
        value = substr(value, 2, length(value) - 2)
      }
    }
    END {
      if (count != 1 || value == "") {
        exit 2
      }
      print value
    }
  ' "${env_file}"
}

require_absolute_path() {
  local value="$1"
  local label="$2"
  [[ "${value}" == /* && "${value}" != *$'\n'* ]] \
    || industry_fail "${label} 必须是非空绝对路径。"
}

require_industry_env() {
  local env_file="$1"
  [[ "${env_file}" == /* && -f "${env_file}" && ! -L "${env_file}" ]] \
    || industry_fail "env 必须是绝对路径下的普通文件。"
  local alias
  alias="$(exact_env_value "${env_file}" RAG_QDRANT_ALIAS)" \
    || industry_fail "env 缺少唯一 RAG_QDRANT_ALIAS。"
  [[ "${alias}" == "rag-industry-active" ]] \
    || industry_fail "RAG_QDRANT_ALIAS 必须是 rag-industry-active。"
}

require_release_directory() {
  local release_dir="$1"
  [[ "${release_dir}" == /* && -d "${release_dir}" && ! -L "${release_dir}" ]] \
    || industry_fail "release-dir 必须是绝对路径下的真实目录。"
  [[ -f "${release_dir}/RELEASE_MANIFEST.json" \
    && -f "${release_dir}/SHA256SUMS" ]] \
    || industry_fail "release-dir 缺少 release 身份文件。"
}

industry_compose_file() {
  local env_file="$1"
  local expected
  expected="$(exact_env_value "${env_file}" RAG_INDUSTRY_COMPOSE_FILE)" \
    || industry_fail "env 缺少唯一 RAG_INDUSTRY_COMPOSE_FILE。"
  require_absolute_path "${expected}" RAG_INDUSTRY_COMPOSE_FILE
  [[ -f "${expected}" && ! -L "${expected}" ]] \
    || industry_fail "RAG_INDUSTRY_COMPOSE_FILE 不是普通文件。"
  printf '%s\n' "${expected}"
}

wait_industry_health() {
  local container="$1"
  local timeout_seconds="${2:-300}"
  local deadline=$((SECONDS + timeout_seconds))
  local state
  while ((SECONDS < deadline)); do
    state="$(docker inspect --format \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "${container}" 2>/dev/null || true)"
    case "${state}" in
      healthy)
        return 0
        ;;
      unhealthy|exited|dead)
        industry_fail "${container} 状态为 ${state}。"
        ;;
    esac
    sleep 2
  done
  industry_fail "${container} 未在时限内进入 healthy。"
}

wait_industry_http() {
  local url="$1"
  local timeout_seconds="$2"
  local deadline=$((SECONDS + timeout_seconds))
  while ((SECONDS < deadline)); do
    if curl --fail --silent --show-error "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}
