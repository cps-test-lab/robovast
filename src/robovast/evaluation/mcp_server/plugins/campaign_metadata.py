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

"""MCP plugin for browsing campaign-level results.

Provides tools for listing campaigns, reading campaign metadata,
scenario descriptions, run files, transient files, and configurations.
"""

import logging
from typing import Any

import yaml
from mcp.server.fastmcp import FastMCP

from robovast.evaluation.mcp_server import results_resolver

from ..plugin_common import _list_files_relative, _read_text_paginated, read_campaign_metadata

logger = logging.getLogger(__name__)


# -- Tool functions ----------------------------------------------------------


def list_campaigns(limit: int = 20, offset: int = 0) -> list[dict]:
    """List available campaigns with their metadata.

    Returns a paginated list of campaign records containing campaign_id
    and all metadata fields that are not lists of dicts, keeping response
    size manageable.

    Args:
        limit: Maximum number of campaigns to return (default 20).
        offset: Number of campaigns to skip (default 0).
    """
    campaigns = []
    for d in results_resolver.list_campaigns():
        campaign_record = {"campaign_id": d.name}
        
        campaigns.append(campaign_record)
    return campaigns[offset : offset + limit]


def get_campaign_summary(campaign_id: str) -> dict:
    """Get aggregated statistics about a campaign.

    Returns number of configurations, runs per configuration,
    success/fail/unknown counts, the 3 worst-performing configs,
    and scenario parameters.

    Args:
        campaign_id: Campaign name (e.g. ``campaign-2026-03-04-152130``).
    """
    campaign_path = results_resolver.resolve_campaign_path(campaign_id)
    data = read_campaign_metadata(campaign_path)

    exec_info = data.get("execution", {})
    configurations = data.get("configurations", [])

    configs_info: list[dict[str, Any]] = []
    total_runs = 0
    total_success = 0
    total_failed = 0
    total_unknown = 0

    for c in configurations:
        test_results = c.get("test_results", [])
        c_success = sum(1 for r in test_results if str(r.get("success", "")).lower() == "true")
        c_failed = sum(1 for r in test_results if str(r.get("success", "")).lower() == "false")
        c_unknown = len(test_results) - c_success - c_failed
        configs_info.append({
            "name": c.get("name"),
            "num_runs": len(test_results),
            "success": c_success,
            "failed": c_failed,
            "unknown": c_unknown,
            "scenario_parameters": c.get("config", {}),
        })
        total_runs += len(test_results)
        total_success += c_success
        total_failed += c_failed
        total_unknown += c_unknown

    worst = sorted(configs_info, key=lambda c: (-(c["failed"] + c["unknown"]), c["name"]))[:3]

    return {
        "campaign_id": campaign_id,
        "num_configs": len(configurations),
        "num_runs": total_runs,
        "num_success": total_success,
        "num_failed": total_failed,
        "num_unknown": total_unknown,
        "execution_time": exec_info.get("execution_time"),
        "robovast_version": exec_info.get("robovast_version"),
        "execution_type": exec_info.get("execution_type"),
        "image": exec_info.get("image"),
        "image_revision": exec_info.get("image_revision"),
        "worst_configs": worst,
    }


def get_campaign_scenario(campaign_id: str) -> str:
    """Return the full scenario description used within a campaign.

    Args:
        campaign_id: Campaign name.
    """
    config_dir = results_resolver.resolve_campaign_path(campaign_id) / "_config"
    scenario = config_dir / "scenario.osc"
    if not scenario.exists():
        return "No scenario.osc found in campaign _config/."
    return scenario.read_text(encoding="utf-8")


def get_campaign_scenario_parameters(campaign_id: str, max_entries: int = 20) -> dict:
    """Return scenario parameters and their unique values across all configurations.

    Aggregates scenario parameters from every configuration in the campaign
    and shows the distinct values each parameter takes, giving a concise
    overview of what varies across the campaign.

    Args:
        campaign_id: Campaign name.
        max_entries: Maximum number of distinct values to return per parameter
            (default 20). Use a smaller value to limit response size.
    """
    campaign_path = results_resolver.resolve_campaign_path(campaign_id)
    data = read_campaign_metadata(campaign_path)
    configurations = data.get("configurations", [])

    param_values: dict[str, list] = {}
    for c in configurations:
        params = c.get("config", {})
        if not isinstance(params, dict):
            continue
        for key, val in params.items():
            seen = param_values.setdefault(key, [])
            if val not in seen:
                seen.append(val)

    return {key: vals[:max_entries] for key, vals in param_values.items()}


def list_campaign_run_files(campaign_id: str) -> list[str]:
    """List files available during every run in every configuration.

    These files (alongside the docker image and scenario) define
    the execution during a run.

    Args:
        campaign_id: Campaign name.
    """
    campaign_path = results_resolver.resolve_campaign_path(campaign_id)
    data = read_campaign_metadata(campaign_path)
    run_files = data.get("run_files")
    if run_files is not None:
        return run_files
    return _list_files_relative(campaign_path / "_config")


def get_campaign_run_file(
    campaign_id: str, file_name: str, lines: int = 100, offset: int = 0,
) -> dict:
    """Read a single campaign run file (or a page of it).

    Returns paginated text content. Binary files are not returned.

    Args:
        campaign_id: Campaign name.
        file_name: file name (e.g. ``"files/growth_sim.py"``).
        lines: Maximum number of lines to return (default 100).
        offset: Line offset to start reading from (default 0).
    """
    config_dir = results_resolver.resolve_campaign_path(campaign_id) / "_config"
    path = config_dir / file_name
    if not path.exists():
        return {"error": f"File not found: {file_name}"}
    return _read_text_paginated(path, lines, offset)


