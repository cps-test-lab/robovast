#!/usr/bin/env python3
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

"""Read a campaign's results from ``data.db``, scoped to the notebook's ``DATA_DIR``.

A notebook is handed ``DATA_DIR`` — the directory of the tree node the reader selected, which is
the campaign root, one configuration, or one run. Everything here takes that path and works out
the rest: which campaign it belongs to, and which rows of a campaign-wide table are *this* node's.
So the same cell reads one run or the whole campaign, and a notebook never names a file.

Reading tables instead of files is what makes an analysis portable across the substrate. The
per-run files differ by simulator and by whether the run had ROS at all — ``poses.csv`` exists
only when a rosbag was recorded, ``behaviors.csv`` was replaced by ``behaviors.jsonl`` in
2026-08 — while ``data.db`` normalizes all of it into tables keyed the same way. The
behaviour-tree ingest is the clearest case: it drops the JSONL header record and splits the
status into the numeric ``status`` and the ``status_name`` that analysis actually filters on, so
``read_table(DATA_DIR, "behaviors")`` is the behaviour-tree reader and there is no format left
for a notebook to know about.

There is deliberately no fallback to reading those files when ``data.db`` is absent. It is
absent for one of a few knowable reasons — postprocessing has not run, it failed, or the campaign
is still going — and each has a remedy the caller should hear, none of which is "silently answer a
different question with less data".
"""

import sqlite3
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple, Union

import pandas as pd

#: Layout of ``data.db`` that this reader understands, stamped by the builder into sqlite's
#: ``PRAGMA user_version``.
#:
#: Unlike :data:`robovast.common.store.SCHEMA_VERSION` there is **no migration table beside
#: this constant, and there should never be one.** ``campaign.db`` is authored — written as the
#: campaign runs, and the only record of what happened — so an old one has to be migrated in
#: place. ``data.db`` is derived: postprocessing deletes and rebuilds it from the run
#: directories, which keep their CSV/JSONL forever. Re-running postprocessing re-executes no
#: trial, so regeneration is both exact and cheap, and a migration would be code maintained to
#: reproduce what the builder already does.
#:
#: Bump this when the builder's layout changes. It does not gate reads: what decides whether a
#: query works is whether the columns are there, which :func:`read_table`'s ``require`` checks
#: directly. The version only sharpens the error when they are not.
DATA_DB_SCHEMA_VERSION = 2

_LEVELS = ("campaign", "config", "run")


class CampaignDataError(RuntimeError):
    """A campaign's results cannot be read, with the remedy in the message.

    A plain exception rather than ``SystemExit``: the web renderer turns ``SystemExit`` into a
    neutral "no data" page, which is right for "this run has nothing to show" and wrong for
    every condition raised here, all of which the caller can act on. Raised as an error, the
    message reaches the Explorer's alert.
    """


def campaign_root(data_dir: Union[str, Path]) -> Path:
    """The campaign directory containing *data_dir*, which may be the campaign itself.

    Found by walking up for the campaign's own files rather than by matching the directory
    *name*: a results directory gets renamed, copied and mounted at a different path, and the
    campaign is still the campaign.
    """
    path = Path(data_dir).resolve()
    for candidate in (path, *path.parents):
        if (candidate / "campaign.db").exists() or (candidate / "_execution").is_dir():
            return candidate
    raise CampaignDataError(
        f"{data_dir} is not inside a campaign directory (looked for campaign.db or "
        f"_execution/ here and in every parent). DATA_DIR should be a campaign, a "
        f"configuration, or a run directory under a campaign.")


def run_scope(data_dir: Union[str, Path]) -> Tuple[str, Optional[str], Optional[int]]:
    """What *data_dir* selects: ``(level, config_name, run_id)``.

    The three levels mirror the tree the reader picked a node from — a campaign root, a
    configuration under it, or a run under that. Anything deeper is one of a run's own
    subdirectories (``rosbag2/``, ``capture/``), which still identifies that run.
    """
    root = campaign_root(data_dir)
    parts = Path(data_dir).resolve().relative_to(root).parts

    if not parts:
        return "campaign", None, None
    if len(parts) == 1:
        return "config", parts[0], None
    try:
        return "run", parts[0], int(parts[1])
    except ValueError:
        raise CampaignDataError(
            f"{data_dir} is inside campaign {root.name} but is not a campaign, configuration "
            f"or run directory: expected a numeric run directory, got {parts[1]!r}.") from None


