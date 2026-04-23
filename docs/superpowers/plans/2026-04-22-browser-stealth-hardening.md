# Browser stealth hardening — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden `crawly-mcp` against search-provider bot detection by swapping to `patchright`, introducing per-provider persistent browser contexts for search (incognito stays for fetch), adding a homepage warm-up hop + jitter, TZ-aware context options, client-hint headers, an optional Xvfb mode, a release-gated fingerprint canary, and gated profile-directory cleanup at container startup.

**Architecture:** Library swap (`playwright` → `patchright`), narrow `BrowserManager` refactor adding a `search_context(provider) → SearchContextHandle(context, guard, first_use)` path alongside the existing shared-browser + incognito-context path used by fetch. `URLSafetyGuard` becomes page-keyed so one long-lived guard can serve concurrent searches. `service.search()` gains a two-tier timeout (context acquisition outside the per-request budget, navigation inside it) and a best-effort warm-up via `browser_manager.goto()`. Everything else is infra (Xvfb wrapper, Dockerfile, CI job, docs).

**Tech Stack:** Python 3.11+, `patchright` (drop-in Playwright fork), `uv`, `pytest` + `pytest-asyncio`, `loguru`, `ruff`.

**Reference spec:** [docs/superpowers/specs/2026-04-22-browser-stealth-hardening-design.md](../specs/2026-04-22-browser-stealth-hardening-design.md). When this plan is terse, the spec has the semantic detail.

---

## File structure

One new top-level dataclass (`SearchContextHandle`) lives in `browser.py` alongside `BrowserManager`. No new modules — the spec explicitly chose to keep this inline. Files touched:

**Runtime modules (modified):**
- `src/crawly_mcp/constants.py` — new env vars, new constants, updated `STANDARD_HEADERS` (client hints), slightly updated `STANDARD_USER_AGENT`.
- `src/crawly_mcp/security.py` — `URLSafetyGuard._blocked_requests` becomes `dict[Page, list[BlockedRequest]]`; `pop_blocked_error()` takes required `page`; import swap.
- `src/crawly_mcp/browser.py` — import swap; `_context_options()`; `SearchContextHandle` dataclass; `search_context()`; gated profile cleanup in `start()`; Xvfb preflight; `close()` updated for persistent contexts.
- `src/crawly_mcp/service.py` — import swap; `search()` uses handle + two-tier timeouts + warm-up + jitter; `_fetch_one()` uses page-keyed `pop_blocked_error()`.
- `src/crawly_mcp/parsing.py` — Yandex URL template → `yandex.ru`.

**Tests (modified):**
- `tests/test_security.py` — guard signature updates.
- `tests/test_browser.py` — monkeypatch retarget; new tests for `search_context`, `_context_options`, cleanup.
- `tests/test_service.py` — update for handle and new timeout structure.
- `tests/test_parsing.py` — Yandex URL fixtures.

**New files:**
- `scripts/fingerprint_check.py` — inline-only canary.
- `scripts/run-with-xvfb.sh` — Xvfb wrapper for the Docker entrypoint.

**Docs and config:**
- `pyproject.toml` — drop `playwright`, add `patchright` pinned.
- `Dockerfile` — install `xvfb`, export `CRAWLY_PROFILE_CLEANUP_ON_START=true`, optional xvfb entrypoint.
- `.github/workflows/tests.yml` — tag-gated `fingerprint-check` job.
- `AGENTS.md` — lines 20, 61, 66 updated per spec.
- `README.md` — new "Stealth configuration" section.
- `CHANGELOG.md` — `[Unreleased]` entry.

---

## Chunk 1: Foundation — deps, constants, URLSafetyGuard, AGENTS.md

This chunk lands the dependency swap, new constants/env vars, the URLSafetyGuard page-keyed refactor (which is a prerequisite for the BrowserManager work), and the AGENTS.md policy update. All tests pass after this chunk; nothing yet uses the new constants or persistent contexts — that comes in Chunk 2.

### Task 1: Add patchright alongside playwright

**Why not remove `playwright` now:** `browser.py`, `service.py`, and `security.py` still `import playwright.async_api` until Task 3 and Task 5 swap them. Removing the package first would make the suite red mid-chunk. We add `patchright` here, swap the imports in Tasks 3/5, and finally drop `playwright` as the last step of Task 5.

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add patchright pinned**

Run: `uv add 'patchright>=1.49,<2.0'`
Expected: `patchright` added to `pyproject.toml` `[project.dependencies]`; `uv.lock` updated; `playwright` still present. (Adjust the version range to what actually installs; check `uv pip list | grep -E "patchright|playwright"` afterward.)

- [ ] **Step 2: Install patchright's chromium download**

Run: `uv run patchright install chromium --with-deps`
Expected: exits 0; downloads complete. (Required for later tasks that invoke Chromium.)

- [ ] **Step 3: Verify the package is importable**

Run: `uv run python -c "import patchright.async_api as p; print(p.async_playwright)"`
Expected: prints the coroutine factory; no `ImportError`. `playwright` must also still import (`uv run python -c "import playwright.async_api"`) until Task 5.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add patchright alongside playwright (migration staging)"
```

### Task 2: Add new constants and env vars

**Files:**
- Modify: `src/crawly_mcp/constants.py`

- [ ] **Step 1: Write a failing test asserting the new constants exist**

Create `tests/test_constants.py` (new file; the repo has no existing constants test file, and constant assertions don't belong in `test_models.py`):

```python
# tests/test_constants.py
from crawly_mcp import constants


def test_new_stealth_constants_exported() -> None:
    assert constants.WARMUP_PAGE_TIMEOUT_SECONDS == 3
    assert constants.SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS == 10
    assert constants.CRAWLY_USE_XVFB_ENV_VAR == "CRAWLY_USE_XVFB"
    assert constants.CRAWLY_XVFB_GEOMETRY_ENV_VAR == "CRAWLY_XVFB_GEOMETRY"
    assert constants.CRAWLY_PROFILE_DIR_ENV_VAR == "CRAWLY_PROFILE_DIR"
    assert constants.CRAWLY_PROFILE_CLEANUP_ON_START_ENV_VAR == "CRAWLY_PROFILE_CLEANUP_ON_START"
    assert constants.CRAWLY_PROFILE_MAX_AGE_DAYS_ENV_VAR == "CRAWLY_PROFILE_MAX_AGE_DAYS"
    assert constants.CRAWLY_SEARCH_JITTER_MS_ENV_VAR == "CRAWLY_SEARCH_JITTER_MS"
    assert constants.DEFAULT_XVFB_GEOMETRY == "1280x720x24"
    assert constants.DEFAULT_PROFILE_DIR == "~/.cache/crawly/profiles"
    assert constants.DEFAULT_PROFILE_MAX_AGE_DAYS == 14
    assert constants.DEFAULT_SEARCH_JITTER_MS == (500, 1500)
    assert constants.DEFAULT_TIMEZONE_ID == "America/New_York"


def test_client_hint_headers_present() -> None:
    assert "sec-ch-ua" in constants.STANDARD_HEADERS
    assert "sec-ch-ua-mobile" in constants.STANDARD_HEADERS
    assert "sec-ch-ua-platform" in constants.STANDARD_HEADERS
    assert '"Linux"' in constants.STANDARD_HEADERS["sec-ch-ua-platform"]


def test_provider_homepages_present() -> None:
    assert constants.PROVIDER_HOMEPAGE["duckduckgo"] == "https://duckduckgo.com/"
    assert constants.PROVIDER_HOMEPAGE["google"] == "https://www.google.com/"
    assert constants.PROVIDER_HOMEPAGE["yandex"] == "https://yandex.ru/"
```

- [ ] **Step 2: Run tests; confirm they fail**

Run: `uv run pytest tests/test_constants.py -v`
Expected: `AttributeError` on the new constant names.

- [ ] **Step 3: Add constants to `src/crawly_mcp/constants.py`**

Append to the existing file (preserve the existing ordering style — env var name, then default):

```python
# --- stealth / persistent-profile configuration ---

CRAWLY_USE_XVFB_ENV_VAR = "CRAWLY_USE_XVFB"
CRAWLY_XVFB_GEOMETRY_ENV_VAR = "CRAWLY_XVFB_GEOMETRY"
CRAWLY_PROFILE_DIR_ENV_VAR = "CRAWLY_PROFILE_DIR"
CRAWLY_PROFILE_CLEANUP_ON_START_ENV_VAR = "CRAWLY_PROFILE_CLEANUP_ON_START"
CRAWLY_PROFILE_MAX_AGE_DAYS_ENV_VAR = "CRAWLY_PROFILE_MAX_AGE_DAYS"
CRAWLY_SEARCH_JITTER_MS_ENV_VAR = "CRAWLY_SEARCH_JITTER_MS"

