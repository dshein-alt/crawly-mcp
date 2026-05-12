# SearXNG provider — design

**Date:** 2026-05-12
**Scope:** add `searxng` as an opt-in fourth `SearchProvider` in `crawly-mcp` that routes a query through one user-supplied SearXNG instance via its JSON API. The instance URL comes from the `CRAWLY_SEARXNG_URL` env var; the provider is meaningful only when pointed at a SearXNG that the user controls and that has JSON output enabled.
**Status:** shipped in 0.3.0.

## Problem

`crawly` ships three browser-driven search providers — DuckDuckGo, Google, Yandex. All three actively fight automation, so each query pays the cost of a real Chromium navigation plus per-provider fingerprint maintenance, and any of them can hard-block us at any time.

SearXNG is a self-hostable metasearch engine that, when configured to emit JSON, exposes a `GET /search?q=…&format=json` API returning a clean list of organic results sourced from whichever upstreams (Google, Bing, DuckDuckGo, Brave, etc.) that instance speaks to. Routing a query through a SearXNG you control gives you:

- A query that fans out across several upstream engines instead of being pinned to one.
- A hop where the operator that hits Google is the SearXNG instance, not crawly — so crawly's IP and fingerprint never appear to those upstreams.
- A trivial transport (plain JSON over HTTP/HTTPS) that doesn't need Playwright, persistent profiles, or challenge handling.

## Why opt-in, not default

The shipped design treats SearXNG as opt-in. Public instances listed on `https://searx.space` are not a workable substrate for an automated client: SearXNG's built-in `botdetection` middleware responds to programmatic clients with `429`, an HTTP redirect to `/`, or a `200` with an empty results page — and modern SearXNG ships with `search.formats: [html]` by default, so `?format=json` is silently disabled on most public instances. Operators keep botdetection strict intentionally, to protect their upstream rate-limit budgets. Pre-release testing of 15 randomly-sampled "healthy" instances from `searx.space` returned 0 usable JSON responses.

Consequently the provider has no instance registry, no automatic cross-provider fallback, and no public-instance default. If the caller selects `provider="searxng"` and `CRAWLY_SEARXNG_URL` is not set, the call returns `invalid_input`. If the configured instance fails, the error surfaces to the caller. Pointing the env var at a self-hosted SearXNG with `search.formats: [..., json]` and `server.limiter: false` makes the provider work; community projects (`searxng-mcp`, `ihor-sokoliuk/mcp-searxng`, etc.) follow the same self-hosted assumption.

## Goals

1. Add `"searxng"` to `SearchProvider`.
2. Reach SearXNG via the JSON API over `httpx` — no browser, no persistent profile, no challenge handling for this provider.
3. Require `CRAWLY_SEARXNG_URL`; raise `InvalidInputError` when unset.
4. Leave the existing DDG / Google / Yandex code paths byte-identical. The new code path is additive and lives behind a single dispatch branch in `WebSearchService.search()`.
5. Surface the new provider correctly in the MCP `tools/list` schema so MCP clients see `searxng` as one of the enum values.

## Non-goals

- **Browser-driven SearXNG.** Instances that disable `format=json` are unusable through this provider, full stop. The dual code path would double surface area for no real gain.
- **Instance registry / discovery.** Public instances actively reject our request shape; iterating over them is a waste of every party's time.
- **Cross-provider fallback** for `provider="searxng"`. The user explicitly chose SearXNG and supplied a URL; silently rerouting their query through Google's frontend would surprise them.
- **`searxng` as the default provider.** `DEFAULT_PROVIDER` stays at `"duckduckgo"`.
- **HTTPS-only enforcement.** Both `http://` and `https://` are accepted so localhost / LAN-private SearXNG deployments work without TLS.
- **Caching of search results.** Same posture as today.

## Public surface

### MCP `search` tool

No structural change. The change is entirely driven by the `SearchProvider` literal in [constants.py](src/crawly_mcp/constants.py):

```python
SearchProvider = Literal["duckduckgo", "google", "yandex", "searxng"]
DEFAULT_PROVIDER: SearchProvider = "duckduckgo"
```

