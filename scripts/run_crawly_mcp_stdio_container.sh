#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
container_engine="${CRAWLY_CONTAINER_ENGINE:-docker}"
image_name="${CRAWLY_MCP_IMAGE:-crawly-mcp:local}"
container_user="${CRAWLY_MCP_CONTAINER_USER:-pwuser}"
skip_build="${CRAWLY_MCP_SKIP_BUILD:-0}"

if ! command -v "$container_engine" >/dev/null 2>&1; then
  printf 'container engine not found: %s\n' "$container_engine" >&2
  exit 1
fi

if [ "$skip_build" != "1" ]; then
  printf 'building %s from %s\n' "$image_name" "$repo_root" >&2
  "$container_engine" build -t "$image_name" "$repo_root"
fi

exec "$container_engine" run \
  --rm \
  --init \
  -i \
  --user "$container_user" \
  "$image_name" \
  crawly-mcp \
  --transport stdio \
  "$@"
