# SearXNG + crawly-mcp compose example

Self-contained recipe to run the opt-in `searxng` provider against a local SearXNG container, using the published crawly-mcp image from GHCR by default.

## What it ships

- `docker-compose.yml` — two services on a shared compose network: `searxng` and `crawly-mcp`.
- `settings.yml` — minimal SearXNG configuration with JSON output enabled (`search.formats: [html, json]`) and the rate limiter disabled (`server.limiter: false`). Both are required for crawly's JSON-API client to reach the instance.
- `.env.example` — template for the environment file that controls the image tag and host port bindings.

## Prerequisites

- A working container engine (`docker` or `podman` with the `docker` CLI shim).

## Configure

```sh
cd examples/searxng-compose
cp .env.example .env
# edit .env as desired
```

The defaults in `.env.example`:

| Variable | Default | What it does |
|---|---|---|
| `CRAWLY_MCP_IMAGE` | `ghcr.io/dshein-alt/crawly-mcp:latest` | Container image for the crawly-mcp service. Override with a local build (`localhost/crawly-mcp:local`) when developing. |
| `CRAWLY_HOST_PORT` | `10000` | Host port that crawly's MCP HTTP transport binds to. Clients reach the server at `http://127.0.0.1:${CRAWLY_HOST_PORT}/mcp/`. |
| `SEARXNG_HOST_PORT` | `10080` | Host port for the SearXNG UI / API. Handy for sanity-checking the instance in a browser. |
| `CRAWLY_LOG_LEVEL` | `INFO` | Loguru level for crawly. |

If you change `SEARXNG_HOST_PORT`, also update the `server.base_url` in `settings.yml` to match.

## Run

```sh
docker compose up -d

# Crawly's MCP HTTP transport is reachable at:
#   http://127.0.0.1:10000/mcp/         (or whatever CRAWLY_HOST_PORT you set)
# The SearXNG UI is at http://127.0.0.1:10080/  (or your SEARXNG_HOST_PORT)
```

Smoke-test the SearXNG provider via the MCP `search` tool:

```sh
uv run python - <<'PY'
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client("http://127.0.0.1:10000/mcp/") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("search",
                {"provider": "searxng", "context": "python async playwright"})
            for b in res.content:
                if getattr(b, "text", None):
                    print(b.text)

asyncio.run(main())
PY
```

Expect a JSON object with up to 5 organic result URLs.

## Tear down

```sh
docker compose down
```

## Notes

- `server.limiter: false` is appropriate **only** because this compose deployment exposes SearXNG to localhost on the host. Do not reuse this configuration for a publicly-reachable SearXNG.
- `secret_key` in `settings.yml` is a placeholder. Replace it before exposing the instance to anything beyond your machine.
- The `.env` file is gitignored by convention; check it in only if it contains no secrets.
