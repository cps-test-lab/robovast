# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the plugin_metadata MCP tools."""

from robovast.mcp_server.plugins.plugin_metadata import (get_plugin_details,
                                                         list_plugins)


def test_list_plugins_no_group_spans_all_groups():
    """Regression: the loop used to ``return`` after the first group only."""
    records = list_plugins("")
    groups = {r.get("group") for r in records if "group" in r}
    # More than one extension group has plugins registered, so an all-groups
    # listing must contain more than one distinct group.
    assert len(groups) > 1
    # Variation types are always registered by the base package.
    assert "robovast.variation_types" in groups


def test_list_plugins_single_group_filters():
    records = list_plugins("robovast.variation_types")
    assert records, "expected variation plugins to be registered"
    assert all(r["group"] == "robovast.variation_types" for r in records)


def test_list_plugins_unknown_group_reports_error():
    result = list_plugins("robovast.not_a_real_group")
    assert result == [{"error": "No plugins found in group 'robovast.not_a_real_group'."}]


def test_get_plugin_details_includes_parameter_schema():
    details = get_plugin_details("robovast.variation_types", "ParameterVariationList")
    params = {p["name"]: p for p in details.get("parameters", [])}
    assert "name" in params and "values" in params
    assert params["name"]["type"] == "str | list[str]"
    assert params["values"]["type"] == "list[float | int | bool | dict | list]"
    assert params["name"]["required"] is True
    assert params["values"]["required"] is True


def test_get_plugin_details_omits_parameters_without_model():
    # An MCP plugin declares no CONFIG_CLASS/PARAMS_MODEL parameter model.
    details = get_plugin_details("robovast.mcp_plugins", "reference")
    assert "error" not in details
    assert "parameters" not in details


def test_get_plugin_details_unknown_plugin_reports_error():
    details = get_plugin_details("robovast.variation_types", "NotAPlugin")
    assert details == {
        "error": "No plugin 'NotAPlugin' found in group 'robovast.variation_types'."}