DEFAULT_XVFB_GEOMETRY = "1280x720x24"
DEFAULT_PROFILE_DIR = "~/.cache/crawly/profiles"
DEFAULT_PROFILE_MAX_AGE_DAYS = 14
DEFAULT_SEARCH_JITTER_MS: tuple[int, int] = (500, 1500)
DEFAULT_TIMEZONE_ID = "America/New_York"

WARMUP_PAGE_TIMEOUT_SECONDS = 3
SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS = 10

PROVIDER_HOMEPAGE: dict[str, str] = {
    "duckduckgo": "https://duckduckgo.com/",
    "google": "https://www.google.com/",
    "yandex": "https://yandex.ru/",
}
```

Update `STANDARD_HEADERS`:

```python
STANDARD_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="146", "Not)A;Brand";v="8", "Google Chrome";v="146"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}
```

(Keep `STANDARD_USER_AGENT` unchanged — it already advertises Chrome 146 on Linux, consistent with the new client hints.)

- [ ] **Step 4: Run tests; confirm they pass**

Run: `uv run pytest tests/test_constants.py -v && uv run ruff check src/crawly_mcp/constants.py`
Expected: all new tests pass; no lint errors.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/constants.py tests/test_constants.py
git commit -m "feat(constants): add stealth env vars, client-hint headers, provider homepages"
```

### Task 3: Refactor URLSafetyGuard to page-keyed blocked-request tracking

**Files:**
- Modify: `src/crawly_mcp/security.py`
- Modify: `tests/test_security.py`

This change breaks the guard's public API (`pop_blocked_error()` now requires a `page`). That's atomic with the test updates — we do both in one commit to keep the tree green.

- [ ] **Step 1: Write a failing test for page-keyed tracking**

Add to `tests/test_security.py`:

```python
from types import SimpleNamespace

from crawly_mcp.security import BlockedRequest, URLSafetyGuard
from crawly_mcp.errors import URLSafetyError


def _fake_page() -> object:
    """Opaque sentinel used as a dict key; URLSafetyGuard only uses identity."""
    return SimpleNamespace()


def test_pop_blocked_error_returns_page_scoped_error() -> None:
    guard = URLSafetyGuard()
    page_a = _fake_page()
    page_b = _fake_page()
    err_a = URLSafetyError("blocked_target", "A")
    err_b = URLSafetyError("blocked_target", "B")

    # Simulate what handle_route() would do internally:
    guard._blocked_requests.setdefault(page_a, []).append(BlockedRequest(url="https://a/", error=err_a))
    guard._blocked_requests.setdefault(page_b, []).append(BlockedRequest(url="https://b/", error=err_b))

    assert guard.pop_blocked_error(page_a) is err_a
    assert guard.pop_blocked_error(page_b) is err_b
    # Draining the list cleans the entry:
    assert page_a not in guard._blocked_requests
    assert page_b not in guard._blocked_requests


def test_pop_blocked_error_returns_none_for_unknown_page() -> None:
    guard = URLSafetyGuard()
    assert guard.pop_blocked_error(_fake_page()) is None


def test_close_event_cleans_up_dict_entry() -> None:
    """If a page had blocked subresources but closed without the caller
    draining pop_blocked_error, the close handler still releases the dict
    entry so long-lived contexts don't leak memory."""
    guard = URLSafetyGuard()

    close_handlers: list[object] = []

    class _Page:
        def on(self, event: str, handler: object) -> None:
            assert event == "close"
            close_handlers.append(handler)

    page = _Page()
    # Simulate what handle_route's first-seen path does:
    bucket: list[BlockedRequest] = []
    guard._blocked_requests[page] = bucket
    page.on("close", lambda p=page: guard._blocked_requests.pop(p, None))
    bucket.append(BlockedRequest(url="https://blocked/", error=URLSafetyError("blocked_target", "x")))

    assert page in guard._blocked_requests
    # Fire the close handler without anyone calling pop_blocked_error:
    close_handlers[0]()
    assert page not in guard._blocked_requests
```

- [ ] **Step 2: Run; confirm failure**

Run: `uv run pytest tests/test_security.py::test_pop_blocked_error_returns_page_scoped_error -v`
Expected: `TypeError: pop_blocked_error() takes 1 positional argument but 2 were given` (or similar).

- [ ] **Step 3: Refactor `URLSafetyGuard` in `src/crawly_mcp/security.py`**

Replace the top-level `playwright` import with `patchright` (patchright is installed alongside playwright per Task 1, and `browser.py` still imports `playwright.async_api` — both coexist safely until Task 5 finishes the migration):

```python
from patchright.async_api import BrowserContext, Page, Route
```

Change `_blocked_requests`:

```python
class URLSafetyGuard:
    def __init__(self) -> None:
        self._resolve_cache: dict[str, tuple[IPAddress, ...]] = {}
        self._cache_lock = asyncio.Lock()
        self._blocked_requests: dict[Page, list[BlockedRequest]] = {}
```

Update `handle_route()` to key by `route.request.frame.page` and subscribe to the page's `close` event on first-seen (so dict entries are cleaned up whether or not the caller ever calls `pop_blocked_error()`):

```python
async def handle_route(self, route: Route) -> None:
    request_url = route.request.url
    try:
        await self._validate(request_url, allow_local_schemes=True)
    except URLSafetyError as exc:
        logger.warning(
            "ssrf reject url={!r} reason={} message={}",
            request_url, exc.error_type, exc.message,
        )
        page = route.request.frame.page
        bucket = self._blocked_requests.get(page)
        if bucket is None:
            bucket = []
            self._blocked_requests[page] = bucket
            # Subscribe once per page so the dict entry is released when
            # the page closes, even if the caller never inspected errors.
            page.on("close", lambda p=page: self._blocked_requests.pop(p, None))
        bucket.append(BlockedRequest(url=request_url, error=exc))
        await route.abort("blockedbyclient")
        return
    await route.continue_()
```

Replace `pop_blocked_error()`:

```python
def pop_blocked_error(self, page: Page) -> URLSafetyError | None:
    bucket = self._blocked_requests.get(page)
    if not bucket:
        return None
    error = bucket.pop(0).error
    if not bucket:
        # The `close` handler will also release the dict entry eventually.
        # Dropping it here too is safe because `pop_blocked_error` is
        # idempotent — a subsequent call returns None.
        del self._blocked_requests[page]
    return error
```

- [ ] **Step 4: Update any other callers of `pop_blocked_error` — grep first**

Run: `rg "pop_blocked_error" src tests`
Expected: call sites in `src/crawly_mcp/service.py` (two — `search()` and `_fetch_one()`). Update both to pass `page`:

```python
blocked = guard.pop_blocked_error(page)
```

(The service.py change here is just the API update; the bigger `search()` rework comes in Chunk 3. Do the minimal signature fix now so the suite stays green.)

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all pass; no lint errors.

- [ ] **Step 6: Commit**

```bash
git add src/crawly_mcp/security.py src/crawly_mcp/service.py tests/test_security.py
git commit -m "refactor(security): page-keyed blocked-request tracking in URLSafetyGuard"
```

### Task 4: Update AGENTS.md policy statements

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Update the Tooling line (line ~20)**

Replace:
```
- **Playwright + Chromium** — browser automation. Host mode uses a system Chromium binary; bundled mode uses Playwright-managed Chromium.
```

With:
```
- **patchright + Chromium** — browser automation via the `patchright` Playwright fork, which adds the fingerprint patches we depend on for search-provider traffic. Host mode uses a system Chromium binary; bundled mode uses patchright-managed Chromium (`patchright install chromium`).
```

- [ ] **Step 2: Update the "Browser lifecycle" line (line ~61)**

Replace:
```
- **Browser lifecycle.** One Playwright Chromium browser per process, fresh incognito context per request; no cookies or storage persist. Browser manager restarts on crash/disconnect.
```

With:
```
- **Browser lifecycle.** One Chromium browser per process for fetch (fresh incognito context per request; no cookies persist across fetches). Per-search-provider persistent contexts with on-disk profiles keyed by provider. Profile dirs live under `CRAWLY_PROFILE_DIR` (default `~/.cache/crawly/profiles`) and are age-pruned at startup when `CRAWLY_PROFILE_CLEANUP_ON_START=true` (set automatically by the Docker entrypoint). Browser manager restarts on crash/disconnect.
```

