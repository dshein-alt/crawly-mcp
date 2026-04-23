# Page search — design

**Date:** 2026-04-23
**Scope:** new MCP tool `page_search` in `crawly-mcp` — on-page search with a three-tier cascade
**Status:** spec — awaiting review

## Problem

Small-context local LLMs routinely need to locate a specific passage inside a documentation page without pulling the whole rendered page into their context window. The existing `fetch()` tool returns everything or nothing (subject to `CRAWLY_FETCH_MAX_SIZE`); there is no way to ask "find this on that page."

Many documentation sites ship their own search — Algolia DocSearch (React, Vue, Astro, Svelte, Tailwind, etc.), Readthedocs API, OpenSearch descriptors, or plain GET-style HTML forms. Using those is cheaper and more precise than find-in-page text matching, because the site already indexes its own content. But not every page has a usable search facility, and some that appear to (Sphinx, MkDocs Material's lunr) are pure-JS client-rendered and impractical to drive headlessly.

## Goals

1. Expose a single MCP tool `page_search(url, query)` that returns a small ranked list of matches with snippets.
2. Prefer the site's own search when we can detect and drive it reliably.
3. Always produce an answer via text-level find-in-page when the site has no usable search.
4. Keep the endpoint self-contained: one MCP call, one fetch of the source page, cascade internally.
5. Reuse existing infrastructure (`BrowserManager`, `URLSafetyGuard`, challenge handling, fetch-size cap) — no new abstractions.

## Non-goals

- **Multi-URL input.** The endpoint takes a single `url` per call. Multi-URL like `fetch()` multiplies navigation work and complicates the response shape; out of scope for v1.
- **JS-only client-rendered search** (Sphinx `search.html` with searchindex.js, MkDocs Material lunr-based search, Algolia modal UI with no scrapable config). These fall through to the text tier, which searches the source page's rendered content directly and is often good enough.
- **POST-form search.** Tier 2 handles only `method="GET"` forms. POST requires replaying form encoding and is rare for on-page search.
- **Caching.** No caching of Algolia credentials, OpenSearch descriptors, or results across calls. Revisit if latency becomes a complaint.
- **Site-specific result extractors** for arbitrary unknown result pages. Tier 2 reuses the generic text-match extractor on the results page instead.

## Public surface

New MCP tool registered alongside `search` and `fetch`. CLI subcommand `page-search` for parity.

**Request model** (`src/crawly_mcp/models.py`):

```python
class PageSearchRequest(BaseModel):
    url: str
    query: str  # non-empty, validated like SearchRequest.context

    model_config = ConfigDict(extra="forbid")
```

**Response model:**

```python
PageSearchMode = Literal["algolia", "opensearch", "readthedocs", "form", "text"]

class PageSearchResult(BaseModel):
    snippet: str            # always present, <= PAGE_SEARCH_SNIPPET_CONTEXT_CHARS
    url: str | None         # present for tiers 1a/1b/1c/2, None for tier 3
    title: str | None       # best-effort page/result heading

class PageSearchResponse(BaseModel):
    mode: PageSearchMode            # the tier that produced `results`
    attempted: list[PageSearchMode] # ordered list of tiers actually invoked
    source_url: str                 # URL the caller passed
    results_url: str | None         # navigated results-page URL (tiers 1b/2); None for 1a/1c/3
    results: list[PageSearchResult] # capped at MAX_SEARCH_RESULTS (5)
    truncated: bool                 # true if response body hit CRAWLY_FETCH_MAX_SIZE
```

**Mode / attempted semantics:**

- `mode` names the single tier that produced `results`. If all tiers were tried and text produced zero matches, `mode="text"`, `results=[]`.
- `attempted` is the ordered list of tiers that were actually invoked. Tiers skipped by detection (e.g. no Algolia config on the page) do not appear in `attempted`; only tiers whose execute step ran do.
- A response where `mode == attempted[-1]` means every earlier tier was either not detected or failed; a response where `mode == attempted[0]` means the first-detected tier succeeded on its first attempt.
- **Example:** Algolia config detected but returned 0 hits; OpenSearch not detected; Readthedocs not detected (wrong host); generic form detected and produced results. Then `attempted=["algolia","form"]`, `mode="form"`.

**Tier interface (informal):**

```python
@dataclass
class DetectResult:
    ...  # tier-specific payload (e.g. Algolia config, form action+input name)

class Tier(Protocol):
    name: PageSearchMode
    def detect(self, source_html: str, source_url: str) -> DetectResult | None: ...
    async def execute(self, hit: DetectResult, query: str) -> list[PageSearchResult]: ...
```

Each tier is an independent unit: `detect` is pure (HTML in, payload out, no I/O), `execute` does the I/O. The orchestrator owns timeouts, error isolation, and `attempted` bookkeeping.

## Three-tier cascade

The cascade is orchestrated by a new `PageSearchService` in `src/crawly_mcp/page_search.py`. Each tier has a detect step (cheap, runs on the source page HTML) and an execute step (runs only if detection succeeds). The first tier whose execute step returns at least one result wins; failures (timeout, network error, detection-true-but-execution-empty) record the tier in `attempted` and fall through.

### Tier 1a — Algolia DocSearch

- **Detect:** inspect source HTML for references to `@docsearch/js` / `@docsearch/react` in `<script>` tags, or a `<div id="docsearch">` / `<input id="docsearch-input">`, or an inline initialization block containing `appId`, `apiKey`, `indexName` (pattern match on common forms: `docsearch({...})`, `window.docSearchConfig = {...}`, JSON island `<script type="application/json">`). Extract the three config values.
- **Execute:** POST to the Algolia query endpoint with headers `X-Algolia-Application-Id`, `X-Algolia-API-Key` and body `{"params": "query=<urlencoded query>&hitsPerPage=5"}`. Use `httpx.AsyncClient` with `PAGE_SEARCH_TIER_TIMEOUT_SECONDS`. Endpoint host is `{appId}-dsn.algolia.net` (the canonical DocSearch DSN) unless the detected config specifies an explicit `apiUrl` / `host`, in which case honor it **only after** passing it through `URLSafetyGuard.validate_user_url()` — an attacker-controlled Algolia config on a malicious page must not be able to redirect us to a private-IP host.
- **Extract:** for each `hit` in the response, emit `PageSearchResult(url=hit["url"], title=_join_hierarchy(hit["hierarchy"]), snippet=hit["_snippetResult"]["content"]["value"])`. Strip Algolia's `<em>` highlight markers from snippet before returning.
- **Credentials note:** Algolia appId/apiKey in DocSearch configs are intentionally public — they're frontend JS keys scoped to the index. This is not a secrets leak.

### Tier 1b — OpenSearch descriptor

- **Detect:** look in source `<head>` for `<link rel="search" type="application/opensearchdescription+xml" href="...">`. Resolve `href` against the source URL.
- **Execute:** `httpx` GET the descriptor XML. Parse it (stdlib `xml.etree.ElementTree`) for the first `<Url type="text/html" template="...{searchTerms}...">`. Substitute `{searchTerms}` with the url-encoded query (and `{startIndex}`, `{count}`, `{language}`, `{inputEncoding}`, `{outputEncoding}` with sensible defaults per the OpenSearch 1.1 spec: `1`, `5`, `*`, `UTF-8`, `UTF-8`). Navigate to the resulting URL via an ephemeral `BrowserManager` context.
- **Extract:** reuse tier-3's `build_snippets(text, query, ...)` on the results page's rendered content. Snippet-only extraction; `url` on each result remains `None` because we cannot generically identify result links on an unknown search UI. `results_url` on the response is set to the navigated URL.

### Tier 1c — Readthedocs API

- **Detect:** source URL hostname ends with `.readthedocs.io` or `.readthedocs-hosted.com`, AND the hostname has a non-empty subdomain, AND the path matches `/<language>/<version>/<...>` with at least two leading segments. Extract `project = <subdomain>`, `version = <path-segment-2>`. Skip (detection miss, not failure) when any condition doesn't hold — this naturally drops subprojects, translations, and the readthedocs.org root.
- **Execute:** `httpx` GET `https://readthedocs.org/api/v2/search/?q=<query>&project=<slug>&version=<version>`. Timeout as above.
- **Extract:** the API returns `results[]` with nested `blocks[]`; flatten to the top 5 blocks with `PageSearchResult(url=<project result URL + block anchor>, title=<block title>, snippet=<block content highlighted text with <span> markers stripped>)`.

### Tier 2 — Generic GET form

- **Detect:** parse source HTML. Search for a `<form>` matching in priority order:
  1. `form[role="search"]`
  2. `form` containing `input[type="search"]`
  3. `form` containing `input[name]` where `name` ∈ `{q, query, search, s}`
  The form must have `method` unset or `method="get"` (case-insensitive) and a non-empty `action` attribute. Record the form's `action` and the matched input's `name`.
- **Execute:** resolve `action` against the source URL. Construct `{resolved_action}?{input_name}={urlencoded query}` preserving any existing query params on `action`. Navigate via ephemeral `BrowserManager` context.
- **Extract:** same as tier 1b — `build_snippets` on the results page, `url=None` per result, `results_url` set on the response.

### Tier 3 — Find-in-page (always runs if nothing earlier wins)

- **Execute:** run `extract_readable_text(source_html)` (the existing helper from `service.py`) to get main-content text. Call `build_snippets(text, query, max_matches=MAX_SEARCH_RESULTS, context_chars=PAGE_SEARCH_SNIPPET_CONTEXT_CHARS)`.
- **Extract:** each snippet becomes `PageSearchResult(snippet=..., url=None, title=<source page <title>>)`. `results_url` on the response is `None`.
- **Zero-match case:** valid response, `mode="text"`, `results=[]`, `attempted` contains every tier that tried.

## Orchestration

```
async with asyncio.timeout(FETCH_TOTAL_TIMEOUT_SECONDS):  # outer clamp
    source_html = fetch source page (raw HTML, before text extraction)
    if source fetch fails → raise WebSearchError("navigation_failed")

    for tier in [algolia, opensearch, readthedocs, form]:
        hit = tier.detect(source_html, source_url)
        if hit is None:
            continue
        attempted.append(tier.name)
        try:
            results = await asyncio.wait_for(tier.execute(hit, query),
                                             PAGE_SEARCH_TIER_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, TierExecutionError) as exc:
            logger.warning("page_search tier={} failed: {}", tier.name, exc)
            continue
        if results:
            return PageSearchResponse(mode=tier.name, attempted=attempted, ...)

    # Text tier is unconditional (but still subject to the outer clamp)
    attempted.append("text")
    text_results = build_snippets(source_html, query)
    return PageSearchResponse(mode="text", attempted=attempted, results=text_results, ...)
```

- **Per-tier budget:** `PAGE_SEARCH_TIER_TIMEOUT_SECONDS = 10`.
- **Total endpoint budget:** `FETCH_TOTAL_TIMEOUT_SECONDS = 35`, enforced by an outer `asyncio.timeout()`.
- **Worst-case cascade is longer than the outer budget.** Source fetch may use up to `FETCH_PAGE_TIMEOUT_SECONDS = 15`, and four tiers × 10s = 40s, summing to 55s. The outer clamp will therefore fire mid-cascade in pathological cases.
- **On outer-timeout mid-cascade:** the `asyncio.TimeoutError` from the outer `asyncio.timeout()` is mapped to `TimeoutExceededError` (same mapping `search()` uses today). The client receives a timeout error, not a partial response. Tier 3 is **not** guaranteed to run — if tier 1a burns most of the budget, the tier-3 fallback may be preempted. This is documented behavior; callers who want a text-only fallback under tight budgets should call `page_search` again with a narrower effective scope (future enhancement: `mode_preference` param to skip tier 1/2).
- **Per-tier isolation:** a raised exception inside a tier's execute step is caught and logged at warn level; cascade continues. Only an outer-clamp timeout or a source-fetch failure aborts the whole call.
- **Raw HTML, not rendered text.** The source fetch captures the *raw* HTML DOM (what `page.content()` returns), not the text-extracted form produced by `render_fetch_content`. All detectors need the original DOM — Algolia config is in `<script>` tags that `extract_readable_text` strips; OpenSearch `<link>` lives in `<head>`; the generic form detector needs the full `<form>` tree.

## Network & browser strategy

| Tier | Mechanism | Context |
|------|-----------|---------|
| source fetch | `BrowserManager` ephemeral context | Same as `fetch()`: SSRF guard, challenge handling, page timeout |
| 1a Algolia | `httpx.AsyncClient` direct HTTPS | After `URLSafetyGuard.validate_user_url()` on the Algolia endpoint |
| 1b OpenSearch | `httpx` for descriptor; `BrowserManager` ephemeral for results nav | Both URLs pre-validated |
| 1c Readthedocs | `httpx.AsyncClient` direct HTTPS | After `validate_user_url()` |
| 2 Generic form | `BrowserManager` ephemeral context | SSRF guard attached |
| 3 Text | none | operates on already-fetched source HTML |

Ephemeral contexts (non-persistent) are used throughout. Persistent per-provider profiles exist only for the curated `search()` providers; arbitrary doc sites hit by `page_search` must not pollute them.

SSRF protection: `URLSafetyGuard.validate_user_url(url)` is called before every outbound HTTP call in tiers 1a/1b/1c (for both Algolia/RTD API endpoints and the OpenSearch descriptor URL). Browser-navigated tiers get route-level SSRF via the existing `guard.attach(context)` path.

## Dependencies

- **New explicit dependency:** `httpx` (already transitive via `mcp`, promoted to direct in `pyproject.toml`). Version: whatever the MCP SDK pins, with a sensible lower bound.
- No other new runtime dependencies. `BeautifulSoup` is already present and reused for detectors and snippet extraction.

## Module layout

| File | Change |
|------|--------|
| `src/crawly_mcp/page_search.py` | **New.** `PageSearchService`, tier classes, `_fetch_source_html`, `_run_cascade`. |
| `src/crawly_mcp/models.py` | Add `PageSearchRequest`, `PageSearchResult`, `PageSearchResponse`. |
| `src/crawly_mcp/constants.py` | Add `PageSearchMode` Literal, `PAGE_SEARCH_TIER_TIMEOUT_SECONDS`, `PAGE_SEARCH_SNIPPET_CONTEXT_CHARS`. |
| `src/crawly_mcp/parsing.py` | Add `detect_algolia_config`, `detect_opensearch_href`, `detect_search_form`, `build_snippets`. |
| `src/crawly_mcp/mcp_server.py` | Register `page_search` tool. |
| `src/crawly_mcp/cli.py` | Add `page-search` subcommand. |
| `src/crawly_mcp/service.py` | Expose `extract_readable_text` as module-level function (it already is — just import) for reuse from `page_search.py`. |
| `pyproject.toml` | Bump version `0.1.0` → `0.2.0`; add explicit `httpx` dependency. |
| `CHANGELOG.md` | Add a `## [0.2.0]` section above the existing `## [0.1.0]` stub (leave 0.1.0 intact — it ships empty from the initial release and is not rewritten here). |
| `README.md` | Document the new tool under "Tools" and `page-search` CLI under "Integration Setup". |

**CLI surface:** `crawly-cli page-search --url URL --query TEXT` prints the `PageSearchResponse` as JSON on stdout. Errors go to stderr as structured JSON with non-zero exit, matching the existing `search` / `fetch` subcommands.

## Limits and truncation

- No new env vars. Reuse `MAX_SEARCH_RESULTS=5` for the result cap and `CRAWLY_FETCH_MAX_SIZE` for the response-body cap.
- Per-tier timeout `PAGE_SEARCH_TIER_TIMEOUT_SECONDS = 10`; snippet context window `PAGE_SEARCH_SNIPPET_CONTEXT_CHARS = 240`. Both are constants; add env-var tunability only on demand.
- **Source-HTML fetch is not size-capped by `CRAWLY_FETCH_MAX_SIZE`.** Truncating the source HTML would cause detectors to miss Algolia / OpenSearch config past the cut and would shrink the tier-3 search corpus. The source fetch receives the full raw HTML that `page.content()` returns; Chromium itself bounds the worst case. The `CRAWLY_FETCH_MAX_SIZE` cap applies only to the outgoing `PageSearchResponse`.
- **Response truncation is new logic, not a reuse of `service.truncate_content`.** `truncate_content` caps a single string; the response is a structured object. Add `_truncate_page_search_response(response, limit_bytes)` in `page_search.py`:

  ```
  serialize response to JSON, encode UTF-8; if byte length <= limit_bytes: return.
  Otherwise drop the last element of response.results, re-serialize, repeat until
  byte length <= limit_bytes or results is empty. Set response.truncated = True
  on any drop.
  ```

  Snippet-sized results (each ≤ 240 chars + small URL/title) make triggering this a safety net, not a normal path.

## Error handling

| Condition | Behavior |
|-----------|----------|
| `url` empty or not a string | `InvalidInputError` (Pydantic ValidationError → mapped as in `fetch()`) |
| `url` SSRF-blocked | `URLSafetyError` raised by guard on source fetch, mapped to MCP error |
| `query` empty | `InvalidInputError` |
| Source page navigation fails | `WebSearchError("navigation_failed", ...)` — consistent with `fetch()` |
| Source page is a challenge/CAPTCHA | bubbles up as `ChallengeBlockedError` from the existing challenge handler |
| Tier detect step returns false | tier not in `attempted`, cascade continues silently |
| Tier execute step times out | tier in `attempted`, cascade continues, warn-level log |
| Tier execute step raises | tier in `attempted`, cascade continues, warn-level log with exception summary |
| Outer budget (`FETCH_TOTAL_TIMEOUT_SECONDS`) elapses mid-cascade | raised as `TimeoutExceededError` — caller sees an error, no partial response. Tier 3 is not guaranteed to have run. |
| All tiers exhausted, text tier finds zero matches | valid response, `mode="text"`, `results=[]`, `truncated=False` |
| Response body exceeds `CRAWLY_FETCH_MAX_SIZE` after serialization | `_truncate_page_search_response` drops trailing results, sets `truncated=True` |

## Logging

Follow existing loguru patterns:

- `logger.info("page_search entry url={!r} query={!r}", ...)` at the start.
- `logger.info("page_search tier={} detected={} duration={:.2f}s results={}", ...)` for each tier that ran.
- `logger.info("page_search done mode={} attempted={} results={} duration={:.2f}s", ...)` at the end.
- `logger.warning` for tier-internal failures with the exception type and message.

No DEBUG-level logs by default. No request/response payload dumps in logs.

## Testing

All tests are unit-level with mocked network/browser. Same conventions as the existing suite.

**New/extended test files:**

- `tests/test_parsing.py` — extend with detectors and `build_snippets`.
- `tests/test_page_search.py` — **new.** `PageSearchService` orchestration + tier classes.
- `tests/test_models.py` — extend with `PageSearchRequest` validation and tool-schema introspection.
- `tests/test_cli.py` — extend with `page-search` subcommand.
- `tests/test_constants.py` — extend with new constants.

**Test taxonomy:**

1. **Detector units** — true-positive + true-negative per detector. Fixtures inline unless they grow >~500 lines, then `tests/fixtures/page_search/`.
2. **Snippet builder** — matches at start/middle/end of text, case-insensitive, word-boundary handling, dedup, result cap, snippet length bound.
3. **Tier execution** — one per tier:
   - Algolia: `httpx.MockTransport` with canned hits; verify request body carries the query and result mapping preserves `url`/`title`/`snippet`.
   - OpenSearch: mocked descriptor XML; assert `{searchTerms}` substitution and downstream navigation.
   - Readthedocs: `httpx.MockTransport` with sample API payload; verify slug/version extraction and result mapping.
   - Form: fake source HTML with a known form; assert constructed URL matches `action?name=query`.
   - Text: fake rendered HTML; verify snippet payload and `url=None`, `title` populated.
4. **Cascade orchestration:**
   - First-tier-wins: only winning tier in `attempted`, others not invoked.
   - Tier failure cascades: `attempted` grows, final mode is the succeeding tier.
   - All exhausted → text with zero matches: valid empty response.
   - Per-tier timeout → recorded and skipped.
   - Source fetch failure raises before any tier.
5. **Tool schema** — `create_server().list_tools()` exposes `page_search` with expected input schema.
6. **SSRF** — private-IP URL on the source rejected upfront; Algolia/RTD URL pointing at a blocked host rejected via `validate_user_url` before HTTP.
7. **CLI** — `page-search --url ... --query ...` prints JSON-shaped response; invalid input prints structured error.

**Reused fakes:**

- `_StrictFakeContext` pattern from `test_browser.py` for browser-navigating tiers.
- New local factories `build_mock_algolia_transport(hits)` / `build_mock_readthedocs_transport(blocks)` using `httpx.MockTransport`.

**Coverage floor:** every detector gets ≥1 TP + ≥1 TN; every tier execute step gets happy-path + one failure path; cascade gets all four orchestration scenarios above.

## Files touched — summary

```
new:     docs/superpowers/specs/2026-04-23-page-search-design.md (this file)
new:     src/crawly_mcp/page_search.py
new:     tests/test_page_search.py
modify:  pyproject.toml (version bump, httpx dep)
modify:  src/crawly_mcp/constants.py
modify:  src/crawly_mcp/models.py
modify:  src/crawly_mcp/parsing.py
modify:  src/crawly_mcp/mcp_server.py
modify:  src/crawly_mcp/cli.py
modify:  tests/test_parsing.py
modify:  tests/test_models.py
modify:  tests/test_cli.py
modify:  tests/test_constants.py
modify:  CHANGELOG.md (new [0.2.0] section)
modify:  README.md (tool + CLI docs)
```

No changes to `browser.py`, `service.py` (aside from possibly a small import-surface tweak), `security.py`, `challenge.py`, `_logging.py`, Dockerfile, or CI workflows.

## Open questions

None blocking; the following are YAGNI'd for v1 and can be addressed later if demand appears:

- Configurable per-tier timeout via env var.
- Caching of OpenSearch descriptors and Algolia configs per-hostname.
- Multi-URL request shape.
- Site-specific result extractors for tier 1b/2 (currently generic `build_snippets` on the results page).
- Exposing the individual tier functions as separate MCP tools for advanced callers.
