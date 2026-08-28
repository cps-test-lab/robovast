# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Sphinx extension providing the argument-free ``.. http-routes::`` directive.

Usage in ``.rst`` files::

    .. http-routes::

The directive **builds the real FastAPI app** and walks its routing table, rendering one
table per tag. Because it reads what the service actually registers, the documented route
table cannot drift from the served one: a new route shows up automatically, and a removed
route cannot stay documented — which is how the retired synthetic run-file route came to
look documented while matching no directory on disk.

The rules for *which* routes count and how each is described live in
:mod:`robovast.service.app` (``api_routes``, ``route_description``, ``ROUTE_TAG_ORDER``),
not here: they are properties of the API, and the test suite checks them without a docs
build. This module only renders. Modelled on :mod:`mcp_tools`, which does the same for the
MCP tool surface.
"""

from docutils import nodes
from docutils.parsers.rst import Directive
from docutils.statemachine import StringList
from sphinx.application import Sphinx


class HTTPRoutesDirective(Directive):
    """Render one table of HTTP routes per tag, introspected from the live app."""

    required_arguments = 0
    has_content = False

    def run(self):
        from robovast.service.app import (ROUTE_TAG_ORDER, api_routes, build_app,
                                          route_description)

        # ``build_app`` only captures *impl* in handler closures and the lifespan, so it
        # never touches it at construction time — a stub is enough to get the route table.
        class _Stub:
            """Stands in for a RobovastInterface; no handler runs during a docs build."""

        by_tag: dict[str, list[tuple[str, str, str]]] = {}
        undescribed: list[str] = []
        for route in api_routes(build_app(_Stub())):
            methods = ", ".join(sorted(route.methods or []))
            description = route_description(route)
            if not description:
                undescribed.append(f"{methods} {route.path}")
            tags = route.tags or ["(untagged)"]
            by_tag.setdefault(tags[0], []).append((methods, route.path, description))

        if undescribed:
            # Loud, not a blank row: a route table with empty cells is how a stale
            # hand-maintained list looks complete.
            raise self.error(
                "http-routes: no description for " + ", ".join(sorted(undescribed))
                + " — give the handler a docstring, a summary=, or a matching "
                  "RobovastInterface method with a docstring.")

        lines: list[str] = []
        ordered = [t for t in ROUTE_TAG_ORDER if t in by_tag]
        ordered += [t for t in sorted(by_tag) if t not in ordered]
        for tag in ordered:
            lines += [f".. rubric:: {tag}", ""]
            lines += [
                ".. list-table::",
                "   :header-rows: 1",
                "   :widths: 10 40 50",
                "",
                "   * - Method",
                "     - Path",
                "     - Description",
            ]
            for methods, path, description in sorted(by_tag[tag],
                                                     key=lambda r: (r[1], r[0])):
                lines.append(f"   * - ``{methods}``")
                lines.append(f"     - ``{path}``")
                lines.append(f"     - {description}")
            lines.append("")

        node = nodes.section()
        node.document = self.state.document
        self.state.nested_parse(StringList(lines), self.content_offset, node)
        return list(node.children)


def setup(app: Sphinx):
    app.add_directive("http-routes", HTTPRoutesDirective)
    return {"version": "0.1", "parallel_read_safe": True}
