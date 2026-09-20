# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Recording a tool call never runs on the event loop.

A record may flush the buffer to the index -- a connect and a ``COPY``. The MCP server is
mounted in the service, so on the event loop that write would stall every request the service
is serving, ``/healthz`` included, for as long as the index takes to answer.
"""

import asyncio

from fastmcp import Client, FastMCP

from robovast.mcp_server import server, tool_stats


def test_a_tool_call_is_recorded_off_the_event_loop(monkeypatch):
    sites = []

    def record(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            sites.append("event loop")
        except RuntimeError:
            sites.append("worker thread")

    monkeypatch.setattr(tool_stats.LOG, "record", record)

    mcp = FastMCP("test")

    @mcp.tool
    def ping() -> dict:
        return {"ok": True}

    server._install_tool_stats(mcp)

    async def _go():
        async with Client(mcp) as client:
            return await client.call_tool("ping", {})

    asyncio.run(_go())
    assert sites == ["worker thread"]
