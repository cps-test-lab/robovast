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


#: Campaign terminal-outcome record — the final ``Status`` (phase/error/…) serialized
#: beside ``controller.log`` in ``_execution/``. Written on terminal exit so a controller
#: crash that never builds ``data.db`` still leaves a durable, queryable reason.
_OUTCOME_FILENAME = "outcome.json"


def write_execution_outcome(campaign_root: Path, status) -> None:
    """Persist the campaign's terminal outcome to ``_execution/outcome.json``.

    ``status`` is a :class:`robovast.execution.control_server.Status`; it is stored
    verbatim (``model_dump_json``) so the reader gets the same model back. Used by
    both the local worker and the in-pod controller, so a failed campaign leaves the
    record at the **same** campaign-relative path regardless of backend.
    """
    exec_dir = Path(campaign_root) / "_execution"
    exec_dir.mkdir(parents=True, exist_ok=True)
    (exec_dir / _OUTCOME_FILENAME).write_text(status.model_dump_json(), encoding="utf-8")


def read_execution_outcome(campaign_dir: Path):
    """Read ``_execution/outcome.json`` back into a ``Status``; ``None`` if absent.

    Returns a :class:`robovast.execution.control_server.Status`.
    """
    from robovast.execution.control_server import Status  # lazy: keep this module light
    path = Path(campaign_dir) / "_execution" / _OUTCOME_FILENAME
    if not path.exists():
        return None
    return Status.model_validate_json(path.read_text(encoding="utf-8"))


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

    ``collect_sysinfo.py`` writes it into the **job** directory, which each run
    dir exposes as its ``job`` symlink (``_jobs/batch-<n>/job-<m>/sysinfo.yaml``) —
    on both backends. Older/other layouts kept it in the run dir or its ``logs/``,
    so all three locations are accepted.

    Args:
        run_dir: Path to the run directory.

    Returns:
        Dictionary with platform, CPU, memory, etc.

    Raises:
        FileNotFoundError: If sysinfo.yaml does not exist.
    """
    candidates = [run_dir / "job" / "sysinfo.yaml",
                  run_dir / "sysinfo.yaml",
                  run_dir / "logs" / "sysinfo.yaml"]
    path = next((p for p in candidates if p.exists()), None)
    if path is None:
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


# Campaign-level directories that are not configuration directories.
RESERVED_CAMPAIGN_DIRS = {"_config", "_execution", "_transient", "_jobs", "_control"}


def list_config_dirs(campaign_dir: Path) -> list[Path]:
    """Configuration directories directly under a campaign dir (sorted)."""
    return sorted(
        d for d in Path(campaign_dir).iterdir()
        if d.is_dir() and d.name not in RESERVED_CAMPAIGN_DIRS and not d.name.startswith(".")
    )


def list_run_dirs(config_dir: Path) -> list[Path]:
    """Numeric run directories under a config dir, ascending."""
    try:
        return sorted(
            (d for d in Path(config_dir).iterdir() if d.is_dir() and d.name.isdigit()),
            key=lambda d: int(d.name),
        )
    except (OSError, ValueError):
        return []


def aggregate_run_status(run_dirs: list[Path]) -> str:
    """Aggregate per-run pass/fail (from each run's ``test.xml``) into one status.

    Returns ``passed`` (all runs passed), ``failed`` (none passed), ``mixed``
    (some of each), or ``no_runs`` (no runs present). A run missing ``test.xml``
    counts against the config.
    """
    passed = failed = 0
    for run_dir in run_dirs:
        try:
            result = read_test_result(run_dir)
        except FileNotFoundError:
            failed += 1
            continue
        if result["success"]:
            passed += 1
        else:
            failed += 1
    if not run_dirs:
        return "no_runs"
    if failed == 0:
        return "passed"
    if passed == 0:
        return "failed"
    return "mixed"
