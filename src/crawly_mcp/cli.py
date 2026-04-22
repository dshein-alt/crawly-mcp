from __future__ import annotations

import argparse
import asyncio
import json
import sys

from crawly_mcp._logging import configure_logging
from crawly_mcp.browser import BrowserManager
from crawly_mcp.errors import WebSearchError
from crawly_mcp.service import WebSearchService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Browser-backed external web search CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Run a search query and print result URLs as JSON.")
    search_parser.add_argument("--provider", default=None, help="Search provider: duckduckgo, google, or yandex.")
    search_parser.add_argument("--context", required=True, help="Search query text.")

    fetch_parser = subparsers.add_parser("fetch", help="Fetch URLs and print rendered HTML as JSON.")
    fetch_parser.add_argument("urls", nargs="+", help="Up to 5 URLs to fetch.")

    return parser


async def run_search_command(provider: str | None, context: str) -> int:
    browser_manager = BrowserManager()
    service = WebSearchService(browser_manager)
    try:
        result = await service.search(provider=provider, context=context)
    finally:
        await browser_manager.close()
    print(result.model_dump_json(indent=2))
    return 0


async def run_fetch_command(urls: list[str]) -> int:
    browser_manager = BrowserManager()
    service = WebSearchService(browser_manager)
    try:
        result = await service.fetch(urls=urls)
    finally:
        await browser_manager.close()
    print(result.model_dump_json(indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging()

    try:
        if args.command == "search":
            return asyncio.run(run_search_command(args.provider, args.context))
        if args.command == "fetch":
            return asyncio.run(run_fetch_command(args.urls))
    except WebSearchError as exc:
        print(json.dumps({"error": exc.to_payload()}, indent=2), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    parser.error(f"unknown command: {args.command}")
    return 2
