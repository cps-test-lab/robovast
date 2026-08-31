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

"""Read a campaign's results from the central index, scoped to the notebook's ``DATA_DIR``.

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


def open_campaign_db(data_dir: Union[str, Path]):
    """Open the campaign's data read-only. The caller must ``close()`` the connection.

    A connection to the central index, where campaign dimensions live in schema ``campaign``
    and the ``run_view``/``config_view`` views are available -- see
    :func:`robovast.results_processing.data_query.open_data_db`, which does that work.

    **What comes back is Postgres, not sqlite.** Parameters are ``%s``, and every table holds
    every campaign, so SQL written against it must say which campaign it means. The readers
    in this module do that for you; :func:`read_sql` hands you the id to do it yourself.
    """
    _require_ingested(campaign_root(data_dir))
    # Imported here, not at module scope: `robovast.results_processing` pulls in the whole
    # postprocessing stack on import, and this package is what a notebook imports first.
    from robovast.results_processing import index_query
    # Deliberately NOT `data_query.open_data_db`, which is the plugin seam and hands back
    # dict rows. `pandas.read_sql` over a dict-row connection returns each column's *name*
    # as its value in every cell -- a frame of the right shape and the right length, full of
    # header strings, with nothing raised. Tuple rows are what pandas expects, so that is
    # what the notebook path opens.
    return index_query.open_index(readonly=True)


def campaign_id(data_dir: Union[str, Path]) -> str:
    """Which campaign *data_dir* belongs to -- the value every query must filter on."""
    return campaign_root(data_dir).name


def _require_ingested(root: Path) -> None:
    """Fail with the remedy when the campaign's results are not in the index.

    The distinction this exists to preserve: **not ingested and ingested-but-empty are
    different answers.** One index holds every campaign, so a query for a campaign that was
    never postprocessed returns zero rows exactly as a campaign that genuinely measured
    nothing does -- and zero rows is a claim about the experiment, not about the pipeline.
    Without this check a notebook would plot an empty frame and read it as a result.
    """
    from robovast.results_processing import index_query
    from robovast.common.errors import IndexUnreachableError
    try:
        if index_query.campaign_is_ingested(root.name):
            return
    except IndexUnreachableError:
        raise
    raise CampaignDataError(index_query.missing_campaign_note(root.name))


def _outdated_hint(conn) -> str:
    """No layout stamp to report against.

    ``data.db`` carried a ``PRAGMA user_version`` so a stale rebuild could be named in an
    error. The index has no equivalent and needs none: it is derived, re-ingest is the
    definition of correct, and a campaign is either ingested or it is not -- which
    :func:`_require_ingested` has already established by the time any of this runs.
    """
    del conn
    return ""


def _sql_name(conn, table: str) -> str:
    """The table's SQL name, which the ingest may have derived from a filename.

    ``_table_name_map`` is the ingest's own record of that mapping (``run.clock_map.csv`` ->
    ``run_clock_map``), so a caller can name the table the way the campaign does.
    """
    try:
        row = conn.execute(
            "SELECT sql_name FROM _table_name_map WHERE display_name = %s LIMIT 1",
            (table,)).fetchone()
    except Exception:  # pylint: disable=broad-except
        return table
    if not row:
        return table
    return row["sql_name"] if isinstance(row, dict) else row[0]


def list_tables(data_dir: Union[str, Path]) -> list:
    """Every result table this campaign has, excluding the ingest's own bookkeeping.

    Which tables exist depends on what the campaign produced: a run with no rosbag has no
    ``poses``, a non-nav2 stack no ``nav2_behaviors``. This is how a notebook checks instead
    of assuming.
    """
    conn = open_campaign_db(data_dir)
    try:
        return _result_tables(conn, campaign_id(data_dir))
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


def _result_tables(conn, campaign: str) -> list:
    """The tables *campaign* actually has rows in, excluding the ingest's bookkeeping.

    Scoped, and that is the whole point. ``list_tables`` describes the index, which holds
    every campaign -- so unscoped it would answer "what tables exist anywhere" to a caller
    asking "what did this campaign produce", and a campaign that recorded no rosbag would be
    told it has ``poses`` because some other campaign does.
    """
    from robovast.results_processing import index_query
    return sorted(t["table"] for t in index_query.list_tables(conn, campaign_id=campaign)
                  if t.get("rows"))


def _table_columns(conn, sql_name: str) -> "dict[str, str]":
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position",
        (sql_name,)).fetchall()
    if rows and isinstance(rows[0], dict):
        return {r["column_name"]: r["data_type"] for r in rows}
    return {r[0]: r[1] for r in rows}


def _check_table(conn, campaign: str, table: str, sql_name: str) -> "dict[str, str]":
    """Resolve a table to its columns, or explain what is missing and why.

    "Missing" is judged **for this campaign**, not for the index. Under one ``data.db`` per
    campaign the two were the same question; they no longer are, and taking the index's
    answer would silently retire the explanations below -- a campaign whose image shipped a
    scenario_execution predating ``--bt-log`` would stop being told why it has no behaviour
    tree the moment any other campaign in the index had one, and get an empty frame instead.
    """
    columns = _table_columns(conn, sql_name)
    present = _result_tables(conn, campaign)
    if columns and table in present:
        return columns

    available = ", ".join(present) or "(none)"
    hint = ""
    if table == "behaviors":
        # How a passing run ends up with no scenario tree. Silent by construction, so the
        # error is the only place it can be named.
        hint = (" A campaign has no behaviours table when its execution image ships a "
                "scenario_execution predating --bt-log: the flag is dropped rather than "
                "refused, so the run passes and writes nothing.")
    raise CampaignDataError(
        f"No table {table!r} in this campaign's results. Tables it has: {available}."
        + hint + _outdated_hint(conn))


def _check_columns(conn, table: str, columns: "dict[str, str]",
                   require: Iterable) -> None:
    missing = [c for c in require if c not in columns]
    if not missing:
        return
    raise CampaignDataError(
        f"Table {table!r} has no column(s) {', '.join(repr(c) for c in missing)}. "
        f"It has: {', '.join(columns)}." + _outdated_hint(conn))


def _scope_clause(campaign: str, level: str, config_name: Optional[str],
                  run_id: Optional[int], columns: "dict[str, str]") -> Tuple[str, list]:
    """The ``WHERE`` restricting a table to the selected node.

    **``campaign_id`` is always applied, and that is the load-bearing part.** Every campaign
    now shares one index, so the campaign level -- which used to mean "no filter", because the
    file already was the campaign -- would otherwise mean *all 153 of them*: a notebook opened
    on one campaign would silently plot the corpus, and the frame would look entirely
    ordinary. Widening a scope is the one failure here that produces no error and no empty
    result, so it is the one worth spending a predicate on unconditionally.

    Below that, scoping is applied only to tables actually keyed by run. The campaign-wide
    ones (``config_view``, and anything a future ingest adds) have no such columns, and
    filtering them would be an error rather than a narrowing.
    """
    clause = ["campaign_id = %s"] if "campaign_id" in columns else []
    values = [campaign] if clause else []
    if level != "campaign" and "config_name" in columns:
        clause.append("config_name = %s")
        values.append(config_name)
        if level == "run" and "run_id" in columns:
            clause.append("run_id = %s")
            values.append(run_id)
    if not clause:
        return "", []
    return "WHERE " + " AND ".join(clause), values


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
        where: Extra SQL predicate, ANDed with the scope restriction. Use ``%s``
            placeholders (the index is Postgres; ``?`` is a syntax error there).
        params: Values for those placeholders.
        with_params: Attach each scenario parameter of the owning run as a column — see
            :func:`attach_params`. What a metric varied *with* is usually the question, and
            the parameters live in ``runs`` rather than on the metric table.

    Returns:
        A DataFrame, empty (with the right columns) when the node produced no rows.
    """
    level, config_name, run_id = run_scope(data_dir)
    campaign = campaign_id(data_dir)
    conn = open_campaign_db(data_dir)
    try:
        sql_name = _sql_name(conn, table)
        available = _check_table(conn, campaign, table, sql_name)
        if require:
            _check_columns(conn, table, available, require)
        if columns:
            _check_columns(conn, table, available, columns)

        selected = ", ".join(f'"{c}"' for c in columns) if columns else "*"
        clause, values = _scope_clause(campaign, level, config_name, run_id, available)
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
    """Run *sql* against the index and return the result.

    The escape hatch, for joins and for the ``run_view``/``config_view`` views. Use ``%s``
    placeholders, or ``%(name)s`` named ones -- this is Postgres, not sqlite.

    **Not scoped, and that now means every campaign.** It always said so, but when each
    campaign had its own file the widest a query could reach was the campaign it was written
    for. One index holds all of them, so an unfiltered ``SELECT * FROM poses`` returns the
    whole corpus -- as a perfectly ordinary frame, with no error and nothing empty to notice.

    Which is also the feature: comparing the campaigns of a search arm is now a ``WHERE``
    rather than nine databases fetched and attached. So the id is bound for you as
    ``%(campaign_id)s``, always available whether *params* is a sequence or a mapping:

        read_sql(DATA_DIR, "SELECT * FROM poses WHERE campaign_id = %(campaign_id)s")

    Say it when you mean this campaign; leave it out when you mean more than one.
    """
    campaign = campaign_id(data_dir)
    if isinstance(params, dict):
        bound = {"campaign_id": campaign, **params}
    elif params:
        # Positional and named placeholders cannot be mixed in one psycopg statement, so a
        # caller passing positional params gets them through unchanged rather than a
        # confusing failure about %(campaign_id)s -- they can filter with a positional %s.
        bound = list(params)
    else:
        bound = {"campaign_id": campaign}
    conn = open_campaign_db(data_dir)
    try:
        return pd.read_sql(sql, conn, params=bound)
    finally:
        conn.close()