def open_campaign_store(data_dir: Union[str, Path]) -> sqlite3.Connection:
    """Open ``campaign.db`` alone, read-only. The caller must ``close()`` the connection.

    The campaign's own record -- what was proposed, in which batch, and what it scored -- as
    opposed to :func:`open_campaign_db`, which is the postprocessed *measurements* and needs
    ``data.db`` to exist for them. A search notebook wants this one: it is written as the
    search runs, so a batch or archive view works while the campaign is still going and on one
    that was never postprocessed, which is exactly when watching a search is worth anything.

    Tables are unqualified here (``batch``, ``unit``, ``run``, ``campaign``, ``job``), not
    under a ``campaign.`` prefix as they are when attached by :func:`open_campaign_db`.

    Exists so that reading the search store is not a raw ``sqlite3.connect`` on a hand-built
    path in every notebook that wants it -- which is what it was, in each of them separately,
    each with its own guess at where the file lives.
    """
    root = campaign_root(data_dir)
    store = root / "campaign.db"
    if not store.exists():
        raise CampaignDataError(
            f"Campaign {root.name} has no campaign.db, so there is no record of what it "
            f"proposed or scored. A results tree copied without it, or one produced outside "
            f"a controller, has none.")
    return sqlite3.connect(f"file:{store}?mode=ro", uri=True)


def open_campaign_db(data_dir: Union[str, Path]) -> sqlite3.Connection:
    """Open the campaign's databases read-only. The caller must ``close()`` the connection.

    ``campaign.db`` is attached as schema ``campaign``, and the ``run_view``/``config_view``
    temp views are available — see :func:`robovast.results_processing.data_query.open_data_db`,
    which does that work.
    """
    root = campaign_root(data_dir)
    _require_data_db(root)
    # Imported here, not at module scope: `robovast.results_processing` pulls in the whole
    # postprocessing stack on import, and this package is what a notebook imports first.
    from robovast.results_processing.data_query import open_data_db
    return open_data_db(root)


def _require_data_db(root: Path) -> None:
    """Fail with the remedy when the campaign has no postprocessed results.

    ``open_data_db`` deliberately degrades to an empty in-memory database so ``campaign.db``
    stays reachable for callers that only want live progress. For a notebook reading metrics
    that would surface one table at a time as ``no such table: behaviors``, which names neither
    the real cause nor the fix.
    """
    if (root / "_execution" / "data.db").exists():
        return
    raise CampaignDataError(
        f"Campaign {root.name} has no _execution/data.db, so there are no results to read "
        f"yet.\n\n"
        f"If the campaign has finished, postprocessing did not run or did not succeed "
        f"(check `postprocessed` and `postprocessing_error` in its status). Build it with "
        f"`run_postprocessing`, or `vast results postprocess {root}` -- this re-runs no "
        f"trial, it rebuilds the tables from the run directories and _jobs/, which are kept.\n"
        f"If it is still running, data.db is written when it finishes.")


