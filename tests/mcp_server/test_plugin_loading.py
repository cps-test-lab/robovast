# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A registered MCP plugin loads, or the server does not start.

An entry point exists only where its distribution is installed, so one that cannot be
loaded is a broken install: logging it and serving the rest would leave part of the
surface missing from every client and from the generated tool reference, unnoticed.
"""

import subprocess
import sys
import types

import pytest
from fastmcp import FastMCP

from robovast.mcp_server import registry


def test_every_plugin_loads_in_a_fresh_interpreter():
    """The first import of a plugin happens inside the registry's own load -- in the docs
    build and at service start -- so a plugin whose import loads the registry again only
    fails there, never in a process that imported it earlier. The docs plugin's tool listing
    is such a load, and the page carrying it must still be served."""
    code = ("from robovast.mcp_server.registry import load_registered_tool_details\n"
            "print(sorted(load_registered_tool_details()))\n"
            "from robovast.mcp_server.plugins import docs\n"
            "if 'mcp' in docs._doc_files:\n"
            "    print(docs.search_docs(page='mcp').get('error', 'served'))\n")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          check=False)
    assert proc.returncode == 0, proc.stderr
    listed, *served = proc.stdout.splitlines()
    assert "'docs'" in listed
    assert served in ([], ["served"]), proc.stdout + proc.stderr


def _entry_point(name, load):
    return types.SimpleNamespace(name=name, value=f"pkg.{name}:Plugin", load=load,
                                 dist=types.SimpleNamespace(name="some-dist"))


def _serve(monkeypatch, *eps):
    monkeypatch.setattr(registry, "entry_points", lambda group: list(eps))
    return registry.load_plugins(FastMCP("test"))


def test_a_plugin_that_cannot_be_imported_stops_the_load(monkeypatch):
    def load():
        raise ImportError("no module named pkg")
    with pytest.raises(registry.PluginLoadError,
                       match=r"'broken' \(pkg.broken:Plugin, from some-dist\).*ImportError"):
        _serve(monkeypatch, _entry_point("broken", load))


def test_a_plugin_whose_register_raises_stops_the_load(monkeypatch):
    class Plugin:
        name = "raising"

        def register(self, mcp):
            raise ValueError("bad tool")
    with pytest.raises(registry.PluginLoadError, match="register.. raised ValueError"):
        _serve(monkeypatch, _entry_point("raising", lambda: Plugin))


def test_an_entry_point_that_is_no_plugin_stops_the_load(monkeypatch):
    with pytest.raises(registry.PluginLoadError, match="MCPPlugin protocol"):
        _serve(monkeypatch, _entry_point("not_a_plugin", lambda: object))
