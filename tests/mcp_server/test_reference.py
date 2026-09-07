# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the reference MCP tools."""

from robovast.mcp_server.plugins.reference import get_config_schema


def test_config_schema_name_carries_naming_constraint():
    schema = get_config_schema()
    name_prop = schema["$defs"]["ConfigurationConfig"]["properties"]["name"]
    desc = name_prop.get("description", "").lower()
    assert "lowercase" in desc
    assert "underscore" in desc


def test_an_unknown_cli_command_is_an_error_result_naming_what_is_there():
    """Raised, this reaches an MCP client as a protocol failure -- which reads as a broken
    server rather than as a misspelled argument. And a refusal that does not name the
    alternatives leaves the caller guessing at a tree it cannot see."""
    from robovast.mcp_server.plugins.reference import get_cli_help
    result = get_cli_help("nosuchcommand")
    assert "nosuchcommand" in result["error"]
    assert "campaign" in result["error"]  # what 'vast' does offer

    nested = get_cli_help("campaign nosuchverb")
    assert "vast campaign" in nested["error"]
    assert "status" in nested["error"]


def test_a_command_asked_for_a_subcommand_says_it_is_not_a_group():
    from robovast.mcp_server.plugins.reference import get_cli_help
    assert "not a group" in get_cli_help("doctor extra")["error"]


def test_a_real_command_still_returns_its_help():
    from robovast.mcp_server.plugins.reference import get_cli_help
    result = get_cli_help("campaign wait")
    assert result["command"] == "campaign wait"
    assert "Usage" in result["help"]


def test_the_bare_listing_is_the_groups_and_not_every_leaf():
    """A caller that wants one area should not be charged for the whole tree, and one that
    is hunting a particular verb should be searching -- so the free listing is the map."""
    from robovast.mcp_server.plugins.reference import get_cli_help
    paths = [e["command"] for e in get_cli_help()["commands"]]
    assert "vast campaign" in paths and "vast cluster" in paths
    assert "vast campaign log" not in paths  # groups only, no leaves


def test_search_finds_a_command_by_a_word_from_its_help():
    """The question the tree listing was being read for: "is there a command for X". A
    keyword that appears only in the one-line help still has to find it, because a caller
    who knew the command's name would not be searching."""
    from robovast.mcp_server.plugins.reference import get_cli_help
    found = [e["command"] for e in get_cli_help(search="postprocessing")["commands"]]
    assert "vast campaign postprocess" in found


def test_search_terms_are_required_together():
    """OR over two ordinary words returns most of the tree, which is the listing the search
    exists to avoid."""
    from robovast.mcp_server.plugins.reference import get_cli_help
    both = {e["command"] for e in get_cli_help(search="campaign log")["commands"]}
    assert "vast campaign log" in both
    assert "vast campaign stop" not in both
