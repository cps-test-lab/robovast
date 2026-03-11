# Copyright (C) 2026 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""MCP plugin for browsing configuration-level results.

Provides tools for inspecting individual configurations, their run files,
and transient output files.
"""

import logging

from mcp.server.fastmcp import FastMCP

from robovast.evaluation.mcp_server import results_resolver

from ..plugin_common import _read_text_paginated, _get_config_by_identifier_or_name

logger = logging.getLogger(__name__)


# -- Tool functions ----------------------------------------------------------


def get_configuration_summary(campaign_id: str, configuration_id: str) -> dict:
    """Get details about a specific configuration.

    Returns scenario parameters, the unique configuration identifier
    (a configuration may be executed across multiple campaigns), and
    per-run test results.

    Args:
        campaign_id: Campaign name.
        configuration_id: Configuration name or identifier (e.g. ``"test-1-1"``).
    """

    config_entry = _get_config_by_identifier_or_name(campaign_id, configuration_id)
    if config_entry is None:
        return {"error": f"Configuration not found: {configuration_id}"}

    config_name = config_entry.get("name")
    config_identifier = config_entry.get("config_identifier")

    runs = []
    for tr in config_entry.get("test_results", []):
        run_num = tr.get("dir", "").split("/")[-1]
        passed = tr.get("success")
        if passed is not None:
            passed = str(passed).lower() == "true"
        run_info: dict = {"run": run_num, "success": passed}
        if tr.get("start_time"):
            run_info["start_time"] = tr["start_time"]
        if tr.get("end_time"):
            run_info["end_time"] = tr["end_time"]
        sysinfo = tr.get("sysinfo")
        if sysinfo:
            run_info["instance_type"] = sysinfo.get("instance_type")
            run_info["cpu_name"] = sysinfo.get("cpu_name")
        runs.append(run_info)

    return {
        "name": config_name,
        "configuration_id": config_identifier,
        "created_at": config_entry.get("created_at"),
        "num_runs": len(runs),
        "num_runs_successful": sum(1 for r in runs if r["success"] is True),
        "num_runs_failed": sum(1 for r in runs if r["success"] is False),
        "runs": runs,
    }


def list_configuration_transient_files(
    campaign_id: str, configuration_id: str,
) -> list[str]:
    """List transient files of a configuration.

    Transient files are created during configuration variatio.
    (e.g. ``"json-ld/coordinate.json"``).

    Args:
        campaign_id: Campaign name.
        configuration_id: Configuration name or identifier.
    """
    config_entry = _get_config_by_identifier_or_name(campaign_id, configuration_id)
    if config_entry is None:
        return []
    config_name = config_entry.get("name", configuration_id)
    prefix = f"{config_name}/_transient/"
    return [
        f[len(prefix):] if f.startswith(prefix) else f
        for f in config_entry.get("transient_files", [])
    ]


def get_configuration_transient_file(
    campaign_id: str,
    configuration_id: str,
    file_name: str,
    lines: int = 100,
    offset: int = 0,
) -> dict:
    """Read a single configuration transient file.

    Returns paginated text content. Binary files are not returned.

    Args:
        campaign_id: Campaign name.
        configuration_id: Configuration name or identifier.
        file_name: path to transient file (e.g. ``"json-ld/coordinate.json"``).
        lines: Maximum number of lines to return (default 100).
        offset: Line offset to start reading from (default 0).
    """
    config_entry = _get_config_by_identifier_or_name(campaign_id, configuration_id)
    if config_entry is None:
        return {"error": f"Configuration not found: {configuration_id}"}
    config_name = config_entry.get("name", configuration_id)
    campaign_path = results_resolver.resolve_campaign_path(campaign_id)
    path = campaign_path / config_name / "_transient" / file_name
    if not path.exists():
        return {"error": f"File not found: {file_name}"}
    return _read_text_paginated(path, lines, offset)


def get_configuration_scenario_parameter(
    campaign_id: str,
    configuration_id: str,
) -> dict:
    """Get the scenario parameter values of a configuration.

    Args:
        campaign_id: Campaign name.
        configuration_id: Configuration name or identifier.
    """
    config_entry = _get_config_by_identifier_or_name(campaign_id, configuration_id)
    if config_entry is None:
        return {"error": f"Configuration not found: {configuration_id}"}
    config = config_entry.get("config", {}) or {}
    return config


def list_configuration_config_files(
    campaign_id: str, configuration_id: str,
) -> list[str]:
    """List config files specific to a configuration.

    These files define the execution for runs within this
    configuration and were created during configuration
    variation.

    Args:
        campaign_id: Campaign name.
        configuration_id: Configuration name or identifier.
    """
    config_entry = _get_config_by_identifier_or_name(campaign_id, configuration_id)
    if config_entry is None:
        return []
    config_name = config_entry.get("name", configuration_id)
    prefix = f"{config_name}/_config/"
    return [
        f[len(prefix):] if f.startswith(prefix) else f
        for f in config_entry.get("config_files", [])
    ]


def get_configuration_config_file(
    campaign_id: str,
    configuration_id: str,
    file_name: str,
    lines: int = 100,
    offset: int = 0,
) -> dict:
    """Read a single configuration config file (or a page of it).

    Returns paginated text content. Binary files are not returned.

    Args:
        campaign_id: Campaign name.
        configuration_id: Configuration name or identifier.
        file_name: Configuration config file name.
        lines: Maximum number of lines to return (default 100).
        offset: Line offset to start reading from (default 0).
    """
    config_entry = _get_config_by_identifier_or_name(campaign_id, configuration_id)
    if config_entry is None:
        return {"error": f"Configuration not found: {configuration_id}"}
    config_name = config_entry.get("name", configuration_id)
    campaign_path = results_resolver.resolve_campaign_path(campaign_id)
    path = campaign_path / config_name / "_config" / file_name
    if not path.exists():
        return {"error": f"File not found: {file_name}"}
    return _read_text_paginated(path, lines, offset)


def get_configuration_variations(campaign_id: str, configuration_id: str) -> list[dict]:
    """Return the variation steps that produced this configuration.

    Each entry describes one variation step: its name, when it started,
    how long it took, and any extra metadata (e.g. scenery-builder image,
    floor-plan file).

    Args:
        campaign_id: Campaign name.
        configuration_id: Configuration name or identifier.
    """
    config_entry = _get_config_by_identifier_or_name(campaign_id, configuration_id)
    if config_entry is None:
        return []
    return config_entry.get("variations", [])


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    get_configuration_summary,
    get_configuration_scenario_parameter,
    get_configuration_variations,
    list_configuration_transient_files,
    get_configuration_transient_file,
    list_configuration_config_files,
    get_configuration_config_file,
]


class ConfigurationMetadataPlugin:
    """Expose configuration-level results as MCP tools."""

    name = "configuration_metadata"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
