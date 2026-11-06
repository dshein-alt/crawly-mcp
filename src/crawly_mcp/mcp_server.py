from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from crawly_mcp.browser import BrowserManager
from crawly_mcp.constants import (
    DEFAULT_FETCH_CONTENT_FORMAT,
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORT,
    DEFAULT_PROVIDER_CHOICE,
    MAX_FETCH_URLS,
    FetchContentFormat,
    SearchProviderChoice,
)
from crawly_mcp.errors import WebSearchError
from crawly_mcp.models import (
    FetchResponse,
    PageSearchResponse,
    ProvidersResponse,
    SearchResponse,
)
from crawly_mcp.page_search import PageSearchService
from crawly_mcp.service import WebSearchService
from crawly_mcp.version import get_package_version


def create_server(
    *, host: str = DEFAULT_MCP_HOST, port: int = DEFAULT_MCP_PORT
) -> FastMCP:
    browser_manager = BrowserManager()
    service = WebSearchService(browser_manager)
    page_search_service = PageSearchService(browser_manager)

    @asynccontextmanager
    async def lifespan(_: FastMCP):
        await browser_manager.start()
        try:
            yield
        finally:
            await service.aclose()
            await browser_manager.close()

    server = FastMCP(
        name="crawly",
        instructions=(
            "Four tools are available: `search` for broad web result URLs, "
            "`page_search(url, query)` for bounded search within a known site or docs "
            "entrypoint, `fetch` for browser-rendered page content, and `providers` "
            "for what this deployment can search with. Use tools silently: do not "
            "narrate tool calls or internal reasoning. "
            "Every result carries `status`, `next_action`, and `detail`; read them "
            "before drawing a conclusion. "
            '`status="no_results"` means a provider searched and genuinely found '
            "nothing, so change the query. "
            '`status="blocked"`, `"unavailable"`, `"timeout"`, or '
            '`"navigation_failed"` means providers could not answer: that is NOT '
            "evidence that the topic is empty, so call again with a "
            "`suggested_providers` entry, use `providers` to see what is usable, or "
            "report the failure accurately. Never tell the user a search found "
            "nothing when the status says a provider failed. "
            "`search` already cascades providers on its own, so do not re-run the "
            "same query unchanged. "
            "Prefer `page_search(url, query)` before broad `search` when the site is "
            'already known. Prefer `fetch(..., content_format="text")` when reading '
            "docs or articles for a final prose answer; `fetch` reports per-URL "
            "errors and partial success is normal. Final answers should be concise "
            "prose unless the user explicitly asks for JSON. Do not claim a timeout, "
            "fetch, or search failure unless a status field actually reported one."
        ),
        host=host,
        port=port,
        lifespan=lifespan,
    )
    server._mcp_server.version = get_package_version()

    @server.tool(
        name="search",
        description=(
            "Run a web search in a real browser. `context` is the search query text. "
            "Tries providers in cascade order until one answers and reports each one "
            "in `attempted`; `provider` names the one that produced `urls`. Read "
            "`status` before concluding anything from `urls`: `ok` has results, "
            "`no_results` means a provider searched and found nothing (refine the "
            "query), and `blocked`/`unavailable`/`timeout`/`navigation_failed` mean "
            "providers could not answer, which is not evidence that the topic is "
            "empty - retry with a `suggested_providers` entry or report the failure. "
            "An empty `urls` with a non-`no_results` status is a tool failure, never "
            "an empty web."
        ),
    )
    async def search(
        # NOTE: typed as SearchProviderChoice (not ... | None) so the MCP JSON
        # schema advertises a flat enum with an explicit `default` rather than
        # an `anyOf` branching on `null`. `auto` is a real enum value, so
        # omitting the parameter and asking for `auto` mean the same thing:
        # start from the operator's configured priority order.
        provider: Annotated[
            SearchProviderChoice,
            Field(
                default=DEFAULT_PROVIDER_CHOICE,
                description=(
                    "`auto` (the default) starts from the operator's configured "
                    "provider priority order and cascades through the providers "
                    "available in this deployment until one answers. Name a "
                    "provider (`duckduckgo`, `google`, `yandex`, `searxng`) to "
                    "start there instead; it is still tried first and the cascade "
                    "continues from it. A provider the operator excluded, or one "
                    "absent from the configured priority order, is refused as "
                    "invalid input. "
                    "`searxng` is only usable when the server has "
                    "`CRAWLY_SEARXNG_URL` set (a self-hosted SearXNG with JSON "
                    "output enabled; public instances block automated clients) and "
                    "is reported unavailable otherwise. Call `providers` to see "
                    "what is usable here and how each one last behaved."
                ),
            ),
        ] = DEFAULT_PROVIDER_CHOICE,
        *,
        context: Annotated[
            str,
            Field(
                description=(
                    "Search query text. Must be a non-empty string. Use plain, "
                    "plausible search-engine words: a `no_results` status means the "
                    "query is the problem, so reword it rather than repeating it."
                ),
                min_length=1,
            ),
        ],
    ) -> SearchResponse:
        try:
            return await service.search(provider=provider, context=context)
        except WebSearchError as exc:
            raise exc.to_mcp_error() from exc

    @server.tool(
        name="providers",
        description=(
            "Report which search providers this deployment can actually use, which "
            "ones are excluded by operator configuration or missing their settings, "
            "and how each one last behaved in this process (successes, failures, "
            "repeated-failure skips). Call it before blaming a search failure on the "
            "query, and to decide what is worth retrying. `cascade_order` is the "
            "priority order `search` walks and `max_providers` how many it may try."
        ),
    )
    async def providers() -> ProvidersResponse:
        return service.providers()

    @server.tool(
        name="fetch",
        description=(
            "Fetch up to 5 URLs and return final browser-rendered page content per URL. "
            'Use `content_format="html"` for raw HTML or `content_format="text"` '
            'for extracted readable text. Prefer `content_format="text"` for docs, '
            "articles, and final prose answers. Each URL is handled on its own: a "
            "refused, unsafe, unreachable, timed-out, or challenge-walled URL lands "
            "in `errors` with its own reason and `next_action`, and the other URLs "
            "still return. Partial success is normal, so read `errors` rather than "
            "assuming the call failed; `status` is `ok` whenever at least one URL "
            "returned content."
        ),
    )
    async def fetch(
        urls: Annotated[
            list[str],
            Field(
                description=(
                    f"List of 1 to {MAX_FETCH_URLS} absolute http(s) URLs to fetch. "
                    "Each URL is loaded in a real browser and returned as one entry "
                    "in the response."
                ),
                min_length=1,
                max_length=MAX_FETCH_URLS,
            ),
        ],
        content_format: Annotated[
            FetchContentFormat,
            Field(
                description=(
                    'Output format for each fetched page. "html" returns the raw '
                    'rendered HTML; "text" returns extracted readable text and is '
                    "preferred for docs, articles, and final prose answers."
                ),
            ),
        ] = DEFAULT_FETCH_CONTENT_FORMAT,
    ) -> FetchResponse:
        try:
            return await service.fetch(urls=urls, content_format=content_format)
        except WebSearchError as exc:
            raise exc.to_mcp_error() from exc

    @server.tool(
        name="page_search",
        description=(
            "Search for content on a single page or docs site using a three-tier cascade: "
            "known site-search facilities (Algolia DocSearch, OpenSearch, "
            "Readthedocs) first, then generic GET form detection, then "
            "find-in-page text fallback. The response includes `status`, "
            "`next_action`, `detail`, `mode`, one entry per tier in `attempted` "
            "(`ok`, `empty`, `failed`, or `unavailable`), optional `results_url`, up "
            "to 5 `results`, and `truncated`. If a result entry includes `url`, it is "
            "a real result URL to fetch next. If result URLs are absent, the tool "
            "only found text snippets on the source or search-results page. "
            "`status=no_results` means the site has nothing matching the query "
            "(`refine_query`); `status=unavailable` means the site has a search "
            "facility and it failed, so fetch the site directly instead of "
            "concluding the topic is absent."
        ),
    )
    async def page_search(
        url: Annotated[
            str,
            Field(
                description=(
                    "Absolute URL of the page or docs site to search within. "
                    "The tool starts from this URL and applies the three-tier "
                    "cascade described above."
                ),
                min_length=1,
            ),
        ],
        query: Annotated[
            str,
            Field(
                description="Free-text query to look for on the target site.",
                min_length=1,
            ),
        ],
    ) -> PageSearchResponse:
        try:
            return await page_search_service.search(url=url, query=query)
        except WebSearchError as exc:
            raise exc.to_mcp_error() from exc

    return server
