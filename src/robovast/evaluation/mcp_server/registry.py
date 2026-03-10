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

from mcp.server.fastmcp import FastMCP

from .plugin import MCPPlugin

ENTRY_POINT_GROUP = "robovast.mcp_plugins"

logger = logging.getLogger(__name__)


def _get_tool_names(mcp: FastMCP) -> set[str]:
    """Return the set of currently registered tool names."""
    return set(mcp._tool_manager._tools.keys())  # pylint: disable=protected-access


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
            before = _get_tool_names(mcp)
            plugin.register(mcp)
            after = _get_tool_names(mcp)
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
