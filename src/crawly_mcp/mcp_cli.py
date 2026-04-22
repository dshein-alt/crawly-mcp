from __future__ import annotations

import argparse
import os

from crawly_mcp.constants import (
    CRAWLY_HOST_ENV_VAR,
    CRAWLY_PORT_ENV_VAR,
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORT,
)
from crawly_mcp.mcp_server import create_server


def _default_port() -> int:
    value = os.environ.get(CRAWLY_PORT_ENV_VAR)
    if value is None:
        return DEFAULT_MCP_PORT
    return int(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the crawly MCP server.")
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio", "sse", "streamable-http"),
        help="MCP transport to expose.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get(CRAWLY_HOST_ENV_VAR, DEFAULT_MCP_HOST),
        help="Host for HTTP-based transports.",
    )
    parser.add_argument(
        "--port",
        default=_default_port(),
        type=int,
        help="Port for HTTP-based transports.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    server = create_server(host=args.host, port=args.port)
    server.run(transport=args.transport)
    return 0
