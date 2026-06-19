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

"""Post-hoc indexer that builds a campaign store from a batch results tree.

Local batch execution creates its campaign directory inside the generated run
script (Python ``os.execv``s away), so the store cannot be written live. Instead
this scans a finished ``campaign-<id>/`` directory and records the same
:class:`~robovast.common.store.CampaignStore` schema that the search loop writes
live — giving the results GUI one model for both modes. Search campaigns write
their own store and are not indexed here.
"""

import logging
from pathlib import Path

from .campaign_data import (aggregate_run_status, list_config_dirs,
                            list_run_dirs, read_scenario_config)
from .common import load_config
from .store import STORE_FILENAME, CampaignStore

logger = logging.getLogger(__name__)


def _newest_mtime(campaign_dir: Path) -> float:
    """Newest ``test.xml`` mtime in the tree (0.0 if none)."""
    times = [p.stat().st_mtime for p in campaign_dir.glob("*/*/test.xml")]
    return max(times) if times else 0.0


def build_campaign_store(campaign_dir, *, force: bool = False) -> Path:
    """Build (or refresh) ``campaign.db`` for a batch campaign directory.

    Idempotent: if the store already exists and is newer than the results tree,
    it is left untouched unless ``force`` is set. Returns the store path.
    """
    campaign_dir = Path(campaign_dir)
    store_path = campaign_dir / STORE_FILENAME

    if store_path.exists() and not force:
        if store_path.stat().st_mtime >= _newest_mtime(campaign_dir):
            logger.debug("Campaign store up to date: %s", store_path)
            return store_path
    if store_path.exists():
        store_path.unlink()  # rebuild from scratch (schema/state may have changed)

    # The vast copy carries evaluation.visualization for the GUI; tolerate absence.
    config_dir = campaign_dir / "_config"
    config_json: dict = {}
    vast_files = sorted(config_dir.glob("*.vast")) if config_dir.is_dir() else []
    if vast_files:
        try:
            config_json = load_config(str(vast_files[0]))
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("Could not load %s for campaign store: %s", vast_files[0], e)

    with CampaignStore(store_path) as store:
        campaign_id = store.create_campaign(
            campaign_dir.name, config_json, mode="batch", config_dir=str(config_dir))
        batch_id = store.open_batch(campaign_id, 0, str(campaign_dir))
        for cfg_dir in list_config_dirs(campaign_dir):
            run_dirs = list_run_dirs(cfg_dir)
            try:
                params = read_scenario_config(cfg_dir)
            except FileNotFoundError:
                params = {}
            store.record_unit(
                batch_id=batch_id,
                paramset_id=cfg_dir.name,
                config_name=cfg_dir.name,
                params=params,
                objectives={},
                measures={},
                status=aggregate_run_status(run_dirs),
                result_dir=str(cfg_dir),
                n_samples=len(run_dirs),
            )
    logger.info("Built campaign store: %s", store_path)
    return store_path
