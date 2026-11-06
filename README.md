# Crawly-MCP

Browser-backed web search and page fetch for local LLMs, exposed as MCP tools and a CLI.

The design history is tracked in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

## License

This project is licensed under the [MIT License](LICENSE).

## Naming

- Python distribution: `crawly-mcp`
- Import package: `crawly_mcp`
- CLI executable: `crawly-cli`
- MCP server executable: `crawly-mcp`

## Tools

Every tool result carries `status`, `next_action`, and `detail` first, so a caller can tell "the web has nothing on this" apart from "this provider could not answer" without a human having to say "try google".

| `status` | what it means | `next_action` says |
|---|---|---|
| `ok` | usable data returned | `none` |
| `no_results` | the source answered and genuinely had nothing | `refine_query` — change the query |
| `blocked` | CAPTCHA, consent wall, bot page | `try_other_provider` / `report_to_user` |
| `unavailable` | not configured, unreachable, interstitial, or the page shape is no longer parseable | `try_other_provider` / `fetch_directly` |
| `timeout` | the source did not answer in budget | `retry_later` |
| `navigation_failed` | the request failed at the network layer | `retry_later` |
| `unsafe_url` | the URL was refused by the safety guard | `report_to_user` |
| `invalid_input` | the arguments are wrong | `report_to_user` — fix the arguments |

An empty `urls` list with a status other than `no_results` is a tool failure, never an empty web.

- `search(provider, context)` runs a browser-backed search and **cascades providers** in the configured priority order until one answers. `provider` defaults to `auto` (start from the configured order); naming one starts there and cascades from it. Returns `urls` plus `provider` (the winner), `attempted` (one entry per provider tried, with its status, page kind, and final URL), `available_providers`, and `suggested_providers`.
- `fetch(urls, content_format)` fetches `1..5` URLs. **Each URL is handled on its own**: a refused, unsafe, unreachable, timed-out, or challenge-walled URL lands in `errors` with its own reason and `next_action`, and the other URLs still return. Partial success is normal, so `status` is `ok` whenever at least one URL returned content. `content_format="html"` returns raw HTML, `"text"` returns extracted readable text.
- `page_search(url, query)` searches within a single page: known site-search facilities first (Algolia DocSearch, OpenSearch descriptor, Readthedocs API), then GET form detection, then find-in-page text. `attempted` reports one entry per tier as `ok`, `empty`, `failed`, or `unavailable`, so a broken site-search is not reported as a site with nothing on it.
- `providers()` reports which providers this deployment can use, which are excluded by configuration or missing settings, the cascade order, and how each provider last behaved in this process. Call it before blaming a search failure on the query.

`context` is intentionally the search query string for caller compatibility.

### What the cascade does, and does not, retry

A provider is retried past when it is `blocked`, `unavailable`, `timeout`, or `navigation_failed`. A provider that reports `no_results` **stops** the cascade: an honest empty answer is an answer, and cascading on top of it turns "the topic is empty" into three more requests. Providers that fail repeatedly are skipped as fill slots in later calls, and the `providers` tool reports them as `skipped`.

### Configuring provider priority and exclusion

Operators own the provider set; callers only ever see what survives it.

```sh
# Prefer a self-hosted SearXNG, then DuckDuckGo; never touch Google or Yandex.
export CRAWLY_SEARCH_PROVIDER_ORDER="searxng,duckduckgo"
export CRAWLY_SEARCH_PROVIDER_EXCLUDE="google,yandex"

# One provider per call: no cascading, lowest latency, cheapest bill.
export CRAWLY_SEARCH_MAX_PROVIDERS=1
```

- A provider missing from `CRAWLY_SEARCH_PROVIDER_ORDER` is not used.
- A provider in `CRAWLY_SEARCH_PROVIDER_EXCLUDE` is never used, even when a caller names it — that call is refused as `invalid_input` saying it was excluded, and `providers()` lists it with `excluded: true` and the reason.
- `providers()` reports the resulting `cascade_order`, so the setting can be verified without guessing.

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

