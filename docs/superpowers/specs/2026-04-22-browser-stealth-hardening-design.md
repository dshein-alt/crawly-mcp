# Browser stealth hardening — design

**Date:** 2026-04-22
**Scope:** search-provider bot-detection mitigations in `crawly-mcp`
**Status:** spec — awaiting review

## Problem

`crawly-mcp` navigates search providers (DuckDuckGo, Google, Yandex) via Playwright-controlled headless Chromium. After the first or second search, providers flag the session and serve CAPTCHA / consent / challenge pages. The current setup leaks multiple well-known fingerprints: legacy `headless=True`, `navigator.webdriver=true`, UTC timezone paired with `en-US` locale, fresh incognito context per request (no cookie continuity), no client-hint headers, no CDP `Runtime.enable` leak patch.

## Goals

1. Raise the bar against JS-visible bot-detection checks used by major search providers.
2. Preserve per-request isolation for `fetch()` (third-party targets) while allowing cookie continuity for `search()` (trusted providers).
3. Keep architectural decisions narrow — the change is a library swap, a new context-management path, a URLSafetyGuard refactor, and a canary. No new abstraction layers or service reorganization.
4. Ship a regression canary that runs in CI on release tags.

**Scope is not small.** The change touches every file that imports `playwright.async_api` (four of them), the `URLSafetyGuard`, the tests that cover both, the Dockerfile, a CI workflow, and several docs. See "Files touched" for the full list. The architectural shape is narrow; the edit surface is wide.

## Non-goals

- Proxy/IP-reputation handling. Residential proxies are out of scope; a datacenter IP will still get flagged eventually regardless of fingerprint.
- Switching away from Chromium. Firefox/Marionette alternatives were considered and rejected — Chromium's ~70% market share is itself the best camouflage.
- Supporting multiple concurrent operators of one MCP instance. Profiles are shared across all callers; this MCP is single-tenant.

## Policy change

This design is a **deliberate reversal** of two statements in the current repo contract and must be reflected in project documentation before or alongside the code change.

**What the current docs say:**

- [AGENTS.md:61](../../../AGENTS.md) under "Approaches": *"One Playwright Chromium browser per process, fresh incognito context per request; no cookies or storage persist."*
- [AGENTS.md:66](../../../AGENTS.md) under "Approaches": *"No CAPTCHA solving, stealth plugins, proxy rotation, or fingerprint spoofing."*

**Why the reversal.** Real-world usage shows providers flag the current configuration within one or two queries, making the tool unreliable for its stated purpose. The "no stealth plugins or fingerprint spoofing" posture was defensible when the tool worked without them; it does not. The cost of keeping that posture is a tool that fails to deliver search results. The cost of reversing it is a patchright dependency, a persistent on-disk profile directory per provider, and a fingerprint canary.

**Required doc updates** (must land with or before the implementation):

- `AGENTS.md:20` — replace *"Playwright + Chromium"* with *"patchright (Playwright fork) + Chromium"*; note that `patchright install chromium` replaces `playwright install chromium`.
- `AGENTS.md:61` — rewrite to describe the new lifecycle: shared incognito browser for fetch, per-provider persistent contexts for search, 14-day age-based profile cleanup at container start.
- `AGENTS.md:66` — remove the "no stealth plugins or fingerprint spoofing" clause. Keep the "no CAPTCHA solving" and "no proxy rotation" clauses (both still accurate). Add a short sentence naming what *is* done: "uses patchright's fingerprint patches, per-provider persistent profiles, client-hint headers, and a homepage warm-up hop to blend with normal traffic."
- `README.md` — new "Stealth configuration" section (already in Files touched), plus a short note in the overview that the tool now keeps per-provider persistent profiles on disk.

## Decisions

### Library: `patchright` (fork of `playwright`)

Swap `playwright.async_api` for `patchright.async_api`. Drop-in API compatibility; same class names (`Browser`, `BrowserContext`, `Page`, `Playwright`, `Error`, `TimeoutError`). Patches the CDP `Runtime.enable` leak plus `navigator.webdriver`, `window.chrome` shape, WebGL renderer, and the permissions-vs-Notification contradiction. Chromium-only.

