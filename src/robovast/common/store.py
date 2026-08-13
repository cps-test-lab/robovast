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

A single writer records the campaign, so its scientific data is live-queryable
while it runs and the schema is the seam an in-cluster controller / web UI can
later read. This store is the source of truth for the campaign's **results data**
(the params/objectives/measures/results the GUI reads), *not* for its live
execution status: the run's phase/error/progress live in the controller's
:class:`~robovast.execution.control_server.Status` while a process drives it, and
its durable terminal record is ``_execution/outcome.json`` (which survives even a
crash that never creates this database). The one way to recover a Status when no
process is driving a campaign is
:func:`robovast.execution.status_recovery.reconstruct_status_from_disk`. The
schema is intentionally simple:

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

The canonical on-disk filename is :data:`STORE_FILENAME` (``campaign.db``),
written at each campaign's root directory.

The schema is versioned via sqlite's ``PRAGMA user_version`` (:data:`SCHEMA_VERSION`),
and defined in **two** places with distinct roles — both must be updated together:

* :data:`_SCHEMA` is the **full current layout**, applied to a *fresh* database in one
  step. It is what a reader consults to answer "what columns does ``run`` have?".
* :data:`_MIGRATIONS` upgrades an *existing* database whose ``user_version`` is lower.
  It is append-only: *add* an entry and bump :data:`SCHEMA_VERSION`, never edit one that
  has shipped, since some store on disk has already applied it.

Splitting them keeps ``_SCHEMA`` readable as the schema instead of a starting point that
must be mentally replayed through every migration. The two cannot drift silently:
``test_fresh_and_migrated_schemas_match`` builds a store both ways and compares the
resulting ``sqlite_master``.

Reads use ``SELECT *`` / explicit columns, so a store written by a *newer* robovast
(unknown extra columns/tables) is still read best-effort.
"""

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Sentinel for "caller gave no start time, stamp now" — distinct from an explicit
# ``None``, which records an unknown start time (see ``create_campaign``).
_STAMP_NOW = object()

# Canonical store filename, written at the root of every campaign directory
# (a batch ``campaign-<id>/`` or a ``search-<ts>/`` root).
STORE_FILENAME = "campaign.db"

#: The full current layout, applied to a fresh database. Mirrors the cumulative effect of
#: every entry in :data:`_MIGRATIONS`; see the module docstring for why both exist.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign (
    id            INTEGER PRIMARY KEY,
    name          TEXT,
    mode          TEXT,
    config_dir    TEXT,
    config_json   TEXT,
    created_at    REAL,
    strategy_state BLOB,
    stop_kind     TEXT,           -- which criterion ended a search (batches/metric/…)
    stop_reason   TEXT,           -- human-readable explanation
    batches       INTEGER,        -- batches completed
    elapsed_s     REAL,           -- wall-clock seconds
    description   TEXT,           -- the launcher's "what is this run for?"
    -- Execution provenance, lifted from _execution/execution.yaml (see record_execution).
    -- Typed columns for the fields compared ACROSS campaigns; execution_json keeps the
    -- whole document so cluster_info/env/run_as_user stay reachable via json_extract.
    robovast_version     TEXT,
    execution_type       TEXT,    -- local | cluster
    image                TEXT,
    image_revision       TEXT,    -- repo@sha256:… the runs actually used
    execution_started_at TEXT,    -- ISO 8601; execution.yaml's misnamed "execution_time"
    execution_json       TEXT
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
CREATE TABLE IF NOT EXISTS job (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaign(id),
    job_dir      TEXT,             -- campaign-relative, e.g. _jobs/batch-0/job-3
    sysinfo_json TEXT,             -- sysinfo.yaml verbatim: one host record per job
    created_at   REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_dir ON job (campaign_id, job_dir);
CREATE TABLE IF NOT EXISTS run (
    id              INTEGER PRIMARY KEY,
    unit_id         INTEGER NOT NULL REFERENCES unit(id),
    run_id          INTEGER NOT NULL,   -- numeric run index within the config dir
    status          TEXT,               -- passed / failed / error / killed / unknown
    passed          INTEGER,            -- 0/1
    errors          INTEGER,
    failures        INTEGER,
    tests           INTEGER,
    duration_s      REAL,
    start_time      TEXT,               -- ISO 8601
    failure_message TEXT,               -- NULL when passed
    created_at      REAL,
    job_id          INTEGER REFERENCES job(id)
);
CREATE INDEX IF NOT EXISTS idx_run_unit ON run (unit_id);
"""

