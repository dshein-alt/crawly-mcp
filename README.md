# Crawly-MCP

Browser-backed web search and page fetch for local LLMs, exposed as MCP tools and a CLI.

The design history is tracked in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## Naming

- Python distribution: `crawly-mcp`
- Import package: `crawly_mcp`
- CLI executable: `crawly-cli`
- MCP server executable: `crawly-mcp`

## Tools

- `search(provider, context)` runs a browser-backed search on `duckduckgo` (default), `google`, or `yandex` and returns up to 5 organic result URLs.
- `fetch(urls)` fetches `1..5` URLs and returns browser-rendered HTML with per-URL `pages`, `errors`, and `truncated` fields.

`context` is intentionally the search query string for caller compatibility.

## Setup

```sh
uv sync
chromium --version
```

For host usage, crawly defaults to launching a system Chromium binary. If Chromium is installed in a non-standard location, set:

```sh
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium
```

To force Playwright-managed Chromium instead of a host browser:

```sh
PLAYWRIGHT_BROWSER_SOURCE=bundled
```

## Usage

Run the CLI directly:

```sh
uv run crawly-cli search --context "python async playwright"
uv run crawly-cli fetch https://example.com
```

Run the MCP server over stdio:

```sh
uv run crawly-mcp
```

Expose HTTP transport instead of stdio:

```sh
uv run crawly-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

The MCP server also reads:

- `CRAWLY_HOST`
- `CRAWLY_PORT`

## Container

The container image uses Playwright-managed Chromium and defaults to HTTP MCP on port `8000`.

Build locally:

```sh
docker build -t crawly-mcp:local .
```

Run locally:

```sh
docker run --rm --init -p 8000:8000 crawly-mcp:local
```

Override the transport to stdio:

```sh
docker run --rm --init -i crawly-mcp:local crawly-mcp --transport stdio
```

Launch the stdio MCP server from the current checkout with an auto-build step:

```sh
./scripts/run_crawly_mcp_stdio_container.sh
```

Launch the HTTP MCP server from the current checkout:

```sh
./scripts/run_crawly_mcp_http_container.sh
```

The container defaults to:

- `PLAYWRIGHT_BROWSER_SOURCE=bundled`
- `CRAWLY_HOST=0.0.0.0`
- `CRAWLY_PORT=8000`

The HTTP MCP endpoint is unauthenticated in v1. Deploy it behind localhost, a private network, or an auth/TLS reverse proxy.

Published images are intended to be:

- `ghcr.io/<owner>/crawly-mcp`
- `<dockerhub-namespace>/crawly-mcp`

The first GHCR publish may need a one-time manual visibility change to make the package public.

## MCP Client Config

For MCP clients that can launch a local command, point them at the project script so the
server comes from the current checkout:

```yaml
mcpServers:
  - name: Crawly MCP
    command: /path/to/crawly/scripts/run_crawly_mcp_stdio_container.sh
    args: []
    env:
      CRAWLY_CONTAINER_ENGINE: docker
```

Replace `/path/to/crawly` with your checkout path. The launcher rebuilds
`crawly-mcp:local` before starting the stdio server so container contents stay aligned
with local source changes. Set `CRAWLY_MCP_SKIP_BUILD=1` if you want to skip that build
when the local image is already current.

For clients that support HTTP MCP, start the local containerized server first:

```sh
./scripts/run_crawly_mcp_http_container.sh
```

Then point the client at:

```text
http://127.0.0.1:8000/mcp
```

If your client's MCP config accepts direct URLs, the entry is typically shaped like:

```yaml
mcpServers:
  - name: Crawly MCP
    url: http://127.0.0.1:8000/mcp
```

Set `CRAWLY_HTTP_BIND_HOST` or `CRAWLY_HTTP_BIND_PORT` before launching if you need the
local listener on a different interface or port.

## Design Notes

- One shared browser per process, with a fresh incognito context per request.
- `PLAYWRIGHT_BROWSER_SOURCE=system` uses a host Chromium binary.
- `PLAYWRIGHT_BROWSER_SOURCE=bundled` uses Playwright-managed Chromium.
- Global navigation concurrency cap of `3`.
- Timeouts: `15s` per page, `20s` total for `search`, `35s` total for `fetch`.
- SSRF guard: `http/https` only, no embedded credentials, blocks loopback/private/link-local/reserved IPs before navigation and on browser subrequests.
- JavaScript challenge pages get a bounded `10s` settle window; there is no CAPTCHA solving, stealth fingerprinting, or site-specific bypass logic.
- HTML is capped at `1 MiB` per URL; oversized responses are truncated and reported in `truncated`.
- `robots.txt` is not consulted in v1.

## Development

```sh
source .venv/bin/activate
ruff check .
pytest
```

Smoke checks:

```sh
rg -n "web-search|web_search_mcp" README.md AGENTS.md CHANGELOG.md pyproject.toml src tests
.venv/bin/python scripts/http_mcp_smoke.py --url http://127.0.0.1:8000/mcp
```

Parser tests run against saved HTML fixtures; selector drift is an expected maintenance cost.
