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

"""Sphinx extension providing the argument-free ``.. mcp-tools::`` directive.

Usage in ``.rst`` files::

    .. mcp-tools::

The directive loads **every installed MCP plugin via the registry** (the same
entry-point discovery the server uses at startup) and renders one table per
plugin — tool name + first docstring line. Because it reads what the server
actually registers, the docs cannot drift from the tool surface: a new plugin
shows up automatically, and an unregistered module cannot be documented.
"""

from docutils import nodes
from docutils.parsers.rst import Directive
from docutils.statemachine import StringList
from sphinx.application import Sphinx


class MCPToolsDirective(Directive):
    """Render one MCP tools table per registered plugin (registry-driven)."""

    required_arguments = 0
    has_content = False

    def run(self):
        from robovast.mcp_server.registry import load_registered_tool_details

        plugins = load_registered_tool_details()
        lines: list[str] = []
        for plugin_name, tools in sorted(plugins.items()):
            if not tools:  # e.g. the prompts plugin registers prompts, not tools
                continue
            lines += [f".. rubric:: {plugin_name}", ""]
            lines += [
                ".. list-table::",
                "   :header-rows: 1",
                "   :widths: 35 65",
                "",
                "   * - Tool",
                "     - Description",
            ]
            for tool in tools:
                lines.append(f"   * - ``{tool['name']}``")
                lines.append(f"     - {tool['summary']}")
            lines.append("")

        node = nodes.section()
        node.document = self.state.document
        self.state.nested_parse(StringList(lines), self.content_offset, node)
        return list(node.children)


def setup(app: Sphinx):
    app.add_directive("mcp-tools", MCPToolsDirective)
    return {"version": "0.2", "parallel_read_safe": True}
