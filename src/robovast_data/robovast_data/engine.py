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

"""SQL over campaign directories: the tables a query names are built, then DuckDB answers.

**The campaign directory is the database.** A query's connection is in-process and lives for
that query. It sees, per campaign in scope:

* every table as a view over the parquet files the campaign's ``.cache/MANIFEST.json`` names
  for the runs in scope -- never a directory listing, so a table being rewritten is seen whole
  or not at all;
* ``runs``, computed from ``campaign.db`` (:mod:`robovast_decode.runs`);
* the campaign's record under the ``campaign`` schema (:mod:`robovast_data.record`) and the
  views over it and over the tables (:mod:`robovast_data.views`).

Several campaigns are one query: each view is the union of theirs, with ``campaign_id`` in
every row.

**A table is built the first time something names it, and kept.** Before a query runs, the
tables it names -- and the tables the views it names read -- are built for the runs in scope
that do not have them yet, narrowed by the statement's ``WHERE`` where it restricts a table to
some runs (:mod:`robovast_data.statement`). A finished run's entry is final, so asking again
costs a lookup; a run still going is looked at again -- unless a live session
(:mod:`robovast_decode.live`) is writing it in parts, or deriving it whole again as the run
goes, whose entry carries a ``live`` stamp younger than :data:`LIVE_STALE_S` seconds: the
query reads what is written so far. A stamp older than that is an abandoned session's, and
the table is rebuilt whole. What a build could not do is reported with the answer, by table
and run, never dropped.

**What a connection may touch** is the campaigns' table files and nothing else: external access
is off except for those directories, the configuration is locked, the statement is a single
``SELECT`` (checked on DuckDB's own parse), and a query that runs past its time is interrupted.

Two functions keep SQL already written against the earlier engines meaning what it meant:
``PERCENTILE(value, p)`` with ``p`` in 0..100, and ``REGEXP(pattern, value)`` as a search.
``MEDIAN``, ``STDDEV`` and ``VARIANCE`` are DuckDB's own.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import duckdb
import pyarrow as pa

from robovast_decode.authored import header, run_files
from robovast_decode.build import (CAMPAIGN_TABLES, DERIVED_TABLES, RECORDING_TABLE, Run,
                                   available_tables, build, find_runs, settled)
from robovast_decode.layout import decoder_config
from robovast_decode.compact import CompactReport
from robovast_decode.compact import compact as compact_campaign
from robovast_decode.runs import RUNS_TABLE, StoreError, build_runs
from robovast_decode.tables import (LIVE_STALE_S, MANIFEST, TABLES_DIR, FileCatalog,
                                    RunCatalog, cache_root, compacted_runs, merge_fragments,
                                    read_manifest, run_fragment, schema_of, written_here)

from . import record, views
from .statement import Narrowing, QueryError, Statement, parse

#: How long a query may run before it is interrupted, by default.
DEFAULT_TIMEOUT_S = 120.0

#: The record's tables, as a query names them.
RECORD_SCHEMA = "campaign"

#: Up to this many runs to build, a build runs in this process: starting worker processes
#: costs more than it saves.
_IN_PROCESS = 2

#: While worker processes build, what they built is entered in the manifest at most this
#: often: a reader sees progress, and the manifest is not rewritten once per run.
_MERGE_EVERY_S = 2.0

_MACROS = (
    "CREATE MACRO percentile(v, p) AS quantile_cont(v, greatest(0, least(100, p)) / 100.0)",
    "CREATE MACRO regexp(p, v) AS "
    "CASE WHEN v IS NULL OR p IS NULL THEN false ELSE regexp_matches(v, p) END",
)

_MANIFESTS: Dict[str, Tuple[tuple, dict]] = {}
_MANIFESTS_LOCK = threading.Lock()


def _manifest(campaign_dir: str) -> dict:
    """The campaign's manifest, parsed once per version of the file and shared: the engine
    reads it several times a query and never changes it.

    A manifest is only ever replaced whole by a rename, so the file's identity, size and
    modification time name one version of it.
    """
    path = os.path.join(cache_root(campaign_dir), MANIFEST)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return read_manifest(campaign_dir)
    key = (st.st_ino, st.st_size, st.st_mtime_ns)
    with _MANIFESTS_LOCK:
        held = _MANIFESTS.get(path)
        if held is not None and held[0] == key:
            return held[1]
    manifest = read_manifest(campaign_dir)
    with _MANIFESTS_LOCK:
        _MANIFESTS[path] = (key, manifest)
    return manifest


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class Scope:
    """What a query sees of one campaign: all of it, one configuration, or one run."""
    campaign_dir: str
    config_name: Optional[str] = None
    run_id: Optional[int] = None

    def __post_init__(self):
        object.__setattr__(self, "campaign_dir", os.path.abspath(self.campaign_dir))
        if self.run_id is not None and self.config_name is None:
            raise ValueError("a run is named by its configuration and its id")

    @property
    def campaign_id(self) -> str:
        return os.path.basename(self.campaign_dir)

    @property
    def whole(self) -> bool:
        return self.config_name is None

    def admits(self, config_name: Optional[str], run_id: Optional[int]) -> bool:
        if self.config_name is not None and config_name != self.config_name:
            return False
        return self.run_id is None or run_id == self.run_id

    def predicate(self) -> str:
        """SQL selecting this scope's rows of a relation with the context columns."""
        clauses = [f"campaign_id = {_quote(self.campaign_id)}"]
        if self.config_name is not None:
            clauses.append(f"config_name = {_quote(self.config_name)}")
        if self.run_id is not None:
            clauses.append(f"run_id = {int(self.run_id)}")
        return "(" + " AND ".join(clauses) + ")"


