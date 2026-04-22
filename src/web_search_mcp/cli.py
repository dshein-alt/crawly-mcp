from __future__ import annotations

import argparse
import asyncio
import json
import sys

from web_search_mcp.browser import BrowserManager
from web_search_mcp.errors import WebSearchError
from web_search_mcp.mcp_server import create_server
from web_search_mcp.service import WebSearchService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Browser-backed external web search for local LLMs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Run a search query and print result URLs as JSON.")
    search_parser.add_argument("--provider", default=None, help="Search provider: duckduckgo, google, or yandex.")
    search_parser.add_argument("--context", required=True, help="Search query text.")

    fetch_parser = subparsers.add_parser("fetch", help="Fetch URLs and print rendered HTML as JSON.")
    fetch_parser.add_argument("urls", nargs="+", help="Up to 5 URLs to fetch.")

    serve_parser = subparsers.add_parser("serve-mcp", help="Run the MCP server.")
    serve_parser.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio", "sse", "streamable-http"),
        help="MCP transport to expose.",
    )
    serve_parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP-based transports.")
    serve_parser.add_argument("--port", default=8000, type=int, help="Port for HTTP-based transports.")

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

    if args.command == "serve-mcp":
        server = create_server(host=args.host, port=args.port)
        server.run(transport=args.transport)
        return 0

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
