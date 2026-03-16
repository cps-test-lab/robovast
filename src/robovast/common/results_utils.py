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
from typing import Any, Iterator, Optional, Tuple

from omegaconf import OmegaConf

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


def find_campaign_config(results_dir: str) -> tuple[Optional[str], Optional[str]]:
    """Find the Hydra config from the most recent campaign in results_dir.

    Searches ``results_dir/<campaign-name>-<timestamp>/.hydra/config.yaml``
    and returns the path from the last (most recent, lexicographically)
    campaign.

    Args:
        results_dir: Path to the project results directory (parent of campaign directories).

    Returns:
        Tuple ``(config_path, campaign_dir)`` where *campaign_dir* is the
        campaign directory, or ``(None, None)`` if no campaign config is found.
    """
    root = Path(results_dir)
    if not root.is_dir():
        return None, None

    # Reverse-sorted so the most recent campaign comes first
    for campaign_item in sorted(root.iterdir(), reverse=True):
        if not campaign_item.is_dir() or not is_campaign_dir(campaign_item.name):
            continue
        hydra_config = campaign_item / ".hydra" / "config.yaml"
        if hydra_config.is_file():
            return str(hydra_config), str(campaign_item)
    return None, None


def load_campaign_config(results_dir: str, subsection: Optional[str] = None) -> dict:
    """Load the Hydra config from the most recent campaign.

    Args:
        results_dir: Path to the project results directory.
        subsection: Optional config subsection to extract.

    Returns:
        Full config dict or the requested subsection.

    Raises:
        FileNotFoundError: If no campaign config is found.
    """
    config_path, _ = find_campaign_config(results_dir)
    if config_path is None:
        raise FileNotFoundError(f"No campaign config found in {results_dir}")

    cfg = OmegaConf.load(config_path)
    config_dict = OmegaConf.to_container(cfg, resolve=True)

    if subsection:
        return config_dict.get(subsection, {})
    return config_dict