- [ ] **Step 3: Update the "Challenge handling" line (line ~66)**

Replace:
```
- **Challenge handling.** Normal in-browser JS execution with bounded wait for interstitial → target transition. No CAPTCHA solving, stealth plugins, proxy rotation, or fingerprint spoofing.
```

With:
```
- **Challenge handling.** Normal in-browser JS execution with bounded wait for interstitial → target transition. Uses patchright's fingerprint patches, per-provider persistent profiles, client-hint headers, and a homepage warm-up hop to blend with normal traffic. No CAPTCHA solving and no proxy rotation.
```

- [ ] **Step 4: Run lint + test to confirm nothing regressed**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md
git commit -m "docs: update AGENTS.md for stealth/persistent-profile policy reversal"
```

---

## Chunk 2: BrowserManager refactor

This chunk adds the new browser lifecycle without touching `service.py` call sites (beyond what Chunk 1 already did). After this chunk, `BrowserManager` supports `search_context()` and `new_context()` returns contexts built from `_context_options()`, but the service doesn't yet call `search_context()` — that wiring happens in Chunk 3.

### Task 5: Swap playwright imports in browser.py

**Files:**
- Modify: `src/crawly_mcp/browser.py`
- Modify: `tests/test_browser.py`

- [ ] **Step 1: Rewrite imports at the top of `browser.py`**

Change:
```python
import playwright.async_api as playwright_api
from playwright.async_api import (
    Browser, BrowserContext, Error as PlaywrightError, Page, Playwright,
)
```
To:
```python
import patchright.async_api as playwright_api
from patchright.async_api import (
    Browser, BrowserContext, Error as PlaywrightError, Page, Playwright,
)
```

(Keep the module-local alias `playwright_api` — `tests/test_browser.py` monkey-patches it.)

- [ ] **Step 2: Retarget the monkeypatch in `tests/test_browser.py`**

Change `import playwright.async_api as playwright_api` at the top to `import patchright.async_api as playwright_api`.

Then find the two `monkeypatch.setattr(playwright_api, "async_playwright", ...)` call sites (around lines 127 and 178) — they already use the `playwright_api` alias, so retargeting just requires the import change. Confirm via grep after editing.

- [ ] **Step 3: Run the existing browser suite**

Run: `uv run pytest tests/test_browser.py -v`
Expected: all pass unchanged.

- [ ] **Step 4: Also swap `src/crawly_mcp/service.py` import**

At the top of `service.py`, change:
```python
from playwright.async_api import (
    Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError,
)
```
To:
```python
from patchright.async_api import (
    Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError,
)
```

- [ ] **Step 5: Run full suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 6: Remove the now-unused `playwright` dependency**

All `playwright.async_api` imports are gone after steps 1–4. Drop the package:

Run: `uv remove playwright`
Expected: `playwright` removed from `pyproject.toml`; `uv.lock` updated. `patchright` remains.

Run: `uv run pytest -q && uv run python -c "import playwright" 2>&1 | grep -E "ModuleNotFoundError|OK"`
Expected: tests pass; `import playwright` now raises `ModuleNotFoundError` (confirms the removal).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/crawly_mcp/browser.py src/crawly_mcp/service.py tests/test_browser.py
git commit -m "refactor: swap playwright imports for patchright and drop playwright dep"
```

### Task 6: Add `_context_options()` helper; wire into `new_context()`

**Files:**
- Modify: `src/crawly_mcp/browser.py`
- Modify: `tests/test_browser.py`

- [ ] **Step 1: Write a failing test**

Add to `tests/test_browser.py`:

```python
def test_context_options_reads_tz_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Berlin")
    manager = BrowserManager()
    opts = manager._context_options()
    assert opts["timezone_id"] == "Europe/Berlin"
    assert opts["locale"] == "en-US"
    assert opts["viewport"] == {"width": 1366, "height": 768}
    assert opts["java_script_enabled"] is True
    assert "sec-ch-ua" in opts["extra_http_headers"]


def test_context_options_defaults_timezone_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    manager = BrowserManager()
    opts = manager._context_options()
    assert opts["timezone_id"] == "America/New_York"
```

- [ ] **Step 2: Run; confirm failure**

Run: `uv run pytest tests/test_browser.py::test_context_options_reads_tz_from_env -v`
Expected: `AttributeError: ... no attribute '_context_options'`.

- [ ] **Step 3: Add `_context_options()` to `BrowserManager`**

In `src/crawly_mcp/browser.py`:

```python
def _context_options(self) -> dict[str, Any]:
    tz = os.environ.get("TZ") or DEFAULT_TIMEZONE_ID
    return {
        "user_agent": STANDARD_USER_AGENT,
        "locale": "en-US",
        "timezone_id": tz,
        "viewport": {"width": 1366, "height": 768},
        "java_script_enabled": True,
        "extra_http_headers": STANDARD_HEADERS,
    }
```

Import `DEFAULT_TIMEZONE_ID` from `crawly_mcp.constants` at the top of the file alongside the existing constants import.

Update the existing `new_context()` to use it:

```python
async def new_context(self) -> BrowserContext:
    browser = await self._ensure_browser()
    return await browser.new_context(**self._context_options())
```

(Remove the duplicated literals that `new_context()` currently inlines.)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_browser.py -v && uv run ruff check src/crawly_mcp/browser.py`
Expected: all pass; no lint.

- [ ] **Step 5: Commit**

```bash
git add src/crawly_mcp/browser.py tests/test_browser.py
git commit -m "refactor(browser): extract _context_options() helper; TZ from env"
```

### Task 7: Add `SearchContextHandle` dataclass and `search_context()` method

**Files:**
- Modify: `src/crawly_mcp/browser.py`
- Modify: `tests/test_browser.py`

- [ ] **Step 1: Write a failing test for `search_context()`**

Add to `tests/test_browser.py`. First, ensure these imports are at the top of the file (some may already be present — check before adding):

```python
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from crawly_mcp.browser import SearchContextHandle
```

Then add the tests:

```python
def test_search_context_handle_is_frozen_dataclass() -> None:
    # Use real sentinels — just type-shape check
    ctx = object()
    guard = object()
    handle = SearchContextHandle(context=ctx, guard=guard, first_use=True)
    assert handle.context is ctx
    assert handle.guard is guard
    assert handle.first_use is True
    with pytest.raises(FrozenInstanceError):
        handle.first_use = False  # type: ignore[misc]


