from __future__ import annotations

import argparse
import asyncio
import json
import sys

from crawly_mcp._logging import configure_logging
from crawly_mcp.browser import BrowserManager
from crawly_mcp.constants import (
    DEFAULT_FETCH_CONTENT_FORMAT,
    FetchContentFormat,
)
from crawly_mcp.errors import WebSearchError
from crawly_mcp.page_search import PageSearchService
from crawly_mcp.service import WebSearchService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Browser-backed external web search CLI."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser(
        "search",
        help="Run a search query and print the result envelope as JSON.",
    )
    search_parser.add_argument(
        "--provider",
        default=None,
        help=(
            "Provider to start with; the call cascades to the other available "
            "providers until one answers. Omit to start from the configured "
            "cascade order."
        ),
    )
    search_parser.add_argument("--context", required=True, help="Search query text.")

    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch URLs and print rendered content plus per-URL errors as JSON.",
    )
    fetch_parser.add_argument("urls", nargs="+", help="Up to 5 URLs to fetch.")
    fetch_parser.add_argument(
        "--content-format",
        choices=["html", "text"],
        default=DEFAULT_FETCH_CONTENT_FORMAT,
        help="Rendered output format (default: html).",
    )

    page_search_parser = subparsers.add_parser(
        "page-search",
        help="Search for content on a single page via the cascade.",
    )
    page_search_parser.add_argument("--url", required=True, help="Page URL to search.")
    page_search_parser.add_argument("--query", required=True, help="Search query text.")

    subparsers.add_parser(
        "providers",
        help="Print which providers are usable here and how each last behaved.",
    )

    return parser


async def run_search_command(provider: str | None, context: str) -> int:
    browser_manager = BrowserManager()
    service = WebSearchService(browser_manager)
    try:
        result = await service.search(provider=provider, context=context)
    finally:
        await service.aclose()
        await browser_manager.close()
    print(result.model_dump_json(indent=2))
    return 0


async def run_fetch_command(
    urls: list[str], content_format: FetchContentFormat = DEFAULT_FETCH_CONTENT_FORMAT
) -> int:
    browser_manager = BrowserManager()
    service = WebSearchService(browser_manager)
    try:
        result = await service.fetch(urls=urls, content_format=content_format)
    finally:
        await service.aclose()
        await browser_manager.close()
    print(result.model_dump_json(indent=2))
    return 0


async def _run_page_search(url: str, query: str) -> int:
    browser_manager = BrowserManager()
    try:
        service = PageSearchService(browser_manager)
        response = await service.search(url=url, query=query)
        print(response.model_dump_json())
    finally:
        await browser_manager.close()
    return 0


async def _run_providers() -> int:
    browser_manager = BrowserManager()
    service = WebSearchService(browser_manager)
    try:
        await browser_manager.start()
        print(service.providers().model_dump_json(indent=2))
    finally:
        await service.aclose()
        await browser_manager.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging()

    handlers = {
        "search": lambda: run_search_command(args.provider, args.context),
        "fetch": lambda: run_fetch_command(args.urls, args.content_format),
        "page-search": lambda: _run_page_search(args.url, args.query),
        "providers": _run_providers,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
        return 2

    try:
        return asyncio.run(handler())
    except WebSearchError as exc:
        print(json.dumps({"error": exc.to_payload()}, indent=2), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