`tools/list` automatically surfaces the new enum value. The `provider` argument description string in [mcp_server.py](src/crawly_mcp/mcp_server.py) is updated to mention `searxng` as an opt-in option requiring `CRAWLY_SEARXNG_URL`.

### Pydantic request model

[models.py](src/crawly_mcp/models.py) `SearchRequest.provider` is unchanged.

### CLI

`uv run crawly-cli search --provider searxng --context "…"` works once `CRAWLY_SEARXNG_URL` is set. Without it, the call returns the same `invalid_input` error as the MCP path.

### Env vars

| Variable | Default | Effect |
|---|---|---|
| `CRAWLY_SEARXNG_URL` | unset | The URL of a SearXNG instance with JSON output enabled. Must be `http://` or `https://`. Trailing slash optional (added if missing). **Required** whenever `provider="searxng"` is selected. |

That is the entire new operator-visible surface. Per-instance timeout lives as a source-level constant.

## Module layout

One new module plus a small set of light edits:

| File | Change |
|---|---|
| [src/crawly_mcp/searxng.py](src/crawly_mcp/searxng.py) | **new** — `searxng_search()` adapter (JSON request, error mapping, result normalization). |
| [src/crawly_mcp/constants.py](src/crawly_mcp/constants.py) | extend `SearchProvider` literal; add `CRAWLY_SEARXNG_URL_ENV_VAR` and `SEARXNG_PER_INSTANCE_TIMEOUT_SECONDS`; one-line comment marking `PROVIDER_HOMEPAGE` as keyed by browser providers only. |
| [src/crawly_mcp/service.py](src/crawly_mcp/service.py) | split `WebSearchService.search()` into a dispatcher + two adapter methods; own a shared `httpx.AsyncClient`; add `aclose()`. |
| [src/crawly_mcp/mcp_server.py](src/crawly_mcp/mcp_server.py) | extend `lifespan` to call `WebSearchService.aclose()` before `BrowserManager.close()` on teardown; update the `provider` argument description. |
| [src/crawly_mcp/cli.py](src/crawly_mcp/cli.py) | call `service.aclose()` in the `finally` of `run_search_command` and `run_fetch_command` so the HTTP client doesn't leak on CLI invocations. |
| [src/crawly_mcp/parsing.py](src/crawly_mcp/parsing.py) | add a one-line comment marking the four browser-provider dicts as browser-only. |

No changes to [browser.py](src/crawly_mcp/browser.py), [security.py](src/crawly_mcp/security.py), [challenge.py](src/crawly_mcp/challenge.py), or [page_search.py](src/crawly_mcp/page_search.py).

## Search adapter

```python
async def searxng_search(
    instance_url: str,
    query: str,
    *,
    client: httpx.AsyncClient,
    timeout: float,
) -> list[str]:
```

**Request.** `GET {instance_url}search` with query parameters `q=<query>`, `format=json`, `safesearch=0`, `language=en`. Headers: `User-Agent: STANDARD_USER_AGENT`, `Accept: application/json`. No cookies, no referer.

**Response handling.**

