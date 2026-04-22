# web-search-mcp

External web search and page fetch for local LLMs, exposed as MCP tools and a CLI. The implementation is Playwright-first, async, and launches the system-installed Chromium binary by default. The design history is documented in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## Tools

- `search(provider, context)` runs a browser-backed search on `duckduckgo` (default), `google`, or `yandex` and returns up to 5 organic result URLs.
- `fetch(urls)` fetches `1..5` URLs and returns browser-rendered HTML with per-URL `pages`, `errors`, and `truncated` fields.

`context` is intentionally the search query string for caller compatibility.

## Setup

```sh
uv sync
chromium --version
```

If Chromium is installed in a non-standard location, set
`PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium`.

## Usage

Run the MCP server over stdio:

```sh
uv run web-search serve-mcp
```

Run the CLI directly:

```sh
uv run web-search search --context "python async playwright"
uv run web-search fetch https://example.com
```

Expose HTTP transport instead of stdio:

```sh
uv run web-search serve-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

## Design notes

- One shared browser per process, with a fresh incognito context per request.
- Playwright launches the system Chromium binary, not a Playwright-downloaded browser bundle.
- System Chromium worked with Playwright while the tested system Firefox build exited immediately after launch. This repo therefore targets system Chromium as the supported host-browser path.
- Browser smoke tests should be run from a normal host shell. Restricted sandboxes can block Chromium startup even when the host browser works correctly.
- Global navigation concurrency cap of `3`.
- Timeouts: `15s` per page, `20s` total for `search`, `35s` total for `fetch`.
- SSRF guard: `http/https` only, no embedded credentials, blocks loopback/private/link-local/reserved IPs before navigation and on browser subrequests.
- JavaScript challenge pages get a bounded `10s` settle window; there is no CAPTCHA solving, stealth fingerprinting, or site-specific bypass logic.
- HTML is capped at `1 MiB` per URL; oversized responses are truncated and reported in `truncated`.
- `robots.txt` is not consulted in v1.

## Development

```sh
source .venv/bin/activate
pytest
```

Parser tests run against saved HTML fixtures; selector drift is an expected maintenance cost.
