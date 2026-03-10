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

"""Common utilities for results directory layout (<campaign-name>-<timestamp>/<config>/<run-number>)."""
from pathlib import Path
from typing import Iterator, Optional, Tuple

from robovast.common.execution import is_campaign_dir


def iter_run_folders(results_dir: str) -> Iterator[Tuple[str, str, str, Path]]:
    """Iterate over all run folders under a results directory.

    Discovers the standard layout: results_dir/<campaign-name>-<timestamp>/<config>/<run-number>/.
    Under results_dir, only directories matching the campaign naming pattern are
    considered; under each campaign, subdirs are config names; under each config,
    subdirs whose names are numeric are run numbers.

    Args:
        results_dir: Path to the project results directory (parent of campaign directories).

    Yields:
        Tuples (campaign, config_name, run_number, folder_path) where folder_path
        is the full path to <campaign-name>-<timestamp>/<config>/<run-number>.
    """
    root = Path(results_dir)
    if not root.is_dir():
        return

    for campaign_item in sorted(root.iterdir()):
        if not campaign_item.is_dir() or not is_campaign_dir(campaign_item.name):
            continue
        if campaign_item.name == "_config":
            continue
        campaign = campaign_item.name

        for config_item in sorted(campaign_item.iterdir()):
            if not config_item.is_dir():
                continue
            config_name = config_item.name

            for run_item in sorted(config_item.iterdir()):
                if not run_item.is_dir() or not run_item.name.isdigit():
                    continue
                run_number = run_item.name
                folder_path = run_item
                yield campaign, config_name, run_number, folder_path


def find_campaign_vast_file(results_dir: str) -> tuple[Optional[str], Optional[str]]:
    """Find the .vast file from the most recent campaign in results_dir.

    Searches ``results_dir/<campaign-name>-<timestamp>/_config/*.vast`` and returns the
    path from the last (most recent, lexicographically) campaign that has a
    ``.vast`` file.

    Args:
        results_dir: Path to the project results directory (parent of campaign directories).

    Returns:
        Tuple ``(vast_file_path, config_dir)`` where *config_dir* is the
        ``_config/`` directory containing the ``.vast`` file, or
        ``(None, None)`` if no campaign with a ``.vast`` file is found.
    """
    root = Path(results_dir)
    if not root.is_dir():
        return None, None

    # Reverse-sorted so the most recent campaign comes first
    for campaign_item in sorted(root.iterdir(), reverse=True):
        if not campaign_item.is_dir() or not is_campaign_dir(campaign_item.name):
            continue
        config_dir = campaign_item / "_config"
        if config_dir.is_dir():
            vast_files = [f for f in sorted(config_dir.iterdir()) if f.is_file() and f.suffix == ".vast"]
            if len(vast_files) > 1:
                names = ", ".join(f.name for f in vast_files)
                raise ValueError(
                    f"Multiple .vast files found in {config_dir}: {names}. "
                    "Expected exactly one."
                )
            if vast_files:
                return str(vast_files[0]), str(config_dir)
    return None, None