@dataclass
class Problem:
    """A table a query needed that could not be built for a run, or that holds only part of
    what the run recorded (*partial*), and why."""
    campaign_id: str
    table: str
    run: str
    reason: str
    partial: bool = False

    def __str__(self) -> str:
        what = "incomplete" if self.partial else "not built"
        return f"{self.table} {what} for {self.campaign_id}/{self.run}: {self.reason}"


@dataclass
class Prepared:
    """A query ready to run: its rewritten SQL and what building for it could not do."""
    statement: Statement
    problems: List[Problem] = field(default_factory=list)


@dataclass
class Built:
    """What :meth:`Engine.build` did: the tables it could not build for some run, and what
    compacting did, per campaign."""
    problems: List[Problem]
    compacted: Dict[str, CompactReport]


@dataclass
class _Files:
    """Where one table's rows are, across the scopes."""
    #: Campaign files: rows of every run compacted into them, or of an imported export.
    campaign: List[str] = field(default_factory=list)
    #: Each run's own files.
    runs: List[str] = field(default_factory=list)
    #: ``campaign_id/config/run`` of the runs compacted into a campaign file and written again
    #: since, whose rows there are superseded by their own files.
    rewritten: List[str] = field(default_factory=list)
    schemas: List[List[List[str]]] = field(default_factory=list)


def _read_parquet(files: List[str]) -> str:
    return "read_parquet([" + ", ".join(_quote(f) for f in files) + "], union_by_name = true)"


def _build_one(campaign_dir: str, tables: Sequence[str], run_key: str, config: dict,
               fragment: dict):
    """One run's build in a worker process, against its own entries in memory; what it
    entered, for the parent to merge."""
    catalog = RunCatalog(fragment)
    build(campaign_dir, tables=list(tables), runs=[run_key], config=config, catalog=catalog)
    return campaign_dir, catalog.manifest, catalog.superseded


class _Merger:
    """What worker processes built, entered in each campaign's manifest in batches."""

    def __init__(self):
        self.pending: Dict[str, List[Tuple[dict, List[str]]]] = {}
        self.last = time.monotonic()

    def add(self, campaign_dir: str, fragment: dict, superseded: List[str]) -> None:
        self.pending.setdefault(campaign_dir, []).append((fragment, superseded))
        if time.monotonic() - self.last >= _MERGE_EVERY_S:
            self.flush()

    def flush(self) -> None:
        for campaign_dir, done in self.pending.items():
            catalog = FileCatalog(campaign_dir)
            with catalog.update() as manifest:
                merge_fragments(manifest, (fragment for fragment, _ in done))
            catalog.discard([rel for _, superseded in done for rel in superseded])
        self.pending.clear()
        self.last = time.monotonic()


