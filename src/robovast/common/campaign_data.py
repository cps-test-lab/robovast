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

"""Shared data-gathering functions for campaign results.

These functions provide a common interface for reading campaign data,
used by both MCP plugins and the FAIR metadata generator.
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def read_execution_metadata(campaign_dir: Path) -> dict[str, Any]:
    """Read execution metadata from ``_execution/execution.yaml``.

    Args:
        campaign_dir: Path to the ``campaign-<id>`` directory.

    Returns:
        Dictionary with execution_time, robovast_version, runs,
        execution_type, image, cluster_info, etc.

    Raises:
        FileNotFoundError: If execution.yaml does not exist.
    """
    path = campaign_dir / "_execution" / "execution.yaml"
    if not path.exists():
        raise FileNotFoundError(f"execution.yaml not found in {campaign_dir}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_scenario_config(config_dir: Path) -> dict[str, Any]:
    """Read scenario configuration from ``_config/scenario.config``.

    Unwraps the single-key wrapper (scenario name) that wraps the
    actual parameter values.

    Args:
        config_dir: Path to the configuration directory
            (e.g. ``campaign-<id>/<config-name>``).

    Returns:
        Dictionary of resolved parameter key-value pairs.

    Raises:
        FileNotFoundError: If scenario.config does not exist.
    """
    path = config_dir / "_config" / "scenario.config"
    if not path.exists():
        raise FileNotFoundError(f"scenario.config not found in {config_dir}")
    with open(path, "r", encoding="utf-8") as f:
        content = yaml.safe_load(f)

    # Unwrap single-key wrapper (e.g. {test_scenario: {param: val}} → {param: val})
    if isinstance(content, dict) and len(content) == 1:
        content = next(iter(content.values()))

    return content


def read_test_result(run_dir: Path) -> dict[str, Any]:
    """Parse JUnit test result from ``test.xml``.

    Args:
        run_dir: Path to the run directory (e.g. ``campaign-<id>/<config>/0``).

    Returns:
        Dictionary with keys: passed (bool), duration_sec (float),
        start_time (ISO string), errors (int), failures (int), tests (int).

    Raises:
        FileNotFoundError: If test.xml does not exist.
    """
    path = run_dir / "test.xml"
    if not path.exists():
        raise FileNotFoundError(f"test.xml not found in {run_dir}")

    tree = ET.parse(path)
    root = tree.getroot()

    errors = int(root.get("errors", "0"))
    failures = int(root.get("failures", "0"))
    tests = int(root.get("tests", "0"))

    testcase = root.find("testcase")
    duration = float(testcase.get("time", "0")) if testcase is not None else 0.0

    # Extract start_time from properties
    start_time_iso = None
    if testcase is not None:
        properties = testcase.find("properties")
        if properties is not None:
            for prop in properties.findall("property"):
                if prop.get("name") == "start_time":
                    ts = float(prop.get("value", "0"))
                    start_time_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
                    break

    # Extract failure message if present
    failure_message = None
    if testcase is not None:
        failure_elem = testcase.find("failure")
        if failure_elem is not None:
            failure_message = failure_elem.get("message") or failure_elem.text

    return {
        "success": errors == 0 and failures == 0,
        "duration_sec": duration,
        "start_time": start_time_iso,
        "errors": errors,
        "failures": failures,
        "tests": tests,
        "failure_message": failure_message,
    }


def read_sysinfo(run_dir: Path) -> dict[str, Any]:
    """Read system information from ``sysinfo.yaml``.

    Args:
        run_dir: Path to the run directory.

    Returns:
        Dictionary with platform, CPU, memory, etc.

    Raises:
        FileNotFoundError: If sysinfo.yaml does not exist.
    """
    path = run_dir / "sysinfo.yaml"
    if not path.exists():
        raise FileNotFoundError(f"sysinfo.yaml not found in {run_dir}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_resolved_configurations(campaign_dir: Path) -> dict[str, Any]:
    """Read fully resolved configurations from ``_transient/configurations.yaml``.

    Args:
        campaign_dir: Path to the ``campaign-<id>`` directory.

    Returns:
        Dictionary with configs list, execution info, run_files, etc.

    Raises:
        FileNotFoundError: If configurations.yaml does not exist.
    """
    path = campaign_dir / "_transient" / "configurations.yaml"
    if not path.exists():
        raise FileNotFoundError(f"configurations.yaml not found in {campaign_dir}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def list_output_files(run_dir: Path) -> list[str]:
    """List all files in a run directory as relative paths.

    Args:
        run_dir: Path to the run directory.

    Returns:
        Sorted list of file paths relative to run_dir.
    """
    if not run_dir.is_dir():
        return []
    files = []
    for f in run_dir.rglob("*"):
        if f.is_file():
            files.append(str(f.relative_to(run_dir)))
    return sorted(files)


def get_vast_configuration_info(
    campaign_dir: Path,
    config_dirs: list[Path] | None = None,
    list_runs_fn=None,
) -> dict[str, Any]:
    """Gather important statistics about a VAST campaign configuration.

    This function collects key metrics from a campaign, including the number
    of jobs/configurations, runs, test results, and execution details.

    Args:
        campaign_dir: Path to the ``campaign-<id>`` directory.
        config_dirs: Optional list of configuration directory paths. If not
            provided, they will be discovered by excluding reserved directories.
        list_runs_fn: Optional callback function that takes a config_dir Path
            and returns a list of run directory Paths. If not provided, run
            directories are discovered by looking for numeric subdirectories.

    Returns:
        Dictionary containing:
        - campaign_name: str - Name of the campaign directory
        - num_configs: int - Number of job configurations
        - num_runs: int - Total number of runs across all configs
        - num_passed: int - Number of passed tests
        - num_failed: int - Number of failed tests
        - num_errors: int - Number of errors
        - total_duration_sec: float - Total execution time in seconds
        - execution_info: dict - Execution metadata (version, type, image, etc.)
        - configs: list[dict] - Per-configuration statistics

    Raises:
        FileNotFoundError: If required campaign files are missing.
    """
    # Get execution metadata
    exec_meta = read_execution_metadata(campaign_dir)

    # Discover config directories if not provided
    if config_dirs is None:
        reserved = {"_config", "_execution", "_transient"}
        config_dirs = [
            d for d in campaign_dir.iterdir()
            if d.is_dir() and d.name not in reserved and not d.name.startswith(".")
        ]
        config_dirs = sorted(config_dirs)

    # Default run directory discovery
    def default_list_runs(cfg_dir: Path) -> list[Path]:
        return sorted(
            [d for d in cfg_dir.iterdir() if d.is_dir() and d.name.isdigit()],
            key=lambda x: int(x.name)
        )

    run_discovery_fn = list_runs_fn or default_list_runs

    # Count configs and gather per-config stats
    configs_info = []
    num_runs = 0
    num_passed = 0
    num_failed = 0
    num_errors = 0
    total_duration = 0.0

    for config_dir in config_dirs:
        config_name = config_dir.name
        run_dirs = run_discovery_fn(config_dir)

        config_runs = len(run_dirs)
        config_passed = 0
        config_failed = 0
        config_errors = 0
        config_duration = 0.0

        for run_dir in run_dirs:
            try:
                result = read_test_result(run_dir)
                if result["success"]:
                    config_passed += 1
                else:
                    if result["errors"] > 0:
                        config_errors += 1
                    if result["failures"] > 0:
                        config_failed += 1
                config_duration += result.get("duration_sec", 0.0)
            except FileNotFoundError:
                # Run may not have completed
                pass

        configs_info.append({
            "name": config_name,
            "num_runs": config_runs,
            "passed": config_passed,
            "failed": config_failed,
            "errors": config_errors,
            "duration_sec": config_duration,
        })

        num_runs += config_runs
        num_passed += config_passed
        num_failed += config_failed
        num_errors += config_errors
        total_duration += config_duration

    return {
        "campaign_name": campaign_dir.name,
        "num_configs": len(config_dirs),
        "num_runs": num_runs,
        "num_passed": num_passed,
        "num_failed": num_failed,
        "num_errors": num_errors,
        "total_duration_sec": total_duration,
        "execution_info": {
            "execution_time": exec_meta.get("execution_time"),
            "robovast_version": exec_meta.get("robovast_version"),
            "execution_type": exec_meta.get("execution_type"),
            "image": exec_meta.get("image"),
            "cluster_info": exec_meta.get("cluster_info"),
        },
        "configs": configs_info,
    }
