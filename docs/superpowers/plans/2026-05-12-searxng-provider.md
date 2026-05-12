# SearXNG provider — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `searxng` as an opt-in fourth `SearchProvider` in `crawly-mcp` that routes one query through a user-supplied SearXNG instance via its JSON API. The instance URL is required via the `CRAWLY_SEARXNG_URL` env var; the provider is intended for self-hosted SearXNG with JSON output enabled.

**Architecture:** New self-contained module `src/crawly_mcp/searxng.py` containing one public coroutine `searxng_search()` that hits `{instance}/search?q=…&format=json` via `httpx`. `WebSearchService.search()` becomes a thin dispatcher with two adapter methods: `_search_via_browser` (today's body, byte-identical) and `_search_via_searxng` (new). The SearXNG path requires `CRAWLY_SEARXNG_URL` and has no retries, no fallback, and no instance registry. The shared `httpx.AsyncClient` is owned by `WebSearchService` and disposed via `aclose()` from both `mcp_server.lifespan` and `cli.py`.

**Tech Stack:** Python 3.11+, `httpx` (existing dep), `patchright` (existing, untouched on SearXNG path), `loguru`, `pytest` + `pytest-asyncio`, `ruff`.

**Reference spec:** [docs/superpowers/specs/2026-05-12-searxng-provider-design.md](../specs/2026-05-12-searxng-provider-design.md). When this plan is terse, the spec has the semantic detail.

---

## File structure

**New runtime module:**
- `src/crawly_mcp/searxng.py` — `searxng_search()` coroutine and supporting `_BLOCKING_STATUS` set.

**Modified runtime modules:**
- `src/crawly_mcp/constants.py` — extend `SearchProvider` literal with `"searxng"`; add `CRAWLY_SEARXNG_URL_ENV_VAR` and `SEARXNG_PER_INSTANCE_TIMEOUT_SECONDS`; one-line comment marking `PROVIDER_HOMEPAGE` as keyed by browser providers only.
- `src/crawly_mcp/service.py` — refactor `WebSearchService.search()` into a dispatcher + `_search_via_browser` (lifted body) + `_search_via_searxng` (new); add `_http: httpx.AsyncClient` and `aclose()`.
- `src/crawly_mcp/mcp_server.py` — extend `lifespan` to call `WebSearchService.aclose()` before `BrowserManager.close()`; update the `provider` argument description string.
- `src/crawly_mcp/cli.py` — call `service.aclose()` in `finally` of `run_search_command` and `run_fetch_command`.
- `src/crawly_mcp/parsing.py` — add a one-line comment marking `SEARCH_URL_TEMPLATES`, `RESULT_SELECTORS`, `SEARCH_BLOCK_MARKERS`, `PROVIDER_HOST_SUFFIXES` as browser-only.

**New test files:**
- `tests/test_searxng_adapter.py` — unit tests for `searxng_search()`.
- `tests/test_service_searxng.py` — `WebSearchService` orchestration tests for the SearXNG path.

**Modified test files:**
- `tests/test_models.py` — `tools/list` schema test already parametrizes over `ALLOWED_PROVIDERS`, so the enum check picks up `"searxng"` automatically; no edit needed beyond verifying it still passes.
- `tests/test_parsing.py` — small rename of `test_build_search_url_uses_duckduckgo_by_default` to make the test independent of `DEFAULT_PROVIDER` (pass `"duckduckgo"` explicitly).

**New test fixtures:**
- `tests/fixtures/searxng/search_response_good.json` — captured SearXNG `?format=json` response with 5 organic results.
- `tests/fixtures/searxng/search_response_empty.json` — `format=json` payload with `"results": []`.
- `tests/fixtures/searxng/search_response_blocked.html` — non-JSON response for `Content-Type` guard testing.

**Config & docs:**
- `pyproject.toml` — bump version `0.2.1` → `0.3.0`.
- `CHANGELOG.md` — new `## [0.3.0]` section above `## [0.2.1]`.
- `README.md` — update Tools section; add an opt-in SearXNG subsection with the required env var and a note about public-instance unsuitability.
- `AGENTS.md` — update Approaches → Search bullet to document the dual code path.

---

## Tasks

### Task 1: SearXNG constants and provider literal

**Files:**
- Modify: `src/crawly_mcp/constants.py`

- [ ] **Step 1: Extend the `SearchProvider` literal**

Edit `src/crawly_mcp/constants.py` (the `SearchProvider` line):

```python
SearchProvider = Literal["duckduckgo", "google", "yandex", "searxng"]
ALLOWED_PROVIDERS: tuple[SearchProvider, ...] = get_args(SearchProvider)
DEFAULT_PROVIDER: SearchProvider = "duckduckgo"  # unchanged
```

- [ ] **Step 2: Add SearXNG-only constants**

Append to `src/crawly_mcp/constants.py`, after the existing `PROVIDER_HOMEPAGE` mapping:

```python
# --- SearXNG provider configuration ---
#
# SearXNG is an opt-in provider: the caller must pass provider="searxng" AND
# set CRAWLY_SEARXNG_URL to the URL of a SearXNG instance that exposes the
# JSON API (format=json). Public instances on https://searx.space generally
# return 429 / redirect / empty results to non-browser clients via their
# botdetection middleware, so this provider is intended for self-hosted or
# privately-known instances. There is no instance registry and no automatic
# cross-provider fallback for searxng — failure surfaces to the caller.

CRAWLY_SEARXNG_URL_ENV_VAR = "CRAWLY_SEARXNG_URL"
SEARXNG_PER_INSTANCE_TIMEOUT_SECONDS = 8
```

- [ ] **Step 3: Mark `PROVIDER_HOMEPAGE` as browser-providers-only**

Add a one-line comment above `PROVIDER_HOMEPAGE`:

```python
# Keyed by browser-driven providers only; searxng is handled separately and
# does not use the warm-up hop or homepage hint.
PROVIDER_HOMEPAGE: dict[SearchProvider, str] = {...}
```

- [ ] **Step 4: Verify existing tests still pass**

Run: `uv run pytest -q`
Expected: all existing tests pass (the schema test in `test_models.py` parametrizes over `ALLOWED_PROVIDERS`, so it picks up the new enum value automatically).

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/crawly_mcp/constants.py
git add src/crawly_mcp/constants.py
git commit -m "feat(constants): add searxng provider literal and env-var config"
```

---

### Task 2: SearXNG JSON adapter — fixtures, failing tests, implementation

**Files:**
- Create: `tests/fixtures/searxng/search_response_good.json`
- Create: `tests/fixtures/searxng/search_response_empty.json`
- Create: `tests/fixtures/searxng/search_response_blocked.html`
- Create: `tests/test_searxng_adapter.py`
- Create: `src/crawly_mcp/searxng.py`

- [ ] **Step 1: Create the fixtures**

`tests/fixtures/searxng/search_response_good.json` — a captured SearXNG `?format=json` response with 5 organic `results[*].url` entries.

`tests/fixtures/searxng/search_response_empty.json` — `{"results": [], "answers": [], ...}`.

`tests/fixtures/searxng/search_response_blocked.html` — minimal non-JSON 403 HTML stub.

- [ ] **Step 2: Write failing adapter tests**

Create `tests/test_searxng_adapter.py` with tests covering: happy path (returns 5 dedup'd URLs), cap-at-5 with 12 distinct URLs, dedupe with 4 URLs containing 1 duplicate, empty results, non-JSON Content-Type, parametrized blocking statuses (`401, 403, 429`), 503 server error, non-http(s) scheme drop, and malformed JSON. Use `httpx.MockTransport` to fake responses.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_searxng_adapter.py -v`
Expected: every test fails with `ModuleNotFoundError: No module named 'crawly_mcp.searxng'`.

- [ ] **Step 4: Implement the adapter**

Create `src/crawly_mcp/searxng.py` with:

```python
async def searxng_search(
    instance_url: str,
    query: str,
    *,
    client: httpx.AsyncClient,
    timeout: float,
) -> list[str]:
    """Query one SearXNG instance via its JSON API and return up to 5 result URLs.

    Raises ProviderBlockedError for refused/non-JSON responses and 401/403/429.
    Propagates httpx.TimeoutException, httpx.RequestError, and httpx.HTTPStatusError
    for transport-level or other HTTP failures so the caller can surface them.
    """
```

The request shape is `GET {instance_url.rstrip('/')}/search` with params `q, format=json, safesearch=0, language=en`, headers `User-Agent: STANDARD_USER_AGENT`, `Accept: application/json`. Response handling per the spec's "Search adapter" section.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_searxng_adapter.py -v`
Expected: all 10 tests pass.

- [ ] **Step 6: Lint and commit**

```bash
uv run ruff check src/crawly_mcp/searxng.py tests/test_searxng_adapter.py
uv run ruff format --check src/crawly_mcp/searxng.py tests/test_searxng_adapter.py
git add src/crawly_mcp/searxng.py tests/test_searxng_adapter.py tests/fixtures/searxng/
git commit -m "feat(searxng): add JSON API search adapter and fixtures"
```

---

### Task 3: WebSearchService dispatcher and SearXNG path

**Files:**
- Create: `tests/test_service_searxng.py`
- Modify: `src/crawly_mcp/service.py`
- Modify: `src/crawly_mcp/cli.py`
- Modify: `tests/test_parsing.py` (rename one test)

- [ ] **Step 1: Locate the current `WebSearchService.search()` body**

Run: `grep -n "def \|class WebSearchService" src/crawly_mcp/service.py | head -30`
Open the file and note that today's `search()` body wraps `SearchRequest(...)` in `try/except ValidationError → InvalidInputError`. That wrapping moves to the new dispatcher.

- [ ] **Step 2: Write failing orchestration tests**

Create `tests/test_service_searxng.py` covering: missing env → `InvalidInputError`, env set → adapter called once with the normalized URL, trailing-slash normalization, zero results → empty `SearchResponse`, `ProviderBlockedError` propagation, non-http(s) scheme rejection, `http://` accepted, non-SearXNG providers skip the SearXNG path, `aclose()` closes the HTTP client. Construct `WebSearchService` instances via `__new__` to bypass `__init__`'s browser-manager wiring; stub dependencies with `AsyncMock`.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_service_searxng.py -v`
Expected: all 9 tests fail with `AttributeError`/`ImportError` on the not-yet-present `_search_via_searxng` / `searxng_search` re-export / `_http`.

- [ ] **Step 4: Add the shared HTTP client and `aclose()` to `__init__`**

In `WebSearchService.__init__`:

```python
self._http = httpx.AsyncClient(
    timeout=httpx.Timeout(10.0),
    follow_redirects=True,
    max_redirects=3,
)
```

Add:

```python
async def aclose(self) -> None:
    await self._http.aclose()
```

- [ ] **Step 5: Refactor `search()` into a dispatcher + lifted `_search_via_browser`**

Rename today's `WebSearchService.search` to `_search_via_browser` and change its signature to accept the already-validated `request: SearchRequest`. Drop the inner `SearchRequest(...)` construction and the `try/except ValidationError` block (both move to the new dispatcher). The rest of the body is byte-identical.

Add the new `search()` dispatcher above it that wraps the construction in `try/except ValidationError → InvalidInputError`, logs `search entry`, and routes `provider == "searxng"` to `_search_via_searxng`.

- [ ] **Step 6: Implement `_search_via_searxng`**

```python
async def _search_via_searxng(self, request: SearchRequest) -> SearchResponse:
    pinned = os.environ.get(CRAWLY_SEARXNG_URL_ENV_VAR)
    if not pinned:
        raise InvalidInputError(
            f"provider='searxng' requires {CRAWLY_SEARXNG_URL_ENV_VAR} to be set..."
        )
    if not pinned.startswith(("https://", "http://")):
        raise InvalidInputError(...)
    instance_url = pinned if pinned.endswith("/") else pinned + "/"
    try:
        urls = await searxng_search(instance_url, request.context,
                                    client=self._http,
                                    timeout=SEARXNG_PER_INSTANCE_TIMEOUT_SECONDS)
    except httpx.TimeoutException as exc:
        raise TimeoutExceededError(...) from exc
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        raise ProviderBlockedError(...) from exc
    return SearchResponse(urls=urls)
```

Add `searxng_search` to the top-of-file imports so `crawly_mcp.service.searxng_search` resolves for `monkeypatch.setattr`.

- [ ] **Step 7: Plug `aclose()` into `cli.py`**

In `src/crawly_mcp/cli.py`, in both `run_search_command` and `run_fetch_command`, add `await service.aclose()` inside the `finally` block, before `await browser_manager.close()`.

- [ ] **Step 8: Make the parsing test independent of `DEFAULT_PROVIDER`**

In `tests/test_parsing.py`, rename `test_build_search_url_uses_duckduckgo_by_default` to `test_build_search_url_builds_duckduckgo_url` and pass `"duckduckgo"` explicitly instead of relying on the `None → DEFAULT_PROVIDER` coercion. Keeps the test robust against future default flips.

- [ ] **Step 9: Run tests and verify**

Run: `uv run pytest tests/test_service_searxng.py tests/test_parsing.py -v && uv run pytest -q`
Expected: 9/9 service tests pass; full suite green.

- [ ] **Step 10: Lint and commit**

```bash
uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/
git add src/crawly_mcp/service.py src/crawly_mcp/cli.py tests/test_service_searxng.py tests/test_parsing.py
git commit -m "feat(service): dispatch to searxng with required env-var"
```

---

### Task 4: MCP wiring — schema description, lifespan, schema test

**Files:**
- Modify: `src/crawly_mcp/mcp_server.py`
- Modify: `tests/test_models.py`

- [ ] **Step 1: Extend `lifespan` to dispose the HTTP client**

In `src/crawly_mcp/mcp_server.py`, find the existing `lifespan` async context manager (the `WebSearchService` instance is bound as `service`). Add `await service.aclose()` BEFORE `await browser_manager.close()` in the `finally` block.

- [ ] **Step 2: Update the `provider` argument description**

In the `search` MCP tool registration, update the `provider` argument description to mention `searxng` as an opt-in option requiring `CRAWLY_SEARXNG_URL`. Keep the description short.

- [ ] **Step 3: Verify the schema test picks up the new enum value**

Run: `uv run pytest tests/test_models.py::test_search_tool_schema_advertises_provider_enum_and_descriptions -v`
Expected: PASS — the test already parametrizes over `ALLOWED_PROVIDERS`, so it asserts each value (including `"searxng"`) appears in the description string. No test edit needed beyond confirming.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: all tests pass.

- [ ] **Step 5: Lint and commit**

```bash
uv run ruff check src/crawly_mcp/mcp_server.py
git add src/crawly_mcp/mcp_server.py tests/test_models.py
git commit -m "feat(mcp): expose searxng in tool schema; close client in lifespan"
```

---

### Task 5: parsing.py annotation

**Files:**
- Modify: `src/crawly_mcp/parsing.py`

- [ ] **Step 1: Add a header comment above the four browser-provider dicts**

Edit `src/crawly_mcp/parsing.py` just above the `PROVIDER_HOST_SUFFIXES` declaration:

```python
# The four mappings below (PROVIDER_HOST_SUFFIXES, SEARCH_URL_TEMPLATES,
# RESULT_SELECTORS, SEARCH_BLOCK_MARKERS) are keyed by browser-driven providers
# only ("duckduckgo", "google", "yandex"). The searxng provider uses the JSON
# API in crawly_mcp.searxng and never traverses this module.
```

- [ ] **Step 2: Run tests and commit**

```bash
uv run pytest -q
git add src/crawly_mcp/parsing.py
git commit -m "docs(parsing): note that provider dicts are browser-only"
```

---

### Task 6: README + AGENTS documentation

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Update the README Tools section**

Update the `search(...)` description to list `duckduckgo` as the default and `searxng` as an opt-in option requiring `CRAWLY_SEARXNG_URL`.

- [ ] **Step 2: Add a SearXNG subsection**

Add a `## SearXNG (opt-in)` section explaining the two conditions to use it, an example invocation, and a short note about why public instances generally don't work (botdetection + JSON disabled by default).

- [ ] **Step 3: Update AGENTS.md Approaches → Search bullet**

Rewrite the Search bullet to describe the two code paths: default browser-driven (`duckduckgo`, `google`, `yandex`) and opt-in `searxng` via JSON over `httpx`.

- [ ] **Step 4: Commit**

```bash
git add README.md AGENTS.md
git commit -m "docs: README and AGENTS for the searxng opt-in provider"
```

---

### Task 7: Version bump + CHANGELOG

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock` (version line only)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump version**

Edit `pyproject.toml`: `version = "0.3.0"`.

Run `uv lock` to refresh `uv.lock` with the new package version.

- [ ] **Step 2: Add `[0.3.0]` CHANGELOG section**

Insert above the `[0.2.1]` entry:

```markdown
## [0.3.0] - 2026-05-12

Add SearXNG as an opt-in fourth search provider for self-hosted instances.

### Added

- `searxng` provider value on the `search` tool. Routes the query through a single SearXNG instance via its JSON API (`?format=json`) over `httpx`. The instance URL is supplied via the `CRAWLY_SEARXNG_URL` env var; without it the call returns an `invalid_input` error.
- The `search` MCP tool's `provider` parameter is now advertised in `tools/list` as a non-nullable enum with an explicit `default` value (still `duckduckgo`).

### Changed

- `searxng` is **not** the default — `duckduckgo` remains the default. Public instances on `searx.space` actively block automated clients, so an aspirational default would be a slow no-op.

### Fixed

```

- [ ] **Step 3: Verify and commit**

```bash
uv run python -c "from crawly_mcp.version import get_package_version; print(get_package_version())"
# expect: 0.3.0
uv run pytest -q
git add pyproject.toml uv.lock CHANGELOG.md
git commit -m "release: 0.3.0"
```

---

### Task 8: Full CI-equivalent verification

**Files:** (none — verification only)

- [ ] **Step 1: Lint everything**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean.

- [ ] **Step 2: Run the entire test suite**

Run: `uv run pytest -q`
Expected: green.

- [ ] **Step 3: Build the container**

```bash
docker build -t localhost/crawly-mcp:local .
```

- [ ] **Step 4: Live smoke against a self-hosted SearXNG**

Spin up a SearXNG container with `search.formats: [html, json]` and `server.limiter: false` on a shared network with crawly-mcp. Set `CRAWLY_SEARXNG_URL=http://searxng:8080/` on the crawly container.

Issue an MCP `search(provider="searxng", context="...")` call and confirm 5 real URLs come back. Issue a second call without `CRAWLY_SEARXNG_URL` set and confirm `invalid_input`.

---

## Done

Acceptance criteria from the spec are satisfied:

1. `provider="searxng"` against a self-hosted SearXNG returns ≤5 URLs — Task 8 Step 4.
2. `provider="searxng"` without `CRAWLY_SEARXNG_URL` returns `invalid_input` — Task 3 (tests) + Task 8 Step 4.
3. Wrong scheme on env var returns `invalid_input` — Task 3 (test).
4. `http://` URLs accepted — Task 3 (test).
5. `tools/list` advertises the new enum + default — Task 4 (test).
6. Non-SearXNG providers skip the SearXNG path — Task 3 (test).
7. Test suite passes with new coverage — Task 8 Step 2.
