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


# -- an argument a tool lacks on purpose -------------------------------------


def test_an_argument_the_tool_lacks_on_purpose_is_answered_with_why():
    """The reason reaches the caller in the rejection, the one message it is sure to read,
    and costs nothing in the tool's description."""
    from robovast.mcp_server.lacks import lacks

    mcp = FastMCP("test")

    @lacks(timeout="the bound follows from what is run")
    def runs_something(command: str = "") -> dict:
        return {"ok": command}

    mcp.tool(runs_something)
    _install_argument_help(mcp)

    with pytest.raises(Exception) as excinfo:
        _call(mcp, "runs_something", {"command": "ls", "timeout": 60})

    message = str(excinfo.value)
    assert "runs_something accepts: command." in message
    assert "There is no `timeout`: the bound follows from what is run." in message
    assert "Unexpected keyword argument" in message, "the real error stays"


def test_an_undeclared_unexpected_argument_gets_no_invented_reason():
    mcp = FastMCP("test")

    @mcp.tool
    def plain(command: str = "") -> dict:
        return {"ok": command}

    _install_argument_help(mcp)

    with pytest.raises(Exception) as excinfo:
        _call(mcp, "plain", {"timeout": 60})

    assert "There is no" not in str(excinfo.value)


def test_a_tool_with_no_arguments_says_so_and_why():
    """Nothing to list is still something to say: without it the caller gets pydantic's
    bare error and scopes the next call the same way."""
    from robovast.mcp_server.lacks import lacks

    mcp = FastMCP("test")

    @lacks("there is only one")
    def stop_the_one() -> dict:
        return {"stopped": True}

    @mcp.tool
    def stop_anything() -> dict:
        return {"stopped": True}

    mcp.tool(stop_the_one)
    _install_argument_help(mcp)

    with pytest.raises(Exception) as excinfo:
        _call(mcp, "stop_the_one", {"workspace_id": "ws-1"})
    assert "stop_the_one takes no arguments: there is only one." in str(excinfo.value)

    with pytest.raises(Exception) as excinfo:
        _call(mcp, "stop_anything", {"workspace_id": "ws-1"})
    assert "stop_anything takes no arguments." in str(excinfo.value)


def test_the_real_surface_says_why_a_container_run_has_no_timeout():
    from robovast.mcp_server.server import create_server

    mcp = create_server()

    with pytest.raises(Exception) as excinfo:
        _call(mcp, "exec_in_container", {"command": "true", "timeout": 60})
    assert "There is no `timeout`:" in str(excinfo.value)

    with pytest.raises(Exception) as excinfo:
        _call(mcp, "search_run_logs", {"campaign_id": "c", "top": 60})
    assert "There is no `top`: summarize=True" in str(excinfo.value)

    with pytest.raises(Exception) as excinfo:
        _call(mcp, "stop_container", {"workspace_id": "ws-1"})
    assert "stop_container takes no arguments: exec_in_container holds one" in str(excinfo.value)


def test_every_declaration_names_a_tool_and_an_argument_it_really_lacks():
    """A declaration outlives its reason the moment the tool gains the argument or is renamed,
    and then it explains a rejection that no longer happens."""
    from robovast.mcp_server.lacks import arguments_it_lacks, why_it_takes_none
    from robovast.mcp_server.registry import registered_tools
    from robovast.mcp_server.server import create_server

    declaring = 0
    for name, tool in registered_tools(create_server()).items():
        lacks, why = arguments_it_lacks(tool), why_it_takes_none(tool)
        declaring += bool(lacks or why)
        accepted = set((tool.parameters or {}).get("properties", {}))
        assert not accepted & set(lacks), f"{name} takes an argument it declares absent"
        if why:
            assert not accepted, f"{name} says it takes no arguments but takes {accepted}"
    assert declaring, "the surface declares at least the container tools"


def test_a_rejection_carries_no_detail_meant_for_a_browser():
    """Pydantic's error type, echoed input and documentation link are noise to a model, and
    every rejected call would pay for them. The error itself -- which argument, what was
    wrong -- stays."""
    with pytest.raises(Exception) as excinfo:
        _call(_server(), "needs_address", {"address": "/x", "top": 5})

    message = str(excinfo.value)
    assert "top\n  Unexpected keyword argument" in message
    assert "errors.pydantic.dev" not in message
    assert "[type=" not in message and "input_value" not in message
