# External Web Search MCP Tool for Local LLMs

## Summary
Create a new Python project managed with `uv` that exposes two async operations as MCP tools and a CLI:

- `search(provider: str | None = "duckduckgo", context: str)`:
  run a browser-backed search on `duckduckgo`, `google`, or `yandex` and return up to 5 organic result URLs.
- `fetch(urls: list[str])`:
  fetch up to 5 URLs and return browser-rendered HTML for each successful URL.

The implementation is Playwright-first, uses a shared long-lived browser with isolated per-request contexts, includes explicit SSRF protections, fixed concurrency/timeouts, and supports normal in-browser JavaScript execution for sites that gate access behind browser-computed challenge pages.

## Public Interfaces
- MCP tool `search`
  - Input:
    - `provider`: optional, default `"duckduckgo"`, allowed `duckduckgo|google|yandex`
    - `context`: required string query
  - Output:
    - `urls: list[str]`
  - Error cases:
    - `invalid_provider`
    - `invalid_input`
    - `browser_unavailable`
    - `provider_blocked`
    - `timeout`
  - Behavior:
    - successful search with zero organic results returns `urls: []`
    - `context` is kept intentionally for caller compatibility and documented as “search query text”
- MCP tool `fetch`
  - Input:
    - `urls: list[str]`, required, length `1..5`
  - Output:
    - `pages: dict[str, str]`
    - `errors: dict[str, { type: str, message: str }]`
    - `truncated: list[str]`
  - Behavior:
    - partial success is normal
    - MCP-level error only for invalid input or browser startup failure
    - per-URL failures are reported in `errors`
- CLI wrapper
  - `web-search search --provider duckduckgo --context "..."`
  - `web-search fetch <url1> <url2> ...`
  - JSON output mirrors the MCP payloads

## Implementation Changes
- Bootstrap with `uv`:
  - `uv init`
  - `uv add playwright <mcp-lib> pydantic`
  - `uv add --dev pytest pytest-asyncio`
  - document requirement for a host Chromium binary on `PATH` or an explicit `PLAYWRIGHT_CHROMIUM_EXECUTABLE` override
- Browser/process model:
  - one shared Playwright browser per server process
  - one fresh incognito browser context per tool invocation
  - one or more pages inside that context for the request
  - browser manager recreates the shared browser if it crashes or disconnects
  - no cookies, storage, or session state persist across requests
- Concurrency and timeouts:
  - process-wide semaphore limiting active page navigations to `3`
  - `search` page timeout: `15s`, overall tool timeout: `20s`
  - `fetch` per-URL timeout: `15s`, overall tool timeout: `35s`
  - use `domcontentloaded` plus provider/page-specific readiness checks, not `networkidle`
- SSRF and URL safety:
  - accept only `http` and `https`
  - reject URLs with embedded credentials
  - block `localhost`, loopback, link-local, private, multicast, reserved, and unspecified IP targets
  - resolve DNS before navigation; if any resolved A/AAAA record is non-public, reject the URL
  - enforce the same checks on redirects and browser network requests via Playwright request interception
  - explicitly block metadata-style targets such as `169.254.169.254`
- JavaScript challenge handling:
  - allow first-party page JavaScript to execute in a real browser context
  - for `fetch`, after initial navigation, detect whether the page is still on an intermediate challenge screen
  - wait up to a bounded “challenge settle” window of `10s` for same-tab navigation, DOM replacement, or a recognizable transition from challenge page to target page
  - if the browser reaches the destination page within that window, return the final rendered HTML
  - if the page remains blocked, report a per-URL `challenge_blocked` error
  - do not add site-specific bypass code, CAPTCHA solving, proxy rotation, stealth plugins, or nonstandard fingerprint spoofing
- Search extraction:
  - separate browser navigation from HTML parsing/extraction logic
  - parse provider result HTML using provider-specific adapters
  - deduplicate and return the first 5 organic external URLs
  - explicitly support known redirect wrappers:
    - DuckDuckGo `/l/?uddg=...`
    - Google `/url?q=...` and `/url?url=...`
    - Yandex current wrapper/selectors are fixture-pinned from captured real result pages during implementation
  - treat CAPTCHA/consent/challenge pages as `provider_blocked`
- Fetch behavior:
  - fetch pages concurrently within the global semaphore
  - return `page.content()` HTML
  - cap returned HTML at `1 MiB` per URL, UTF-8 truncated safely
  - record truncated URLs in `truncated`
- Documentation:
  - README includes setup with `uv`, browser install, CLI usage, MCP usage, timeout/error semantics, and an explicit note that v1 does not consult `robots.txt`

## Test Plan
- Unit tests:
  - provider validation/defaulting
  - URL validation and max-5 enforcement
  - SSRF guard for scheme, credentials, DNS resolution, and blocked IP classes
  - redirect-unwrapping helpers
  - truncation behavior and structured error shaping
- Parser tests using saved HTML fixtures from real search result pages:
  - DuckDuckGo extraction
  - Google extraction
  - Yandex extraction
  - challenge/consent/no-results pages
- Integration-style tests with mocked Playwright boundaries:
  - browser manager lifecycle and restart-on-disconnect
  - request interception blocks disallowed targets
  - fetch partial-success behavior under timeout/navigation failures
  - concurrency cap enforcement
  - challenge flow where JS transitions from interstitial page to final page within timeout
  - challenge flow that never resolves and returns `challenge_blocked`
- CLI tests:
  - command parsing
  - JSON output shape
  - invalid input exits non-zero with useful error text
- Acceptance scenarios:
  - omitted `provider` defaults to DuckDuckGo
  - successful `search` returns `0..5` URLs
  - blocked provider returns `provider_blocked`
  - `fetch` returns `pages`, `errors`, and `truncated`
  - private/loopback/link-local targets are rejected before fetch

## Assumptions and Defaults
- Greenfield repo; no existing project structure must be preserved.
- Python baseline: `>=3.11`; local environment currently has Python `3.13` and `uv 0.11.7`.
- v1 is Playwright/Chromium-only, using a system-installed Chromium binary rather than a Playwright-downloaded browser bundle.
- `context` remains the public parameter name for compatibility with the requested tool contract.
- `search` returning fewer than 5 URLs is acceptable; zero URLs is not itself an error.
- `fetch` returns raw browser HTML, not readability text.
- v1 does not honor `robots.txt`; this is an explicit product decision.
- Selector/wrapper maintenance is an expected ongoing cost; fixture-based parser tests are the main guard against silent breakage.
