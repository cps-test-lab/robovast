# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""Plugin registry: discovers and loads ``robovast.mcp_plugins`` entry points."""

import logging
from importlib.metadata import entry_points

from fastmcp import FastMCP

from .plugin import MCPPlugin

ENTRY_POINT_GROUP = "robovast.mcp_plugins"

logger = logging.getLogger(__name__)


def registered_tools(mcp: FastMCP) -> dict:
    """``{tool_name: Tool}`` for every tool registered on *mcp*.

    The one place that reaches into FastMCP's component store. Its public
    ``list_tools()`` is a coroutine, and every caller here is synchronous —
    ``load_plugins`` runs during server construction, and the docs directive
    renders in Sphinx — so introducing an event loop to read a dict would be the
    larger hazard. Pinned against ``fastmcp == 3.4.4`` (see ``pyproject.toml``):
    if that pin moves and this attribute is gone, this function is the only thing
    to fix, and :mod:`tests.mcp_server.test_plugin_registry_sync` fails loudly.
    """
    from fastmcp.tools.tool import Tool  # pylint: disable=import-outside-toplevel
    return {c.name: c for c in mcp._local_provider._components.values()  # pylint: disable=protected-access
            if isinstance(c, Tool)}


def registered_text(mcp: FastMCP) -> dict[str, str]:
    """``{"<kind> <name>": description}`` for everything registered on *mcp*.

    The whole of what this server puts in front of a model, which is not only its tools:
    a resource's description, a template's and a prompt's are shipped to a client and
    read the same way. They are also registered from *inside* a plugin's ``register()``,
    so nothing that enumerates module-level functions can see them -- which is why a
    check over "what this server says" has to be written against this rather than
    against :func:`registered_tools`.

    Reaches the same component store as :func:`registered_tools`, for the same reason:
    one place in this package touches FastMCP's internals, so a version bump has one
    thing to fix.
    """
    from fastmcp.prompts.prompt import Prompt  # pylint: disable=import-outside-toplevel
    from fastmcp.resources.resource import Resource  # pylint: disable=import-outside-toplevel
    from fastmcp.resources.template import ResourceTemplate  # pylint: disable=import-outside-toplevel
    from fastmcp.tools.tool import Tool  # pylint: disable=import-outside-toplevel

    kinds = ((Tool, "tool"), (ResourceTemplate, "resource template"),
             (Resource, "resource"), (Prompt, "prompt"))
    out: dict[str, str] = {}
    for component in mcp._local_provider._components.values():  # pylint: disable=protected-access
        for cls, label in kinds:
            if isinstance(component, cls):
                out[f"{label} {component.name}"] = getattr(component, "description", "") or ""
                break
    return out


def load_plugins(mcp: FastMCP) -> list[MCPPlugin]:
    """Discover all installed plugins and register them with *mcp*.

    Parameters
    ----------
    mcp:
        The :class:`~mcp.server.fastmcp.FastMCP` instance to register
        tools/resources on.

    Returns
    -------
    list[MCPPlugin]
        The instantiated plugin objects that were successfully loaded.
    """
    loaded: list[MCPPlugin] = []
    plugin_tools: dict[str, list[str]] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            plugin_cls = ep.load()
            plugin: MCPPlugin = plugin_cls()
            if not isinstance(plugin, MCPPlugin):
                logger.warning(
                    "Entry point %r does not satisfy MCPPlugin protocol – skipped.", ep.name
                )
                continue
            before = set(registered_tools(mcp))
            plugin.register(mcp)
            after = set(registered_tools(mcp))
            plugin_tools[plugin.name] = sorted(after - before)
            loaded.append(plugin)
            logger.debug("Loaded MCP plugin %r from %r.", plugin.name, ep.value)
        except Exception:
            logger.exception("Failed to load MCP plugin from entry point %r.", ep.name)
    _last_plugin_tools.update(plugin_tools)
    return loaded


#: Mapping of plugin name → list of tool names, populated by :func:`load_plugins`.
_last_plugin_tools: dict[str, list[str]] = {}


def get_plugin_tools() -> dict[str, list[str]]:
    """Return the plugin-name → tool-names mapping from the last load."""
    return dict(_last_plugin_tools)


def get_plugin_tool_details(mcp: FastMCP) -> dict[str, list[dict]]:
    """Return ``{plugin_name: [{name, summary}, ...]}`` for the plugins on *mcp*.

    ``summary`` is the first line of each tool's description. Pairs the plugin→tool
    mapping from :func:`load_plugins` with the registered tool objects, so callers
    (notably the docs ``.. mcp-tools::`` directive) get a description **without**
    reaching into FastMCP internals themselves.
    """
    by_name = registered_tools(mcp)

    def _summary(name: str) -> str:
        tool = by_name.get(name)
        text = (getattr(tool, "description", "") or "") if tool else ""
        return text.strip().split("\n", 1)[0]

    return {
        plugin: [{"name": n, "summary": _summary(n)} for n in tool_names]
        for plugin, tool_names in _last_plugin_tools.items()
    }


def load_registered_tool_details() -> dict[str, list[dict]]:
    """Load all installed plugins into a throwaway server and return their tools.

    A single source of truth for "what tools does the server actually expose",
    consumed by the docs build so the reference cannot drift from the registry.
    """
    mcp = FastMCP("robovast-docs")
    load_plugins(mcp)
    return get_plugin_tool_details(mcp)
