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
