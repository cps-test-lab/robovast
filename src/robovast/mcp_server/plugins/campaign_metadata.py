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

"""MCP plugin for campaign-level metadata.

Lists campaigns and answers the parsed questions about one: its summary, how it was
executed, what postprocessing produced, which configurations it holds.

Campaign **files** are not here — they are reached through the one address space
(``read_file`` / ``list_files`` over ``/results/<campaign_id>/<path>``), so there is a
single way to name a file rather than one per scope.
"""

import logging
from typing import Any

from fastmcp import FastMCP

from robovast.common.store import (read_campaign_created_at,
                                   read_campaign_description)
from robovast.mcp_server import results_resolver

from ..plugin_common import read_campaign_metadata

logger = logging.getLogger(__name__)


# -- Tool functions ----------------------------------------------------------


def _newest_first(entry: dict) -> tuple:
    """Sort key ordering campaigns by recorded start time, unknown last.

    Used with ``reverse=True``. Deliberately not the campaign id: that is
    ``<name>-<timestamp>`` with a user-supplied name, so sorting on it orders
    alphabetically by name. The id only breaks ties between identical start times.
    """
    started = entry.get("started_at")
    return (started is not None, started or "", entry["campaign_id"])


def list_campaigns(limit: int = 20, offset: int = 0) -> dict:
    """List available campaigns, newest first.

    Ordered by the campaign's recorded start time (``campaign.created_at`` in its
    ``campaign.db``), so ``limit`` returns the most recent campaigns — the first page
    answers "what did I just run?". A campaign whose start time is unrecorded sorts
    last, with ``started_at`` null.

    Each entry carries the ``description`` its launcher gave (see ``start_campaign``),
    omitted for a campaign started without one — that text is the only thing telling two
    same-day ``campaign-<timestamp>`` ids apart.

    Args:
        limit: Maximum number of campaigns to return (default 20).
        offset: Number of campaigns to skip (default 0).
    """
    with_metadata: list[dict] = []
    missing_metadata: list[dict] = []

    for d in results_resolver.list_campaigns():
        metadata = read_campaign_metadata(d)
        entry = {"campaign_id": d.name, "started_at": read_campaign_created_at(d)}
        description = read_campaign_description(d)
        if description:
            entry["description"] = description
        if metadata:
            exec_info = metadata.get("execution", {})
            entry["execution_time"] = exec_info.get("execution_time")
            with_metadata.append(entry)
        else:
            missing_metadata.append(entry)

    with_metadata.sort(key=_newest_first, reverse=True)
    missing_metadata.sort(key=_newest_first, reverse=True)
    page = with_metadata[offset : offset + limit]
    result: dict = {
        "campaigns": page,
        "total": len(with_metadata),
        "offset": offset,
    }
    if missing_metadata:
        result["missing_metadata"] = missing_metadata
        result["missing_metadata_hint"] = (
            "These campaigns have no metadata. "
            "Run 'vast results postprocess' to generate it."
        )
    return result


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


# -- Plugin class ------------------------------------------------------------

# Files are not here: every campaign file is reached through the one address space
# (``read_file`` / ``list_files`` over ``/results/<campaign_id>/<path>``) — the scenario
# is ``_config/scenario.osc``, the run files are ``_config/``, the transient files are
# ``_transient/``. What stays are the *metadata views*: parsed answers that are not a
# file. The resolved per-config params come from get_configuration_variations, and the
# whole .vast from the run_data SQL tools (campaign.config_json).
_TOOLS = [
    list_campaigns,
    get_campaign_summary,
    get_campaign_execution_details,
    get_campaign_postprocessing_details,
    list_campaign_configurations,
]


class CampaignMetadataPlugin:
    """Expose campaign-level results as MCP tools."""

    name = "campaign_metadata"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
