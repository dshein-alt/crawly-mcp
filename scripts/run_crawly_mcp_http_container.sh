#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
container_engine="${CRAWLY_CONTAINER_ENGINE:-docker}"
image_name="${CRAWLY_MCP_IMAGE:-crawly-mcp:local}"
container_user="${CRAWLY_MCP_CONTAINER_USER:-pwuser}"
skip_build="${CRAWLY_MCP_SKIP_BUILD:-0}"
bind_host="${CRAWLY_HTTP_BIND_HOST:-127.0.0.1}"
bind_port="${CRAWLY_HTTP_BIND_PORT:-8000}"
server_host="${CRAWLY_HOST:-0.0.0.0}"
server_port="${CRAWLY_PORT:-8000}"

if ! command -v "$container_engine" >/dev/null 2>&1; then
  printf 'container engine not found: %s\n' "$container_engine" >&2
  exit 1
fi

if [ "$skip_build" != "1" ]; then
  printf 'building %s from %s\n' "$image_name" "$repo_root" >&2
  "$container_engine" build -t "$image_name" "$repo_root"
fi

printf 'serving crawly MCP at http://%s:%s/mcp\n' "$bind_host" "$bind_port" >&2

exec "$container_engine" run \
  --rm \
  --init \
  --user "$container_user" \
  -e CRAWLY_HOST="$server_host" \
  -e CRAWLY_PORT="$server_port" \
  -p "${bind_host}:${bind_port}:${server_port}" \
  "$image_name" \
  crawly-mcp \
  --transport streamable-http \
  --host "$server_host" \
  --port "$server_port" \
  "$@"