# 0 -> 1: the initial schema, frozen as it shipped. This is deliberately NOT
# :data:`_SCHEMA`: that constant now carries the *current* layout, so reusing it here would
# jump a v0 store straight to the newest tables and the later ``ALTER TABLE ... ADD
# COLUMN`` steps would then fail on columns that already exist. A shipped migration is a
# historical record — it describes what version 1 was, not what the schema is now. Uses
# ``IF NOT EXISTS`` so a store created by a pre-versioning robovast (tables already
# present, ``user_version`` 0) adopts version 1 without modification.
_MIGRATION_INITIAL = """
CREATE TABLE IF NOT EXISTS campaign (
    id            INTEGER PRIMARY KEY,
    name          TEXT,
    mode          TEXT,
    config_dir    TEXT,
    config_json   TEXT,
    created_at    REAL,
    strategy_state BLOB,
    stop_kind     TEXT,
    stop_reason   TEXT,
    batches       INTEGER,
    elapsed_s     REAL
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
    objective     REAL,
    objectives_json TEXT,
    measures_json TEXT,
    n_samples     INTEGER,
    status        TEXT,
    result_dir    TEXT,
    created_at    REAL
);
"""

# 1 -> 2: per-run outcomes. ``unit`` stops one level short of the results tree —
# ``n_samples`` and ``status`` are lossy roll-ups of runs that had no row of their
# own. The ``run`` table completes ``campaign -> batch -> unit -> run`` and mirrors
# each run's ``test.xml`` (the runner's contract), so pass/fail is queryable live
# and ``data.db`` can be built from it instead of re-parsing the XML.
_MIGRATION_ADD_RUN = """
CREATE TABLE IF NOT EXISTS run (
    id              INTEGER PRIMARY KEY,
    unit_id         INTEGER NOT NULL REFERENCES unit(id),
    run_id          INTEGER NOT NULL,   -- numeric run index within the config dir
    status          TEXT,               -- passed / failed / error / killed / unknown
    passed          INTEGER,            -- 0/1
    errors          INTEGER,
    failures        INTEGER,
    tests           INTEGER,
    duration_s      REAL,
    start_time      TEXT,               -- ISO 8601
    failure_message TEXT,               -- NULL when passed
    created_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_run_unit ON run (unit_id);
"""

# 2 -> 3: the campaign's free-text description. ``name`` cannot carry it: it is an id
# fragment (sanitised, no spaces — see ``campaign_id_for``), whereas this is the
# launch-time "what is this run for?" a caller passes to ``start_campaign``. Stored
# beside the campaign it describes, so it survives a service restart and travels with a
# downloaded results tree instead of living only in the launcher's memory.
_MIGRATION_ADD_DESCRIPTION = """
ALTER TABLE campaign ADD COLUMN description TEXT;
"""

# 3 -> 4: the execution job, and the campaign's own provenance.
#
# ``job`` is its own table rather than columns on ``run`` because ``sysinfo.yaml`` is
# written once per *job*, not per run: a packed multi-config job runs several
# (config, run) pairs and they share one host record through each run dir's ``job``
# symlink. A ``run.sysinfo_json`` would repeat the same blob across those runs and
# destroy the fact that they shared a machine — which is exactly what makes "did the slow
# runs land together?" answerable.
#
# The ``campaign`` columns lift ``_execution/execution.yaml`` into the row it describes.
# They are the fields compared ACROSS campaigns ("which of these ran which image?"), and
# the SQL interface can attach several campaigns at once, so keeping them in a YAML file
# meant that join could not be written. ``execution_json`` retains the whole document;
# the typed columns follow the convention ``unit.objective`` already sets by lifting the
# scalar out of ``objectives_json``.
_MIGRATION_ADD_JOB_AND_PROVENANCE = """
CREATE TABLE IF NOT EXISTS job (
    id           INTEGER PRIMARY KEY,
    campaign_id  INTEGER NOT NULL REFERENCES campaign(id),
    job_dir      TEXT,
    sysinfo_json TEXT,
    created_at   REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_dir ON job (campaign_id, job_dir);
ALTER TABLE run ADD COLUMN job_id INTEGER REFERENCES job(id);
ALTER TABLE campaign ADD COLUMN robovast_version TEXT;
ALTER TABLE campaign ADD COLUMN execution_type TEXT;
ALTER TABLE campaign ADD COLUMN image TEXT;
ALTER TABLE campaign ADD COLUMN image_revision TEXT;
ALTER TABLE campaign ADD COLUMN execution_started_at TEXT;
ALTER TABLE campaign ADD COLUMN execution_json TEXT;
"""

