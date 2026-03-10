"""Minimal example: use the robovast-mcp tools in an OpenAI conversation.

The script:
1. Connects to the robovast-mcp server (stdio or HTTP/SSE).
2. Fetches the available MCP tools and converts them to OpenAI function format.
3. Runs a simple agentic loop: OpenAI may call tools, results are fed back,
   until the model produces a final text answer.

Usage::

    # stdio – server started automatically
    OPENAI_API_KEY=sk-... python examples/openai_prompt.py

    # HTTP/SSE – start server first: robovast-mcp --transport sse
    OPENAI_API_KEY=sk-... python examples/openai_prompt.py --transport sse
"""

import argparse
import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from openai import OpenAI

DEFAULT_SSE_URL = "http://localhost:8000/sse"
DEFAULT_HTTP_URL = "http://localhost:8000/mcp"

USER_PROMPT = (
    "What navigation variation types are available in RoboVAST "
    "and what do the nav data-model types look like?"
)


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
    openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    async with _session(transport, url) as session:
        # --- discover tools -------------------------------------------------
        tools_result = await session.list_tools()
        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema,
                },
            }
            for t in tools_result.tools
        ]

        # --- agentic loop ---------------------------------------------------
        messages = [{"role": "user", "content": USER_PROMPT}]

        while True:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=openai_tools,
                tool_choice="auto",
            )
            choice = response.choices[0]
            messages.append(choice.message)

            if choice.finish_reason == "stop":
                print(choice.message.content)
                break

            if choice.finish_reason == "tool_calls":
                for tc in choice.message.tool_calls:
                    args = json.loads(tc.function.arguments)
                    result = await session.call_tool(tc.function.name, args)
                    content = json.dumps([c.text for c in result.content])
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": content,
                        }
                    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chat with OpenAI using robovast-mcp tools.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport to use (default: stdio).",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Server URL (default: /sse for sse, /mcp for streamable-http).",
    )
    args = parser.parse_args()
    if args.url is None:
        args.url = DEFAULT_HTTP_URL if args.transport == "streamable-http" else DEFAULT_SSE_URL
    asyncio.run(main(args.transport, args.url))
