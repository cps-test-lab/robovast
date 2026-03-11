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

"""Plugin protocol for extending the robovast MCP server."""

from typing import Protocol, runtime_checkable

from mcp.server.fastmcp import FastMCP


@runtime_checkable
class MCPPlugin(Protocol):
    """Protocol that every robovast MCP plugin must satisfy.

    Plugins are discovered via the ``robovast.mcp_plugins`` entry-point group.
    Each entry point should point to a class that implements this protocol.

    Example ``pyproject.toml`` registration::

        [tool.poetry.plugins."robovast.mcp_plugins"]
        my_plugin = "my_package.mcp_plugin:MyMCPPlugin"
    """

    @property
    def name(self) -> str:
        """A short unique identifier for this plugin (e.g. ``"nav"``)."""

    def register(self, mcp: FastMCP) -> None:
        """Register tools and resources with the MCP server.

        Called once during server startup.  Use the *mcp* instance to attach
        tools (``@mcp.tool()``) and resources (``@mcp.resource()``).

        Parameters
        ----------
        mcp:
            The :class:`~mcp.server.fastmcp.FastMCP` instance.
        """