# Current schema version, stored in the database as ``PRAGMA user_version``.
SCHEMA_VERSION = 4

# Ordered, append-only migrations: ``_MIGRATIONS[i]`` is the SQL that upgrades a
# database from ``user_version == i`` to ``user_version == i + 1``. To change the
# layout, append a migration (``ALTER TABLE ... ADD COLUMN``, ``CREATE TABLE``, a
# backfill ``UPDATE`` ...), mirror it into :data:`_SCHEMA` **in the same column order**,
# and bump :data:`SCHEMA_VERSION`; never edit an existing entry. This lets a store
# written by any older robovast migrate forward on open, while ``_SCHEMA`` stays
# readable as the current layout.
_MIGRATIONS = [
    _MIGRATION_INITIAL,
    _MIGRATION_ADD_RUN,
    _MIGRATION_ADD_DESCRIPTION,
    _MIGRATION_ADD_JOB_AND_PROVENANCE,
]
assert len(_MIGRATIONS) == SCHEMA_VERSION  # one migration per version step


class CampaignStore:
    """Thin sqlite wrapper for recording a search campaign."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        """Bring the store up to :data:`SCHEMA_VERSION`.

        A brand-new database gets :data:`_SCHEMA` in one step — the current layout,
        applied directly rather than reconstructed by replaying every migration. An
        existing database (lower ``user_version``, or 0 for the pre-versioning schema) is
        upgraded in place through the pending :data:`_MIGRATIONS`. One written by a
        *newer* robovast (higher ``user_version``) is left untouched and read
        best-effort: our queries use ``SELECT *`` / explicit columns, so unknown
        columns and tables are simply ignored.
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            if version > SCHEMA_VERSION:
                logger.warning(
                    "Campaign store %s is schema v%d but this robovast supports "
                    "v%d; reading best-effort.", self.db_path, version, SCHEMA_VERSION)
            return
        if version == 0 and not self._has_tables():
            # Fresh database: apply the current layout wholesale. Distinguished from a
            # pre-versioning store (``user_version`` 0 *with* tables), which must go
            # through the ladder so its existing rows keep their columns.
            self._conn.executescript(_SCHEMA)
        else:
            for v in range(version, SCHEMA_VERSION):
                self._conn.executescript(_MIGRATIONS[v])
        # PRAGMA can't be parameterised; SCHEMA_VERSION is a constant we control.
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    def _has_tables(self) -> bool:
        """True when this database already holds a campaign schema."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchone()
        return bool(row[0])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "CampaignStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def create_campaign(self, name: str, config: dict, mode: str = "search",
                        config_dir: str = "", created_at: Any = _STAMP_NOW,
                        description: str = "") -> int:
        """Insert the campaign row. ``created_at`` is the campaign's START time.

        Omitting it stamps now, which is correct for the live path: the controller calls
        this as the campaign begins, in both modes. A caller building a store *after the
        fact* (:func:`robovast.common.campaign_index.build_campaign_store`, for local
        batch runs whose store cannot be written live) must pass the start time it
        recovered from the results tree — "now" would mean indexing time, and the whole
        service reads this column as the campaign's start (listing order, ``started_at``
        in the UI). Passing ``None`` explicitly records NULL: an unknown start time,
        which is the honest answer when the results tree has no record of it.

        ``description`` is the launcher's free text about *this* run (empty when none
        was given); it is recorded verbatim and never derived from the config.
        """
        cur = self._conn.execute(
            "INSERT INTO campaign (name, mode, config_dir, config_json, created_at, "
            "description) VALUES (?, ?, ?, ?, ?, ?)",
            (name, mode, config_dir, json.dumps(config, default=str),
             time.time() if created_at is _STAMP_NOW else created_at,
             description or None),
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
    ) -> int:
        # Surface the sole objective as a queryable REAL column for the common
        # single-objective case; keep the full dict in JSON regardless.
        objective_scalar = next(iter(objectives.values())) if len(objectives) == 1 else None
        cur = self._conn.execute(
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
        return cur.lastrowid

    def upsert_job(self, campaign_id: int, job_dir: str,
                   sysinfo: Optional[dict]) -> Optional[int]:
        """Record the execution job at *job_dir*, returning its row id.

        Idempotent on ``(campaign_id, job_dir)``: a packed multi-config job is reached
        once per run that ran inside it, and all of them describe the same host, so the
        second and later calls resolve to the existing row instead of duplicating it.

        ``sysinfo`` may be ``None`` — a job whose ``sysinfo.yaml`` never appeared still
        gets a row, because *which* job a run belonged to is worth recording even when the
        host record is missing. Returns ``None`` for an empty ``job_dir`` (a run with no
        resolvable job), so the caller stores a NULL ``run.job_id`` rather than inventing
        a job.
        """
        if not job_dir:
            return None
        row = self._conn.execute(
            "SELECT id, sysinfo_json FROM job WHERE campaign_id = ? AND job_dir = ?",
            (campaign_id, job_dir)).fetchone()
        sysinfo_json = json.dumps(sysinfo, default=str) if sysinfo else None
        if row is not None:
            # Fill in a host record that was absent when the job row was first created
            # (the first run seen may have preceded sysinfo.yaml being written).
            if sysinfo_json and not row["sysinfo_json"]:
                self._conn.execute("UPDATE job SET sysinfo_json = ? WHERE id = ?",
                                   (sysinfo_json, row["id"]))
                self._conn.commit()
            return row["id"]
        cur = self._conn.execute(
            "INSERT INTO job (campaign_id, job_dir, sysinfo_json, created_at) "
            "VALUES (?, ?, ?, ?)", (campaign_id, job_dir, sysinfo_json, time.time()))
        self._conn.commit()
        return cur.lastrowid

    def record_runs(self, unit_id: int, runs: list[dict]) -> None:
        """Record per-run outcomes for a unit (one ``run`` row per run).

        Each dict is a :func:`robovast.common.campaign_data.read_run_outcome`
        result: ``run_id``, ``status``, ``passed``, ``errors``, ``failures``,
        ``tests``, ``duration_s``, ``start_time``, ``failure_message``.

        When those dicts also carry ``job_dir`` / ``sysinfo`` (i.e. the caller passed a
        campaign root to ``read_run_outcome``), the corresponding ``job`` rows are
        upserted here and each run gets its ``job_id``. The owning campaign is looked up
        from *unit_id* rather than taken as an argument, so it cannot be passed
        inconsistently with the unit.
        """
        if not runs:
            return
        job_ids: dict[str, Optional[int]] = {}
        if any(r.get("job_dir") for r in runs):
            row = self._conn.execute(
                "SELECT b.campaign_id AS cid FROM unit u JOIN batch b ON u.batch_id = b.id "
                "WHERE u.id = ?", (unit_id,)).fetchone()
            if row is not None:
                for r in runs:
                    job_dir = r.get("job_dir")
                    if job_dir and job_dir not in job_ids:
                        job_ids[job_dir] = self.upsert_job(
                            row["cid"], job_dir, r.get("sysinfo"))
        for r in runs:
            r["job_id"] = job_ids.get(r.get("job_dir") or "")
        now = time.time()
        self._conn.executemany(
            "INSERT INTO run (unit_id, run_id, status, passed, errors, failures, "
            "tests, duration_s, start_time, failure_message, created_at, job_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (unit_id, r["run_id"], r["status"], r["passed"], r["errors"],
                 r["failures"], r["tests"], r["duration_s"], r["start_time"],
                 r["failure_message"], now, r.get("job_id"))
                for r in runs
            ],
        )
        self._conn.commit()

    def record_outcome(self, campaign_id: int, *, stop_kind: str, stop_reason: str,
                       batches: int, elapsed_s: float) -> None:
        """Persist how/why a search ended (queryable from the ``campaign`` row)."""
        self._conn.execute(
            "UPDATE campaign SET stop_kind = ?, stop_reason = ?, batches = ?, "
            "elapsed_s = ? WHERE id = ?",
            (stop_kind, stop_reason, batches, elapsed_s, campaign_id),
        )
        self._conn.commit()

    def record_execution(self, campaign_id: int, execution: dict) -> None:
        """Lift ``_execution/execution.yaml`` onto the campaign row.

        Called once the backend has produced that file — which is *after* campaign
        creation on both lanes, and on the local lane happens inside the run itself (a
        generated shell script writes it), so it cannot be folded into
        :meth:`create_campaign`.

        The typed columns are the fields compared across campaigns; ``execution_json``
        keeps the whole document so ``cluster_info`` / ``env`` / ``run_as_user`` stay
        reachable. Note ``execution_time`` in the YAML is a *start timestamp*, not a
        duration (``datetime.now()`` at execution start), hence the honest column name
        ``execution_started_at``; the elapsed wall clock is ``elapsed_s``.
        """
        if not execution:
            return
        self._conn.execute(
            "UPDATE campaign SET robovast_version = ?, execution_type = ?, image = ?, "
            "image_revision = ?, execution_started_at = ?, execution_json = ? "
            "WHERE id = ?",
            (execution.get("robovast_version"), execution.get("execution_type"),
             execution.get("image"), execution.get("image_revision"),
             execution.get("execution_time"),
             json.dumps(execution, default=str), campaign_id),
        )
        self._conn.commit()

    def record_elapsed(self, campaign_id: int, elapsed_s: float) -> None:
        """Record the campaign's wall-clock duration.

        Batch mode has no ``record_outcome`` (there is no search criterion that stopped
        it), so without this its ``elapsed_s`` stayed NULL and "how long did this
        campaign take" was unanswerable from the store for every batch campaign.
        """
        self._conn.execute("UPDATE campaign SET elapsed_s = ? WHERE id = ?",
                           (elapsed_s, campaign_id))
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

    def runs(self, unit_id: int) -> list[sqlite3.Row]:
        """Per-run outcome rows of a unit, in run-index order."""
        return list(self._conn.execute(
            "SELECT * FROM run WHERE unit_id = ? ORDER BY run_id", (unit_id,)
        ).fetchall())

    def run_counts(self, campaign_id: int) -> dict[str, int]:
        """Pass/fail tallies for a campaign, from one ``GROUP BY`` over ``run``.

        Returns ``num_runs`` (all rows), ``num_passed``, ``num_failed`` (status
        ``failed`` only), ``num_errors`` (status ``error``) and ``num_killed`` (a job an
        operator stopped by hand). An ``unknown`` run counts toward ``num_runs`` but none
        of the others, so the four may sum to < ``num_runs``.

        ``num_killed`` is reported apart from ``num_failed`` on purpose: a run somebody
        stopped says nothing about the system under test, and folding it into the failures
        would put a human intervention into the campaign's measured outcome.

        ``num_composition_failed`` counts *units* rather than runs (a search draw
        that could not be composed never produced one), so it is reported beside
        the run tallies rather than folded into them: ``num_runs`` keeps meaning
        "runs that happened".
        """
        rows = self._conn.execute(
            "SELECT r.status AS status, COUNT(*) AS n FROM run r "
            "JOIN unit u ON r.unit_id = u.id "
            "JOIN batch b ON u.batch_id = b.id "
            "WHERE b.campaign_id = ? GROUP BY r.status", (campaign_id,)
        ).fetchall()
        by_status = {row["status"]: row["n"] for row in rows}
        composition_failed = self._conn.execute(
            "SELECT COUNT(*) FROM unit u JOIN batch b ON u.batch_id = b.id "
            "WHERE b.campaign_id = ? AND u.status = 'composition_failed'",
            (campaign_id,)).fetchone()[0]
        return {
            "num_runs": sum(by_status.values()),
            "num_passed": by_status.get("passed", 0),
            "num_failed": by_status.get("failed", 0),
            "num_errors": by_status.get("error", 0),
            "num_killed": by_status.get("killed", 0),
            "num_composition_failed": composition_failed,
        }


def read_campaign_mode(campaign_dir: str | Path) -> Optional[str]:
    """Best-effort read of ``campaign.mode`` ('search'/'batch') from ``campaign.db``.

    Returns ``None`` when the store is absent or unreadable. A read-only helper for
    status reconstruction (see
    :func:`robovast.execution.status_recovery.reconstruct_status_from_disk`), so it
    never creates or migrates the store — it opens it read-only or gives up.
    """
    db = Path(campaign_dir) / STORE_FILENAME
    if not db.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT mode FROM campaign LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def read_campaign_created_at(campaign_dir: str | Path) -> Optional[str]:
    """Best-effort read of the campaign's start time as an ISO-8601 UTC string.

    Reads ``campaign.created_at`` — recorded when the campaign begins, in both modes
    (see :meth:`CampaignStore.create_campaign`). Opened **read-only**, like the other
    readers here, so listing never migrates or locks a store a running campaign is
    still writing.

    Returns ``None`` when the store is absent, unreadable, or the column is NULL: a
    genuinely unknown start time, not a swallowed error. Callers order such campaigns
    last rather than substituting a directory mtime — a guessed start time would be
    indistinguishable from a recorded one.
    """
    db = Path(campaign_dir) / STORE_FILENAME
    if not db.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT created_at FROM campaign ORDER BY created_at LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    return datetime.fromtimestamp(row[0], tz=timezone.utc).isoformat()


def read_campaign_description(campaign_dir: str | Path) -> Optional[str]:
    """Best-effort read of the campaign's free-text description from ``campaign.db``.

    Opened **read-only**, like the other readers here, so listing never migrates or
    locks a store a running campaign is still writing — which also means a store
    written before the ``description`` column existed (schema < 3) is *not* migrated
    on read: the ``sqlite3.Error`` for the unknown column is caught and reported as
    "no description", the same as a campaign launched without one.
    """
    db = Path(campaign_dir) / STORE_FILENAME
    if not db.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT description FROM campaign LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


def read_run_counts(campaign_dir: str | Path) -> Optional[dict[str, int]]:
    """Best-effort read of per-run pass/fail tallies from ``campaign.db``.

    Aggregates the ``run`` table (each ``campaign.db`` holds one campaign). Read
    only — it never creates or migrates the store — so it is safe on the summary
    hot path. Returns ``None`` when the store is absent, pre-``run``-table (schema
    v1), or unreadable, letting the caller fall back to a disk walk; the returned
    keys match :meth:`CampaignStore.run_counts`.
    """
    db = Path(campaign_dir) / STORE_FILENAME
    if not db.is_file():
        return None
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM run GROUP BY status").fetchall()
            # Counts units, not runs: a search draw that could not be composed never
            # produced one. Reported beside the run tallies rather than inside them, so
            # ``num_runs`` keeps meaning "runs that happened" -- while the draw itself
            # stops being invisible in every summary that reads these counts. Its own
            # try: a store without a ``unit`` table still has usable run tallies, and
            # losing them to a missing column would be the worse failure.
            try:
                composition_failed = conn.execute(
                    "SELECT COUNT(*) FROM unit WHERE status = 'composition_failed'"
                ).fetchone()[0]
            except sqlite3.Error:
                composition_failed = 0
    except sqlite3.Error:
        return None  # no ``run`` table (v1) or unreadable store
    by_status = {r[0]: r[1] for r in rows}
    return {
        "num_runs": sum(by_status.values()),
        "num_passed": by_status.get("passed", 0),
        "num_failed": by_status.get("failed", 0),
        "num_errors": by_status.get("error", 0),
        "num_killed": by_status.get("killed", 0),
        "num_composition_failed": composition_failed,
    }
