from __future__ import annotations

from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from crawly_mcp.browser import BrowserManager
from crawly_mcp.constants import DEFAULT_MCP_HOST, DEFAULT_MCP_PORT, FetchContentFormat
from crawly_mcp.errors import WebSearchError
from crawly_mcp.models import FetchResponse, SearchResponse
from crawly_mcp.service import WebSearchService
from crawly_mcp.version import get_package_version


def create_server(
    *, host: str = DEFAULT_MCP_HOST, port: int = DEFAULT_MCP_PORT
) -> FastMCP:
    browser_manager = BrowserManager()
    service = WebSearchService(browser_manager)

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
            "Two tools are available: `search` for top result URLs and `fetch` for "
            "browser-rendered page content. `fetch` accepts `content_format=html|text` "
            "and returns content in that format. The `context` field on `search` is "
            "the search query text."
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
    async def search(provider: str | None = None, *, context: str) -> SearchResponse:
        try:
            return await service.search(provider=provider, context=context)
        except WebSearchError as exc:
            raise exc.to_mcp_error() from exc

    @server.tool(
        name="fetch",
        description=(
            "Fetch up to 5 URLs and return final browser-rendered page content per URL. "
            'Use `content_format="html"` for raw HTML or `content_format="text"` '
            "for extracted readable text."
        ),
    )
    async def fetch(
        urls: list[str],
        content_format: FetchContentFormat = "html",
    ) -> FetchResponse:
        try:
            return await service.fetch(urls=urls, content_format=content_format)
        except WebSearchError as exc:
            raise exc.to_mcp_error() from exc

    return server
