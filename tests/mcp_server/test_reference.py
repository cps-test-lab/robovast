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