class Engine:
    """Queries over *scopes*: one or more campaigns, or parts of them.

    *workers* builds that many runs' tables at once in separate processes (``1`` builds in
    this process); *threads* and *memory_limit* bound what one query may use; *progress* is
    called with ``(done, total)`` while tables are built.
    """

    def __init__(self, scopes: Iterable[Scope], *, workers: Optional[int] = None,
                 threads: Optional[int] = None, memory_limit: Optional[str] = None,
                 timeout_s: Optional[float] = DEFAULT_TIMEOUT_S,
                 progress: Optional[Callable[[int, int], None]] = None):
        self.scopes = list(scopes)
        if not self.scopes:
            raise ValueError("a query needs at least one campaign")
        for scope in self.scopes:
            if not os.path.isdir(scope.campaign_dir):
                raise FileNotFoundError(f"no campaign directory at {scope.campaign_dir}")
        self.workers = workers if workers is not None else min(8, os.cpu_count() or 1)
        self.threads = threads or min(4, os.cpu_count() or 1)
        self.memory_limit = memory_limit
        self.timeout_s = timeout_s
        self.progress = progress

    # -- which runs ------------------------------------------------------------------------

    def _runs(self, scope: Scope) -> List[Run]:
        return [r for r in find_runs(scope.campaign_dir)
                if scope.admits(r.config_name, r.run_id)]

    # -- building ------------------------------------------------------------------------

    def ensure(self, tables: Iterable[str], narrowing: Optional[Dict[str, Narrowing]] = None
               ) -> List[Problem]:
        """Build *tables* for the runs in scope that lack them; what could not be built."""
        tables = sorted(set(tables) - CAMPAIGN_TABLES - {RUNS_TABLE})
        narrowing = narrowing or {}
        work: List[Tuple[Scope, dict, str, Tuple[str, ...]]] = []
        demanded: List[Tuple[Scope, str, str]] = []
        for scope in self.scopes:
            manifest = _manifest(scope.campaign_dir)
            config = decoder_config(scope.campaign_dir)
            for run in self._runs(scope):
                todo = []
                for table in tables:
                    narrowed = narrowing.get(table)
                    if narrowed is not None and not narrowed.admits(run.config_name,
                                                                    run.run_id):
                        continue
                    demanded.append((scope, table, run.key))
                    entry = manifest.get("tables", {}).get(table, {}).get("runs", {}).get(
                        run.key)
                    if settled(scope.campaign_dir, table, entry):
                        # Final, or a live session is appending its parts as the run records:
                        # the query reads the parts written so far.
                        continue
                    todo.append(table)
                if todo:
                    work.append((scope, config, run.key, tuple(todo),
                                 run_fragment(manifest, run.key)))
        self._run_builds(work)
        return self._problems(demanded)

    def _run_builds(self, work) -> None:
        total = len(work)
        if not total:
            return
        if self.workers <= 1 or total <= _IN_PROCESS:
            for done, (scope, config, key, todo, _) in enumerate(work, 1):
                build(scope.campaign_dir, tables=list(todo), runs=[key], config=config)
                if self.progress:
                    self.progress(done, total)
            return
        merger = _Merger()
        # Spawned, not forked: the caller may hold DuckDB's threads, which a fork copies
        # mid-flight.
        try:
            with ProcessPoolExecutor(max_workers=min(self.workers, total),
                                     mp_context=multiprocessing.get_context("spawn")) as pool:
                futures = [pool.submit(_build_one, scope.campaign_dir, todo, key, config,
                                       fragment)
                           for scope, config, key, todo, fragment in work]
                for done, future in enumerate(as_completed(futures), 1):
                    merger.add(*future.result())
                    if self.progress:
                        self.progress(done, total)
        finally:
            # What finished is entered even when a worker failed, so it is not built again.
            merger.flush()

    def build(self, tables: Optional[Iterable[str]] = None, *, compact: bool = True,
              progress: Optional[Callable[[str, int, int], None]] = None) -> "Built":
        """Build *tables* for every run in scope (every table the recordings can give, for
        ``None``), then compact each whole campaign's tables into one file each.

        *progress* is called with ``(phase, done, total)``: ``"build"`` counts runs,
        ``"compact"`` tables. Compacting needs the whole campaign in scope: a configuration or a run
        is built and left as it is.
        """
        if tables is not None:
            names = set(tables)
        else:
            names = set()
            for scope in self.scopes:
                names |= set(available_tables(scope.campaign_dir,
                                              decoder_config(scope.campaign_dir)))
        saved = self.progress
        self.progress = (lambda done, total: progress("build", done, total)) if progress else None
        try:
            problems = self.ensure(sorted(names))
        finally:
            self.progress = saved
        reports: Dict[str, CompactReport] = {}
        if compact:
            for scope in self.scopes:
                if scope.whole:
                    reports[scope.campaign_id] = compact_campaign(
                        scope.campaign_dir, workers=self.workers,
                        progress=(lambda d, t: progress("compact", d, t)) if progress else None)
        return Built(problems, reports)

    def _problems(self, demanded) -> List[Problem]:
        problems = []
        manifests: Dict[str, dict] = {}
        for scope, table, key in demanded:
            manifest = manifests.get(scope.campaign_dir)
            if manifest is None:
                manifest = manifests[scope.campaign_dir] = _manifest(scope.campaign_dir)
            entry = manifest.get("tables", {}).get(table, {}).get("runs", {}).get(key) or {}
            if entry.get("reason"):
                problems.append(Problem(scope.campaign_id, table, key, entry["reason"],
                                        partial=bool(entry.get("files"))))
        return problems

    # -- what a query names ----------------------------------------------------------------

    def pose_tables(self) -> List[str]:
        """Tables that may follow the pose contract: ``poses``, ``sim_poses`` and every run
        file that does."""
        names = {"poses", "sim_poses"}
        for scope in self.scopes:
            for run in self._runs(scope):
                for table, path in run_files(run.path, reserved=DERIVED_TABLES).tables.items():
                    columns = header(path)
                    if columns is not None and views.is_pose_table(
                            {"campaign_id", "config_name", "run_id", *columns}):
                        names.add(table)
        return sorted(names)

    def tables_for(self, relations: Iterable[str]) -> List[str]:
        """The tables to build for a query naming *relations*."""
        out = set()
        for relation in relations:
            schema, _, relation = relation.rpartition(".")
            if schema not in ("", "main") or relation in (
                    RUNS_TABLE, "run_view", "config_view", "container_failure_view"):
                continue
            if relation == "pose_track_view":
                out.update(self.pose_tables())
            elif relation in views.VIEW_TABLES:
                out.update(views.VIEW_TABLES[relation])
            else:
                out.add(relation)
        return sorted(out)

    def prepare(self, sql: str, registered: Iterable[str] = ()) -> Prepared:
        """*sql* parsed and checked, and the tables it names built for the runs in scope.

        *registered* are relations the caller supplies itself (:meth:`execute`'s *tables*);
        nothing is built for a name among them.
        """
        statement = parse(sql)
        registered = set(registered)
        tables = [t for t in self.tables_for(statement.relations) if t not in registered]
        narrowing = {t: n for t, n in statement.narrowing.items() if t in tables}
        return Prepared(statement, self.ensure(tables, narrowing))

    # -- the connection --------------------------------------------------------------------

    def _files(self, table: str) -> "_Files":
        """The parquet files of *table* for the scopes, and the schemas they were written with."""
        out = _Files()
        for scope in self.scopes:
            manifest = _manifest(scope.campaign_dir)
            entries = manifest.get("tables", {}).get(table, {}).get("runs", {})
            in_scope = None if scope.whole else {r.key for r in self._runs(scope)}
            root = cache_root(scope.campaign_dir)
            for key, entry in sorted(entries.items()):
                if in_scope is not None and key not in in_scope:
                    continue
                if not entry.get("files"):
                    continue
                out.runs.extend(os.path.join(root, f) for f in entry["files"])
                out.schemas.append(schema_of(manifest, entry))
            whole = manifest.get("tables", {}).get(table, {}).get("campaign")
            if whole and whole.get("files"):
                out.campaign.extend(os.path.join(root, f) for f in whole["files"])
                out.schemas.append(schema_of(manifest, whole))
                # A run compacted into the campaign file and written again since has its own
                # files, and its rows are read from those.
                compacted = compacted_runs(manifest, table)
                out.rewritten.extend(f"{scope.campaign_id}/{key}"
                                     for key in whole.get("compacted") or [] if key not in compacted)
        return out

    def _examined(self, table: str) -> bool:
        """Has *table* been looked for in some run in scope, whether or not it has rows?"""
        for scope in self.scopes:
            runs = _manifest(scope.campaign_dir).get("tables", {}).get(table, {}).get(
                "runs", {})
            keys = runs if scope.whole else [r.key for r in self._runs(scope) if r.key in runs]
            if any(runs[k].get("files") or runs[k].get("known") or runs[k].get("compacted")
                   for k in keys):
                return True
        return False

    def _define_table(self, con, table: str) -> Optional[set]:
        """Define *table* as a view over its files; its columns, or ``None`` if it has none.

        A table looked for in the runs in scope but with rows in none of them is defined
        empty, so a query over it answers "nothing" beside the reasons rather than "no such
        table".
        """
        files = self._files(table)
        if not (files.campaign or files.runs):
            if self._examined(table):
                con.execute(f"CREATE VIEW {_ident(table)} AS SELECT "
                            "CAST(NULL AS VARCHAR) AS campaign_id, "
                            "CAST(NULL AS VARCHAR) AS config_name, "
                            "CAST(NULL AS BIGINT) AS run_id WHERE false")
                return {"campaign_id", "config_name", "run_id"}
            return None
        parts = []
        if files.campaign:
            part = f"SELECT * FROM {_read_parquet(files.campaign)}"
            if files.rewritten:
                listed = ", ".join(_quote(k) for k in files.rewritten)
                part += (" WHERE campaign_id || '/' || config_name || '/' || "
                         f"CAST(run_id AS VARCHAR) NOT IN ({listed})")
            parts.append(part)
        if files.runs:
            parts.append(f"SELECT * FROM {_read_parquet(files.runs)}")
        # A campaign-level file holds every run's rows, so a scope narrower than the campaign
        # is applied to the rows as well as to the file set.
        where = ("" if all(s.whole for s in self.scopes)
                 else " WHERE " + " OR ".join(s.predicate() for s in self.scopes))
        body = (parts[0] if len(parts) == 1
                else "SELECT * FROM (" + " UNION ALL BY NAME ".join(parts) + ")")
        con.execute(f"CREATE VIEW {_ident(table)} AS SELECT * FROM ({body}){where}")
        return {name for schema in files.schemas for name, _ in schema}

    def _define_record(self, con) -> None:
        by_table: Dict[str, List[pa.Table]] = {}
        for scope in {s.campaign_dir: s for s in self.scopes}.values():
            for name, rows in record.read_record(scope.campaign_dir, scope.campaign_id).items():
                by_table.setdefault(name, []).append(rows)
        con.execute(f"CREATE SCHEMA {RECORD_SCHEMA}")
        merged = {name: pa.concat_tables(parts, promote_options="default")
                  for name, parts in by_table.items()}
        for name, rows in merged.items():
            self._materialise(con, f"{RECORD_SCHEMA}.{_ident(name)}", rows)
        if "campaign" in merged:
            self._materialise(con, "config_view", record.config_tree(merged["campaign"]))
        if "container_failure" in merged:
            self._materialise(con, "container_failure_view",
                              record.container_failures(merged["container_failure"]))
        unit_columns = set(merged["unit"].column_names) if "unit" in merged else set()
        run_view = views.run_view_sql(set(merged), unit_columns)
        if run_view:
            where = " OR ".join(s.predicate() for s in self.scopes)
            con.execute(f"CREATE VIEW run_view AS SELECT * FROM ({run_view}) WHERE {where}")

    def _define_runs(self, con) -> None:
        parts = []
        for scope in self.scopes:
            try:
                rows = build_runs(scope.campaign_dir)
            except StoreError as exc:
                raise QueryError(f"the runs of {scope.campaign_id} cannot be read: {exc}") from exc
            if not scope.whole:
                keep = [scope.admits(c, r) for c, r in zip(rows.column("config_name").to_pylist(),
                                                           rows.column("run_id").to_pylist())]
                rows = rows.filter(pa.array(keep))
            parts.append(rows)
        self._materialise(con, RUNS_TABLE, pa.concat_tables(parts, promote_options="default"))

    @staticmethod
    def _materialise(con, name: str, rows: pa.Table) -> None:
        con.register("_incoming", rows)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM _incoming")
        con.unregister("_incoming")

    def connect(self, relations: Iterable[str] = (), *, writable: Optional[str] = None,
                tables: Optional[Dict[str, object]] = None) -> duckdb.DuckDBPyConnection:
        """A locked-down connection defining *relations* (and what they read) for the scopes.

        *writable* is one directory a ``COPY ... TO`` on this connection may write into --
        what an export uses to write the tables as files. Every other path stays off limits:
        the connection reads the campaigns' table files and writes there and nowhere else.
        *tables* are the caller's own relations (a DataFrame or an Arrow table each),
        registered under their names; a campaign table of the same name is not defined.
        """
        config = {"threads": self.threads}
        if self.memory_limit:
            config["memory_limit"] = self.memory_limit
        con = duckdb.connect(config=config)
        try:
            for macro in _MACROS:
                con.execute(macro)
            # ``runs`` first: ``run_view`` reads its ``live`` column.
            self._define_runs(con)
            self._define_record(con)
            for name, rows in (tables or {}).items():
                con.register(name, rows)
            relations = set(relations)
            columns: Dict[str, set] = {}
            for table in self.tables_for(relations):
                if tables and table in tables:
                    continue
                found = self._define_table(con, table)
                if found is not None:
                    columns[table] = found
            if ("run_validity_view" in relations
                    and views.VALIDITY_COLUMNS <= columns.get("system_usage", set())):
                con.execute("CREATE VIEW run_validity_view AS "
                            + views.run_validity_sql(columns["system_usage"]))
            if "pose_track_view" in relations:
                pose_track = views.pose_track_sql(columns)
                if pose_track:
                    con.execute("CREATE VIEW pose_track_view AS " + pose_track)
            directories = [os.path.join(cache_root(d), TABLES_DIR) + os.sep
                           for d in sorted({s.campaign_dir for s in self.scopes})]
            if writable:
                directories.append(os.path.abspath(writable) + os.sep)
            allowed = ", ".join(_quote(d) for d in directories)
            con.execute(f"SET allowed_directories = [{allowed}]")
            con.execute("SET enable_external_access = false")
            con.execute("SET lock_configuration = true")
        except Exception:
            con.close()
            raise
        return con

    # -- queries ---------------------------------------------------------------------------

    @contextmanager
    def execute(self, sql: str, params=None, tables: Optional[Dict[str, object]] = None
                ) -> Iterator[Tuple[duckdb.DuckDBPyConnection, List[Problem]]]:
        """Run *sql*; yields the connection holding its result and the build problems.

        The caller reads the result from the connection (``fetchmany``, ``fetch_arrow_table``,
        ``df``) inside the ``with`` block; the connection is closed on the way out. *tables*
        are the caller's own relations the query may name (:meth:`connect`).
        """
        prepared = self.prepare(sql, registered=tables or ())
        con = self.connect(prepared.statement.relations, tables=tables)
        timer = None
        if self.timeout_s:
            timer = threading.Timer(self.timeout_s, con.interrupt)
            timer.start()
        try:
            try:
                con.execute(prepared.statement.sql, params or [])
            except duckdb.InterruptException as exc:
                raise QueryError(f"the query ran past {self.timeout_s:g} s and was "
                                 "stopped") from exc
            except duckdb.Error as exc:
                message = _explain(exc, prepared.statement.relations, con)
                if prepared.problems:
                    message += "\n" + "\n".join(str(p) for p in prepared.problems[:10])
                raise QueryError(message) from exc
            yield con, prepared.problems
        finally:
            if timer is not None:
                timer.cancel()
            con.close()

    def arrow(self, sql: str, params=None) -> pa.Table:
        """The result of *sql* as an Arrow table."""
        with self.execute(sql, params) as (con, _problems):
            return con.to_arrow_table()

    # -- the catalog -----------------------------------------------------------------------

    def catalog(self) -> Dict[str, dict]:
        """Every table and view the scopes can answer, without building anything.

        ``{name: {"kind": "table"|"view"|"record", "runs": n, "built": n,
        "failed": {run: reason}, "columns": [[name, type], ...] | None}}``. A table's columns
        are known once it is built for some run by this decoder under its contract; ``None``
        until then.
        """
        out: Dict[str, dict] = {}
        for scope in self.scopes:
            keys = None if scope.whole else [r.key for r in self._runs(scope)]
            counts = available_tables(scope.campaign_dir, decoder_config(scope.campaign_dir),
                                      runs=keys)
            manifest = _manifest(scope.campaign_dir)
            if RECORDING_TABLE in manifest.get("tables", {}):
                recorded = manifest["tables"][RECORDING_TABLE]["runs"]
                in_scope = [k for k in recorded if keys is None or k in keys]
                counts.setdefault(RECORDING_TABLE, {"runs": len(in_scope), "built": len(in_scope),
                                                    "failed": {}})
            # A table the copy carries whole (an export's) is there for every run it holds,
            # whether or not the records here could build it.
            for table, table_entry in manifest.get("tables", {}).items():
                whole = table_entry.get("campaign")
                if whole and table not in counts:
                    held = whole.get("runs")
                    held = len(self._runs(scope)) if held is None else held
                    counts[table] = {"runs": held, "built": held, "failed": {}}
            for table, count in counts.items():
                entry = out.setdefault(table, {"kind": "table", "runs": 0, "built": 0,
                                               "failed": {}, "columns": None, "rows": 0})
                entry["runs"] += count["runs"]
                entry["built"] += count["built"]
                built = manifest.get("tables", {}).get(table, {}).get("runs", {})
                entry["rows"] += sum(e.get("rows", 0) for k, e in built.items()
                                     if (keys is None or k in keys) and written_here(e))
                entry["failed"].update({f"{scope.campaign_id}/{k}": v
                                        for k, v in count["failed"].items()})
                table_entry = manifest.get("tables", {}).get(table, {})
                held_whole = table_entry.get("campaign")
                # The first type seen for a name is the one listed; one pass per column,
                # since a table can be thousands of columns wide.
                seen = {c for c, _ in entry["columns"] or []}
                current = [e for e in table_entry.get("runs", {}).values() if written_here(e)]
                for run_entry in current + ([held_whole] if held_whole else []):
                    for name, kind in schema_of(manifest, run_entry):
                        if name in seen:
                            continue
                        seen.add(name)
                        columns = entry["columns"] = entry["columns"] or []
                        columns.append([name, kind])
        ready = [name for name, reads in views.VIEW_TABLES.items()
                 if all(out.get(t, {}).get("built", 0) >= out.get(t, {}).get("runs", 0)
                        for t in reads)]
        con = self.connect(ready)
        try:
            for name, kind in _relations(con):
                if out.get(name, {}).get("kind") == "table":
                    continue      # a measurement table: its counts come from the manifest
                rows = con.execute(f"SELECT count(*) FROM {_qualified(name)}").fetchone()[0]
                out[name] = {"kind": "view" if name in views.VIEWS else kind, "runs": None,
                             "built": None, "failed": {}, "columns": _columns(con, name),
                             "rows": rows}
        finally:
            con.close()
        for name, reads in views.VIEW_TABLES.items():
            if name in out:
                continue
            # A view not defined over what is built is listed while a table it reads is still
            # to be built; once all of them are, it is absent because what it needs was never
            # recorded, and listing it would promise an answer the campaign cannot give.
            if reads and all(out.get(t, {}).get("built", 0) >= out.get(t, {}).get("runs", 0)
                             for t in reads):
                continue
            out[name] = {"kind": "view", "runs": None, "built": None, "failed": {},
                         "columns": None, "rows": None}
        return out


