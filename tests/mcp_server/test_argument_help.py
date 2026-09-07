# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A rejected call is answered with the arguments the tool does have.

Pydantic knows which argument was wrong and not which ones would have been right, so a
caller is told ``address`` is missing and never told what an address looks like, or that
``top`` is unexpected and never that this tool spells it ``limit``. It guesses again.

The two shapes this covers are the two the surface actually produces: a caller carrying
the ``workspace_id``/``campaign_id`` dialect into a tool that takes an ``address``, and a
caller inventing a name for "how many".
"""

import asyncio

import pytest
from fastmcp import Client, FastMCP

from robovast.mcp_server.server import _install_argument_help


def _server():
    mcp = FastMCP("test")

    @mcp.tool
    def needs_address(address: str, limit: int = 100, recursive: bool = False) -> dict:
        return {"ok": address}

    @mcp.tool
    def blows_up(campaign_id: str) -> dict:
        raise RuntimeError("the campaign is on fire")

    _install_argument_help(mcp)
    return mcp


def _call(mcp, tool, args):
    async def _go():
        async with Client(mcp) as client:
            return await client.call_tool(tool, args)
    return asyncio.run(_go())


def test_the_wrong_addressing_dialect_is_told_the_right_one():
    with pytest.raises(Exception) as excinfo:
        _call(_server(), "needs_address", {"workspace_id": "ws-1", "path": "/"})

    message = str(excinfo.value)
    assert "needs_address accepts: address, limit, recursive" in message
    assert "/sources/<workspace_id>/<path>" in message
    assert "/results/<campaign_id>/<path>" in message


def test_an_invented_argument_is_told_the_real_ones():
    """``top``, ``tail``, ``offset``, ``max_matches`` — the surface has a name for this,
    and the rejection is the moment to say which."""
    with pytest.raises(Exception) as excinfo:
        _call(_server(), "needs_address", {"address": "/x", "top": 5})

    message = str(excinfo.value)
    assert "needs_address accepts: address, limit, recursive" in message
    # No address hint here: the address was given, so pointing at its form is noise.
    assert "/sources/" not in message


def test_a_tool_that_raised_while_running_keeps_its_own_message():
    """Only a rejected *call* gets the parameter list. A tool that ran and failed has said
    something specific, and appending its own signature to that is noise."""
    with pytest.raises(Exception) as excinfo:
        _call(_server(), "blows_up", {"campaign_id": "camp-1"})

    message = str(excinfo.value)
    assert "the campaign is on fire" in message
    assert "accepts:" not in message


def test_help_never_replaces_the_real_error():
    """The enrichment is additive. A caller must still see which argument was wrong."""
    with pytest.raises(Exception) as excinfo:
        _call(_server(), "needs_address", {})

    message = str(excinfo.value)
    assert "address" in message
    assert "Missing required argument" in message


def test_the_hint_is_not_triggered_by_a_tool_whose_name_contains_address():
    """The condition is the error's own line for ``address``, not the word anywhere in the
    text — which every tool with "address" in its *name* would have satisfied."""
    mcp = FastMCP("test")

    @mcp.tool
    def resolve_address(address: str, limit: int = 10) -> dict:
        return {"ok": address}

    _install_argument_help(mcp)

    with pytest.raises(Exception) as excinfo:
        _call(mcp, "resolve_address", {"address": "/x", "top": 5})

    assert "/sources/" not in str(excinfo.value)


def test_the_real_surface_answers_a_dialect_mistake():
    """Against the actual server, not a fixture: the two classes from the call record."""
    from robovast.mcp_server.server import create_server

    mcp = create_server()

    with pytest.raises(Exception) as excinfo:
        _call(mcp, "list_files", {"workspace_id": "ws-1", "path": "/"})
    message = str(excinfo.value)
    assert "list_files accepts:" in message and "/sources/<workspace_id>/<path>" in message

    with pytest.raises(Exception) as excinfo:
        _call(mcp, "search_run_logs", {"campaign_id": "c", "top": 60})
    assert "search_run_logs accepts:" in str(excinfo.value)
