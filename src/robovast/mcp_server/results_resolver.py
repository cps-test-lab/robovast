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

import os
from pathlib import Path

from robovast.common.results_root import local_results_root
from robovast.common.execution import is_campaign_dir

# The campaign layout (which dirs are reserved vs. configurations) is defined once
# in robovast.common.campaign_data — see list_config_dirs() below.


def _campaigns_root() -> Path:
    """Where local campaigns live — :func:`local_results_root`, shared with the service.

    One implementation, so this reader and a ``vast serve`` cannot disagree about where a
    campaign is. This is a **local** root: a cluster campaign's home is the object store,
    so results there are reached through the service, not here.
    """
    return local_results_root()


def resolve_results_dir() -> Path:
    """Resolve the local results directory campaign ids are looked up under.

    Returns:
        Absolute path to the results directory.

    Raises:
        ValueError: If that directory does not exist. Not "project not initialized":
            there is no project to initialize any more — a campaign runs a workspace's
            ``.vast`` and results land in the shared root resolved by
            :func:`_campaigns_root`.
    """
    path = _campaigns_root()
    if not path.is_dir():
        raise ValueError(
            f"No local results directory at {path}. Campaigns run by a cluster "
            "service live in the object store and are read through the service "
            "(check 'get_service_info' / 'list_workspaces'); pass an absolute "
            "campaign directory to analyze one directly.")
    return path


def resolve_campaign_path(campaign: str) -> Path:
    """Build and validate the path to a campaign directory.

    A campaign folder is self-contained for analysis, so ``campaign`` may be
    either a campaign **name** resolved under the initialized project's results
    directory, or an **absolute path** to a campaign directory — the latter lets
    analysis tools operate on any campaign folder with no initialized project.

    Args:
        campaign: Campaign name (e.g. ``campaign-2026-03-04-152130``) or an
            absolute path to a campaign directory.

    Returns:
        Absolute path to the campaign directory.

    Raises:
        ValueError: If the campaign directory does not exist.
    """
    # Absolute path → use it directly, skipping the project/results-dir lookup.
    if os.path.isabs(campaign):
        path = Path(campaign)
        if not path.is_dir():
            raise ValueError(f"Campaign directory not found: {campaign}")
        return path
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
    """List the campaigns present in the local results root.

    An absent root yields ``[]`` rather than an error: "no campaigns here yet" is
    genuinely-absent data, and a fresh service has no results directory until its
    first run. (Contrast :func:`resolve_results_dir`, which *does* raise — there a
    caller named a campaign it expects to find.)

    Returns:
        Sorted list of campaign directories matching ``<name>-YYYY-MM-DD-HHMMSS``.
    """
    results_dir = _campaigns_root()
    if not results_dir.is_dir():
        return []
    return sorted(
        d for d in results_dir.iterdir()
        if d.is_dir() and is_campaign_dir(d.name)
    )


def list_config_dirs(campaign: str) -> list[Path]:
    """List configuration directories within a campaign.

    Excludes reserved directories (``_config``, ``_execution``, ``_transient``).

    Args:
        campaign: Campaign name.

    Returns:
        Sorted list of paths to configuration directories.
    """
    from robovast.common.campaign_data import \
        list_config_dirs as _list_config_dirs  # noqa: PLC0415
    return _list_config_dirs(resolve_campaign_path(campaign))


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
