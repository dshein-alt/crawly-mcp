# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.5] - 2026-11-06

### Fixed

- Give a search wall that says it will resolve itself a bounded chance to finish, so a provider mid-check is no longer reported as a refusal, and report the page the check actually ended on.
- Report a wall that asks a human to click at once, naming the wall the provider ended on, and spend no window waiting on it.

## [0.4.4] - 2026-11-06

### Fixed

- Collect more Google click-through result links than the return cap and resolve them a few at a time, so links the provider throttles no longer shrink the answer below what the page offered.
- Retry a throttled click-through result link once, following the fresh redirect token the throttle page hands back.
- Report how many click-through result links resolved when the provider throttled the rest, instead of returning a short URL list with no explanation.

## [0.4.3] - 2026-11-06

### Fixed

- Resolve Google's `/goto?url=` click-through result redirects to their real destination URLs.
- Honor zero-result statements on site-search pages before extracting OpenSearch or form results.

## [0.4.2] - 2026-11-06

### Fixed

- Expose provider order, exclusion, and attempt-limit settings in the SearXNG Compose example and document their `.env` usage.

## [0.4.1] - 2026-11-06

### Fixed

- Pin the release fingerprint check to Ubuntu 24.04 so Patchright can install Chromium after `ubuntu-latest` moved to 26.04.
- Upgrade CI actions to releases that run natively on Node.js 24.

## [0.4.0] - 2026-11-06

Make every tool result say what happened and what to do next, so a caller can tell "the web has nothing on this" apart from "this provider could not answer" without a human pushing it to try another provider.

### Added

- `status`, `next_action`, and `detail` on every `search`, `fetch`, and `page_search` result. Statuses: `ok`, `no_results`, `blocked`, `unavailable`, `timeout`, `navigation_failed`, `unsafe_url`, `invalid_input`. Actions: `none`, `try_other_provider`, `refine_query`, `fetch_directly`, `retry_later`, `report_to_user`.
- Provider cascade on `search`: one call walks the provider priority order until one answers, reporting `provider` (the winner), `attempted` (per-provider status, page kind, final URL, result count), `available_providers`, and `suggested_providers`. Cascading stops on an honest `no_results`, which is an answer rather than a failure.
- `providers` tool reporting which providers are usable, which are excluded by configuration or missing settings, the cascade order, the per-call provider cap, and how each provider last behaved in this process.
- `CRAWLY_SEARCH_PROVIDER_ORDER` (priority), `CRAWLY_SEARCH_PROVIDER_EXCLUDE` (never try, even on request), and `CRAWLY_SEARCH_MAX_PROVIDERS` (per-call cap; `1` disables cascading).
- `providers` CLI subcommand, and `--content-format` on the `fetch` subcommand.
- `CRAWLY_SSRF_ALLOW` for private-network deployments: exact hostnames allowed to resolve to non-public addresses, so an internal wiki, search instance, or fixture site can be fetched. Scheme and credential checks still apply.
- `examples/searxng-compose/docker-compose.test.yml` and `scripts/e2e_stack.py`: a behavioural suite run against a local SearXNG stack plus a fixture site, asserting the blocked / empty / site-search contract deterministically. Live provider probes are opt-in and never fail the suite.

### Changed

- **Breaking:** `search` returns an envelope, not a bare `{"urls": [...]}`. `urls` stays the top-level key.
- **Breaking:** `search`'s `provider` parameter defaults to `auto` (use the configured priority order) instead of `duckduckgo`. Omitting the parameter no longer means an explicit request for DuckDuckGo, so excluding DuckDuckGo no longer breaks the default call path.
- **Breaking:** recoverable outcomes are normal results, not MCP protocol errors. A blocked, unavailable, timed-out, or unreachable provider returns a status; protocol errors are reserved for malformed arguments.
- **Breaking:** `fetch` handles each URL on its own. A refused, unsafe, DNS-failing, timed-out, or challenge-walled URL lands in `errors` with its own reason and `next_action`, and the other URLs still return; previously one such URL aborted the batch and discarded the pages that had succeeded. `FetchResponse` also carries `succeeded` and `failed` counts.
- **Breaking:** `page_search`'s `attempted` is a list of per-tier entries (`tier`, `outcome`, `detail`) with outcomes `ok`, `empty`, `failed`, `unavailable`, replacing the list of tier names.
- A zero-hit results page can no longer yield result URLs. Result links are harvested only from result containers, and a page that states it found nothing is reported as `no_results` whatever its DOM contains. Previously DuckDuckGo's zero-hit page chrome was returned as five plausible, unrelated URLs.
- A page that never reached a results view (DuckDuckGo's "redirecting to the non-JavaScript site" interstitial, Google's redirect interstitial) is reported as `unavailable` and cascades, instead of looking like an empty web.
- Google result extraction follows the current SERP markup (`div.yuRUbf a`, `a.zReHs`), replacing selectors that matched nothing.
- `page_search` matches multi-word queries as tokens ranked by coverage, preferring an exact phrase when present. Previously every multi-word query was matched as one literal string and returned nothing.
- Site-search harvesting uses the page's results container only; a client-side search page that rendered nothing no longer reports its navigation bar as the top five hits.
- `search` skips a provider that has failed repeatedly, and reports the skip.
- Tool descriptions and server instructions state the status contract, including that an empty result with a non-`no_results` status is a tool failure and not evidence of an empty web.

