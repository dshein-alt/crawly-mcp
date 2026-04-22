from __future__ import annotations

import argparse
import asyncio
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _extract_structured_content(result: Any) -> dict[str, Any]:
    content = getattr(result, "structuredContent", None)
    if isinstance(content, dict):
        return content
    raise RuntimeError("expected structured content from MCP tool result")


async def _run(url: str, fetch_url: str, private_url: str | None) -> None:
    async with streamable_http_client(url) as (read_stream, write_stream, _):
        session = ClientSession(read_stream, write_stream)
        async with session:
            await session.initialize()

            tools = await session.list_tools()
            tool_names = sorted(tool.name for tool in tools.tools)
            if tool_names != ["fetch", "search"]:
                raise RuntimeError(f"unexpected tools list: {tool_names}")

            fetch_result = await session.call_tool("fetch", {"urls": [fetch_url]})
            if fetch_result.isError:
                raise RuntimeError(f"fetch smoke test failed: {fetch_result}")

            payload = _extract_structured_content(fetch_result)
            pages = payload.get("pages", {})
            if fetch_url not in pages:
                raise RuntimeError(f"missing fetched page for {fetch_url}")

            if private_url is not None:
                private_result = await session.call_tool("fetch", {"urls": [private_url]})
                if not private_result.isError:
                    raise RuntimeError("expected private URL fetch to fail")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run an HTTP MCP smoke test against crawly.")
    parser.add_argument("--url", required=True, help="HTTP MCP endpoint URL, e.g. http://127.0.0.1:8000/mcp")
    parser.add_argument("--fetch-url", default="https://example.com", help="Public URL to fetch successfully.")
    parser.add_argument(
        "--private-url",
        default=None,
        help="Private/container-network URL that should be rejected by the SSRF guard.",
    )
    args = parser.parse_args()

    asyncio.run(_run(args.url, args.fetch_url, args.private_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
