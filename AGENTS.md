# AGENTS.md

## Project

`web-search-mcp` exposes two browser-backed operations — `search` and `fetch` — as MCP tools and a CLI, intended for local LLMs that need external web access. Playwright-first, async, using the system-installed Chromium binary.

The authoritative design document is [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md). The user-facing overview is in [README.md](README.md).

## Tooling

- **uv** — project and dependency manager. Use `uv sync`, `uv add`, `uv add --dev`, `uv run <cmd>`. Do not invoke `pip` directly.
- **Playwright + system Chromium** — browser automation. The app launches the Chromium already installed on the host; override with `PLAYWRIGHT_CHROMIUM_EXECUTABLE` if needed.
- **MCP SDK** (`mcp>=1.27.0`) — tool server, stdio and streamable-http transports.
- **pydantic** — request/response models and validation.
- **beautifulsoup4** — HTML parsing for search result extraction.
- **pytest** + **pytest-asyncio** — test runner; async tests are standard.
- **ruff** — formatter and linter (black-compatible, `line-length = 88`). Configured in [pyproject.toml](pyproject.toml) with `E W F I B UP SIM C90 N S A C4 PIE RET ARG PTH ERA PL TRY RUF` plus isort for `web_search_mcp` as first-party.

Common commands:

```sh
uv sync                            # install
PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chromium uv run web-search ... # optional override
uv run pytest                      # tests
uv run ruff check .                # lint
uv run ruff format .               # format
uv run web-search ...              # CLI
uv run web-search serve-mcp        # MCP over stdio
```

## Layout

- [src/web_search_mcp/](src/web_search_mcp/) — package root, `src/`-layout.
  - [browser.py](src/web_search_mcp/browser.py) — shared browser manager, incognito contexts per request, restart-on-disconnect.
  - [security.py](src/web_search_mcp/security.py) — SSRF guard (scheme/credentials/DNS/IP-class checks, Playwright request interception).
  - [parsing.py](src/web_search_mcp/parsing.py) — provider-specific HTML extraction and redirect unwrapping.
  - [challenge.py](src/web_search_mcp/challenge.py) — bounded JS-challenge settle window.
  - [service.py](src/web_search_mcp/service.py) — orchestration, concurrency/timeouts, error shaping.
  - [models.py](src/web_search_mcp/models.py) — pydantic request/response models.
  - [errors.py](src/web_search_mcp/errors.py) — typed error hierarchy.
  - [constants.py](src/web_search_mcp/constants.py) — timeouts, limits, headers.
  - [mcp_server.py](src/web_search_mcp/mcp_server.py) — MCP tool bindings.
  - [cli.py](src/web_search_mcp/cli.py) — CLI entrypoint.
- [tests/](tests/) — unit + async integration tests, HTML fixtures under [tests/fixtures/](tests/fixtures/).
- [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) — the plan this code tracks.

## Approaches

- **Browser lifecycle.** One Playwright Chromium browser per process, fresh incognito context per request; no cookies or storage persist. Browser manager restarts on crash/disconnect.
- **Host compatibility.** Playwright worked with the system Chromium package and failed with the tested system Firefox package, so system Chromium is the supported host-browser path for this repo. Restricted sandboxes may still block Chromium startup even when the host shell works.
- **Concurrency.** Process-wide semaphore caps active page navigations at 3.
- **Timeouts.** `search`: 15s per page, 20s total. `fetch`: 15s per URL, 35s total. JS-challenge settle: 10s.
- **SSRF.** http/https only, no embedded credentials; block loopback, link-local, private, multicast, reserved, and unspecified IPs. DNS is resolved before navigation and re-validated on browser subrequests via route interception.
- **Challenge handling.** Normal in-browser JS execution with bounded wait for interstitial → target transition. No CAPTCHA solving, stealth plugins, proxy rotation, or fingerprint spoofing.
- **Search.** Browser navigates real provider pages; parsing is a separate step operating on rendered HTML. Adapters per provider; redirect wrappers (DuckDuckGo `/l/?uddg=`, Google `/url?q=` / `/url?url=`, Yandex) explicitly unwrapped. Returns 0..5 URLs; zero results is not an error.
- **Fetch.** Partial success is normal: `pages` maps URL → HTML, `errors` maps URL → structured error, `truncated` lists URLs whose HTML exceeded the 1 MiB per-URL cap (UTF-8 safe).
- **robots.txt.** Not consulted in v1 (explicit product decision).
- **Testing.** Parser tests drive against saved HTML fixtures (selector drift is expected); browser/service tests use async integration with controlled Playwright interactions.

## Working with this repo

- Prefer editing existing modules to adding new ones; the module boundaries above are intentional.
- When adding deps, use `uv add` / `uv add --dev` so `pyproject.toml` and `uv.lock` stay in sync.
- Keep the `context` parameter name in the public `search` schema — it is deliberate for caller compatibility.
- Lint before committing: `uv run ruff check . && uv run ruff format --check .`.
- `.codex` and `.claude/` are gitignored; don't add them to commits.

## Changelog

[CHANGELOG.md](CHANGELOG.md) follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) with `Added` / `Changed` / `Fixed` sections under `[Unreleased]`.

- Update `[Unreleased]` as part of the same change that introduces the behavior; don't batch after the fact.
- Record only what differs from the previous version — user-visible behavior, public interfaces, tooling, and dependencies.
- One line per entry, imperative mood, no in-depth technical details or module paths.
- Match the existing record style: terse, single-sentence items (e.g. "SSRF guard for `fetch` URLs.", "Switched browser engine from Firefox to Chromium.").
- Internal refactors with no observable effect don't belong in the changelog.