### Fixed
- Preserve genuine zero-result messages while excluding query echoes from titles and labelled body text, regardless of case.
- Classify DNS resolution failures as `navigation_failed`, so a transient resolver failure keeps cascading.
- Return structured `page_search` outcomes for source navigation failures, timeouts, and safety refusals.
- Keep short documentation about challenges and CAPTCHAs searchable while rejecting actual interstitials.
- Continue provider cascading when SearXNG returns null results, malformed URLs, or malformed engine-failure metadata.
- Continue provider cascading when browser context or tab creation fails, and close unused ephemeral contexts.

## [0.3.1] - 2026-05-12

### Changed

- The `examples/searxng-compose/` compose example no longer maps SearXNG to a host port; the instance is reachable only on the compose network as `http://searxng:8080/`. `SEARXNG_HOST_PORT` is removed from `.env.example`, and `server.base_url` in the bundled `settings.yml` now reflects the compose-network address.

### Fixed

- `provider="searxng"` no longer fails with `httpx.ClientCloseError("Cannot send a request, as the client has been closed.")` after the first MCP session ends. The shared `httpx.AsyncClient` owned by `WebSearchService` is now built lazily and rebuilt after `aclose()`, so the FastMCP streamable-http transport's per-session lifespan teardown doesn't leave a dead client behind for subsequent sessions.

## [0.3.0] - 2026-05-12

Add SearXNG as an opt-in fourth search provider for self-hosted instances.

### Added

- `searxng` provider value on the `search` tool. Routes the query through a single SearXNG instance via its JSON API (`?format=json`) over `httpx`. The instance URL is supplied via the `CRAWLY_SEARXNG_URL` env var; without it the call returns an `invalid_input` error.
- The `search` MCP tool's `provider` parameter is now advertised in `tools/list` as a non-nullable enum with an explicit `default` value (still `duckduckgo`). Clients that previously sent `{"provider": null}` will now receive a schema validation error; pass the desired provider string or omit the field.

### Changed

- `searxng` is **not** the default — `duckduckgo` remains the default. The provider exists for users who run their own SearXNG; public instances on `searx.space` actively block automated clients (botdetection middleware returns 429 / redirects / empty results), so an aspirational default would be a slow no-op. There is no instance registry and no automatic cross-provider fallback; failures from the configured instance surface to the caller.

### Fixed

- Parse OpenSearch descriptors with hardened XML handling.

## [0.2.1] - 2026-04-30

Surface complete tool argument schemas to MCP clients.

### Changed

- Annotate every `search`, `fetch`, and `page_search` parameter with a description and explicit constraints (provider enum, URL list bounds, non-empty strings) so MCP clients (e.g. Continuw) render proper argument help instead of `(unknown) - No description`.
- Type the `search` tool's `provider` argument as the `SearchProvider` literal so `tools/list` advertises the allowed values (`duckduckgo`, `google`, `yandex`).

### Fixed

- Test coverage now asserts that `tools/list` exposes the provider enum and per-argument descriptions, preventing regressions in the schema surfaced to MCP clients.

## [0.2.0] - 2026-04-24

Add initial `page_search` tool implementation.

### Added

- `page_search(url, query)` MCP tool: three-tier cascade over Algolia DocSearch, OpenSearch descriptor, Readthedocs API, generic GET forms, and find-in-page text fallback. Returns a `mode` discriminator, ordered `attempted` list, and up to 5 result snippets with optional result URLs.
- `crawly-cli page-search --url URL --query TEXT` subcommand mirroring the MCP tool.

### Changed

- Promote `httpx` from a transitive dependency to an explicit project dependency (used by `page_search` for Algolia and Readthedocs API calls).
- Make the bundled web-search skill and Continue prompt default to silent tool use and concise prose answers, reserving JSON-style extraction for larger multi-page runs.
- Expand MCP server and tool metadata so clients prefer silent tool use, prose answers, `page_search` on known sites, and `fetch(..., content_format="text")` for readable follow-up fetches.

### Fixed

- Preserve `results_url` for `page_search` results returned from OpenSearch and generic form tiers.
- Ignore non-query controls when detecting `page_search` search forms and preserve the original parameter-name casing.
- Wait briefly for client-side search pages to populate results before `page_search` snapshots the HTML.
- Extract linked results from OpenSearch and generic form search result pages before falling back to full-query text snippets.
- Add the missing MIT `LICENSE` file and document the project license in package metadata and README.
- Copy `LICENSE` into the Docker builder stage before `uv sync` so container builds satisfy packaged license-file metadata.

## [0.1.1] - 2026-04-23

Regular updates and fixes.

### Added

### Changed
- Run the release fingerprint canary against bundled Chromium and stop treating CI software WebGL renderers as a failure.

### Fixed
- Docker build CI pipeline

## [0.1.0] - 2026-04-23

First viable release.

### Added

### Changed

### Fixed
