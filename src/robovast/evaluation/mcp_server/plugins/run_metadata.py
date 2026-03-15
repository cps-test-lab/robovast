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

"""MCP plugin for browsing individual run results.

A run is a single execution of a configuration.  It does not have its own
configuration or input files — those are inherited from the configuration
(and transitively from the campaign).  The tools here expose run-specific
*output*: test results, system information, and output files produced
during execution.
"""

import logging
from datetime import datetime

from fastmcp import FastMCP

from robovast.evaluation.mcp_server import results_resolver

from ..plugin_common import _get_config_by_identifier_or_name, _read_text_paginated

logger = logging.getLogger(__name__)


# -- Helpers -----------------------------------------------------------------


def _get_test_result_entry(campaign_id: str, configuration_id: str, run: int) -> dict | None:
    """Look up the test_result entry for a specific run from metadata.yaml."""
    config_entry = _get_config_by_identifier_or_name(campaign_id, configuration_id)
    if config_entry is None:
        return None
    config_name = config_entry.get("name", configuration_id)
    run_dir = f"{config_name}/{run}"
    for tr in config_entry.get("test_results", []):
        if tr.get("dir") == run_dir:
            return tr
    return None


# -- Tool functions ----------------------------------------------------------


def get_run_details(
    campaign_id: str, configuration_id: str, run: int,
) -> dict:
    """Get test result details for a single run.

    Returns pass/fail status, start time, end time, and instance info.

    Args:
        campaign_id: Campaign name.
        configuration_id: Configuration name.
        run: Run number (e.g. ``0``).
    """
    tr = _get_test_result_entry(campaign_id, configuration_id, run)
    if tr is None:
        return {"run": run, "error": "Run not found in metadata.yaml."}

    passed = tr.get("success")
    if passed is not None:
        passed = str(passed).lower() == "true"

    start_time = tr.get("start_time")
    end_time = tr.get("end_time")
    duration = None
    if start_time and end_time:
        try:
            dt_start = datetime.fromisoformat(start_time).replace(tzinfo=None)
            dt_end = datetime.fromisoformat(end_time).replace(tzinfo=None)
            duration = (dt_end - dt_start).total_seconds()
        except (ValueError, TypeError):
            pass

    result: dict = {
        "run": run,
        "success": passed,
        "start_time": start_time,
        "end_time": end_time,
        "duration_s": duration,
    }

    output_files = tr.get("output_files", [])
    result["output_files_count"] = len(output_files)

    sysinfo = tr.get("sysinfo", {})
    if sysinfo:
        result["available_cpus"] = sysinfo.get("available_cpus", "unknown")
        result["available_mem"] = sysinfo.get("available_mem_gb", "unknown")

    return result


def get_run_sysinfo(
    campaign_id: str, configuration_id: str, run: int,
) -> dict:
    """Get system information recorded during a run.

    Returns platform, CPU, memory, and other host details captured at
    execution time.

    Args:
        campaign_id: Campaign name.
        configuration_id: Configuration name.
        run: Run number (e.g. ``0``).
    """
    tr = _get_test_result_entry(campaign_id, configuration_id, run)
    if tr is None:
        return {"error": "Run not found in metadata.yaml."}
    sysinfo = tr.get("sysinfo")
    if not sysinfo:
        return {"error": "sysinfo not available."}
    return sysinfo


def list_run_additional_output_files(
    campaign_id: str, configuration_id: str, run: int,
) -> list[str]:
    """List additional output files of a single run, that are not already provided
    through other tools like ``list_run_data_tables``, ``query_run_data_tables`` or ``query_run_log``.

    These files are produced during execution and postprocessing
    (e.g. test results, rosbags). 

    Args:
        campaign_id: Campaign name.
        configuration_id: Configuration name.
        run: Run number (e.g. ``0``).
    """
    config_entry = _get_config_by_identifier_or_name(campaign_id, configuration_id)
    tr = _get_test_result_entry(campaign_id, configuration_id, run)
    if tr is not None and tr.get("output_files"):
        config_name = config_entry.get("name", configuration_id) if config_entry else configuration_id
        prefix = f"{config_name}/{run}/"
        files = [
            f[len(prefix):] if f.startswith(prefix) else f
            for f in tr["output_files"]
        ]
        return [
            f for f in files
            if not f.lower().endswith(".csv")
            and not f.startswith("logs/")
        ]
    return []


def get_run_output_file(
    campaign_id: str,
    configuration_id: str,
    run: int,
    file_name: str,
    lines: int = 100,
    offset: int = 0,
) -> dict:
    """Read a single output file from a run.

    Returns paginated text content. Binary files are not returned.

    Args:
        campaign_id: Campaign name.
        configuration_id: Configuration name.
        run: Run number (e.g. ``0``).
        file_name: Relative path within the run directory
            (e.g. ``"out.csv"``, ``"logs/system.log"``).
        lines: Maximum number of lines to return (default 100).
        offset: Line offset to start reading from (default 0).
    """
    run_path = results_resolver.resolve_run_path(campaign_id, configuration_id, run)
    config_entry = _get_config_by_identifier_or_name(campaign_id, configuration_id)
    config_name = config_entry.get("name", configuration_id) if config_entry else configuration_id
    prefix = f"{config_name}/{run}/"
    if file_name.startswith(prefix):
        file_name = file_name[len(prefix):]
    path = run_path / file_name
    if not path.exists():
        return {"error": f"File not found: {file_name} in run {run}"}
    return _read_text_paginated(path, lines, offset)


# -- Plugin class ------------------------------------------------------------

_TOOLS = [
    get_run_details,
    get_run_sysinfo,
    list_run_additional_output_files,
    get_run_output_file,
]


class RunMetadataPlugin:
    """Expose run-level results as MCP tools."""

    name = "run_metadata"

    def register(self, mcp: FastMCP) -> None:
        """Register all tool functions with the MCP server."""
        for fn in _TOOLS:
            mcp.tool()(fn)