## SearXNG

The `searxng` provider routes the query through a JSON-API call to a SearXNG instance you supply, and it is the only provider here that is not fighting a bot wall — so when configured, it leads the cascade order.

To use it:

1. Set `CRAWLY_SEARXNG_URL` to the URL of a SearXNG instance whose JSON output is enabled (`search.formats: [..., json]` in its `settings.yml`).
2. Ask for it by name (`provider="searxng"`, `--provider searxng`) or leave `provider="auto"` and let the cascade reach it.

Without `CRAWLY_SEARXNG_URL` the provider is reported unavailable and is left out of the cascade; asking for it by name is refused as `invalid_input`.

```sh
CRAWLY_SEARXNG_URL=http://127.0.0.1:8080/ \
  uv run crawly-cli search --provider searxng --context "..."
```

There is no instance registry: public instances on https://searx.space generally return 429 / redirects / empty results to non-browser clients, so this provider is for self-hosted or privately-known instances. When a SearXNG instance refuses, the cascade moves on to the next provider in the order and reports the instance's outcome in `attempted`.

### Run alongside a local SearXNG (compose example)

A self-contained recipe is in [`examples/searxng-compose/`](examples/searxng-compose/): a `docker-compose.yml` that runs both `searxng` and `crawly-mcp` on a shared network, plus a `settings.yml` with `search.formats: [html, json]` and `server.limiter: false` (the two things crawly's `searxng` provider needs from any instance). The crawly image is pulled from GHCR by default; ports are controlled via a `.env` file.

```sh
cd examples/searxng-compose
cp .env.example .env        # edit if you want a non-default host port or local image
docker compose up -d
# MCP HTTP at http://127.0.0.1:10000/mcp/   (CRAWLY_HOST_PORT)
```

SearXNG itself is reachable only on the compose network (`http://searxng:8080/` from crawly); no host port is mapped by default.

See [`examples/searxng-compose/README.md`](examples/searxng-compose/README.md) for the env-var reference and a smoke-test snippet.

### Why public instances generally won't work

Public SearXNG instances listed on `https://searx.space` typically respond to programmatic clients with `429`, an HTTP redirect to `/`, or a `200` with an empty results page — driven by SearXNG's built-in botdetection middleware. The community of instance operators actively discourages automated scraping (it burns down their upstream Google/Bing rate-limit budget). This is why the provider expects a SearXNG you control: self-hosted, on your LAN, or otherwise configured to permit JSON access for trusted clients.

## Usage

Run the CLI directly:

```sh
uv run crawly-cli search --context "python async playwright"
uv run crawly-cli search --provider google --context "python"
uv run crawly-cli fetch https://example.com --content-format text
uv run crawly-cli page-search --url https://docs.example.com/guide --query "authentication"
uv run crawly-cli providers
```

Each subcommand prints its response envelope as JSON: `search` prints `status`, `next_action`, `detail`, `urls`, `provider`, `attempted[]`, `available_providers`, `suggested_providers`; `page-search` prints `status`, `mode`, `attempted[]` (per tier), `results_url`, and `results[]`.

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

The container image uses Patchright-managed Chromium on a plain Python Debian base and defaults to HTTP MCP on port `8000`.

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
- `CRAWLY_FETCH_MAX_SIZE=1048576`
- `CRAWLY_PROFILE_DIR=/data/profiles`
- `CRAWLY_PROFILE_CLEANUP_ON_START=true`

For local LLMs with smaller context windows, call `fetch(..., content_format="text")` and lower the payload cap:

```sh
CRAWLY_FETCH_MAX_SIZE=16384 ./scripts/run_crawly_mcp_stdio_container.sh
```

The HTTP MCP endpoint is unauthenticated in v1. Deploy it behind localhost, a private network, or an auth/TLS reverse proxy.

Published images are intended to be:

- `ghcr.io/<owner>/crawly-mcp`
- `<dockerhub-namespace>/crawly-mcp`

The first GHCR publish may need a one-time manual visibility change to make the package public.

## Integration Setup

### Docker Run

Run the published GHCR image directly:

```sh
docker run --rm --init \
  -p 8000:8000 \
  -e CRAWLY_HOST=0.0.0.0 \
  -e CRAWLY_PORT=8000 \
  -e CRAWLY_FETCH_MAX_SIZE=16384 \
  -e CRAWLY_BROWSER_LANG=en-US \
  -e CRAWLY_BROWSER_LOCATION=America/New_York \
  ghcr.io/dshein-alt/crawly-mcp:latest
```

The most important runtime overrides are:

- `CRAWLY_FETCH_MAX_SIZE`: caps returned fetch payload size for both `content_format="html"` and `content_format="text"`.
- `CRAWLY_BROWSER_LANG`: sets browser locale and primary `Accept-Language`.
- `CRAWLY_BROWSER_LOCATION`: sets browser timezone / location persona.

### MCP Client Config

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

For clients that support HTTP MCP, start a local or published `crawly-mcp` HTTP server first,
then point the client at the running instance:

```text
http://127.0.0.1:8000/mcp
```

For Continue, an HTTP MCP config looks like:

```yaml
name: New MCP server
version: 0.0.1
schema: v1
mcpServers:
  - name: Crawly
    type: streamable-http
    url: http://127.0.0.1:8000/mcp
```

The `url` must match an actually running `crawly-mcp` HTTP instance.

If your client's MCP config accepts direct URLs, the entry is typically shaped like:

```yaml
mcpServers:
  - name: Crawly MCP
    url: http://127.0.0.1:8000/mcp
```

Set `CRAWLY_HTTP_BIND_HOST` or `CRAWLY_HTTP_BIND_PORT` before launching if you need the
listener on a different interface or port.

### Bundled Skill / Prompt

This repo includes two reusable instruction files for small-context web workflows:

- [skills/web-search/SKILL.md](skills/web-search/SKILL.md) — Codex skill guidance under the `web-search` skill name
- [skills/continue-web-search.md](skills/continue-web-search.md) — Continue-native invokable prompt named `Web Search`

Use them when a small local model must search, fetch, and synthesize across multiple pages without overflowing context.

## Browser configuration

crawly uses [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) (a Playwright fork with bundled fingerprint patches) and keeps a small set of per-search-provider persistent profiles on disk. The following env vars tune the browser persona and search trace capture:

| Env var | Default | Purpose |
|---|---|---|
| `CRAWLY_BROWSER_LANG` | `ru-RU` | Browser `locale` and primary `Accept-Language` value passed to Playwright. |
| `CRAWLY_BROWSER_LOCATION` | `Europe/Moscow` | Browser timezone id. `TZ` is used only as a fallback when this env var is unset. |
| `CRAWLY_BROWSER_VIEWPORT` | `1366x768` | Browser viewport in `WIDTHxHEIGHT` form. Invalid values fall back to the default. |
| `CRAWLY_FETCH_MAX_SIZE` | `1048576` | Max bytes returned per fetched URL after rendering the configured content format. This limit applies to both raw HTML and extracted text. Lower it for local LLMs with small context windows. |
| `CRAWLY_USE_PERSISTENT_PROFILES` | `true` | Toggle per-provider persistent search profiles. Set to `false` to make `search()` use a fresh incognito context per request (warm-up still runs). Useful for A/B-testing the persistence feature or for stateless deployments. |
| `CRAWLY_PROFILE_DIR` | `~/.cache/crawly/profiles` | Parent directory for per-provider persistent profiles. **Must be a writable mount in containers.** Ignored when `CRAWLY_USE_PERSISTENT_PROFILES=false`. |
| `CRAWLY_PROFILE_CLEANUP_ON_START` | `false` | Enable age-based profile cleanup at startup. Set to `true` in the Dockerfile entrypoint. **Unsafe when multiple processes share the profile dir.** |
| `CRAWLY_PROFILE_MAX_AGE_DAYS` | `14` | Age threshold for profile cleanup. |
| `CRAWLY_SEARCH_JITTER_MS` | `500,1500` | Min/max ms delay between warm-up and real query. Two-int CSV. |
| `CRAWLY_SEARCH_PROVIDER_ORDER` | `searxng,duckduckgo,google,yandex` | Provider **priority order**. The cascade walks it in this sequence; an explicitly requested provider is still tried first. Unknown names are ignored with a warning. |
| `CRAWLY_SEARCH_PROVIDER_EXCLUDE` | unset | Providers to **exclude entirely**. An excluded provider is never tried, even when requested by name (that is refused as `invalid_input`), and is reported `excluded` by `providers()`. Exclusion wins over ordering. CSV. |
| `CRAWLY_SEARCH_MAX_PROVIDERS` | `3` | How many providers one `search` call may try. Set `1` to disable cascading. Invalid or non-positive values fall back to the default. |
| `CRAWLY_SSRF_ALLOW` | unset | Exact hostnames (CSV) allowed to resolve to non-public addresses, for private-network deployments that must reach an internal wiki, search instance, or fixture site. No CIDRs, no wildcards — every entry is a hole in the guard, so name them. Scheme and credential checks still apply. |
| `CRAWLY_TRACE_DIR` | unset | Opt-in per-search artifact dump directory. When set, each `search()` writes `meta.json`, `fingerprint.json`, `network.jsonl`, `page.html`, and `screenshot.png`. |

### Profile persistence

Each provider (`duckduckgo`, `google`, `yandex`) keeps its own subdirectory under `CRAWLY_PROFILE_DIR` with cookies, localStorage, and session state. In Docker, mount a named volume at whatever path `CRAWLY_PROFILE_DIR` points to (default in the image: `/data/profiles`):

```sh
docker run -v crawly-profiles:/data/profiles crawly-mcp
```

### Fingerprint canary

`scripts/fingerprint_check.py` runs a set of JS assertions against a blank page to verify the browser's JS-visible fingerprint looks like real Chrome:

```sh
uv run python scripts/fingerprint_check.py --verbose
```

Exits non-zero if any check fails. CI runs this on release tags.

### Search tracing

Tracing is disabled by default. Set `CRAWLY_TRACE_DIR` only when you want to compare an automated run with manually collected artifacts:

```sh
CRAWLY_TRACE_DIR=./dump/trace uv run crawly-mcp --transport streamable-http
```

Each traced `search()` call writes one directory containing:

- `meta.json` with provider, query, warm-up/jitter data, final URL/title, and parsed result URLs
- `fingerprint.json` with JS-visible browser properties
- `network.jsonl` with request/response/failure events
- `page.html` and `screenshot.png` from the terminal page state

## Design Notes

- One shared incognito browser per process for `fetch()` (fresh context per request). `search()` uses per-provider persistent contexts with on-disk profiles keyed by provider.
- `PLAYWRIGHT_BROWSER_SOURCE=system` uses a host Chromium binary (driven by patchright).
- `PLAYWRIGHT_BROWSER_SOURCE=bundled` uses patchright-managed Chromium (`patchright install chromium`).
- Global navigation concurrency cap of `3`.
- Timeouts: `15s` per page, `20s` total for `search`, `35s` total for `fetch`.
- SSRF guard: `http/https` only, no embedded credentials, blocks loopback/private/link-local/reserved IPs before navigation and on browser subrequests.
- JavaScript challenge pages get a bounded `10s` settle window. `patchright` provides fingerprint patches against common bot-detection checks; provider-specific warm-up hops and synthetic client-hint headers keep the browser identity stable across requests. No CAPTCHA solving or site-specific bypass logic.
- `fetch()` returns raw HTML by default, or extracted readable text when the request sets `content_format="text"`.
- Returned fetch content is capped at `1 MiB` per URL by default; set `CRAWLY_FETCH_MAX_SIZE` lower when you need smaller MCP payloads. This applies to both `content_format="html"` and `content_format="text"`. Oversized responses are truncated and reported in `truncated`.
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