**Fallback plan:** if `patchright` becomes unmaintained or regresses badly, revert to stock `playwright` + `playwright-stealth`, accepting the CDP leak as a known limitation. Not implemented in v1; documented here as an escape hatch.

### Persistent context: per-provider for search, fresh incognito for fetch

`BrowserManager` maintains `dict[provider, BrowserContext]` of persistent contexts backed by on-disk `user_data_dir` folders. Search requests reuse the profile keyed by provider. Fetch requests continue to use fresh `new_context()` on the shared `Browser` and close after use — third-party targets must not pollute provider sessions, and malicious fetched pages must not leak cookies back to the next search.

**Two launch paths coexist.** Playwright's `chromium.launch_persistent_context(user_data_dir=..., **opts)` returns a `BrowserContext` that owns its own browser process internally — it is not created via `browser.new_context()` on the shared `Browser`. So `BrowserManager` ends up with three kinds of state:

- `self._playwright: Playwright` — single shared Playwright instance.
- `self._browser: Browser` — the shared incognito browser launched via `chromium.launch()`, used for `new_context()` (fetch path, also any future non-search work).
- `self._search_contexts: dict[str, BrowserContext]` — each entry created via a separate `chromium.launch_persistent_context()` call; each owns its own implicit Chromium process.

`_ensure_browser()` retains its current responsibility (manage `self._browser`). Persistent contexts are created by the new `search_context()` path independently.

Concurrency: `self._lock` guards lazy initialization so two concurrent requests for the same provider don't race to create the persistent context twice.

Crash recovery: if a persistent context becomes disconnected, `search_context()` removes the dead entry from the dict and attempts one re-creation inline before returning. If the re-creation also fails, raise `BrowserUnavailableError` for that request; the next call will retry from scratch. The caller never sees an "ephemeral fallback" context — return type is always a real persistent context (or an error). This keeps the lifetime contract simple: the returned context is always owned by `BrowserManager` and must not be closed by the caller.

**Fetch contexts inherit the stealth config.** Though `fetch()`'s lifetime management is unchanged, the options dict passed to `new_context()` — user-agent, locale, `timezone_id` (from `TZ`), viewport, `extra_http_headers` (including new client hints) — is the same dict used for persistent contexts. One shared `_context_options()` helper on `BrowserManager` returns it.

**User-visible behavior change on fetch.** Since `fetch()` targets arbitrary third-party URLs (not our trusted search providers), the shared identity has observable effects on returned HTML:

- **Timezone-sensitive sites** (calendars, scheduling UIs, some news sites) may format timestamps in the configured `TZ` instead of UTC. In practice this is rare because HTML fetching gets pre-rendered server-side content, and server-side TZ decisions are typically IP-based, not client-header-based.
- **Locale-sensitive sites** may return English content given `Accept-Language: en-US` — unchanged from today, since the locale was already `en-US`.
- **Client-hint-aware sites** (a small but growing set) may serve Chrome-optimized assets based on `sec-ch-ua`. That's neutral: we want Chrome-optimized content because we're using Chrome.

**Why not skip the stealth config for fetch.** Two user-agents across two code paths is itself a bot tell if the same IP serves both: a site that logs both endpoints sees inconsistent identity from one client. Consistency matters more than optimality-per-target. The TZ choice is the only real tradeoff, and the honest answer is that the previous UTC default was itself a fingerprint bug, not a feature worth preserving.

### Timezone: `TZ` env var, default `America/New_York`

Read `TZ` from the process environment (Docker convention). If unset, default to `America/New_York` so the common UTC-in-containers case doesn't leak the obvious UTC+en-US-UA bot signal. Pass through to Playwright as `timezone_id` when creating contexts.

### Headed Chromium under Xvfb as optional mode

