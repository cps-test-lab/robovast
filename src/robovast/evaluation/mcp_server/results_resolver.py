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

"""Path resolution helpers for MCP plugins that access campaign results."""

from pathlib import Path

from robovast.common.cli.project_config import ProjectConfig

# Directories at campaign level that are not config directories
_RESERVED_DIRS = {"_config", "_execution", "_transient"}


def resolve_results_dir() -> Path:
    """Resolve the results directory from the project configuration.

    Uses ``ProjectConfig.load()`` to find the ``.robovast_project`` file
    and read the configured ``results_dir``.

    Returns:
        Absolute path to the results directory.

    Raises:
        ValueError: If the project is not initialized or results_dir is missing.
    """
    config = ProjectConfig.load()
    if config is None or not config.results_dir:
        raise ValueError(
            "Project not initialized or results_dir not configured. "
            "Run 'vast init <config-file>' first."
        )
    path = Path(config.results_dir)
    if not path.is_dir():
        raise ValueError(f"Results directory does not exist: {path}")
    return path


def resolve_campaign_path(campaign: str) -> Path:
    """Build and validate the path to a campaign directory.

    Args:
        campaign: Campaign name (e.g. ``campaign-2026-03-04-152130``).

    Returns:
        Absolute path to the campaign directory.

    Raises:
        ValueError: If the campaign directory does not exist.
    """
    path = resolve_results_dir() / campaign
    if not path.is_dir():
        raise ValueError(f"Campaign {campaign} not found.")
    return path


def resolve_config_path(campaign: str, config: str) -> Path:
    """Build and validate the path to a configuration directory.

    Args:
        campaign: Campaign name.
        config: Configuration name (e.g. ``hospital10m0o-1-42-1-3``).

    Returns:
        Absolute path to the configuration directory.

    Raises:
        ValueError: If the config directory does not exist.
    """
    path = resolve_campaign_path(campaign) / config
    if not path.is_dir():
        raise ValueError(f"Configuration {config} not found in campaign {campaign}")
    return path


def resolve_run_path(campaign: str, config: str, run: int) -> Path:
    """Build and validate the path to a run directory.

    Args:
        campaign: Campaign name.
        config: Configuration name.
        run: Run number (e.g. ``"0"``).

    Returns:
        Absolute path to the run directory.

    Raises:
        ValueError: If the run directory does not exist.
    """
    path = resolve_config_path(campaign, config) / str(run)
    if not path.is_dir():
        raise ValueError(f"Run {run} not found in configuration {config} of campaign {campaign}")
    return path


def list_campaigns() -> list[Path]:
    """List all campaigns that are available.

    Returns:
        Sorted list of campaigns (directories in results_dir that start with "campaign-").
    """
    results = resolve_results_dir()
    return sorted(
        d for d in results.iterdir()
        if d.is_dir() and d.name.startswith("campaign-")
    )


def list_config_dirs(campaign: str) -> list[Path]:
    """List configuration directories within a campaign.

    Excludes reserved directories (``_config``, ``_execution``, ``_transient``).

    Args:
        campaign: Campaign name.

    Returns:
        Sorted list of paths to configuration directories.
    """
    campaign_path = resolve_campaign_path(campaign)
    return sorted(
        d for d in campaign_path.iterdir()
        if d.is_dir() and d.name not in _RESERVED_DIRS and not d.name.startswith(".")
    )


def list_run_dirs(campaign: str, config: str) -> list[Path]:
    """List numeric run directories within a configuration.

    Args:
        campaign: Campaign name.
        config: Configuration name.

    Returns:
        Sorted list of paths to run directories (sorted numerically).
    """
    config_path = resolve_config_path(campaign, config)
    runs = [
        d for d in config_path.iterdir()
        if d.is_dir() and d.name.isdigit()
    ]
    return sorted(runs, key=lambda d: int(d.name))
