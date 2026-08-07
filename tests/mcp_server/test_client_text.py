"""The HTML-escape undo applied to free text an MCP client supplies."""

import pytest

from robovast.mcp_server.client_text import unescape_client_text as u


@pytest.mark.parametrize("escaped,expected", [
    # What this exists for: a client escaped the prompt and the entity was stored.
    ("wheels rebuilt post SIM_SUITE_-&gt;ROBOSITO_", "wheels rebuilt post SIM_SUITE_->ROBOSITO_"),
    ("nav2 &lt;-&gt; moveit", "nav2 <-> moveit"),
    ("p&lt;0.05 on 95% CI", "p<0.05 on 95% CI"),
    ("DWB &amp; MPPI", "DWB & MPPI"),
    # All specials, not just the three an escaper emits: named, decimal, hexadecimal.
    ("DWB &ne; MPPI", "DWB ≠ MPPI"),
    ("&Delta;t &le; 0.1s", "Δt ≤ 0.1s"),
    ("goal &#8594; dock", "goal → dock"),
    ("goal &#x2192; dock", "goal → dock"),
    ("&copy; 2026", "\xa9 2026"),
])
def test_decodes_well_formed_references(escaped, expected):
    assert u(escaped) == expected


@pytest.mark.parametrize("text", [
    # A query string is prose here, not markup: html.unescape turns "&param=" into "\xb6m=".
    "see http://host/x?a=1&param=2&para=3",
    # Longest-prefix matching would make this "\xactit;".
    "a &notit; b",
    "R&D pilot on the AT&T lane",
    "runs 1&2",
    "&; and & alone",
    # A name that is not an entity stays put rather than being partly decoded.
    "&nosuchentity; here",
    # Out of range and surrogate codepoints are left as written.
    "&#1114112; &#xD800;",
    "plain description with no references",
])
def test_leaves_everything_else_alone(text):
    assert u(text) == text


def test_decoding_is_single_level():
    """A description that is *about* an entity survives being written down: the
    replacement text is never rescanned, so "&amp;gt;" is not decoded twice."""
    assert u("write &amp;gt; for the entity") == "write &gt; for the entity"
    assert u("&amp;amp;") == "&amp;"


def test_empty_string():
    assert u("") == ""
