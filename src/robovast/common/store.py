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

"""Persistent sqlite store for campaigns (search and batch).

A single writer records the campaign, so status is live-queryable while it runs
and the schema is the seam an in-cluster controller / web UI can later read. It
is the single source of truth the results GUI reads. The schema is intentionally
simple:

    campaign (1) --< batch (1) --< unit (one per param set / config)

A campaign runs one or more *batches*. ``campaign.mode`` is ``'search'`` or
``'batch'``; ``campaign.config_dir`` is the base directory against which
``evaluation.visualization`` notebooks (carried in ``config_json``) resolve.
Batch-mode campaigns have a single batch (``idx=0``) with one unit per
configuration; search campaigns have one batch per ask/tell round with one unit
per evaluated parameter set.

``unit`` holds the sampled params (JSON), the objective(s)/measures (JSON), a
status and the result path. ``campaign.strategy_state`` carries an opaque blob so
a strategy can persist enough to resume.

The canonical on-disk filename is :data:`STORE_FILENAME` (``campaign.sqlite``),
written at each campaign's root directory.
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Canonical store filename, written at the root of every campaign directory
# (a batch ``campaign-<id>/`` or a ``search-<ts>/`` root).
STORE_FILENAME = "campaign.sqlite"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign (
    id            INTEGER PRIMARY KEY,
    name          TEXT,
    mode          TEXT,
    config_dir    TEXT,
    config_json   TEXT,
    created_at    REAL,
    strategy_state BLOB
);
CREATE TABLE IF NOT EXISTS batch (
    id          INTEGER PRIMARY KEY,
    campaign_id INTEGER NOT NULL REFERENCES campaign(id),
    idx         INTEGER NOT NULL,
    dir         TEXT,
    created_at  REAL
);
CREATE TABLE IF NOT EXISTS unit (
    id            INTEGER PRIMARY KEY,
    batch_id      INTEGER NOT NULL REFERENCES batch(id),
    paramset_id   TEXT NOT NULL,
    config_name   TEXT,
    params_json   TEXT,
    objective     REAL,            -- the sole objective value (single-objective); NULL otherwise
    objectives_json TEXT,          -- all named objectives
    measures_json TEXT,            -- named quality-diversity measures
    n_samples     INTEGER,
    status        TEXT,
    result_dir    TEXT,
    created_at    REAL
);
"""


class CampaignStore:
    """Thin sqlite wrapper for recording a search campaign."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CampaignStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def create_campaign(self, name: str, config: dict, mode: str = "search",
                        config_dir: str = "") -> int:
        cur = self._conn.execute(
            "INSERT INTO campaign (name, mode, config_dir, config_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, mode, config_dir, json.dumps(config, default=str), time.time()),
        )
        self._conn.commit()
        return cur.lastrowid

    def open_batch(self, campaign_id: int, idx: int, batch_dir: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO batch (campaign_id, idx, dir, created_at) VALUES (?, ?, ?, ?)",
            (campaign_id, idx, batch_dir, time.time()),
        )
        self._conn.commit()
        return cur.lastrowid

    def record_unit(
        self,
        batch_id: int,
        paramset_id: str,
        config_name: str,
        params: dict,
        objectives: dict,
        measures: dict,
        status: str,
        result_dir: str,
        n_samples: Optional[int] = None,
    ) -> None:
        # Surface the sole objective as a queryable REAL column for the common
        # single-objective case; keep the full dict in JSON regardless.
        objective_scalar = next(iter(objectives.values())) if len(objectives) == 1 else None
        self._conn.execute(
            "INSERT INTO unit (batch_id, paramset_id, config_name, params_json, "
            "objective, objectives_json, measures_json, n_samples, status, result_dir, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                batch_id, paramset_id, config_name,
                json.dumps(params, default=str),
                objective_scalar,
                json.dumps(objectives, default=str),
                json.dumps(measures, default=str),
                n_samples,
                status, result_dir, time.time(),
            ),
        )
        self._conn.commit()

    def save_strategy_state(self, campaign_id: int, state: bytes) -> None:
        self._conn.execute(
            "UPDATE campaign SET strategy_state = ? WHERE id = ?", (state, campaign_id)
        )
        self._conn.commit()

    def load_strategy_state(self, campaign_id: int) -> Optional[bytes]:
        row = self._conn.execute(
            "SELECT strategy_state FROM campaign WHERE id = ?", (campaign_id,)
        ).fetchone()
        return row["strategy_state"] if row else None

    # -- read helpers (used by the results GUI / readers) --------------------

    def list_campaigns(self) -> list[sqlite3.Row]:
        """All campaigns in this store, newest first."""
        return list(self._conn.execute(
            "SELECT * FROM campaign ORDER BY created_at DESC"
        ).fetchall())

    def batches(self, campaign_id: int) -> list[sqlite3.Row]:
        """Batches of a campaign, in execution order (idx ascending)."""
        return list(self._conn.execute(
            "SELECT * FROM batch WHERE campaign_id = ? ORDER BY idx", (campaign_id,)
        ).fetchall())

    def units(self, batch_id: int) -> list[sqlite3.Row]:
        """Units (param sets / configs) of a batch, in insertion order."""
        return list(self._conn.execute(
            "SELECT * FROM unit WHERE batch_id = ? ORDER BY id", (batch_id,)
        ).fetchall())