def _schema_version(conn: sqlite3.Connection) -> int:
    """The layout stamp, or 0 for a database built before stamping."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _outdated_hint(conn: sqlite3.Connection) -> str:
    version = _schema_version(conn)
    if version >= DATA_DB_SCHEMA_VERSION:
        return ""
    return (f" This data.db was built by an older layout (v{version}, current is "
            f"v{DATA_DB_SCHEMA_VERSION}); re-running postprocessing rebuilds it.")


def _sql_name(conn: sqlite3.Connection, table: str) -> str:
    """The table's SQL name, which the ingest may have derived from a filename.

    ``_table_name_map`` is the ingest's own record of that mapping (``run.clock_map.csv`` ->
    ``run_clock_map``), so a caller can name the table the way the campaign does.
    """
    try:
        row = conn.execute(
            "SELECT sql_name FROM _table_name_map WHERE display_name = ?", (table,)).fetchone()
    except sqlite3.Error:
        return table
    return row[0] if row else table


def list_tables(data_dir: Union[str, Path]) -> list:
    """Every result table this campaign has, excluding the ingest's own bookkeeping.

    Which tables exist depends on what the campaign produced: a run with no rosbag has no
    ``poses``, a non-nav2 stack no ``nav2_behaviors``. This is how a notebook checks instead
    of assuming.
    """
    conn = open_campaign_db(data_dir)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE '\\_%' "
            "ESCAPE '\\' ORDER BY name").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def table_info(data_dir: Union[str, Path], table: str) -> "dict[str, str]":
    """The columns *table* actually has, as ``{name: declared type}``.

    For a notebook that adapts to what a campaign carries rather than failing on it — the
    Python counterpart of the web panels' column probe. Returns ``{}`` when the table is absent.
    """
    conn = open_campaign_db(data_dir)
    try:
        return _table_columns(conn, _sql_name(conn, table))
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, sql_name: str) -> "dict[str, str]":
    rows = conn.execute(f'PRAGMA table_info("{sql_name}")').fetchall()
    return {r[1]: r[2] for r in rows}


def _check_table(conn: sqlite3.Connection, table: str, sql_name: str) -> "dict[str, str]":
    """Resolve a table to its columns, or explain what is missing and why."""
    columns = _table_columns(conn, sql_name)
    if columns:
        return columns

    available = ", ".join(
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE '\\_%' "
            "ESCAPE '\\' ORDER BY name")) or "(none)"
    hint = ""
    if table == "behaviors":
        # The two ways to get a passing run with no scenario tree. Both are silent by
        # construction, so the error is the only place they can be named.
        hint = (" A campaign has no behaviours table when execution.bt_log is false, or when "
                "its execution image ships a scenario_execution predating --bt-log: the flag "
                "is dropped rather than refused, so the run passes and writes nothing.")
    raise CampaignDataError(
        f"No table {table!r} in this campaign's data.db. Tables it has: {available}."
        + hint + _outdated_hint(conn))


def _check_columns(conn: sqlite3.Connection, table: str, columns: "dict[str, str]",
                   require: Iterable) -> None:
    missing = [c for c in require if c not in columns]
    if not missing:
        return
    raise CampaignDataError(
        f"Table {table!r} has no column(s) {', '.join(repr(c) for c in missing)}. "
        f"It has: {', '.join(columns)}." + _outdated_hint(conn))


def _scope_clause(level: str, config_name: Optional[str],
                  run_id: Optional[int], columns: "dict[str, str]") -> Tuple[str, list]:
    """The ``WHERE`` restricting a campaign-wide table to the selected node.

    Applied only to tables actually keyed by run. The campaign-wide ones (``config_view``, and
    anything a future ingest adds) have no such columns, and filtering them would be an error
    rather than a narrowing.
    """
    if level == "campaign" or "config_name" not in columns:
        return "", []
    if level == "config" or "run_id" not in columns:
        return "WHERE config_name = ?", [config_name]
    return "WHERE config_name = ? AND run_id = ?", [config_name, run_id]


def read_table(data_dir: Union[str, Path], table: str, columns: Optional[Sequence] = None,
               require: Optional[Iterable] = None, where: str = "",
               params: Sequence = (), with_params: bool = False) -> pd.DataFrame:
    """One of the campaign's tables, restricted to what *data_dir* selects.

    Args:
        data_dir: The notebook's ``DATA_DIR``. A run directory yields that run's rows, a
            configuration directory that configuration's, the campaign root everything.
        table: Table name as the campaign records it (``behaviors``, ``poses``, ``runs``, ...).
            :func:`list_tables` says which exist here.
        columns: Columns to select; all of them by default.
        require: Columns the caller cannot work without. Missing ones raise, naming them and
            what the table does have — so a layout change surfaces as an error rather than as
            a frame that is quietly missing a column, or empty.
        where: Extra SQL predicate, ANDed with the scope restriction. Use ``?`` placeholders.
        params: Values for those placeholders.
        with_params: Attach each scenario parameter of the owning run as a column — see
            :func:`attach_params`. What a metric varied *with* is usually the question, and
            the parameters live in ``runs`` rather than on the metric table.

    Returns:
        A DataFrame, empty (with the right columns) when the node produced no rows.
    """
    level, config_name, run_id = run_scope(data_dir)
    conn = open_campaign_db(data_dir)
    try:
        sql_name = _sql_name(conn, table)
        available = _check_table(conn, table, sql_name)
        if require:
            _check_columns(conn, table, available, require)
        if columns:
            _check_columns(conn, table, available, columns)

        selected = ", ".join(f'"{c}"' for c in columns) if columns else "*"
        clause, values = _scope_clause(level, config_name, run_id, available)
        if where:
            clause = f"{clause} AND ({where})" if clause else f"WHERE {where}"
            values = [*values, *params]

        frame = pd.read_sql(f'SELECT {selected} FROM "{sql_name}" {clause}', conn, params=values)
    finally:
        conn.close()

    return attach_params(frame, data_dir) if with_params else frame


def attach_params(frame: pd.DataFrame, data_dir: Union[str, Path]) -> pd.DataFrame:
    """Add each scenario parameter of the owning run to *frame*, as a column.

    The parameters are stored once per run in ``runs`` as ``param_*``; this joins them onto
    a metric frame on ``(config_name, run_id)`` and drops the prefix, so a plot reads
    ``df["map_file"]`` rather than carrying the storage layout into the analysis.

    A parameter whose name collides with a column already in *frame* raises instead of
    winning or being suffixed: silently shadowing a measured column with a configured value
    is the kind of thing that reads as a plausible plot.
    """
    keys = ["config_name", "run_id"]
    if any(k not in frame.columns for k in keys):
        raise CampaignDataError(
            f"Cannot attach scenario parameters: the frame has no {keys} to join on. "
            f"It has: {', '.join(frame.columns)}.")

    runs = read_runs(data_dir)
    param_cols = [c for c in runs.columns if c.startswith("param_")]
    renamed = {c: c[len("param_"):] for c in param_cols}

    clashes = sorted(set(renamed.values()) & set(frame.columns))
    if clashes:
        raise CampaignDataError(
            f"Scenario parameter(s) {', '.join(clashes)} would overwrite a column of the "
            f"same name already in this table. Read the parameters separately with "
            f"read_runs() and join them under names of your choosing.")

    return frame.merge(runs[keys + param_cols].rename(columns=renamed), on=keys, how="left")


def config_file(data_dir: Union[str, Path], relative_path: str,
                config_name: Optional[str] = None, must_exist: bool = True) -> Path:
    """Resolve a path recorded in a scenario parameter to a file in the campaign snapshot.

    Parameters that name a file — ``map_file``, ``mesh_file`` — hold a path relative to the
    campaign's ``_config/``, which is where the snapshot puts ``environments/``. Joining
    them against the notebook's ``DATA_DIR`` instead is wrong at every scope except the
    campaign root, and a configuration's own ``_config/`` holds only ``config.yaml`` and
    ``scenario.config`` — so that spelling silently finds nothing.

    Args:
        data_dir: The notebook's ``DATA_DIR``, at any scope.
        relative_path: The parameter's value, e.g. ``environments/hexagon/maps/hexagon.yaml``.
        config_name: Checked first under that configuration's own ``_config/``, for a
            campaign that kept a per-configuration copy.
        must_exist: Raise when nothing is found, naming what was tried. Pass ``False`` to
            get the campaign-level candidate back regardless, for a caller that means to
            test existence itself.
    """
    root = campaign_root(data_dir)
    candidates = []
    if config_name is not None:
        candidates.append(root / str(config_name) / "_config" / relative_path)
    candidates.append(root / "_config" / relative_path)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if must_exist:
        raise CampaignDataError(
            f"{relative_path!r} is not in campaign {root.name}'s configuration. Looked at: "
            + ", ".join(str(c) for c in candidates))
    return candidates[-1]


def read_runs(data_dir: Union[str, Path]) -> pd.DataFrame:
    """The ``runs`` dimension table, restricted to what *data_dir* selects.

    One row per run: outcome, duration, the host it ran on, and every scenario parameter as a
    typed ``param_*`` column. Joining it to a metric table on ``(config_name, run_id)`` is how
    an analysis relates what varied to what happened.
    """
    return read_table(data_dir, "runs")


def read_sql(data_dir: Union[str, Path], sql: str, params: Sequence = ()) -> pd.DataFrame:
    """Run *sql* against the campaign's databases and return the result.

    The escape hatch, for joins and for the ``run_view``/``config_view`` views. **Not scoped**
    — a query issued from a run directory still sees the whole campaign unless it says
    otherwise, because a query spanning runs is usually the point of writing one.
    """
    conn = open_campaign_db(data_dir)
    try:
        return pd.read_sql(sql, conn, params=list(params))
    finally:
        conn.close()