- `status_code in (401, 403, 429)` → `ProviderBlockedError`.
- Other 4xx/5xx → `response.raise_for_status()` propagates as `httpx.HTTPStatusError`.
- `Content-Type` does not contain `application/json` → `ProviderBlockedError("instance returned non-JSON; format=json may be disabled")`.
- JSON parse failure → `ProviderBlockedError`.
- Otherwise, iterate `payload.get("results") or []`, take `r["url"]`, normalize (strip, drop non-http(s), dedupe), and stop at `MAX_SEARCH_RESULTS` (=5).
- Zero results from a clean response → return `[]`. Zero results is a valid answer (matches today's "zero results is not an error" semantics).

**Error types.** No new exception classes — reuse `ProviderBlockedError` from [errors.py](src/crawly_mcp/errors.py) for "instance is unwilling/unable to serve us." Transport-level errors (`httpx.TimeoutException`, `httpx.RequestError`, `httpx.HTTPStatusError`) propagate as-is and are caught by the orchestrator.

## Service orchestration

`WebSearchService.__init__` gains a long-lived shared HTTP client:

```python
self._http = httpx.AsyncClient(
    timeout=httpx.Timeout(10.0),
    follow_redirects=True,
    max_redirects=3,
)
```

`WebSearchService.aclose()` disposes the client. The MCP `lifespan` and both `cli.py` command runners call it in their `finally` blocks before `BrowserManager.close()` so the client never leaks.

`WebSearchService.search()` is refactored into a thin dispatcher:

```python
async def search(self, *, provider=None, context):
    try:
        request = SearchRequest(provider=provider, context=context)
    except ValidationError as exc:
        raise InvalidInputError(str(exc.errors()[0]["msg"])) from exc
    logger.info("search entry provider={} context={!r}", request.provider, request.context)
    if request.provider == "searxng":
        return await self._search_via_searxng(request)
    return await self._search_via_browser(request)
```

`_search_via_browser` is today's `search()` body, lifted unchanged so DDG/Google/Yandex behavior is byte-identical. The `ValidationError → InvalidInputError` guard moves to the dispatcher (was at the top of the old `search()`).

`_search_via_searxng` runs:

```python
async def _search_via_searxng(self, request: SearchRequest) -> SearchResponse:
    pinned = os.environ.get(CRAWLY_SEARXNG_URL_ENV_VAR)
    if not pinned:
        raise InvalidInputError(
            f"provider='searxng' requires {CRAWLY_SEARXNG_URL_ENV_VAR} to be set..."
        )
    if not pinned.startswith(("https://", "http://")):
        raise InvalidInputError(
            f"{CRAWLY_SEARXNG_URL_ENV_VAR} must be http(s)://; got {pinned!r}"
        )
    instance_url = pinned if pinned.endswith("/") else pinned + "/"

    try:
        urls = await searxng_search(
            instance_url, request.context,
            client=self._http,
            timeout=SEARXNG_PER_INSTANCE_TIMEOUT_SECONDS,
        )
    except httpx.TimeoutException as exc:
        raise TimeoutExceededError(...) from exc
    except (httpx.RequestError, httpx.HTTPStatusError) as exc:
        raise ProviderBlockedError(...) from exc
    return SearchResponse(urls=urls)
```

Key behaviors:

- **`CRAWLY_SEARXNG_URL` is required.** Unset → `InvalidInputError`. Wrong scheme → `InvalidInputError`. Trailing slash optional.
- **Single attempt.** No retries, no fallback to another provider. The configured instance either works or returns a typed error.
- **Zero results is success.** A clean JSON response with empty `results` is a valid empty `SearchResponse`. Matches existing semantics across providers.
- **Provider-level errors propagate.** Caller sees `ProviderBlockedError` / `TimeoutExceededError` shaped exactly like the browser providers' equivalents.

## Constants

Added to [constants.py](src/crawly_mcp/constants.py):

| Constant | Value | Notes |
|---|---|---|
| `SearchProvider` | `Literal["duckduckgo", "google", "yandex", "searxng"]` | Order is conventional only. |
| `DEFAULT_PROVIDER` | `"duckduckgo"` | Unchanged. |
| `CRAWLY_SEARXNG_URL_ENV_VAR` | `"CRAWLY_SEARXNG_URL"` | |
| `SEARXNG_PER_INSTANCE_TIMEOUT_SECONDS` | `8` | Tighter than `SEARCH_PAGE_TIMEOUT_SECONDS = 15`. |

## Dependencies

`httpx` is already a project dependency. No new dependencies. `bs4` is not used on the searxng path.

## Testing

### Fixtures

Under [tests/fixtures/searxng/](tests/fixtures/searxng/) (new directory):

- `search_response_good.json` — captured `?format=json` payload with 5 organic results.
- `search_response_empty.json` — `format=json` payload with `"results": []`.
- `search_response_blocked.html` — non-JSON response for the `Content-Type` guard.

### Unit tests

`tests/test_searxng_adapter.py` (new) — exercises the adapter against `httpx.MockTransport`:

- Good JSON response → up to 5 deduped URLs, in order.
- Cap at 5 with 12 distinct URLs.
- Dedupe with 4 URLs including 1 duplicate.
- Empty `results` → `[]`.
- Non-JSON `Content-Type` → `ProviderBlockedError`.
- HTTP 401/403/429 (parametrized) → `ProviderBlockedError`.
- HTTP 503 → `httpx.HTTPStatusError` propagates.
- Non-http(s) URLs are dropped.
- Malformed JSON → `ProviderBlockedError`.

`tests/test_service_searxng.py` (new) — exercises `WebSearchService` orchestration with mocked dependencies (constructed via `__new__` to skip browser init):

- `provider="searxng"` without env → `InvalidInputError`.
- `provider="searxng"` with env → adapter called exactly once with the normalized URL.
- Trailing slash normalization.
- Zero results from adapter → empty `SearchResponse`, no fallback.
- `ProviderBlockedError` from adapter propagates.
- `CRAWLY_SEARXNG_URL` with non-http(s) scheme → `InvalidInputError`.
- `http://` URLs accepted (so localhost / LAN-private instances work).
- `provider="duckduckgo"` (and other browser providers) skip the SearXNG path entirely.
- `aclose()` closes the HTTP client.

`tests/test_models.py` (existing) — extended to assert `tools/list` advertises `"searxng"` in the `provider` enum (the test already parametrizes over `ALLOWED_PROVIDERS`).

### Live integration

Verified end-to-end against a real SearXNG container (image `searxng/searxng:latest`) with `search.formats: [html, json]` and `server.limiter: false`, on a shared container network with crawly-mcp. The MCP `search(provider="searxng", context=…)` call returned 5 real result URLs sourced from the upstream engines the SearXNG instance speaks to.

## Privacy posture

- With `searxng` opt-in and not default, no query is silently routed to a third party unless the user explicitly selected the provider and supplied an instance URL.
- The shared `httpx.AsyncClient` does not persist cookies and uses a generic browser User-Agent.

## Rollout

Version bump 0.2.1 → 0.3.0.

CHANGELOG under `## [0.3.0]`:

- **Added** — `searxng` provider routing a query through one SearXNG instance via its JSON API; instance URL supplied via `CRAWLY_SEARXNG_URL`.
- **Changed** — the `search` MCP tool's `provider` parameter is now advertised in `tools/list` as a non-nullable enum with an explicit default value.

## Acceptance criteria

1. `provider="searxng"` against a self-hosted SearXNG (with JSON enabled and limiter disabled) returns up to 5 organic URLs through both MCP and CLI.
2. `provider="searxng"` without `CRAWLY_SEARXNG_URL` returns `invalid_input` through both MCP and CLI.
3. `CRAWLY_SEARXNG_URL=ftp://...` returns `invalid_input` (wrong scheme).
4. `CRAWLY_SEARXNG_URL=http://localhost:8080/` works (no TLS requirement).
5. `tools/list` advertises `provider` enum `["duckduckgo","google","yandex","searxng"]` with default `"duckduckgo"`.
6. `provider="duckduckgo"` / `"google"` / `"yandex"` requests still go through `_search_via_browser` exclusively; the new `_http` client is not used.
7. Test suite passes with the new coverage in `tests/test_searxng_adapter.py` and `tests/test_service_searxng.py`.

## Risks

- **Operator misconfiguration.** A user pointing `CRAWLY_SEARXNG_URL` at an instance with `format=json` disabled gets `ProviderBlockedError` on every call. The README documents the required SearXNG settings.
- **Instance availability.** Crawly is at the mercy of the configured instance. There is no fallback by design.

## Open questions

None blocking. Items deferred to follow-up:

- Whether to expose `categories`, `time_range`, or `language` as MCP arguments. Out of v1.
- Whether to add a small `searxng-compose/` example under the repo with a working `settings.yml` and `docker-compose.yml`. Currently the README inline snippet is enough.