Default launch is `--headless=new` (Chromium's modern headless pipeline, much closer to real Chrome than legacy `--headless`). When `CRAWLY_USE_XVFB=true`, launch headed and require `$DISPLAY` to be set by an external wrapper script (`scripts/run-with-xvfb.sh`). Python does not manage the Xvfb process. Xvfb geometry configurable via `CRAWLY_XVFB_GEOMETRY`, default `1280x720x24`. Playwright viewport stays at `1366x768` (decoupled from virtual display size).

### Client hints in headers

Extend `STANDARD_HEADERS` with `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform` values matching the `Chrome/146` + Linux identity in `STANDARD_USER_AGENT`. Sent on every request, search and fetch alike.

### Warm-up hop + jitter

On first search for a given provider within the current process lifetime, `search()` navigates to the provider homepage (`https://duckduckgo.com/`, `https://www.google.com/`, `https://yandex.ru/`) before the actual search URL, then records the provider in a `set[str]` on `BrowserManager`. Subsequent queries for the same provider skip the warm-up.

**Warm-up navigation goes through the shared navigation path.** The warm-up uses `browser_manager.goto(page, homepage_url, timeout_ms=WARMUP_PAGE_TIMEOUT_SECONDS * 1000)` — the same method the real search uses. This respects the global `_navigation_semaphore` (so warm-ups don't bypass the concurrency cap) and the consistent error-shaping the service already depends on.

**Warm-up is best-effort, not fatal.** Failures are caught locally inside `search()`:

- `PlaywrightTimeoutError` → log at `WARNING`, mark the provider as warmed anyway (so we don't retry next request), proceed to the real search.
- `PlaywrightError` (including SSRF blocks against the homepage, which shouldn't happen but let's be defensive) → same: log and proceed.

Rationale: the warm-up is an optimization for stealth, not a correctness requirement. If the provider homepage is unreachable, the real search will probably fail too — and if it somehow succeeds, that's fine. Crashing the search on a flaky homepage fetch would be a regression.

**Timeout budget.** Two separate budgets replace the current single `SEARCH_TOTAL_TIMEOUT_SECONDS=20s` wrapper.

Context acquisition happens **before** the per-request timeout wrapper because `launch_persistent_context()` on first use spawns a fresh Chromium process — measured at 1–3s on warm hardware, up to 5s in a cold container — and `new_page()` adds another ~100ms. Folding that into the query-response budget is not credible; the 0.5s headroom the earlier draft claimed was fictitious.

New structure:

- `SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS = 10` (new constant) — wraps `browser_manager.search_context(provider)` and `context.new_page()`. Exceeding it raises `TimeoutExceededError("context acquisition timed out")`. Runs **outside** the per-request timeout.
- `SEARCH_TOTAL_TIMEOUT_SECONDS = 20` (unchanged) — wraps everything from warm-up through result parsing.

Budget inside the 20s wrapper for a first-use search:

- warm-up navigation: up to `WARMUP_PAGE_TIMEOUT_SECONDS = 3` (best-effort; tight cap so it can't dominate)
- jitter: up to 1.5s (from `CRAWLY_SEARCH_JITTER_MS` default `500,1500`)
- real search navigation: up to `SEARCH_PAGE_TIMEOUT_SECONDS = 15`
- result parsing + provider-block check: sub-second, budgeted informally
- total: ~19.5s, under 20s

Subsequent queries to the same provider skip the warm-up *and* skip the `launch_persistent_context()` cost (context is cached), so they effectively get the full 20s for the real navigation after a ~100ms `new_page()`.

If the tuning changes (jitter widened, warm-up cap raised, network pathologically slow), `SEARCH_TOTAL_TIMEOUT_SECONDS` must be revisited in the same change. The two-budget split makes it honest about where time goes.

**Why process-lifetime tracking rather than checking cookie presence.** The persistent profile may already contain cookies from a previous process run, so strictly speaking the warm-up is redundant then. We still do it once per process anyway because (a) it's cheap (one extra navigation per provider per process start, capped at 3s), (b) provider sessions may have been invalidated while we were down and the warm-up re-establishes them, and (c) cookie-based detection would add complexity for marginal value.

Between warm-up and real navigation — or between context acquisition and navigation when warm-up is skipped — sleep `random.uniform(min, max)` ms. Bounds read from `CRAWLY_SEARCH_JITTER_MS`, default `500,1500`. Uses `random`, not `secrets`.

### Yandex domain: `.ru` everywhere

Both the warm-up URL and the search URL in `parsing.py` switch from `yandex.com` to `yandex.ru`. The allow-list at `parsing.py:14` already includes `yandex.ru`; only the URL template at `parsing.py:20` and the corresponding test fixtures need updating.

### Stale profile cleanup at startup

**Deployment assumption (explicit).** Cleanup is designed for a **single-instance container-recreation** deployment model: one `crawly-mcp` process owns the profile directory, the container is recreated (not live-upgraded) to pick up new images, and nothing else on the host reads or writes those profile directories. Under this assumption, the profile directory cannot be concurrently in use by another Chromium process at startup, so age-based deletion by mtime is safe.

**Not safe for:** shared bind mounts across multiple concurrent `crawly-mcp` instances on one host, hot-reload deployments where one process's startup overlaps another's shutdown, or any setup where the profile dir is also used by a desktop Chromium session.

**Gate behind an explicit env var.** Cleanup runs only when `CRAWLY_PROFILE_CLEANUP_ON_START=true`. Default is `false`. The `Dockerfile`'s `ENTRYPOINT`/`CMD` will set this to `true`, so container deployments get cleanup automatically; `uv run crawly-mcp` on a dev machine does not touch profile dirs unless the operator opts in. The README must document both the default and the assumption.

**Behavior when enabled.** On `BrowserManager.start()`, scan `CRAWLY_PROFILE_DIR`. For each subdirectory whose mtime is older than `CRAWLY_PROFILE_MAX_AGE_DAYS` days (default `14`), remove it. Logged at `INFO` with count and total reclaimed size. Errors deleting any single directory are logged at `WARNING` and do not abort startup.

### Canary: inline-only, runs in release CI

`scripts/fingerprint_check.py` launches Chromium via `BrowserManager` (same config path as the service), navigates to `about:blank`, and runs a hardcoded list of JS assertions:

- `navigator.webdriver === false`
- `navigator.plugins.length > 0`
- `navigator.languages.length > 0`
- `typeof window.chrome?.runtime !== "undefined"`
- WebGL `UNMASKED_RENDERER_WEBGL` does not contain `"SwiftShader"` or `"llvmpipe"`
- `Notification.permission === "default"` iff `permissions.query({name:"notifications"})` returns `"prompt"` (no contradiction)

Prints a three-column table (`check | status | value`); exits `1` if any check fails, `0` otherwise. A `--verbose` flag prints values even on pass.

No `bot.sannysoft.com` dependency. Deterministic, fully offline after Chromium launch.

CI: new job `fingerprint-check` in `.github/workflows/tests.yml`, gated to release tags (`if: startsWith(github.ref, 'refs/tags/v')`). Runs `uv run patchright install chromium --with-deps` then `uv run python scripts/fingerprint_check.py`. Not run on PR or push (~400MB Chromium download is too heavy for every PR; regressions at release time are the gate that matters).

### URLSafetyGuard: page-keyed blocked-request tracking

The existing `URLSafetyGuard` holds a global `_blocked_requests: list[BlockedRequest]` appended to by the route handler and drained by `pop_blocked_error()`. That works when a guard is scoped to a single request (one context, one page). With a long-lived persistent context serving concurrent searches, one guard would mix blocked-request errors from multiple pages and `pop_blocked_error()` would return a different page's error.

Change `URLSafetyGuard` so blocked requests are keyed by `Page`:

- `_blocked_requests: dict[Page, list[BlockedRequest]]`
- `handle_route()` looks up the originating page via `route.request.frame.page` and appends to that page's list.
- New method `pop_blocked_error(page: Page) -> URLSafetyError | None` pops from that page's list. The old zero-arg form is removed — callers pass the page they own.
- `attach(context)` stays the same; can be called once per context, even when the context is long-lived.
- Pages are cleaned from the dict on `page.on("close", ...)`: when a page first appears in the dict (on its first blocked request), the guard subscribes to its `close` event with a handler that removes the page's entry. This covers both paths: (a) when the navigation fails and the caller drains via `pop_blocked_error(page)`, and (b) when the navigation succeeds but had blocked subresources that the caller never inspected. Relying on `pop_blocked_error()` for cleanup would leak an entry per successful-with-blocked-subresource page, which is unbounded over the lifetime of a long-lived persistent context.

`service.py` call sites update: `search()` passes its `page` to `pop_blocked_error(page)`; `_fetch_one()` does the same. The DNS `_resolve_cache` is unchanged — shared across requests is correct (results don't depend on caller).

### BrowserManager public interface after the change

New and changed methods on `BrowserManager`:

```python
async def search_context(self, provider: str) -> SearchContextHandle
```
Where `SearchContextHandle` is a frozen dataclass `(context: BrowserContext, guard: URLSafetyGuard, first_use: bool)`.

- Lazily creates (or returns cached) persistent `BrowserContext` for `provider` via `chromium.launch_persistent_context(user_data_dir=<profile_dir>/<provider>, **self._context_options())`.
- On first creation, attaches a long-lived `URLSafetyGuard`, stores both in the manager's internal dicts, and returns `first_use=True`. On cache hit, returns `first_use=False`.
- On cached-context disconnect: removes the dead entry, attempts one inline re-creation; on re-creation failure, raises `BrowserUnavailableError`. Never returns a caller-owned fallback context.
- Caller creates pages via `handle.context.new_page()`, uses them, and closes the pages — but NOT the context.
- Caller is responsible for the warm-up navigation and jitter sleep when `handle.first_use` is true.

**Why return the guard in the handle instead of a separate `guard_for(provider)` lookup.** The earlier draft had callers look up the guard by provider name during error handling. That's fragile: a disconnected context, a race, or any "shouldn't happen" state turns the lookup into `KeyError` on the error path, masking the original navigation failure. Returning the guard with the context makes the association lifetime-scoped — the caller has exactly what it needs and the manager has no ambient-state lookup that can fail.

```python
async def new_context(self) -> BrowserContext  # unchanged signature
```
- Now uses `self._context_options()` internally so fetch inherits the new TZ, client-hint headers, UA, etc.

```python
def _context_options(self) -> dict[str, Any]  # new private helper
```
- Returns `{user_agent, locale, timezone_id, viewport, java_script_enabled, extra_http_headers}` computed once from constants + env at method-call time.

```python
async def close(self) -> None  # updated
```
- Closes each persistent context in `self._search_contexts` before closing `self._browser` and stopping `self._playwright`.

```python
async def start(self) -> None  # updated
```
- If `CRAWLY_PROFILE_CLEANUP_ON_START=true`, runs profile cleanup before ensuring the shared browser: iterates `CRAWLY_PROFILE_DIR`, deletes subdirectories with mtime older than `CRAWLY_PROFILE_MAX_AGE_DAYS`. Logs count and reclaimed size. Otherwise no-ops on the cleanup step.

### service.py call-site changes

`search()`:

```python
# context acquisition is OUTSIDE the per-request timeout wrapper because
# launch_persistent_context() can take several seconds on first use.
async with asyncio.timeout(SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS):
    handle = await self._browser_manager.search_context(request.provider)
    page = await handle.context.new_page()

try:
    async with asyncio.timeout(SEARCH_TOTAL_TIMEOUT_SECONDS):
        if handle.first_use:
            try:
                await self._browser_manager.goto(
                    page,
                    PROVIDER_HOMEPAGE[request.provider],
                    timeout_ms=WARMUP_PAGE_TIMEOUT_SECONDS * 1000,
                )
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                logger.warning(
                    "warmup failed provider={} reason={}", request.provider, exc,
                )
                # proceed anyway; warm-up is best-effort
        await _sleep_jitter()
        try:
            await self._browser_manager.goto(
                page, search_url, timeout_ms=SEARCH_PAGE_TIMEOUT_SECONDS * 1000,
            )
        except PlaywrightError as exc:
            blocked = handle.guard.pop_blocked_error(page)
            if blocked is not None:
                raise blocked from exc
            raise NavigationFailedError(...) from exc
        ...
finally:
    await page.close()  # context is NOT closed
```

`fetch()`: unchanged in structure. `new_context()` is still created and closed per request; call sites update `pop_blocked_error()` to pass the page.

## Config surface

| Env var | Default | Purpose |
|---|---|---|
| `CRAWLY_USE_XVFB` | `false` | Launch headed under Xvfb instead of `--headless=new`. |
| `CRAWLY_XVFB_GEOMETRY` | `1280x720x24` | Passed to `xvfb-run -s "-screen 0 <geometry>"`. |
| `CRAWLY_PROFILE_DIR` | `~/.cache/crawly/profiles` | Parent dir for per-provider persistent profiles. Subdirs created on demand. |
| `CRAWLY_PROFILE_CLEANUP_ON_START` | `false` | Enable age-based profile cleanup at startup. Set to `true` by the Dockerfile entrypoint; dev invocations leave it off. Unsafe when profile dir is shared across concurrent processes. |
| `CRAWLY_PROFILE_MAX_AGE_DAYS` | `14` | Startup cleanup threshold. Only consulted when cleanup is enabled. |
| `CRAWLY_SEARCH_JITTER_MS` | `500,1500` | Min/max ms between warm-up and real query. Two-int CSV. |
| `WARMUP_PAGE_TIMEOUT_SECONDS` (constant, not env) | `3` | Per-warmup-navigation timeout; warm-up failures are best-effort. |
| `SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS` (constant, not env) | `10` | Budget for `search_context()` + `new_page()`. Runs outside the per-request `SEARCH_TOTAL_TIMEOUT_SECONDS` wrapper so first-use search isn't penalized by one-shot Chromium process startup. |
| `TZ` | `America/New_York` if unset | Timezone passed to Playwright contexts. Follows Docker convention. |

Existing env vars (`PLAYWRIGHT_BROWSER_SOURCE`, `PLAYWRIGHT_CHROMIUM_EXECUTABLE`, `CRAWLY_HOST`, `CRAWLY_PORT`) are unchanged.

## Files touched

### Patchright migration — import blast radius

The `playwright.async_api` module is imported from four places in the repo today. All four need to switch to `patchright.async_api`. Patchright re-exports the same class names, so the migration is mechanical but not zero-touch: every `import` line must change, and the two test monkeypatches must retarget.

- [src/crawly_mcp/browser.py:9,11](../../../src/crawly_mcp/browser.py) — `import playwright.async_api as playwright_api` + `from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page, Playwright`.
- [src/crawly_mcp/service.py:9](../../../src/crawly_mcp/service.py) — `from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError`.
- [src/crawly_mcp/security.py:11](../../../src/crawly_mcp/security.py) — `from playwright.async_api import BrowserContext, Route`.
- [tests/test_browser.py:5](../../../tests/test_browser.py) — `import playwright.async_api as playwright_api`, plus `monkeypatch.setattr(playwright_api, "async_playwright", fake_async_playwright)` at lines 127 and 178. These monkeypatches must target the patchright module instead, or be restructured to patch via the module the production code imports.

### Runtime and config files

- `src/crawly_mcp/constants.py` — new env vars (`CRAWLY_USE_XVFB`, `CRAWLY_XVFB_GEOMETRY`, `CRAWLY_PROFILE_DIR`, `CRAWLY_PROFILE_CLEANUP_ON_START`, `CRAWLY_PROFILE_MAX_AGE_DAYS`, `CRAWLY_SEARCH_JITTER_MS`), new constants (`WARMUP_PAGE_TIMEOUT_SECONDS`), client-hint headers in `STANDARD_HEADERS`, UA touch-up to match client hints.
- `src/crawly_mcp/browser.py` — import swap; Xvfb preflight; `_context_options()` helper; `search_context(provider)` method returning a new `SearchContextHandle` dataclass `(context, guard, first_use)`; `_search_contexts` and parallel guard dict; gated stale-profile cleanup in `start()`; crash re-creation logic; `close()` updated to close persistent contexts.
- `src/crawly_mcp/service.py` — import swap; `search()` splits into two timeout budgets (context acquisition outside, per-request work inside), acquires persistent context via the new `SearchContextHandle`, does best-effort warm-up via `browser_manager.goto()`, jitters, navigates; `pop_blocked_error(page)` call sites updated in both `search()` and `_fetch_one()`. `fetch()` structure otherwise unchanged.
- `src/crawly_mcp/parsing.py` — Yandex URL template: `yandex.com` → `yandex.ru`. The allow-list tuple at `parsing.py:14` keeps `yandex.com` alongside `yandex.ru` intentionally (redirect normalization / geo-specific landing pages).
- `src/crawly_mcp/security.py` — import swap; `URLSafetyGuard._blocked_requests` becomes a `dict[Page, list[BlockedRequest]]`; `pop_blocked_error()` gains a required `page` parameter; `handle_route()` keys by `route.request.frame.page`.

### Tests

- `tests/test_browser.py` — import swap + monkeypatch retarget (lines 127, 178); add coverage for `search_context()`, `_context_options()`, and gated profile cleanup.
- `tests/test_security.py` — updated for the `pop_blocked_error(page)` signature change.
- `tests/test_parsing.py` — fixtures updated for the Yandex `.ru` change.

### New files

- `scripts/fingerprint_check.py` — standalone inline-only canary.
- `scripts/run-with-xvfb.sh` — wrapper for the Xvfb launch path. Uses `xvfb-run -a` so two processes on the same host don't collide on `$DISPLAY`.

### Docs and config

- `.github/workflows/tests.yml` — new tag-gated `fingerprint-check` job.
- `pyproject.toml` — dependency swap: stock `playwright` → `patchright` (pinned to a specific version).
- `Dockerfile` — install `xvfb` package; switch entrypoint to use `scripts/run-with-xvfb.sh` when `CRAWLY_USE_XVFB=true`; export `CRAWLY_PROFILE_CLEANUP_ON_START=true` in the container environment.
- `AGENTS.md` — updates at lines 20, 61, 66 per the Policy change section above.
- `README.md` — new "Stealth configuration" section covering env vars, profile volume mount, canary invocation; overview note about on-disk profiles.

## Operational notes

- **Profile persistence requires a writable mount.** In Docker, `CRAWLY_PROFILE_DIR` must be backed by a named volume (or bind mount) with read-write permissions for the container's user. Default `~/.cache/crawly/profiles` inside a read-only rootfs will fail at startup. README must call this out.
- **Profile directory is NOT safe for concurrent sharing.** The cleanup step assumes a single-instance container-recreation deployment. Running two `crawly-mcp` processes against the same `CRAWLY_PROFILE_DIR` risks one process deleting profiles the other still has Chromium sessions open on. Sharding by process (separate `CRAWLY_PROFILE_DIR` per container) is the supported way to run multiple instances on one host.
- **Profile size growth** is bounded by the age-based cleanup (default 14 days). Operators running high-volume instances may want to drop this to 7 days or run a separate sweeper; exposed as `CRAWLY_PROFILE_MAX_AGE_DAYS`. Cleanup runs only when `CRAWLY_PROFILE_CLEANUP_ON_START=true` (set by the Dockerfile entrypoint).

## Risks

1. **patchright supply-chain.** Community fork pinned to a specific Playwright version. Mitigation: pin in `pyproject.toml`, review changelog before bumps. Escape hatch: revert to stock playwright + playwright-stealth.
2. **Fingerprint drift.** Anti-bot vendors and browser releases both evolve. The inline canary catches breakage in what we explicitly check, but not novel detections. Expect periodic updates to the check list and occasional stealth-config tuning. Manual spot-check against real providers remains part of the release process.
3. **Shared-context concurrency.** Up to `MAX_CONCURRENT_NAVIGATIONS=3` requests share one `BrowserContext` per provider. Each uses its own `Page`, but cookies/localStorage are shared — same as tabs in a real browser. No defensive code; accepted as correct behavior.

## Acceptance criteria

- All existing `pytest` tests pass with no regression.
- `scripts/fingerprint_check.py` exits `0` on the service's default launch config (`--headless=new`) and on the Xvfb launch config.
- Manual verification: ~20 consecutive DDG searches from a dev machine without a CAPTCHA / challenge page.
- Release CI (`fingerprint-check` job) passes on the tag that publishes this change.
- Profile cleanup logs a reasonable message at startup on a fresh install (zero deletions) and on a stale install (nonzero deletions). Only runs when `CRAWLY_PROFILE_CLEANUP_ON_START=true`.
- **Fetch regression check (new).** Manual spot-check: `crawly-cli fetch` against a curated list of 5 target URLs (a static content page, a JS-rendered SPA, a geo-aware news site, a timezone-aware calendar page, and a site that serves different HTML based on `sec-ch-ua`). Compare returned HTML byte count and presence of an expected marker string before and after the change. Differences in TZ-rendered timestamps are acceptable; missing content or major markup divergence is not. This is a manual, one-time release check — not CI.

## Out of scope / follow-ups

- Residential or mobile proxy integration.
- Profile size-capping independent of age (for very-high-volume deployments).
- A sweeper cron or systemd timer external to `BrowserManager.start()`.
- Stock-playwright fallback code path (documented but not implemented).
- Per-PR fingerprint canary (requires Chromium download caching).
