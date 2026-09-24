#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
vendor_root="${repo_root}/vendor/weknora-chunker"
go_image="golang:1.26.0-bookworm@sha256:2a0ba12e116687098780d3ce700f9ce3cb340783779646aafbabed748fa6677c"

docker run --rm --network none \
  -e CGO_ENABLED=0 \
  -v "${vendor_root}:/src" \
  -w /src \
  "${go_image}" \
  sh -eu -c 'go test ./internal/infrastructure/chunker ./cmd/wb-chunker && go build -trimpath -buildvcs=false -o bin/linux-amd64/wb-chunker ./cmd/wb-chunker'

expected_sha256="491a0bd01577ecff835b0e2c93112e141f550e7bd07fabc523b98f8433e75f6f"
printf '%s  %s\n' "${expected_sha256}" "${vendor_root}/bin/linux-amd64/wb-chunker" | sha256sum --check
