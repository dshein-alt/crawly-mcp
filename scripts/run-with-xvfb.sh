#!/usr/bin/env bash
# Start crawly-mcp under Xvfb when CRAWLY_USE_XVFB=true. Otherwise exec directly.
# Used as the Docker entrypoint; safe to use locally too.
set -euo pipefail

if [[ "${CRAWLY_USE_XVFB:-false}" =~ ^(1|true|yes)$ ]]; then
    geom="${CRAWLY_XVFB_GEOMETRY:-1280x720x24}"
    exec xvfb-run -a -s "-screen 0 ${geom}" "$@"
fi

exec "$@"
