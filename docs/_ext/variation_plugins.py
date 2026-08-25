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
        lines.extend(_outputs_lines(cls))

        node = nodes.section()
        node.document = self.state.document
        self.state.nested_parse(
            StringList(lines), self.content_offset, node,
        )
        return list(node.children)


def _outputs_lines(cls):
    """RST describing what this plugin writes, and how a campaign binds it.

    Read from the config class rather than the docstring so the rendered page cannot drift
    from what validation enforces -- the failure mode a hand-written "Generated outputs"
    section had, and did: two plugins documented parameter names they no longer wrote.
    """
    config_class = getattr(cls, "CONFIG_CLASS", None)
    fields = getattr(config_class, "model_fields", None) or {}
    if not {"scenario", "sim"} <= set(fields):
        return []

    slots = getattr(config_class, "SLOTS", ()) or ()
    lines = ["", "**Outputs**", ""]
    if slots:
        lines.append(
            "This variation writes several values, so a campaign binds each **output slot** "
            "to a channel and a destination:")
        lines += ["", ".. code-block:: yaml", ""]
        lines += [f"    scenario: {{{slots[0]}: <parameter the scenario declares>}}"]
        if len(slots) > 1:
            lines.append(f"    sim:      {{{slots[1]}: <key of the simulator backend>}}")
        lines += ["", "Slots: " + ", ".join(f"``{s}``" for s in slots) + ".",
                  "Every slot must be bound, each to exactly one channel.", ""]
    else:
        lines += [
            "This variation writes one value. Name its destination with **exactly one** of "
            "``scenario:`` (a parameter the scenario file declares) or ``sim:`` (a key of "
            "the simulator backend, or a path under its dotted root):", "",
            ".. code-block:: yaml", "",
            "    scenario: <parameter>", "    # or", "    sim: <backend key or path>", ""]
    return lines


def setup(app: Sphinx):
    app.add_directive("variation-plugin", VariationPluginDirective)
    return {"version": "0.1", "parallel_read_safe": True}
