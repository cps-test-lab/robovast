"""Minimal script to smoke-test the robovast-mcp server.

Connects via stdio (default) or HTTP/SSE, lists all tools, calls each one,
and prints the results.

Usage::

    # stdio – server is started automatically
    python examples/test_mcp_server.py

    # HTTP/SSE – start server first: robovast-mcp --transport sse
    python examples/test_mcp_server.py --transport sse
    python examples/test_mcp_server.py --transport sse --url http://localhost:9000/sse
"""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

DEFAULT_SSE_URL = "http://localhost:8000/sse"
DEFAULT_HTTP_URL = "http://localhost:8000/mcp"


@asynccontextmanager
async def _session(transport: str, url: str) -> AsyncGenerator[ClientSession, None]:
    if transport == "stdio":
        server_params = StdioServerParameters(command="robovast-mcp", args=[])
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    elif transport == "sse":
        async with sse_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
    else:  # streamable-http
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def main(transport: str, url: str) -> None:
    async with _session(transport, url) as session:
        tools = (await session.list_tools()).tools
        print(f"Transport : {transport}")
        print(f"Available tools ({len(tools)}):")
        for t in tools:
            print(f"  - {t.name}: {t.description}")

        print()
        for t in tools:
            result = await session.call_tool(t.name, {})
            print(f"[{t.name}]")
            for c in result.content:
                try:
                    print(json.dumps(json.loads(c.text), indent=2))
                except (json.JSONDecodeError, AttributeError):
                    print(c)
            print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test the robovast-mcp server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport to use (default: stdio).",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Server URL (default: /sse endpoint for sse, /mcp endpoint for streamable-http).",
    )
    args = parser.parse_args()
    if args.url is None:
        args.url = DEFAULT_HTTP_URL if args.transport == "streamable-http" else DEFAULT_SSE_URL
    try:
        asyncio.run(main(args.transport, args.url))
    except* Exception as eg:
        if args.transport in ("sse", "streamable-http"):
            print(f"ERROR: Could not connect to {args.url}")
            print(f"Make sure the server is running:  robovast-mcp --transport {args.transport}")
            raise SystemExit(1) from eg.exceptions[0]
        raise
