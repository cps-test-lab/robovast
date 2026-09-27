# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Sphinx extension providing the ``.. wait-exit-codes::`` directive.

Usage in ``.rst`` files::

    .. wait-exit-codes:: robovast.execution.wait_exit.CampaignWaitExit

Renders a waiting command's exit codes as a table, read from the enum the command raises
(:mod:`robovast.execution.wait_exit`). The codes are defined there once; this only renders
them, so the documented table cannot drift from what the command exits with. Modelled on
:mod:`http_routes`, which does the same for the HTTP route table.
"""

import importlib
import re

from docutils import nodes
from docutils.parsers.rst import Directive
from docutils.statemachine import StringList
from sphinx.application import Sphinx

#: A command-line option in a meaning, set as a literal so it is not typeset as a dash.
_OPTION = re.compile(r"(?<![\w`])(--[a-z][a-z-]*)")


class WaitExitCodesDirective(Directive):
    """Render one table: code, member name, meaning."""

    required_arguments = 1
    has_content = False

    def run(self):
        module_name, _, class_name = self.arguments[0].rpartition(".")
        try:
            codes = getattr(importlib.import_module(module_name), class_name)
        except (ImportError, AttributeError) as e:
            raise self.error(f"wait-exit-codes: cannot load {self.arguments[0]}: {e}")
        lines = [
            ".. list-table::",
            "   :header-rows: 1",
            "   :widths: 8 22 70",
            "",
            "   * - Code",
            "     - Name",
            "     - Means",
        ]
        for member in codes:
            lines.append(f"   * - ``{member.value}``")
            lines.append(f"     - ``{member.name}``")
            lines.append("     - " + _OPTION.sub(r"``\1``", member.full_meaning))
        lines.append("")

        node = nodes.section()
        node.document = self.state.document
        self.state.nested_parse(StringList(lines), self.content_offset, node)
        return list(node.children)


def setup(app: Sphinx):
    app.add_directive("wait-exit-codes", WaitExitCodesDirective)
    return {"version": "0.1", "parallel_read_safe": True}
