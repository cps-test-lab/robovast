# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every registered tool answers what it raises with the one error document."""

import asyncio
import errno
import functools
import importlib
import inspect
import pkgutil

import pytest
from fastmcp import Client, FastMCP

from robovast.common.errors import STORAGE_FULL_DETAIL, ActionableError
from robovast.mcp_server import plugins, service_access, tool_stats
from robovast.mcp_server.registry import load_plugins, registered_tools
from robovast.service.interface import ServiceError, ServiceUnreachable


def _raising(fn, exc):
    """A stand-in for tool *fn* -- same name, signature, annotations and sync or async --
    raising *exc*."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_tool(*_args, **_kwargs):
            raise exc
        return async_tool

    @functools.wraps(fn)
    def tool(*_args, **_kwargs):
        raise exc
    return tool


def _plugin_modules():
    for info in pkgutil.iter_modules(plugins.__path__):
        module = importlib.import_module(f"{plugins.__name__}.{info.name}")
        if getattr(module, "_TOOLS", None):
            yield module


def _server_raising(monkeypatch, exc) -> FastMCP:
    """Every plugin loaded as the server loads it, each tool's body raising *exc*."""
    for module in _plugin_modules():
        monkeypatch.setattr(module, "_TOOLS", [_raising(fn, exc) for fn in module._TOOLS])
    mcp = FastMCP("test")
    load_plugins(mcp)
    return mcp


def _arguments(tool) -> dict:
    """One schema-valid value per required parameter."""
    by_type = {"string": "x", "integer": 1, "number": 1.0, "boolean": False,
               "array": [], "object": {}}
    schema = tool.parameters or {}
    out = {}
    for name in schema.get("required", []):
        prop = schema["properties"][name]
        prop = (prop.get("anyOf") or [prop])[0]
        out[name] = by_type[prop["type"]]
    return out


def _answers(mcp) -> dict:
    """``{tool: structured answer}`` for every registered tool, called through a client."""
    async def call_all():
        async with Client(mcp) as client:
            return {name: (await client.call_tool(name, _arguments(tool),
                                                  raise_on_error=False)).structured_content
                    for name, tool in registered_tools(mcp).items()}
    return asyncio.run(call_all())


class _Bug(Exception):
    pass


_CASES = {
    "no service": (service_access.NoService(), {"error": service_access.NO_SERVICE}),
    "unreachable": (
        ServiceUnreachable("http://service.example", "connection refused"),
        {"error": "no robovast-service answered at http://service.example: connection "
                  f"refused. {service_access.NO_SERVICE}"}),
    "actionable": (ActionableError("the image is not built", next_step="vast image wait b1"),
                   {"error": "the image is not built", "next_step": "vast image wait b1"}),
    "storage full": (OSError(errno.ENOSPC, "No space left on device", "/data/x"),
                     {"error": STORAGE_FULL_DETAIL}),
    "service refusal": (ServiceError(404, "no campaign named c1", "http://service.example/c"),
                        {"error": "no campaign named c1"}),
    "bad input": (ValueError("limit must be positive"), {"error": "limit must be positive"}),
    "unknown id": (KeyError("c1"), {"error": "c1"}),
}


def test_the_stand_ins_cover_sync_and_async_tools():
    tools = [fn for module in _plugin_modules() for fn in module._TOOLS]
    assert {inspect.iscoroutinefunction(fn) for fn in tools} == {True, False}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_every_tool_answers_a_raised_failure_with_the_one_document(monkeypatch, case):
    exc, expected = _CASES[case]
    answers = _answers(_server_raising(monkeypatch, exc))
    assert answers
    wrong = {name: answer for name, answer in answers.items() if answer != expected}
    assert not wrong, f"{case}: {wrong}"


def test_every_tool_answers_a_bug_with_its_type_message_and_frames(monkeypatch):
    answers = _answers(_server_raising(monkeypatch, _Bug("the body broke")))
    for name, answer in answers.items():
        assert list(answer) == ["error"], name
        head, _, frames = answer["error"].partition("\n\n")
        assert head == "_Bug: the body broke", name
        assert "raise exc" in frames, name


def test_an_answered_failure_is_recorded_as_a_failed_call(monkeypatch, tmp_path):
    from robovast.mcp_server.server import _install_tool_stats

    log = tool_stats.ToolCallLog()
    log.open(tmp_path / tool_stats.FILENAME)
    monkeypatch.setattr(tool_stats, "LOG", log)
    mcp = FastMCP("test")

    @mcp.tool
    def ping() -> dict:
        return {"ok": True}

    @mcp.tool
    def refuse() -> dict:
        raise ValueError("no")

    registered_tools(mcp)["refuse"].fn = service_access.answering_errors(
        registered_tools(mcp)["refuse"].fn)
    _install_tool_stats(mcp)

    async def call():
        async with Client(mcp) as client:
            await client.call_tool("ping", {})
            assert (await client.call_tool("refuse", {})).structured_content == {"error": "no"}

    asyncio.run(call())
    log.flush()
    assert {c.tool: c.ok for c in log.read_calls()} == {"ping": True, "refuse": False}
