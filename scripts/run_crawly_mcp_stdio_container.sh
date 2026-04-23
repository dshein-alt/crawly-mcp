#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
container_engine="${CRAWLY_CONTAINER_ENGINE:-docker}"
image_name="${CRAWLY_MCP_IMAGE:-crawly-mcp:local}"
container_user="${CRAWLY_MCP_CONTAINER_USER:-app}"
skip_build="${CRAWLY_MCP_SKIP_BUILD:-0}"
env_args=()

if [ -n "${CRAWLY_FETCH_MAX_SIZE:-}" ]; then
  env_args+=(-e "CRAWLY_FETCH_MAX_SIZE=$CRAWLY_FETCH_MAX_SIZE")
fi

has_local_image() {
  "$container_engine" image inspect "$1" >/dev/null 2>&1
}

resolve_local_image() {
  if [ -n "${CRAWLY_MCP_IMAGE:-}" ]; then
    printf '%s\n' "$image_name"
    return 0
  fi

  for candidate in \
    "crawly-mcp:local" \
    "localhost/crawly-mcp:local" \
    "docker.io/library/crawly-mcp:local"
  do
    if has_local_image "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

if ! command -v "$container_engine" >/dev/null 2>&1; then
  printf 'container engine not found: %s\n' "$container_engine" >&2
  exit 1
fi

if [ "$skip_build" != "1" ]; then
  printf 'building %s from %s\n' "$image_name" "$repo_root" >&2
  "$container_engine" build -t "$image_name" "$repo_root"
else
  if ! image_name="$(resolve_local_image)"; then
    printf 'local image not found; build it first or set CRAWLY_MCP_IMAGE\n' >&2
    printf 'expected one of: crawly-mcp:local, localhost/crawly-mcp:local, docker.io/library/crawly-mcp:local\n' >&2
    exit 1
  fi
fi

exec "$container_engine" run \
  --rm \
  --init \
  -i \
  --user "$container_user" \
  "${env_args[@]}" \
  "$image_name" \
  crawly-mcp \
  --transport stdio \
  "$@"
