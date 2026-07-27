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
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from .campaign_data import (aggregate_run_status, list_config_dirs,
                            list_run_dirs, read_execution_metadata,
                            read_run_outcomes, read_scenario_config)
from .common import load_config
from .store import STORE_FILENAME, CampaignStore

logger = logging.getLogger(__name__)


def _newest_mtime(campaign_dir: Path) -> float:
    """Newest ``test.xml`` mtime in the tree (0.0 if none)."""
    times = [p.stat().st_mtime for p in campaign_dir.glob("*/*/test.xml")]
    return max(times) if times else 0.0


def _recorded_start_time(campaign_dir: Path) -> Optional[float]:
    """The campaign's real start time (epoch seconds), or None if unrecorded.

    This indexer runs *after* the campaign finished, so "now" is the indexing time —
    not a start time. The run itself recorded one: the generated run script writes
    ``execution_time`` into ``_execution/execution.yaml`` as it starts (see
    ``generate_execution_yaml_script``), which is exactly the execution path whose store
    cannot be written live. Reading it keeps ``campaign.created_at`` meaning "campaign
    start" in both modes, and keeps it stable across a rebuild of a stale store.

    Returns None (recorded as NULL) when there is no such record: an unknown start time
    is honest, whereas falling back to now or to a directory mtime would silently put an
    old campaign at the top of a newest-first listing.
    """
    try:
        recorded = read_execution_metadata(campaign_dir).get("execution_time")
    except (FileNotFoundError, OSError, yaml.YAMLError) as e:
        logger.warning("No execution record for %s (%s); campaign start time unknown",
                       campaign_dir.name, e)
        return None
    if isinstance(recorded, datetime):  # yaml may parse the ISO string into a datetime
        return recorded.timestamp()
    if not recorded:
        logger.warning("Execution record of %s has no execution_time; start time unknown",
                       campaign_dir.name)
        return None
    try:
        # The local run script writes ...HH:MM:SSZ (`date -u`); normalise the military
        # suffix so parsing does not depend on Python >= 3.11 accepting it.
        return datetime.fromisoformat(str(recorded).replace("Z", "+00:00")).timestamp()
    except ValueError:
        logger.warning("Unparsable execution_time %r in %s; start time unknown",
                       recorded, campaign_dir.name)
        return None


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
        # Paths are stored relative to the campaign root (the dir holding
        # campaign.db) so the store survives the campaign being relocated.
        campaign_id = store.create_campaign(
            campaign_dir.name, config_json, mode="batch", config_dir="_config",
            created_at=_recorded_start_time(campaign_dir))
        batch_id = store.open_batch(campaign_id, 0, ".")
        for cfg_dir in list_config_dirs(campaign_dir):
            run_dirs = list_run_dirs(cfg_dir)
            try:
                params = read_scenario_config(cfg_dir)
            except FileNotFoundError:
                params = {}
            unit_id = store.record_unit(
                batch_id=batch_id,
                paramset_id=cfg_dir.name,
                config_name=cfg_dir.name,
                params=params,
                objectives={},
                measures={},
                status=aggregate_run_status(run_dirs),
                result_dir=cfg_dir.name,
                n_samples=len(run_dirs),
            )
            store.record_runs(unit_id, read_run_outcomes(cfg_dir))
    logger.info("Built campaign store: %s", store_path)
    return store_path


def backfill_run_rows(campaign_dir) -> int:
    """Populate the ``run`` table of an existing store from on-disk ``test.xml``.

    For a store written by a robovast predating the ``run`` table (schema v1,
    migrated to v2 with an empty ``run`` table on open), this fills in the per-run
    outcomes without disturbing the controller-written ``campaign``/``batch``/``unit``
    rows. Idempotent: a unit that already has ``run`` rows is skipped. Returns the
    number of run rows inserted. Does nothing (returns 0) if the store is absent.

    Resolve each unit's config dir from its ``result_dir`` (stored relative to the
    campaign root), and record :func:`read_run_outcomes` for it — so a run missing
    its ``test.xml`` is still recorded as ``unknown`` rather than dropped.
    """
    campaign_dir = Path(campaign_dir)
    store_path = campaign_dir / STORE_FILENAME
    if not store_path.is_file():
        return 0
    inserted = 0
    with CampaignStore(store_path) as store:
        for campaign in store.list_campaigns():
            for batch in store.batches(campaign["id"]):
                for unit in store.units(batch["id"]):
                    if store.runs(unit["id"]):
                        continue  # already backfilled / written live
                    result_dir = unit["result_dir"]
                    if not result_dir:
                        continue
                    rows = read_run_outcomes(campaign_dir / result_dir)
                    store.record_runs(unit["id"], rows)
                    inserted += len(rows)
    if inserted:
        logger.info("Backfilled %d run row(s) into %s", inserted, store_path)
    return inserted