@pytest.mark.asyncio
async def test_search_context_returns_handle_and_tracks_first_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """First call: first_use=True. Second call for same provider: first_use=False."""
    monkeypatch.setenv("CRAWLY_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("CRAWLY_PROFILE_CLEANUP_ON_START", "false")

    created_dirs: list[str] = []

    class FakeChromium:
        async def launch(self, **kwargs: Any) -> Any:
            return object()  # unused in this test

        async def launch_persistent_context(self, user_data_dir: str, **kwargs: Any) -> Any:
            created_dirs.append(user_data_dir)
            # _AsyncNoop is an async-callable assigned where patchright
            # expects a method; `await ctx.route(...)` invokes __call__.
            ctx = SimpleNamespace(
                route=_AsyncNoop(),
                close=_AsyncNoop(),
                on=lambda *a, **k: None,
                is_closed=lambda: False,
            )
            return ctx

    class FakePlaywright:
        chromium = FakeChromium()
        async def stop(self) -> None: ...

    async def fake_async_playwright() -> FakePlaywright:
        return FakePlaywright()

    monkeypatch.setattr(playwright_api, "async_playwright", lambda: SimpleNamespace(start=fake_async_playwright))

    manager = BrowserManager()
    h1 = await manager.search_context("duckduckgo")
    assert h1.first_use is True

    h2 = await manager.search_context("duckduckgo")
    assert h2.first_use is False
    assert h2.context is h1.context  # same cached instance
    assert len(created_dirs) == 1  # not recreated
```

Note: you'll need a helper `_AsyncNoop` — an async-callable that accepts any args. Add to the test file:

```python
class _AsyncNoop:
    async def __call__(self, *args: Any, **kwargs: Any) -> None: ...
```

- [ ] **Step 2: Run; confirm failure**

Run: `uv run pytest tests/test_browser.py -k search_context -v`
Expected: `ImportError` on `SearchContextHandle` or `AttributeError` on `search_context`.

- [ ] **Step 3: Add the dataclass and method to `src/crawly_mcp/browser.py`**

Near the top (after imports, before `BrowserManager`):

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SearchContextHandle:
    context: BrowserContext
    guard: "URLSafetyGuard"
    first_use: bool
```

Import `URLSafetyGuard` from `crawly_mcp.security` at the top of the file (circular import risk is zero — security doesn't import browser).

Add to `BrowserManager.__init__`:

```python
self._search_contexts: dict[str, BrowserContext] = {}
self._search_guards: dict[str, URLSafetyGuard] = {}
```

(Warm-up tracking is driven solely by the `first_use` field on `SearchContextHandle` — no separate `_warmed_providers` set is needed.)

Add the method:

```python
async def search_context(self, provider: str) -> SearchContextHandle:
    async with self._lock:
        cached = self._search_contexts.get(provider)
        if cached is not None and not cached.is_closed():
            return SearchContextHandle(
                context=cached,
                guard=self._search_guards[provider],
                first_use=False,
            )
        if cached is not None:
            # Stale; drop it and recreate.
            self._search_contexts.pop(provider, None)
            self._search_guards.pop(provider, None)

        await self._ensure_playwright_started()
        ctx = await self._create_persistent_context(provider)
        guard = URLSafetyGuard()
        await guard.attach(ctx)
        self._search_contexts[provider] = ctx
        self._search_guards[provider] = guard
        return SearchContextHandle(context=ctx, guard=guard, first_use=True)


async def _ensure_playwright_started(self) -> None:
    if self._playwright is None:
        self._playwright = await playwright_api.async_playwright().start()


async def _create_persistent_context(self, provider: str) -> BrowserContext:
    profile_parent = Path(
        os.environ.get(CRAWLY_PROFILE_DIR_ENV_VAR, DEFAULT_PROFILE_DIR)
    ).expanduser()
    user_data_dir = profile_parent / provider
    user_data_dir.mkdir(parents=True, exist_ok=True)
    return await self._playwright.chromium.launch_persistent_context(
        user_data_dir=str(user_data_dir),
        **self._launch_options(),
        **self._context_options(),
    )


def _launch_options(self) -> dict[str, Any]:
    """Launch options shared by both the incognito Browser and each
    persistent context. Keep this dict free of keys that overlap with
    _context_options() (user_agent, locale, timezone_id, viewport,
    java_script_enabled, extra_http_headers); those two dicts get
    merged via **unpack at call sites."""
    args = ["--disable-dev-shm-usage"]
    xvfb = os.environ.get(CRAWLY_USE_XVFB_ENV_VAR, "").lower() in ("1", "true", "yes")
    if not xvfb:
        args.append("--headless=new")
    # headless=False in both branches is intentional: under Xvfb the
    # browser is headed inside the virtual display; under --headless=new
    # the arg drives headlessness and Playwright's `headless` kwarg
    # would force legacy headless if set to True.
    return {"headless": False, "args": args}
```

Add imports for `CRAWLY_USE_XVFB_ENV_VAR`, `CRAWLY_PROFILE_DIR_ENV_VAR`, `DEFAULT_PROFILE_DIR` from constants.

Update `_create_persistent_context` to use `_launch_options()` (replaces the earlier `_launch_options_for_persistent()` name):

```python
return await self._playwright.chromium.launch_persistent_context(
    user_data_dir=str(user_data_dir),
    **self._launch_options(),
    **self._context_options(),
)
```

- [ ] **Step 4: Run the new tests**

Run: `uv run pytest tests/test_browser.py -k search_context -v`
Expected: both tests pass.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/crawly_mcp/browser.py tests/test_browser.py
git commit -m "feat(browser): add SearchContextHandle and search_context() persistent path"
```

### Task 8: Gated profile cleanup in `start()`

**Files:**
- Modify: `src/crawly_mcp/browser.py`
- Modify: `tests/test_browser.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_browser.py`:

```python
import time


@pytest.mark.asyncio
async def test_profile_cleanup_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRAWLY_PROFILE_DIR", str(tmp_path))
    monkeypatch.delenv("CRAWLY_PROFILE_CLEANUP_ON_START", raising=False)

    old = tmp_path / "stale"
    old.mkdir()
    os.utime(old, (time.time() - 60 * 24 * 3600, time.time() - 60 * 24 * 3600))

    manager = BrowserManager()
    await manager._cleanup_stale_profiles()  # direct call under test
    assert old.exists()  # NOT deleted, cleanup gate off


@pytest.mark.asyncio
async def test_profile_cleanup_when_enabled_deletes_old_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRAWLY_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("CRAWLY_PROFILE_CLEANUP_ON_START", "true")
    monkeypatch.setenv("CRAWLY_PROFILE_MAX_AGE_DAYS", "14")

    old = tmp_path / "ddg-stale"
    old.mkdir()
    (old / "file").write_text("data")
    os.utime(old, (time.time() - 30 * 24 * 3600, time.time() - 30 * 24 * 3600))

    fresh = tmp_path / "ddg-fresh"
    fresh.mkdir()

    manager = BrowserManager()
    await manager._cleanup_stale_profiles()
    assert not old.exists()
    assert fresh.exists()
```

- [ ] **Step 2: Run; confirm failure**

Run: `uv run pytest tests/test_browser.py -k profile_cleanup -v`
Expected: `AttributeError: '_cleanup_stale_profiles'`.

- [ ] **Step 3: Implement `_cleanup_stale_profiles` + hook into `start()`**

Add to `src/crawly_mcp/browser.py`:

```python
import shutil
import time


async def _cleanup_stale_profiles(self) -> None:
    if os.environ.get(CRAWLY_PROFILE_CLEANUP_ON_START_ENV_VAR, "").lower() not in ("1", "true", "yes"):
        return

    profile_parent = Path(
        os.environ.get(CRAWLY_PROFILE_DIR_ENV_VAR, DEFAULT_PROFILE_DIR)
    ).expanduser()
    if not profile_parent.is_dir():
        return

    max_age_days = int(
        os.environ.get(CRAWLY_PROFILE_MAX_AGE_DAYS_ENV_VAR, str(DEFAULT_PROFILE_MAX_AGE_DAYS))
    )
    threshold = time.time() - max_age_days * 24 * 3600

    deleted = 0
    reclaimed = 0
    for entry in profile_parent.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime >= threshold:
                continue
            size = sum(p.stat().st_size for p in entry.rglob("*") if p.is_file())
            shutil.rmtree(entry)
            deleted += 1
            reclaimed += size
        except OSError as exc:
            logger.warning("profile cleanup failed entry={} error={}", entry, exc)
    if deleted:
        logger.info("profile cleanup deleted={} reclaimed_bytes={}", deleted, reclaimed)
```

Update `start()`:

```python
async def start(self) -> None:
    await self._cleanup_stale_profiles()
    await self._ensure_browser()
```

Add imports for `CRAWLY_PROFILE_CLEANUP_ON_START_ENV_VAR`, `CRAWLY_PROFILE_MAX_AGE_DAYS_ENV_VAR`, `DEFAULT_PROFILE_MAX_AGE_DAYS` from constants.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_browser.py -k profile_cleanup -v`
Expected: both pass.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/crawly_mcp/browser.py tests/test_browser.py
git commit -m "feat(browser): gated age-based profile cleanup on start()"
```

### Task 9: Xvfb preflight and updated `close()`

**Files:**
- Modify: `src/crawly_mcp/browser.py`
- Modify: `tests/test_browser.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_browser.py`:

```python
@pytest.mark.asyncio
async def test_xvfb_preflight_requires_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWLY_USE_XVFB", "true")
    monkeypatch.delenv("DISPLAY", raising=False)

    manager = BrowserManager()
    with pytest.raises(BrowserUnavailableError, match="DISPLAY"):
        await manager._xvfb_preflight()


@pytest.mark.asyncio
async def test_xvfb_preflight_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRAWLY_USE_XVFB", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)

    manager = BrowserManager()
    await manager._xvfb_preflight()  # no error


@pytest.mark.asyncio
async def test_close_closes_persistent_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []

    class FakeCtx:
        def __init__(self, name: str) -> None:
            self.name = name
        async def close(self) -> None:
            closed.append(self.name)

    manager = BrowserManager()
    manager._search_contexts = {"duckduckgo": FakeCtx("ddg"), "google": FakeCtx("g")}
    manager._search_guards = {"duckduckgo": object(), "google": object()}
    await manager.close()
    assert sorted(closed) == ["ddg", "g"]
    assert manager._search_contexts == {}
```

- [ ] **Step 2: Run; confirm failure**

Run: `uv run pytest tests/test_browser.py -k "xvfb_preflight or close_closes_persistent" -v`
Expected: `AttributeError: '_xvfb_preflight'` and `close()` not clearing the dict.

- [ ] **Step 3: Implement**

Add:

```python
async def _xvfb_preflight(self) -> None:
    if os.environ.get(CRAWLY_USE_XVFB_ENV_VAR, "").lower() not in ("1", "true", "yes"):
        return
    if not os.environ.get("DISPLAY"):
        raise BrowserUnavailableError(
            "CRAWLY_USE_XVFB=true but DISPLAY is not set; "
            "use scripts/run-with-xvfb.sh as the entrypoint"
        )
```

Update `_ensure_browser()` to call `await self._xvfb_preflight()` before launching.

Update `close()`:

```python
async def close(self) -> None:
    async with self._lock:
        for provider, ctx in list(self._search_contexts.items()):
            with suppress(Exception):
                await ctx.close()
            self._search_contexts.pop(provider, None)
            self._search_guards.pop(provider, None)
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
```

Update the existing `_ensure_browser()` launch path to use the shared `_launch_options()` helper added in Task 7 (DRY: both the persistent contexts and the shared incognito browser get the same launch args):

```python
# inside _ensure_browser(), replace the inline launch_options dict with:
launch_options = self._launch_options()
if source == BROWSER_SOURCE_SYSTEM:
    launch_options["executable_path"] = resolve_chromium_executable()
self._browser = await self._playwright.chromium.launch(**launch_options)
```

Add a test to cover the shared launch args (add to `tests/test_browser.py`):

```python
def test_launch_options_shared_between_paths() -> None:
    manager = BrowserManager()
    opts = manager._launch_options()
    assert "--disable-dev-shm-usage" in opts["args"]
    # By default (no CRAWLY_USE_XVFB), --headless=new must be present:
    assert "--headless=new" in opts["args"]


def test_launch_options_omits_headless_new_under_xvfb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRAWLY_USE_XVFB", "true")
    manager = BrowserManager()
    opts = manager._launch_options()
    assert "--disable-dev-shm-usage" in opts["args"]
    assert "--headless=new" not in opts["args"]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_browser.py -v`
Expected: all pass.

- [ ] **Step 5: Run full suite**

Run: `uv run pytest -q && uv run ruff check .`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/crawly_mcp/browser.py tests/test_browser.py
git commit -m "feat(browser): xvfb preflight and close() handles persistent contexts"
```

---

## Chunk 3: service.py wiring + parsing.py Yandex fix

This is where the service starts using the persistent context path.

### Task 10: Update `parsing.py` Yandex URL and fixtures

**Files:**
- Modify: `src/crawly_mcp/parsing.py`
- Modify: `tests/test_parsing.py`

- [ ] **Step 1: Update failing tests first**

In `tests/test_parsing.py`, find any test that asserts on the Yandex search URL (grep for `yandex.com/search`). Change the expected URL to `https://yandex.ru/search/?text=...`. If fixtures reference `yandex.com`, keep them as the *allow-list* references but update the *search URL template* assertions to `.ru`.

Run: `uv run pytest tests/test_parsing.py -v`
Expected: the updated tests fail (still pointing at old `.com` URL template).

- [ ] **Step 2: Update `src/crawly_mcp/parsing.py` line 20**

Change:
```python
"yandex": "https://yandex.com/search/?text={query}",
```
To:
```python
"yandex": "https://yandex.ru/search/?text={query}",
```

(Leave the allow-list tuple at line 14 alone — `yandex.com` stays as an allowed host for redirect normalization.)

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/test_parsing.py -v`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add src/crawly_mcp/parsing.py tests/test_parsing.py
git commit -m "feat(parsing): use yandex.ru for search URL template"
```

### Task 11: Rework `service.search()` for persistent context + warm-up + two-tier timeouts

**Files:**
- Modify: `src/crawly_mcp/service.py`
- Modify: `tests/test_service.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_service.py`. Follow existing test patterns — mock `BrowserManager` to avoid real Chromium. Full, self-contained test body:

```python
import pytest

from crawly_mcp.browser import SearchContextHandle
from crawly_mcp.constants import PROVIDER_HOMEPAGE
from crawly_mcp.security import URLSafetyGuard  # used for URL validation only
from crawly_mcp.service import WebSearchService


class _FakePage:
    def __init__(self) -> None:
        self.url = "https://duckduckgo.com/html/?q=test"
        self.closed = False

    async def title(self) -> str: return "results"
    async def content(self) -> str:
        return '<html><a class="result__a" href="https://example.com/1">r1</a></html>'
    async def close(self) -> None: self.closed = True


class _FakeContext:
    def __init__(self) -> None:
        self.page: _FakePage | None = None

    async def new_page(self) -> _FakePage:
        self.page = _FakePage()
        return self.page


class _FakeGuard:
    def pop_blocked_error(self, page: object) -> None: return None


class _FakeBrowserManager:
    def __init__(self) -> None:
        self._first_call_done = False
        self.goto_calls: list[str] = []

    async def search_context(self, provider: str) -> SearchContextHandle:
        first = not self._first_call_done
        self._first_call_done = True
        return SearchContextHandle(
            context=_FakeContext(), guard=_FakeGuard(), first_use=first,
        )

    async def goto(self, page: object, url: str, *, timeout_ms: int) -> None:
        self.goto_calls.append(url)


@pytest.mark.asyncio
async def test_search_warms_up_on_first_use_only(monkeypatch: pytest.MonkeyPatch) -> None:
    # URLSafetyGuard hits DNS on validate_user_url; short-circuit that:
    async def fake_validate(self, url: str) -> None: return None
    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", fake_validate)
    monkeypatch.setenv("CRAWLY_SEARCH_JITTER_MS", "0,0")  # deterministic test

    manager = _FakeBrowserManager()
    service = WebSearchService(browser_manager=manager)

    await service.search(provider="duckduckgo", context="one")
    await service.search(provider="duckduckgo", context="two")

    homepage = PROVIDER_HOMEPAGE["duckduckgo"]
    # First call: warmup + search. Second: search only.
    assert manager.goto_calls.count(homepage) == 1
    assert sum(1 for u in manager.goto_calls if "html/?q=" in u) == 2


@pytest.mark.asyncio
async def test_search_continues_when_warmup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warm-up failures are best-effort and must not fail the search."""
    from patchright.async_api import TimeoutError as PlaywrightTimeoutError

    async def fake_validate(self, url: str) -> None: return None
    monkeypatch.setattr(URLSafetyGuard, "validate_user_url", fake_validate)
    monkeypatch.setenv("CRAWLY_SEARCH_JITTER_MS", "0,0")

    manager = _FakeBrowserManager()
    # Make the homepage goto raise; real search goto succeeds.
    original_goto = manager.goto

    async def failing_goto(page: object, url: str, *, timeout_ms: int) -> None:
        if url == PROVIDER_HOMEPAGE["duckduckgo"]:
            raise PlaywrightTimeoutError("warmup timed out")
        await original_goto(page, url, timeout_ms=timeout_ms)

    manager.goto = failing_goto  # type: ignore[assignment]

    service = WebSearchService(browser_manager=manager)
    response = await service.search(provider="duckduckgo", context="test")
    assert response.urls  # search still succeeded despite warmup failure
```

- [ ] **Step 2: Run; confirm failure**

Run: `uv run pytest tests/test_service.py -k "persistent_context or warm" -v`
Expected: either test fails (service doesn't yet use handle or doesn't warm up).

- [ ] **Step 3: Rewrite `WebSearchService.search()` in `src/crawly_mcp/service.py`**

Replace the body of `search()` (keep the early validation and logging unchanged). The core change is around the two timeout wrappers:

```python
async def search(self, *, provider: str | None = None, context: str) -> SearchResponse:
    try:
        request = SearchRequest(provider=provider, context=context)
    except ValidationError as exc:
        logger.warning("search rejected invalid input: {}", exc.errors()[0]["msg"])
        raise InvalidInputError(str(exc.errors()[0]["msg"])) from exc

    logger.info("search entry provider={} context={!r}", request.provider, request.context)
    started = time.monotonic()

    guard_upfront = URLSafetyGuard()
    search_url = build_search_url(request.provider, request.context)
    await guard_upfront.validate_user_url(search_url)

    # Two timeouts: acquisition outside, per-request work inside.
    try:
        async with asyncio.timeout(SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS):
            handle = await self._browser_manager.search_context(request.provider)
            page = await handle.context.new_page()
    except TimeoutError as exc:
        raise TimeoutExceededError("search context acquisition timed out") from exc

    try:
        async with asyncio.timeout(SEARCH_TOTAL_TIMEOUT_SECONDS):
            if handle.first_use:
                await self._maybe_warmup(page, request.provider)
            await self._sleep_jitter()
            try:
                await self._browser_manager.goto(
                    page, search_url, timeout_ms=SEARCH_PAGE_TIMEOUT_SECONDS * 1000,
                )
            except PlaywrightTimeoutError as exc:
                raise TimeoutExceededError("search timed out before the results page loaded") from exc
            except PlaywrightError as exc:
                blocked = handle.guard.pop_blocked_error(page)
                if blocked is not None:
                    raise blocked from exc
                raise NavigationFailedError(f"search navigation failed: {exc}") from exc

            title = await page.title()
            html = await page.content()
            self._raise_if_provider_blocked(request.provider, page.url, title, html)

            results = extract_search_results(request.provider, html, page.url)
            duration = time.monotonic() - started
            logger.info(
                "search done provider={} results={} final_url={!r} duration={:.2f}s",
                request.provider, len(results), page.url, duration,
            )
            return SearchResponse(urls=results)
    except TimeoutError as exc:
        raise TimeoutExceededError("search exceeded the overall timeout") from exc
    finally:
        with suppress(Exception):
            await page.close()  # context is NOT closed
```

Add the two helpers on `WebSearchService`:

```python
async def _maybe_warmup(self, page: Any, provider: str) -> None:
    try:
        await self._browser_manager.goto(
            page, PROVIDER_HOMEPAGE[provider],
            timeout_ms=WARMUP_PAGE_TIMEOUT_SECONDS * 1000,
        )
    except (PlaywrightTimeoutError, PlaywrightError) as exc:
        logger.warning("warmup failed provider={} reason={}", provider, exc)

async def _sleep_jitter(self) -> None:
    raw = os.environ.get(CRAWLY_SEARCH_JITTER_MS_ENV_VAR)
    if raw:
        try:
            lo, hi = (int(x) for x in raw.split(","))
        except ValueError:
            lo, hi = DEFAULT_SEARCH_JITTER_MS
    else:
        lo, hi = DEFAULT_SEARCH_JITTER_MS
    await asyncio.sleep(random.uniform(lo, hi) / 1000.0)
```

Update imports at the top of `service.py`:

```python
import os
import random
from contextlib import suppress

from crawly_mcp.constants import (
    CHALLENGE_SETTLE_TIMEOUT_SECONDS,
    CRAWLY_SEARCH_JITTER_MS_ENV_VAR,
    DEFAULT_SEARCH_JITTER_MS,
    FETCH_PAGE_TIMEOUT_SECONDS,
    FETCH_TOTAL_TIMEOUT_SECONDS,
    MAX_HTML_BYTES,
    PROVIDER_HOMEPAGE,
    SEARCH_CONTEXT_ACQUIRE_TIMEOUT_SECONDS,
    SEARCH_PAGE_TIMEOUT_SECONDS,
    SEARCH_TOTAL_TIMEOUT_SECONDS,
    WARMUP_PAGE_TIMEOUT_SECONDS,
)
from crawly_mcp.security import URLSafetyGuard
```

Note: `URLSafetyGuard` is now only needed for the upfront SSRF validation on the user-supplied URL — the per-context guard comes from the handle. Keep the import.

**Verify `TimeoutExceededError` doesn't subclass `TimeoutError`** before wiring the new outer `except TimeoutError` handler. Run `grep -n "class TimeoutExceededError" src/crawly_mcp/errors.py` — it should inherit from `WebSearchError` (not Python's built-in `TimeoutError`). If it did, the outer `except TimeoutError` would accidentally catch and rewrap any inner `raise TimeoutExceededError(...)`. Current errors.py doesn't subclass the builtin, but confirm before the commit.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_service.py -v`
Expected: new tests pass; existing ones either pass or need updating for the new guard attachment path.

- [ ] **Step 5: Fix any pre-existing tests that broke due to shape changes**

The old `search()` called `browser_manager.new_context()` and attached the upfront guard to it. Existing tests that mock `new_context` for search will need to mock `search_context` instead. Update them one by one; keep the diff minimal. Expected: all tests pass.

Run: `uv run pytest -q && uv run ruff check .`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/crawly_mcp/service.py tests/test_service.py
git commit -m "feat(service): persistent context, warm-up, two-tier timeouts for search()"
```

### Task 12: Update `_fetch_one()` page-keyed pop_blocked_error

This was already done in Task 3 as part of the atomic guard-signature change. Verify no stragglers.

- [ ] **Step 1: Grep**

Run: `rg "pop_blocked_error\(\)" src tests`
Expected: empty — no zero-arg calls remain.

- [ ] **Step 2: Confirm fetch tests still pass**

Run: `uv run pytest tests/test_service.py -k fetch -v`
Expected: pass.

(No commit needed — verification only.)

---

## Chunk 4: Canary script, Xvfb wrapper, Dockerfile, CI

### Task 13: Create `scripts/fingerprint_check.py`

**Files:**
- Create: `scripts/fingerprint_check.py`

- [ ] **Step 1: Write the script skeleton**

Create `scripts/fingerprint_check.py`:

```python
#!/usr/bin/env python3
"""Inline fingerprint canary for the crawly browser stack.

Exits 0 if the browser's JS-visible fingerprint looks like a real Chrome;
non-zero on the first failing check.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from crawly_mcp.browser import BrowserManager


@dataclass
class Check:
    name: str
    js: str
    predicate: str  # JS expression evaluated server-side; must return bool


CHECKS: list[Check] = [
    Check("navigator.webdriver", "navigator.webdriver", "navigator.webdriver === false"),
    Check("navigator.plugins.length", "navigator.plugins.length", "navigator.plugins.length > 0"),
    Check("navigator.languages.length", "navigator.languages.length", "navigator.languages.length > 0"),
    Check("window.chrome.runtime", "typeof window.chrome?.runtime",
          "typeof window.chrome?.runtime !== 'undefined'"),
    Check(
        "WebGL renderer",
        """(() => {
            const gl = document.createElement('canvas').getContext('webgl');
            const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
            return ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'no-webgl';
        })()""",
        """(() => {
            const gl = document.createElement('canvas').getContext('webgl');
            const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
            if (!ext) return false;
            const r = gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
            return !/SwiftShader|llvmpipe/i.test(r);
        })()""",
    ),
    Check(
        "permissions vs Notification",
        """(async () => {
            const p = await navigator.permissions.query({name:'notifications'});
            return `state=${p.state} notif=${Notification.permission}`;
        })()""",
        """(async () => {
            const p = await navigator.permissions.query({name:'notifications'});
            return !(p.state === 'denied' && Notification.permission === 'default');
        })()""",
    ),
]


async def run(verbose: bool) -> int:
    manager = BrowserManager()
    await manager.start()
    try:
        context = await manager.new_context()
        page = await context.new_page()
        await page.goto("about:blank")

        failures = 0
        for check in CHECKS:
            value = await page.evaluate(check.js)
            ok = await page.evaluate(check.predicate)
            status = "PASS" if ok else "FAIL"
            if verbose or not ok:
                print(f"{check.name:40s} {status:5s} {value!r}")
            if not ok:
                failures += 1
        await context.close()
        return 0 if failures == 0 else 1
    finally:
        await manager.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    return asyncio.run(run(args.verbose))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Make executable**

Run: `chmod +x scripts/fingerprint_check.py`

- [ ] **Step 3: Run it locally (requires `uv run patchright install chromium` done earlier)**

Run: `uv run python scripts/fingerprint_check.py --verbose`
Expected: every check says PASS. If any FAIL, stop and investigate — the stealth config isn't working as designed. (Likely FAIL cause at this stage: webdriver still true, meaning patchright isn't actually being invoked — check imports.)

- [ ] **Step 4: Commit**

```bash
git add scripts/fingerprint_check.py
git commit -m "feat: add inline fingerprint canary script"
```

### Task 14: Create `scripts/run-with-xvfb.sh`

**Files:**
- Create: `scripts/run-with-xvfb.sh`

- [ ] **Step 1: Write the wrapper**

Create `scripts/run-with-xvfb.sh`:

```bash
#!/usr/bin/env bash
# Start crawly-mcp under Xvfb when CRAWLY_USE_XVFB=true. Otherwise exec directly.
# Used as the Docker entrypoint; safe to use locally too.
set -euo pipefail

if [[ "${CRAWLY_USE_XVFB:-false}" =~ ^(1|true|yes)$ ]]; then
    geom="${CRAWLY_XVFB_GEOMETRY:-1280x720x24}"
    exec xvfb-run -a -s "-screen 0 ${geom}" "$@"
fi

exec "$@"
```

- [ ] **Step 2: Make executable**

Run: `chmod +x scripts/run-with-xvfb.sh`

- [ ] **Step 3: Smoke test**

Run: `CRAWLY_USE_XVFB=false ./scripts/run-with-xvfb.sh true`
Expected: exits 0.

Run: `CRAWLY_USE_XVFB=true ./scripts/run-with-xvfb.sh true` (only if `xvfb` installed locally; skip if not)
Expected: exits 0 under Xvfb.

- [ ] **Step 4: Commit**

```bash
git add scripts/run-with-xvfb.sh
git commit -m "feat: add Xvfb wrapper entrypoint for optional headed mode"
```

### Task 15: Update Dockerfile

**Current state:** The actual Dockerfile at [Dockerfile](../../../Dockerfile) uses a two-stage build on `mcr.microsoft.com/playwright/python:v1.58.0-noble`, user `pwuser`, and CMD `["crawly-mcp", "--transport", "streamable-http"]`. It exports `PLAYWRIGHT_BROWSER_SOURCE=bundled` and exposes port 8000. The Playwright base image has Chromium pre-installed at Playwright's standard location; patchright can reuse this Chromium binary, so we don't need to re-download it in the image.

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Install xvfb and copy the wrapper into the runtime stage (as root, before `USER pwuser`)**

Edit the **runtime stage** (after the `FROM ... AS runtime` line and before `USER pwuser`):

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble AS runtime

WORKDIR /app

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY scripts/run-with-xvfb.sh /usr/local/bin/run-with-xvfb.sh
RUN chmod +x /usr/local/bin/run-with-xvfb.sh

RUN mkdir -p /data/profiles && chown -R pwuser:pwuser /data/profiles
```

- [ ] **Step 2: Update the runtime `ENV` block**

Change:
```dockerfile
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSER_SOURCE=bundled \
    CRAWLY_HOST=0.0.0.0 \
    CRAWLY_PORT=8000
```
To:
```dockerfile
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSER_SOURCE=bundled \
    CRAWLY_HOST=0.0.0.0 \
    CRAWLY_PORT=8000 \
    CRAWLY_PROFILE_DIR=/data/profiles \
    CRAWLY_PROFILE_CLEANUP_ON_START=true
```

**Note on `PLAYWRIGHT_BROWSER_SOURCE=bundled` after patchright swap.** Patchright's `launch()` / `launch_persistent_context()` call patterns use the same `executable_path` resolution as Playwright. With `PLAYWRIGHT_BROWSER_SOURCE=bundled`, `browser.py` does NOT pass `executable_path` (`browser.py:77`), so patchright's internal default kicks in: it looks for a Chromium it installed via `patchright install chromium`. But the base image only has **Playwright's** Chromium, not patchright's. Resolution options:

1. **Run `patchright install chromium --with-deps` in the builder stage** so patchright's expected binary is present. Adds ~400 MB to the image. This is the portable fix.
2. **Point patchright at Playwright's already-installed Chromium** via a Dockerfile-level `ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright` (the base image's location) and set `PLAYWRIGHT_BROWSER_SOURCE=system` + `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to the exact path. Saves space; more brittle across base-image upgrades.

Go with **option 1** for v1 — correctness over image size. Add before the `ENTRYPOINT`:

```dockerfile
USER pwuser
RUN uv run patchright install chromium --with-deps
```

(May need `USER root` then back to `pwuser` depending on where `--with-deps` wants to drop files; test the build.)

- [ ] **Step 3: Add ENTRYPOINT**

Before `CMD [...]`, add:
```dockerfile
ENTRYPOINT ["/usr/local/bin/run-with-xvfb.sh"]
```
Keep `CMD ["crawly-mcp", "--transport", "streamable-http"]` unchanged. The entrypoint wrapper is idempotent when `CRAWLY_USE_XVFB` is unset — it just execs `$@`.

- [ ] **Step 4: Restore `USER pwuser` at the very end, before CMD**

Make sure the final Dockerfile ends with:
```dockerfile
USER pwuser
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/run-with-xvfb.sh"]
CMD ["crawly-mcp", "--transport", "streamable-http"]
```

- [ ] **Step 5: Build the image locally**

Run: `docker build -t crawly-mcp:stealth-test .`
Expected: build succeeds. Image size will be ~1.5–2 GB (Playwright base plus patchright Chromium).

- [ ] **Step 6: Smoke-test the entrypoint without Xvfb**

Run: `docker run --rm crawly-mcp:stealth-test python -c "print('ok')"`
Expected: prints `ok`. (The wrapper execs `$@`, so overriding the CMD works.)

- [ ] **Step 7: Smoke-test that the default CMD still starts the MCP HTTP server**

Run: `docker run --rm -p 8000:8000 crawly-mcp:stealth-test` in one terminal; in another, `curl -sf http://127.0.0.1:8000/mcp` or `./scripts/http_mcp_smoke.py --url http://127.0.0.1:8000/mcp`.
Expected: MCP server listens on 8000 and responds. Stop the container.

- [ ] **Step 8: Commit**

```bash
git add Dockerfile
git commit -m "build(docker): install xvfb, profile dir, wrapper entrypoint, patchright chromium"
```

### Task 16: Add fingerprint-check CI job

**Files:**
- Modify: `.github/workflows/tests.yml`

- [ ] **Step 1: Append a tag-gated job to the existing workflow**

Add after the existing `test:` job:

```yaml
  fingerprint-check:
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    needs: test
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - name: Install uv
        run: python -m pip install --upgrade pip uv
      - name: Sync dependencies
        run: uv sync --frozen
      - name: Install Chromium
        run: uv run patchright install chromium --with-deps
      - name: Run fingerprint canary
        run: uv run python scripts/fingerprint_check.py
```

- [ ] **Step 2: Validate YAML locally**

The project has no YAML parser in its dependencies. Use `uvx` (ephemeral install) so no permanent dep is added:

Run: `uvx --from pyyaml python -c "import yaml, sys; yaml.safe_load(open('.github/workflows/tests.yml')); print('ok')"`
Expected: prints `ok`; no exception.

(If `uvx` is not available on your machine, skip this step — GitHub Actions will reject an invalid workflow file on push. The local check is cheap insurance, not a gate.)

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/tests.yml
git commit -m "ci: add release-tag-gated fingerprint-check job"
```

---

## Chunk 5: README + CHANGELOG + final verification

### Task 17: Update README with Stealth configuration + refresh Design Notes

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Insert a new `## Stealth configuration` section after the existing `## Container` section and before `## MCP Client Config`**

The env vars introduced here interact primarily with container deployment, so this placement keeps related content together. Follow the repo's existing headline style.

```markdown
## Stealth configuration

crawly uses [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) (a Playwright fork with bundled fingerprint patches) and keeps a small set of per-search-provider persistent profiles on disk to make its traffic blend with normal user traffic. The following env vars tune the behavior:

| Env var | Default | Purpose |
|---|---|---|
| `CRAWLY_USE_XVFB` | `false` | Launch headed Chromium under Xvfb (instead of `--headless=new`). Requires the wrapper entrypoint. |
| `CRAWLY_XVFB_GEOMETRY` | `1280x720x24` | Virtual display geometry passed to `xvfb-run`. |
| `CRAWLY_PROFILE_DIR` | `~/.cache/crawly/profiles` | Parent directory for per-provider persistent profiles. **Must be a writable mount in containers.** |
| `CRAWLY_PROFILE_CLEANUP_ON_START` | `false` | Enable age-based profile cleanup at startup. Set to `true` in the Dockerfile entrypoint. **Unsafe when multiple processes share the profile dir.** |
| `CRAWLY_PROFILE_MAX_AGE_DAYS` | `14` | Age threshold for profile cleanup. |
| `CRAWLY_SEARCH_JITTER_MS` | `500,1500` | Min/max ms delay between warm-up and real query. Two-int CSV. |
| `TZ` | `America/New_York` if unset | Timezone passed to the browser context. Follows Docker convention. Leave unset unless you have a reason to override. |

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
```

- [ ] **Step 2: Refresh the existing `## Design Notes` section**

Current README.md has two lines that contradict the new behavior (near line 159 and line 165). Update:

Line 159 — replace:
```
- One shared browser per process, with a fresh incognito context per request.
```
with:
```
- One shared incognito browser per process for `fetch()` (fresh context per request). `search()` uses per-provider persistent contexts with on-disk profiles keyed by provider.
```

Line ~165 — replace:
```
- JavaScript challenge pages get a bounded `10s` settle window; there is no CAPTCHA solving, stealth fingerprinting, or site-specific bypass logic.
```
with:
```
- JavaScript challenge pages get a bounded `10s` settle window. `patchright` provides fingerprint patches against common bot-detection checks; provider-specific warm-up hops and client-hint headers blend with normal traffic. No CAPTCHA solving or site-specific bypass logic.
```

Update the `PLAYWRIGHT_BROWSER_SOURCE` lines (around 161–162) to note both paths now go through patchright:
```
- `PLAYWRIGHT_BROWSER_SOURCE=system` uses a host Chromium binary (driven by patchright).
- `PLAYWRIGHT_BROWSER_SOURCE=bundled` uses patchright-managed Chromium (`patchright install chromium`).
```

- [ ] **Step 3: Update the `## Container` section's default env var list**

Around line 100–104, the list currently reads:
```
- `PLAYWRIGHT_BROWSER_SOURCE=bundled`
- `CRAWLY_HOST=0.0.0.0`
- `CRAWLY_PORT=8000`
```
Extend with the new defaults set by the Dockerfile:
```
- `CRAWLY_PROFILE_DIR=/data/profiles`
- `CRAWLY_PROFILE_CLEANUP_ON_START=true`
```

- [ ] **Step 4: Verify rendering locally**

Run: `ls README.md && uv run ruff check .`
Expected: file present; no lint errors (markdown isn't linted, but this confirms no accidental side effects).

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs(readme): stealth configuration, design notes refresh, container env defaults"
```

### Task 18: CHANGELOG entry

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Add entries under `[Unreleased]`**

Follow the existing terse style:

```markdown
### Added
- `patchright` as the Playwright engine for stealth patching (replaces stock `playwright`).
- Per-search-provider persistent browser contexts with on-disk profiles under `CRAWLY_PROFILE_DIR`.
- Homepage warm-up hop and randomized jitter on first search per provider.
- Client-hint headers (`sec-ch-ua*`) consistent with the advertised UA.
- Optional Xvfb mode via `CRAWLY_USE_XVFB` and the `run-with-xvfb.sh` entrypoint.
- Fingerprint canary script (`scripts/fingerprint_check.py`) and a release-gated CI job.
- `TZ` env var support for browser context timezone (default `America/New_York`).
- Age-based profile cleanup at startup (gated by `CRAWLY_PROFILE_CLEANUP_ON_START`, enabled in the Docker image).

### Changed
- `URLSafetyGuard.pop_blocked_error()` now requires a `Page` argument and tracks blocked requests per page.
- `fetch()` browser contexts now inherit the same stealth identity (UA, TZ, client hints) as search contexts; this may alter returned HTML on TZ-aware sites. See the Stealth configuration section in the README for the tradeoff.
- Default headless launch switched from legacy `--headless` to `--headless=new`.

### Fixed
- Yandex search endpoint now consistently targets `yandex.ru` instead of mixing `yandex.ru` warm-up with `yandex.com` search.
```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): document stealth-hardening changes"
```

### Task 19: Final integration verification

- [ ] **Step 1: Full test suite**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 2: Lint**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean.

- [ ] **Step 3: Fingerprint canary**

Run: `uv run python scripts/fingerprint_check.py --verbose`
Expected: every check PASS.

- [ ] **Step 4: Manual DDG smoke test**

Run: `uv run crawly-cli search --context "python asyncio"`
Expected: process exits 0; stdout contains a structured response with a `urls` field. The project contract in AGENTS.md is `0..5 URLs; zero results is not an error` — what we're testing is the *absence* of `ProviderBlockedError` / `TimeoutExceededError`, not a specific URL count. If the call raises either of those, the stealth config is not working — debug before claiming done.

Repeat 5+ times in succession with different queries to confirm the persistent profile doesn't drift into a blocked state:

```sh
for q in python rust golang elixir haskell; do
    uv run crawly-cli search --context "$q" >/tmp/crawly-smoke.out 2>&1 || { echo "FAIL on $q"; cat /tmp/crawly-smoke.out; exit 1; }
    # The response must contain a "urls" field (list may be empty) and
    # must NOT report a provider-block or timeout.
    grep -qE '"urls"\s*:\s*\[' /tmp/crawly-smoke.out || { echo "no urls field for $q"; cat /tmp/crawly-smoke.out; exit 1; }
    if grep -qE 'ProviderBlockedError|TimeoutExceededError|captcha|challenge' /tmp/crawly-smoke.out; then
        echo "block/timeout indicator on $q"; cat /tmp/crawly-smoke.out; exit 1
    fi
done
echo "OK"
```

Expected: prints `OK` at the end; all 5 invocations exit 0 with a `urls` field and no block/timeout indicators.

- [ ] **Step 5: Xvfb mode smoke test (optional)**

If `xvfb` is installed locally:

Run: `CRAWLY_USE_XVFB=true ./scripts/run-with-xvfb.sh uv run crawly-cli search --context "python"`
Expected: process exits 0; same structured output as headless mode.

- [ ] **Step 6: Docker smoke test**

Image already built in Task 15. Run:
```sh
docker run --rm -v crawly-profiles:/data/profiles \
    crawly-mcp:stealth-test crawly-cli search --context "python"
```
(The ENTRYPOINT wrapper's `exec "$@"` passes the overridden command through; no `uv run` prefix needed inside the container because the `ENV PATH` already includes `/app/.venv/bin`.)
Expected: exit 0, urls list in stdout. Running the same command a second time should reuse the mounted profile (observe no fresh `user_data_dir` creation in logs).

- [ ] **Step 7: Final commit (if any cleanup needed)**

If all verifications pass and nothing needs changing, no commit. If verification surfaces a fix, commit it as its own change before wrapping up.

Branch is now ready for PR / merge / tag-push. The tag push will trigger the `fingerprint-check` CI job added in Task 16.

---

## Rollback plan

If patchright turns out to be broken in a way that can't be worked around, the escape hatch is stock `playwright` + `playwright-stealth`. Two revert scopes:

### Full revert (drop everything, return to pre-plan state)

1. Identify the first commit of this plan (the Task 1 `build:` commit). Let its hash be `<FIRST>` and the tip of the feature work `<LAST>`.
2. Run `git revert --no-commit <FIRST>^..<LAST>` on a new branch.
3. Resolve conflicts (there should be none if no other work landed on the same files).
4. Commit and verify tests pass.

### Partial revert (keep refactors, drop patchright only)

Keep the page-keyed `URLSafetyGuard` refactor and the two-tier timeout structure — both are library-agnostic. Drop only the patchright dependency and stealth-specific additions.

1. `uv add 'playwright>=1.49'` and then `uv remove patchright`. Match the previous version pin recorded in the first Task-1 commit's `uv.lock`.
2. In `src/crawly_mcp/browser.py` and `src/crawly_mcp/service.py` and `src/crawly_mcp/security.py`: revert the `from patchright.async_api import ...` imports back to `from playwright.async_api import ...`. **Do NOT revert the `Page`-typed `pop_blocked_error(page)` signature** — that refactor is independent.
3. Remove (or comment out) `PROVIDER_HOMEPAGE`-driven warm-up + jitter from `service.search()`. Keep the two-tier timeout split.
4. Remove `search_context()` and the persistent-context dict from `BrowserManager`. Have `search()` fall back to `new_context()` as before.
5. Optionally apply `playwright-stealth` patches: `uv add playwright-stealth` and wrap the fetch/search contexts with the library's `stealth_async(page)` helper — this is the documented escape hatch. Will not patch the CDP Runtime.enable leak but restores the basic JS-visible stealth tells.
6. Revert the Dockerfile Task 15 changes (`xvfb` install, profile dir env vars, entrypoint wrapper, patchright chromium install step).
7. Revert the AGENTS.md policy reversal (Task 4).

In both paths, run the full test suite + ruff before finalizing the rollback.
