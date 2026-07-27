# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Parsing the one file address, and refusing the shapes that must never be joined.

The rejections matter twice over: an address arrives from a client (an MCP argument, a
URL segment), and the namespace it names decides both which root it resolves against
and whether it may be written at all.

Every rejection is also asserted to *teach* — a caller that got the shape wrong (a bare
relative path, a stale ``run-files/`` habit, an unknown namespace) cannot fix it from
"invalid path", so the message carries the expected form and an example.
"""

import pytest

from robovast.common.file_address import (RESULTS, SOURCES, AddressError,
                                          as_directory, format_address,
                                          is_directory, is_writable,
                                          parse_address, require_writable)


def test_parses_the_three_parts():
    assert parse_address("/results/camp-1/_execution/outcome.json") == (
        RESULTS, "camp-1", "_execution/outcome.json")
    assert parse_address("/sources/ws-ab12/demo.vast") == (
        SOURCES, "ws-ab12", "demo.vast")


def test_an_owner_root_has_an_empty_path():
    """A root is a directory, so it is listable but not readable — the empty path is
    how the transports tell those apart."""
    assert parse_address("/results/camp-1/") == (RESULTS, "camp-1", "")
    assert parse_address("/results/camp-1") == (RESULTS, "camp-1", "")


def test_a_missing_leading_slash_is_tolerated():
    assert parse_address("results/camp-1/x.txt") == (RESULTS, "camp-1", "x.txt")


@pytest.mark.parametrize("address", [
    "",
    "   ",
    "/campaigns/camp-1/status",          # a control namespace, not a content one
    "/run-files/camp-1/nav/0/x",         # the retired synthetic segment
    "/results",                          # no owner
    "/results/",                         # no owner
])
def test_rejected_addresses_name_the_expected_form(address):
    with pytest.raises(AddressError) as excinfo:
        parse_address(address)
    message = str(excinfo.value)
    assert "/<namespace>/<owner>/<path>" in message
    assert "results" in message and "sources" in message
    assert "_execution/outcome.json" in message      # the worked example


@pytest.mark.parametrize("rel", ["../etc/passwd", "a/../../b", "/etc/passwd", "~/secrets"])
def test_escapes_are_refused_before_any_root_is_touched(rel):
    with pytest.raises(AddressError):
        parse_address(f"/results/camp-1/{rel}")


def test_address_error_is_a_value_error():
    """So the service's existing ``except ValueError`` keeps mapping it to a 400."""
    assert issubclass(AddressError, ValueError)


# -- the namespace is the permission ----------------------------------------


def test_only_sources_is_writable():
    assert is_writable(SOURCES) is True
    assert is_writable(RESULTS) is False


def test_refusing_a_results_write_says_where_writes_go():
    with pytest.raises(AddressError) as excinfo:
        require_writable("/results/camp-1/x.txt", RESULTS)
    assert "/sources/<workspace_id>/" in str(excinfo.value)
    require_writable("/sources/ws-1/x.vast", SOURCES)      # no raise


# -- formatting is the inverse ----------------------------------------------


@pytest.mark.parametrize("address", [
    "/results/camp-1/_execution/outcome.json",
    "/sources/ws-ab12/scenes/room.osc",
    "/results/camp-1/",
])
def test_format_is_the_inverse_of_parse(address):
    assert format_address(*parse_address(address)) == address


def test_a_directory_address_ends_in_a_slash():
    """So it concatenates with a listing's entries, which carry the same mark."""
    listed = format_address(RESULTS, "camp-1", "nav/0/")
    assert listed == "/results/camp-1/nav/0/"
    assert listed + "test.xml" == "/results/camp-1/nav/0/test.xml"


def test_a_directory_mark_is_a_hint_both_ways():
    """It is how a client says "list this" over HTTP, but never changes what the
    address points at — otherwise one call means two things."""
    assert is_directory("/results/camp-1/nav/") is True
    assert is_directory("/results/camp-1/nav") is False
    assert as_directory("/results/camp-1/nav") == "/results/camp-1/nav/"
    assert as_directory("/results/camp-1/nav/") == "/results/camp-1/nav/"
    assert parse_address("/results/camp-1/nav/") == parse_address("/results/camp-1/nav")
