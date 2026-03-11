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

"""Sphinx extension providing the ``.. mcp-tools::`` directive.

Usage in ``.rst`` files::

    .. mcp-tools:: robovast.evaluation.mcp_server.plugin_common._TOOLS

The directive imports the referenced list of functions and renders a
two-column table (Tool / Description) from each function's name and
first docstring line.
"""

from importlib import import_module

from docutils import nodes
from docutils.parsers.rst import Directive
from docutils.statemachine import StringList
from sphinx.application import Sphinx


class MCPToolsDirective(Directive):
    """Render an MCP tools table from a Python list of functions."""

    required_arguments = 1  # e.g. "robovast.evaluation.mcp_server.plugin_common._TOOLS"
    has_content = False

    def run(self):
        module_path, attr = self.arguments[0].rsplit(".", 1)
        mod = import_module(module_path)
        tools = getattr(mod, attr)

        # Build RST for a list-table and parse it so inline markup works.
        lines = [
            ".. list-table::",
            "   :header-rows: 1",
            "   :widths: 35 65",
            "",
            "   * - Tool",
            "     - Description",
        ]
        for fn in tools:
            doc = (fn.__doc__ or "").strip().split("\n")[0]
            lines.append(f"   * - ``{fn.__name__}``")
            lines.append(f"     - {doc}")

        node = nodes.section()
        node.document = self.state.document
        self.state.nested_parse(
            StringList(lines), self.content_offset, node,
        )
        return list(node.children)


def setup(app: Sphinx):
    app.add_directive("mcp-tools", MCPToolsDirective)
    return {"version": "0.1", "parallel_read_safe": True}
