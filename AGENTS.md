# AGENTS.md

## Project

`crawly` exposes two browser-backed operations — `search` and `fetch` — as MCP tools and a CLI for local LLM workflows. The implementation is built on `patchright` (a Playwright fork with bundled fingerprint patches), is async, and supports either a host Chromium binary or a patchright-managed Chromium bundle depending on runtime configuration.

The authoritative design document is [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md). The user-facing overview is in [README.md](README.md).

## Naming

- Product name: `crawly`
- Python distribution: `crawly-mcp`
- Import package: `crawly_mcp`
- CLI executable: `crawly-cli`
- MCP server executable: `crawly-mcp`

## Tooling

- **uv** — project and dependency manager. Use `uv sync`, `uv add`, `uv add --dev`, `uv run <cmd>`. Do not invoke `pip` directly.
- **patchright + Chromium** — browser automation via the `patchright` Playwright fork, which adds the fingerprint patches we depend on for search-provider traffic. Host mode uses a system Chromium binary; bundled mode uses patchright-managed Chromium (`patchright install chromium`).
- **MCP SDK** (`mcp>=1.27.0`) — tool server, stdio, SSE, and streamable-http transports.
- **pydantic** — request/response models and validation.
- **beautifulsoup4** — HTML parsing for search result extraction.
- **pytest** + **pytest-asyncio** — test runner; async tests are standard.
- **ruff** — formatter and linter (black-compatible, `line-length = 88`). Configured in [pyproject.toml](pyproject.toml) with `E W F I B UP SIM C90 N S A C4 PIE RET ARG PTH ERA PL TRY RUF` plus isort for `crawly_mcp` as first-party.
- **ast-index** — native Rust CLI for structural code lookup across the Python tree. Use it as the primary code-search tool in this repo: prefer `ast-index search`, `ast-index class`, `ast-index usages`, and `ast-index refs` over ad-hoc `grep`/`rg` when locating symbols, call sites, or implementations. Keep the index fresh with `ast-index update`, or rebuild from scratch with `ast-index rebuild` after large refactors. Useful entry points: `ast-index map`, `ast-index conventions`, `ast-index outline <file>`, and `ast-index changed --base main` before review. Fall back to `rg` only for regex patterns, comment content, or string literals that are not indexed.
- **loguru** — runtime logging. All modules log through `loguru.logger`; the `crawly_mcp._logging.configure_logging` helper installs a single stderr sink (critical for stdio MCP transport) and intercepts stdlib `logging` so uvicorn and MCP SDK logs share the same sink. Level is controlled by `CRAWLY_LOG_LEVEL` (default `INFO`; accepted values `TRACE DEBUG INFO WARNING ERROR CRITICAL`). Log entry/exit of tool calls at `INFO`; reserve `DEBUG` for fine-grained parser/challenge/timing traces.

Common commands:

```sh
uv sync
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium uv run crawly-cli search --context "python"
PLAYWRIGHT_BROWSER_SOURCE=bundled uv run crawly-cli fetch https://example.com
uv run crawly-mcp
CRAWLY_HOST=0.0.0.0 CRAWLY_PORT=8000 uv run crawly-mcp --transport streamable-http
uv run pytest
uv run ruff check .
```

## Layout

- [src/crawly_mcp/](src/crawly_mcp/) — package root, `src/`-layout.
  - [browser.py](src/crawly_mcp/browser.py) — browser manager and browser-source selection.
  - [security.py](src/crawly_mcp/security.py) — SSRF guard and request interception.
  - [parsing.py](src/crawly_mcp/parsing.py) — provider-specific HTML extraction and redirect unwrapping.
  - [challenge.py](src/crawly_mcp/challenge.py) — bounded JS-challenge settle window.
  - [service.py](src/crawly_mcp/service.py) — orchestration, concurrency/timeouts, error shaping.
  - [models.py](src/crawly_mcp/models.py) — pydantic request/response models.
  - [errors.py](src/crawly_mcp/errors.py) — typed error hierarchy.
  - [constants.py](src/crawly_mcp/constants.py) — timeouts, limits, and env names.
  - [mcp_server.py](src/crawly_mcp/mcp_server.py) — MCP tool bindings.
  - [cli.py](src/crawly_mcp/cli.py) — search/fetch CLI entrypoint.
  - [mcp_cli.py](src/crawly_mcp/mcp_cli.py) — MCP server entrypoint.