def _relations(con) -> List[Tuple[str, str]]:
    rows = con.execute(
        "SELECT schema_name, table_name, 'table' FROM duckdb_tables() WHERE NOT internal "
        "UNION ALL SELECT schema_name, view_name, 'view' FROM duckdb_views() "
        "WHERE NOT internal AND schema_name IN ('main', 'campaign') ORDER BY 1, 2").fetchall()
    out = []
    for schema, name, kind in rows:
        qualified = name if schema == "main" else f"{schema}.{name}"
        out.append((qualified, "record" if schema == RECORD_SCHEMA else kind))
    return out


def _qualified(name: str) -> str:
    schema, _, table = name.rpartition(".")
    return f"{_ident(schema)}.{_ident(table)}" if schema else _ident(table)


def _columns(con, name: str) -> List[List[str]]:
    schema, _, table = name.rpartition(".")
    rows = con.execute("SELECT column_name, data_type FROM duckdb_columns() "
                       "WHERE schema_name = ? AND table_name = ? ORDER BY column_index",
                       [schema or "main", table]).fetchall()
    return [[c, t] for c, t in rows]


def _explain(exc: duckdb.Error, relations, con) -> str:
    """DuckDB's message, with the relations a missing one could have been."""
    message = str(exc)
    if isinstance(exc, duckdb.CatalogException) and "does not exist" in message:
        known = ", ".join(sorted(name for name, _ in _relations(con)))
        missing = ", ".join(sorted(relations))
        message += (f"\nNamed: {missing}. A table exists once a run in scope recorded "
                    f"something for it. Defined here: {known}.")
    return message


__all__ = ["Built", "DEFAULT_TIMEOUT_S", "Engine", "LIVE_STALE_S", "Prepared", "Problem",
           "RECORD_SCHEMA", "Scope"]