def get_campaign_config(
    campaign_id: str, section: str | None = None,
) -> str:
    """Return the YAML-formatted VAST configuration file for a campaign.

    The vast file has four sections:
    - ``configuration``: defines campaign configurations (parameter variations)
    - ``execution``: defines how configurations are executed (local/cluster)
    - ``results_processing``: defines post-execution data handling
    - ``evaluation``: defines evaluation parameters

    Args:
        campaign_id: Campaign name.
        section: Optional section to return. If omitted, the full
            file is returned.
    """
    config_dir = results_resolver.resolve_campaign_path(campaign_id) / "_config"
    vast_files = list(config_dir.glob("*.vast"))
    if not vast_files:
        return "No .vast configuration file found in campaign _config/."
    content = yaml.safe_load(vast_files[0].read_text(encoding="utf-8"))
    if section is not None:
        valid_sections = {"configuration", "execution", "results_processing", "evaluation"}
        if section not in valid_sections:
            return f"Invalid section '{section}'. Valid sections: {', '.join(sorted(valid_sections))}"
        section_data = content.get(section)
        if section_data is None:
            return f"Section '{section}' not found in vast file."
        return yaml.dump(section_data, default_flow_style=False)
    return yaml.dump(content, default_flow_style=False)


def get_campaign_execution_details(campaign_id: str) -> dict:
    """Return the full execution details for a campaign.

    Contains comprehensive information about how the campaign was executed,
    including execution environment, timing, orchestration setup, resource
    allocation, cluster info, and other runtime configuration.

    Args:
        campaign_id: Campaign name.
    """
    campaign_path = results_resolver.resolve_campaign_path(campaign_id)
    data = read_campaign_metadata(campaign_path)
    execution = data.get("execution", {})
    if not execution:
        return {"error": "No execution details found in campaign metadata."}
    return execution


def get_campaign_postprocessing_details(campaign_id: str, limit: int = 20, offset: int = 0) -> dict:
    """Return postprocessing details for a campaign.

    Returns structured postprocessing entries with output file, source files,
    plugin name, and parameters for each postprocessing step.

    Args:
        campaign_id: Campaign name.
        limit: Maximum number of entries to return (default 20).
        offset: Number of entries to skip (default 0).
    """
    campaign_path = results_resolver.resolve_campaign_path(campaign_id)
    data = read_campaign_metadata(campaign_path)
    postprocessing = data.get("postprocessing", {})
    entries = postprocessing.get("entries", [])
    page = entries[offset : offset + limit]
    return {
        "generated_by": postprocessing.get("generated_by"),
        "total_entries": len(entries),
        "returned_entries": len(page),
        "offset": offset,
        "entries": page,
    }


def list_campaign_configurations(
    campaign_id: str, limit: int = 20, offset: int = 0,
) -> list[dict]:
    """List fully resolved configurations of a campaign.

    Fully resolved means the result of the configuration variation
    defined in the vast configuration file. Returns name and identifier. 
    Configurations with identical identifiers are considered identical 
    in terms of configuration and input files.

    Args:
        campaign_id: Campaign name.
        limit: Maximum number of configurations to return (default 20).
        offset: Number of configurations to skip (default 0).
    """
    campaign_path = results_resolver.resolve_campaign_path(campaign_id)
    data = read_campaign_metadata(campaign_path)
    configs = data.get("configurations", [])
    page = configs[offset : offset + limit]
    return [
        {
            "name": c.get("name"),
            "identifier": c.get("config_identifier"),
        }
        for c in page
    ]


def get_campaign_agents(campaign_id: str) -> list[dict]:
    """Return the agents defined in the campaign and their configuration files.

    Each entry contains an ``id`` and a list of ``configuration_files``
    used to configure that agent during execution.

    Args:
        campaign_id: Campaign name.
    """
    campaign_path = results_resolver.resolve_campaign_path(campaign_id)
    data = read_campaign_metadata(campaign_path)
    return data.get("metadata", {}).get("agents", [])


def list_campaign_transient_files(campaign_id: str) -> list[str]:
    """List transient files of a campaign.

    Transient files are created during configuration processing,
    execution, and postprocessing.

    Args:
        campaign_id: Campaign name.
    """
    transient_dir = results_resolver.resolve_campaign_path(campaign_id) / "_transient"
    return _list_files_relative(transient_dir)


def get_campaign_transient_file(
    campaign_id: str, file_name: str, lines: int = 100, offset: int = 0,
) -> dict:
    """Read a single campaign transient file (or a page of it).

    Returns paginated text content. Binary files are not returned.

    Args:
        campaign_id: Campaign name.
        file_name: Relative path within the campaign transient files.
        lines: Maximum number of lines to return (default 100).
        offset: Line offset to start reading from (default 0).
    """
    transient_dir = results_resolver.resolve_campaign_path(campaign_id) / "_transient"
    path = transient_dir / file_name
    if not path.exists():
        return {"error": f"File not found: {file_name}"}
    return _read_text_paginated(path, lines, offset)


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    list_campaigns,
    get_campaign_summary,
    get_campaign_scenario,
    get_campaign_scenario_parameters,
    list_campaign_run_files,
    get_campaign_run_file,
    get_campaign_config,
    get_campaign_execution_details,
    get_campaign_postprocessing_details,
    list_campaign_configurations,
    get_campaign_agents,
    list_campaign_transient_files,
    get_campaign_transient_file,
]


class CampaignMetadataPlugin:
    """Expose campaign-level results as MCP tools."""

    name = "campaign_metadata"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