- [tests/](tests/) — unit + async integration tests, with fixtures under [tests/fixtures/](tests/fixtures/).
- [.github/workflows/container.yml](.github/workflows/container.yml) — container build, smoke test, and publish workflow.
- [Dockerfile](Dockerfile) — Playwright-based Ubuntu container image.

## Approaches

- **Browser lifecycle.** One Chromium browser per process for fetch (fresh incognito context per request; no cookies persist across fetches). Per-search-provider persistent contexts with on-disk profiles keyed by provider. Profile dirs live under `CRAWLY_PROFILE_DIR` (default `~/.cache/crawly/profiles`) and are age-pruned at startup when `CRAWLY_PROFILE_CLEANUP_ON_START=true` (set automatically by the Docker entrypoint). Browser manager restarts on crash/disconnect.
- **Browser source.** `PLAYWRIGHT_BROWSER_SOURCE=system` uses a host Chromium binary; `bundled` uses Playwright-managed Chromium without `executable_path`.
- **Concurrency.** Process-wide semaphore caps active page navigations at 3.
- **Timeouts.** `search`: 15s per page, 20s total. `fetch`: 15s per URL, 35s total. JS-challenge settle: 10s.
- **SSRF.** http/https only, no embedded credentials; block loopback, link-local, private, multicast, reserved, and unspecified IPs. DNS is resolved before navigation and re-validated on browser subrequests via route interception.
- **Challenge handling.** Normal in-browser JS execution with bounded wait for interstitial → target transition. Uses patchright's fingerprint patches, per-provider persistent profiles, client-hint headers, and a homepage warm-up hop to blend with normal traffic. No CAPTCHA solving and no proxy rotation.
- **Search.** Browser navigates real provider pages; parsing is a separate step operating on rendered HTML. Adapters per provider; redirect wrappers are explicitly unwrapped. Returns 0..5 URLs; zero results is not an error.
- **Fetch.** Partial success is normal: `pages` maps URL → HTML, `errors` maps URL → structured error, `truncated` lists URLs whose HTML exceeded the 1 MiB per-URL cap.
- **Container interface.** HTTP MCP via `streamable-http` is the primary container interface. The endpoint is unauthenticated in v1 and should sit behind localhost, private networking, or an auth/TLS proxy.

## Working With This Repo

- Prefer editing existing modules to adding new ones; the current boundaries are intentional.
- When adding deps, use `uv add` / `uv add --dev` so `pyproject.toml` and `uv.lock` stay in sync.
- Keep the public `context` parameter name in the `search` schema.
- Lint before committing: `uv run ruff check . && uv run ruff format --check .`.
- `.codex` and `.claude/` are gitignored; don't add them to commits.
- Rebrand verification should be reproducible. Use:

```sh
rg -n "web-search|web_search_mcp" README.md AGENTS.md CHANGELOG.md pyproject.toml src tests
rg -n "\\bcrawly\\b" src tests pyproject.toml  # should list only the MCP server display name; imports use crawly_mcp
```

## Changelog

[CHANGELOG.md](CHANGELOG.md) follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with `Added` / `Changed` / `Fixed` sections under `[Unreleased]`.

- Update `[Unreleased]` as part of the same change that introduces the behavior; don't batch after the fact.
- Record only user-visible behavior, public interfaces, tooling, and dependencies.
- One line per entry, imperative mood, no module paths.
- Match the existing terse style.
