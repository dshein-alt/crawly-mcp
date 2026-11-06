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


async def _check_fetch(session: ClientSession, fetch_url: str) -> None:
    result = await session.call_tool("fetch", {"urls": [fetch_url]})
    if result.isError:
        raise RuntimeError(f"fetch smoke test failed: {result}")
    payload = _extract_structured_content(result)
    if fetch_url not in payload.get("pages", {}):
        raise RuntimeError(f"missing fetched page for {fetch_url}")
    if payload.get("status") != "ok":
        raise RuntimeError(f"expected status ok, got {payload.get('status')}")


async def _check_private_url(session: ClientSession, private_url: str) -> None:
    """Per-URL isolation: a refused URL is one entry in `errors`.

    It used to abort the whole batch as a tool-level error, which voided the
    documented partial-success contract.
    """
    result = await session.call_tool("fetch", {"urls": [private_url]})
    if result.isError:
        raise RuntimeError(
            "a refused URL must be reported per-URL, not as a tool-level "
            f"error: {result}"
        )
    payload = _extract_structured_content(result)
    if payload.get("pages"):
        raise RuntimeError("private URL must not return content")
    if not payload.get("errors"):
        raise RuntimeError("private URL must be listed in errors")
    if payload.get("status") != "unsafe_url":
        raise RuntimeError(f"expected unsafe_url status, got {payload.get('status')}")


async def _check_tool_surface(session: ClientSession) -> None:
    tools = await session.list_tools()
    tool_names = sorted(tool.name for tool in tools.tools)
    if tool_names != ["fetch", "page_search", "providers", "search"]:
        raise RuntimeError(f"unexpected tools list: {tool_names}")

    providers_result = await session.call_tool("providers", {})
    if providers_result.isError:
        raise RuntimeError(f"providers smoke test failed: {providers_result}")
    providers = _extract_structured_content(providers_result)
    if not providers.get("providers"):
        raise RuntimeError("providers report listed no providers")


async def _run(url: str, fetch_url: str, private_url: str | None) -> None:
    async with streamable_http_client(url) as (read_stream, write_stream, _):
        session = ClientSession(read_stream, write_stream)
        async with session:
            await session.initialize()
            await _check_tool_surface(session)
            await _check_fetch(session, fetch_url)
            if private_url is not None:
                await _check_private_url(session, private_url)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run an HTTP MCP smoke test against crawly."
    )
    parser.add_argument(
        "--url",
        required=True,
        help="HTTP MCP endpoint URL, e.g. http://127.0.0.1:8000/mcp",
    )
    parser.add_argument(
        "--fetch-url",
        default="https://example.com",
        help="Public URL to fetch successfully.",
    )
    parser.add_argument(
        "--private-url",
        default=None,
        help="Private/container-network URL that the SSRF guard must refuse per-URL.",
    )
    args = parser.parse_args()

    asyncio.run(_run(args.url, args.fetch_url, args.private_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
