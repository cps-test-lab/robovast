# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The docs served over MCP expand the same ``mcp-tools`` directive Sphinx renders.

``docs/mcp.rst`` carries the directive in its bare form -- the listing is registry-driven and
names nothing -- so a reader of ``search_docs(page="mcp")`` must get the tool list, not the
directive's text.
"""

from robovast.mcp_server.plugins import docs


def test_the_bare_directive_expands_to_the_registered_tools():
    text = docs._served(  # pylint: disable=protected-access
        "some-page", "before\n\n.. mcp-tools::\n\nafter\n")
    assert ".. mcp-tools::" not in text
    assert "``list_campaigns``" in text and "``start_campaign``" in text
    assert text.startswith("before") and text.rstrip().endswith("after")
