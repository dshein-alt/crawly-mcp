from __future__ import annotations

from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from crawly_mcp.browser import BrowserManager
from crawly_mcp.constants import DEFAULT_MCP_HOST, DEFAULT_MCP_PORT
from crawly_mcp.errors import WebSearchError
from crawly_mcp.models import FetchResponse, SearchResponse
from crawly_mcp.service import WebSearchService


def create_server(*, host: str = DEFAULT_MCP_HOST, port: int = DEFAULT_MCP_PORT) -> FastMCP:
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
            "browser-rendered HTML. The `context` field on `search` is the search query text."
        ),
        host=host,
        port=port,
        lifespan=lifespan,
    )

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
        description="Fetch up to 5 URLs and return final browser-rendered HTML per URL.",
    )
    async def fetch(urls: list[str]) -> FetchResponse:
        try:
            return await service.fetch(urls=urls)
        except WebSearchError as exc:
            raise exc.to_mcp_error() from exc

    return server
