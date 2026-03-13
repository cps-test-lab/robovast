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

"""Sphinx extension providing the ``.. variation-plugin::`` directive.

Usage in ``.rst`` files::

    .. variation-plugin:: robovast.common.variation.parameter_variation.ParameterVariationList

The directive imports the referenced class, emits a heading using the class name,
and then parses and renders the class docstring as RST so that inline markup,
code blocks, and lists are fully supported.
"""

import inspect
import textwrap
from importlib import import_module

from docutils import nodes
from docutils.parsers.rst import Directive
from docutils.statemachine import StringList
from sphinx.application import Sphinx


class VariationPluginDirective(Directive):
    """Render a variation plugin's documentation from its class docstring."""

    required_arguments = 1  # e.g. "robovast.common.variation.parameter_variation.ParameterVariationList"
    has_content = False

    def run(self):
        module_path, class_name = self.arguments[0].rsplit(".", 1)
        mod = import_module(module_path)
        cls = getattr(mod, class_name)

        docstring = inspect.getdoc(cls) or ""

        # Use a rubric for the class name so it renders as a heading without
        # creating a new document section (which would cause "Unexpected section
        # title" errors inside nested_parse).
        lines = [
            f".. rubric:: {class_name}",
            "",
        ]
        if docstring:
            lines.extend(textwrap.dedent(docstring).splitlines())
            lines.append("")

        node = nodes.section()
        node.document = self.state.document
        self.state.nested_parse(
            StringList(lines), self.content_offset, node,
        )
        return list(node.children)


def setup(app: Sphinx):
    app.add_directive("variation-plugin", VariationPluginDirective)
    return {"version": "0.1", "parallel_read_safe": True}
