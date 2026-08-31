# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the plugin_metadata MCP tools."""

from robovast.mcp_server.plugins.plugin_metadata import get_plugin_details, list_plugins


def test_no_arguments_lists_the_group_catalogue():
    """With nothing to filter by, the useful answer is what groups exist — with counts."""
    result = list_plugins()
    groups = {g["group"] for g in result["groups"]}
    assert "robovast.variation_types" in groups
    assert result["total"] == len(result["groups"])
    assert all("plugins" in g for g in result["groups"])


def test_a_query_spans_every_group():
    """Regression: a ``return`` inside the loop covers the first group only."""
    result = list_plugins(query="*")
    groups = {p["group"] for p in result["plugins"]}
    assert len(groups) > 1
    assert "robovast.variation_types" in groups


def test_a_group_filters_to_that_group():
    result = list_plugins("robovast.variation_types")
    assert result["plugins"], "expected variation plugins to be registered"
    assert all(p["group"] == "robovast.variation_types" for p in result["plugins"])
    assert result["total"] == len(result["plugins"])


def test_a_query_matches_by_substring_and_by_glob():
    """One search, two idioms — the wildcard is detected, not selected by a flag."""
    substring = list_plugins(query="parametervariation")
    glob = list_plugins(query="ParameterVariation*")
    assert {p["name"] for p in substring["plugins"]} == {p["name"] for p in glob["plugins"]}
    assert substring["plugins"]


def test_unknown_group_reports_error_as_a_dict():
    """Not a list holding an error dict: a listing shape must not double as a refusal."""
    result = list_plugins("robovast.not_a_real_group")
    assert "error" in result and "robovast.variation_types" in result["error"]


def test_get_plugin_details_includes_parameter_schema():
    details = get_plugin_details("robovast.variation_types", "ParameterVariationList")
    params = {p["name"]: p for p in details.get("parameters", [])}
    # An agent authoring a .vast reads this to learn where a factor can land, so both
    # channels must be here -- and the retired `name` must not, since a schema offering a
    # key that is always an error is what sends an agent round the validate loop twice.
    assert {"scenario", "sim", "values"} <= set(params)
    assert "name" not in params
    assert params["scenario"]["type"] == "str | list[str] | dict[str, str] | None"
    assert params["sim"]["type"] == "str | list[str] | dict[str, str] | None"
    assert params["values"]["type"] == "list[float | int | bool | dict | list | str]"
    assert params["values"]["required"] is True


def test_get_plugin_details_omits_parameters_without_model():
    # An MCP plugin declares no CONFIG_CLASS/PARAMS_MODEL parameter model.
    details = get_plugin_details("robovast.mcp_plugins", "reference")
    assert "error" not in details
    assert "parameters" not in details


def test_get_plugin_details_unknown_plugin_names_what_the_group_has():
    """A spelling is the usual cause, so the refusal carries the alternatives -- as the
    tools that refuse an unknown container or campaign do."""
    installed = {p["name"] for p in
                 list_plugins(group="robovast.variation_types")["plugins"]}
    error = get_plugin_details("robovast.variation_types", "NotAPlugin")["error"]
    assert "NotAPlugin" in error
    assert installed & set(error.replace(",", " ").split()), error


def test_get_plugin_details_refuses_an_unknown_group_as_a_group():
    """A mistyped group otherwise came back as "no such plugin in <group>", which reads
    as a verdict about the plugin when the lookup never had a group to look in."""
    error = get_plugin_details("robovast.variation_typos", "OneOfValues")["error"]
    assert "unknown plugin group" in error
    assert "robovast.variation_types" in error
    # The verdict the listing reaches, so the two tools cannot disagree about which
    # groups exist.
    assert "unknown plugin group" in list_plugins(group="robovast.variation_typos")["error"]
