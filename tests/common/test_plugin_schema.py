# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared plugin parameter-schema introspection helper.

The same helper backs the ``get_plugin_details`` MCP tool and the
``vast configuration plugin-info`` CLI command, so both surfaces stay in sync.
"""

from click.testing import CliRunner

from robovast.common.plugin_schema import (describe_pydantic_model,
                                           plugin_parameter_schema)
from robovast.configuration.configuration_utils.cli import configuration


def test_describe_model_renders_types_and_required():
    from robovast.common.variation.parameter_variation import \
        ParameterVariationListConfig

    fields = {f["name"]: f for f in describe_pydantic_model(ParameterVariationListConfig)}
    assert fields["name"]["type"] == "str | list[str]"
    assert fields["name"]["required"] is True
    assert fields["values"]["type"] == "list[float | int | bool | dict | list]"
    assert fields["values"]["required"] is True


def test_describe_model_none_for_non_model():
    assert describe_pydantic_model(None) is None
    assert describe_pydantic_model(str) is None


def test_plugin_parameter_schema_variation():
    fields = plugin_parameter_schema("robovast.variation_types", "ParameterVariationList")
    assert {f["name"] for f in fields} == {"name", "values"}


def test_plugin_parameter_schema_none_without_model():
    # A postprocessing command declares no CONFIG_CLASS/PARAMS_MODEL model.
    assert plugin_parameter_schema("robovast.postprocessing_commands", "command") is None


def test_plugin_parameter_schema_unknown_plugin():
    assert plugin_parameter_schema("robovast.variation_types", "NotAPlugin") is None


def test_cli_plugin_info_prints_schema():
    result = CliRunner().invoke(
        configuration, ["plugin-info", "ParameterVariationList"])
    assert result.exit_code == 0
    assert "list[float | int | bool | dict | list]" in result.output
    assert "str | list[str]" in result.output


def test_cli_plugin_info_unknown_exits_nonzero():
    result = CliRunner().invoke(configuration, ["plugin-info", "NotAPlugin"])
    assert result.exit_code == 1
    assert "No plugin 'NotAPlugin'" in result.output
