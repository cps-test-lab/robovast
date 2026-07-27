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

"""MCP plugin for configuration-level metadata.

Answers what a configuration *is*: its summary, the scenario parameters it resolved to,
and the variation steps that produced it. Its files are read through the address space
(``/results/<campaign_id>/<config_name>/…``), not from here.
"""

import logging

from fastmcp import FastMCP

from ..plugin_common import _get_config_by_identifier_or_name

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

# A configuration's files live at ``/results/<campaign_id>/<config_name>/`` — its
# ``_config/`` and ``_transient/`` are read with the generic file tools. Note the
# listings there are of the **directory**, where the old tools read a ``metadata.yaml``
# list written by postprocessing: the address space answers before that exists.
_TOOLS = [
    get_configuration_summary,
    get_configuration_scenario_parameter,
    get_configuration_variations,
]


class ConfigurationMetadataPlugin:
    """Expose configuration-level results as MCP tools."""

    name = "configuration_metadata"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
