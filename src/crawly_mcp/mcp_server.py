from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from crawly_mcp.browser import BrowserManager
from crawly_mcp.constants import (
    ALLOWED_PROVIDERS,
    DEFAULT_FETCH_CONTENT_FORMAT,
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORT,
    DEFAULT_PROVIDER,
    MAX_FETCH_URLS,
    FetchContentFormat,
    SearchProvider,
)
from crawly_mcp.errors import WebSearchError
from crawly_mcp.models import FetchResponse, PageSearchResponse, SearchResponse
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
            await browser_manager.close()

    server = FastMCP(
        name="crawly",
        instructions=(
            "Three tools are available: `search` for broad web result URLs, "
            "`page_search(url, query)` for bounded search within a known site or docs "
            "entrypoint, and `fetch` for browser-rendered page content. Use tools "
            "silently: do not narrate tool calls or internal reasoning. Prefer "
            "`page_search(url, query)` before broad `search` when the site is already "
            'known. Prefer `fetch(..., content_format="text")` when reading docs or '
            "articles for a final prose answer. Final answers should be concise prose "
            "unless the user explicitly asks for JSON. Do not claim a timeout, fetch, "
            "or search failure unless the tool actually returned one."
        ),
        host=host,
        port=port,
        lifespan=lifespan,
    )
    server._mcp_server.version = get_package_version()

    @server.tool(
        name="search",
        description="Run a web search in a real browser. `context` is the search query text.",
    )
    async def search(
        provider: Annotated[
            SearchProvider | None,
            Field(
                description=(
                    "Search engine to query. One of: "
                    + ", ".join(repr(p) for p in ALLOWED_PROVIDERS)
                    + f". Defaults to {DEFAULT_PROVIDER!r} when omitted or null."
                ),
            ),
        ] = None,
        *,
        context: Annotated[
            str,
            Field(
                description="Search query text. Must be a non-empty string.",
                min_length=1,
            ),
        ],
    ) -> SearchResponse:
        try:
            return await service.search(provider=provider, context=context)
        except WebSearchError as exc:
            raise exc.to_mcp_error() from exc

    @server.tool(
        name="fetch",
        description=(
            "Fetch up to 5 URLs and return final browser-rendered page content per URL. "
            'Use `content_format="html"` for raw HTML or `content_format="text"` '
            'for extracted readable text. Prefer `content_format="text"` for docs, '
            "articles, and final prose answers."
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
            "find-in-page text fallback. The response includes `mode`, ordered "
            "`attempted`, optional `results_url`, up to 5 `results`, and `truncated`. "
            "If a result entry includes `url`, it is a real result URL to fetch next. "
            "If result URLs are absent, the tool only found text snippets on the "
            "source or search-results page."
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
