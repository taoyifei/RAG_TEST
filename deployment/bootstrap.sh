#!/usr/bin/env bash
set -euo pipefail
umask 077

fail() {
  echo "$1" >&2
  exit 1
}

if ((EUID != 0)); then
  fail "bootstrap requires root."
fi
if (($# > 1)); then
  fail "usage: bootstrap.sh [/data/tyf/RAG]"
fi

project_root_input="${1:-/data/tyf/RAG}"
if [[ "${project_root_input}" != /* ]]; then
  fail "project root must be absolute."
fi
if [[ -L "${project_root_input}" ]]; then
  fail "project root must not be a symbolic link."
fi
if [[ ! -d "${project_root_input}" ]]; then
  fail "project root must be an existing directory."
fi
project_root="$(/usr/bin/realpath -e -- "${project_root_input}")"
if [[ "${project_root}" != "${project_root_input}" ]]; then
  fail "project root must be canonical and contain no symbolic link."
fi

directory_specs=(
  "0:0:releases"
  "0:0:shared"
  "0:0:shared/corpora"
  "0:0:shared/env"
  "0:0:shared/env/candidates"
  "0:0:data"
  "0:0:data/qdrant"
  "0:0:backups"
  "10001:10001:data/state"
  "10001:10001:logs"
)

validate_existing_directory() {
  local path="$1"
  local owner="$2"
  local group="$3"
  local actual
  if [[ -L "${path}" ]]; then
    fail "bootstrap target must not be a symbolic link."
  fi
  if [[ ! -e "${path}" ]]; then
    return
  fi
  if [[ ! -d "${path}" ]]; then
    fail "bootstrap target must be a directory."
  fi
  actual="$(/usr/bin/stat -c '%u:%g:%a' -- "${path}")"
  if [[ "${actual}" != "${owner}:${group}:700" ]]; then
    fail "bootstrap target has unsafe owner/mode."
  fi
}

for spec in "${directory_specs[@]}"; do
  IFS=: read -r owner group relative_path <<< "${spec}"
  validate_existing_directory \
    "${project_root}/${relative_path}" "${owner}" "${group}"
done

for spec in "${directory_specs[@]}"; do
  IFS=: read -r owner group relative_path <<< "${spec}"
  target="${project_root}/${relative_path}"
  if [[ ! -e "${target}" && ! -L "${target}" ]]; then
    /usr/bin/install -d -o "${owner}" -g "${group}" -m 0700 -- "${target}"
  fi
done

for spec in "${directory_specs[@]}"; do
  IFS=: read -r owner group relative_path <<< "${spec}"
  validate_existing_directory \
    "${project_root}/${relative_path}" "${owner}" "${group}"
done

echo "bootstrap=passed directories=${#directory_specs[@]}"
