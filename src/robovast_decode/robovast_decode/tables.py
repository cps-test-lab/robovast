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

"""Rows into columns, columns into parquet, and the manifest that says what exists.

**A table is built column-wise from its first row on.** The row's keys fix the table's column
plan, and every later row appends to the plan's lists; a key the plan has not seen extends it
and backfills ``None``. Building a dict per row and deciding columns at the end is where a
decoder spends its time -- measured, it is ten times the cost of deserialising -- so this is
the part of the pipeline that is shaped for speed.

**The manifest is the catalogue.** ``.cache/MANIFEST.json`` names, per table and run, exactly
the files that make up that table, their schema, and the version of the decoder that wrote
them. Readers build their views from it and never from a directory listing, so a reader that
arrives while a table is being rewritten sees the old set or the new one, never a mixture:
files are written first, then the manifest is replaced in one ``rename``.

**A table being written while its run records** is a list of parts. A session following the
recording appends a part per batch and stamps the entry ``live``; a reader reads every file
the entry names, so a query during a run sees what has been decoded so far. When the run
ends the parts are merged into the run's one file, the entry loses its stamp and is
``complete``. A derived table a watcher rebuilds whole as the run goes is one file, replaced
on each derivation, under the same stamp. A stamp older than :data:`LIVE_STALE_S` means the
session died, and the entry is rebuilt whole like any incomplete one.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from typing import Callable, Dict, Iterable, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from . import __version__

#: The campaign-level directory every derived file lives under. RoboVAST already treats a
#: ``.cache`` directory as rebuildable: archives and exports leave it out.
CACHE_DIR = ".cache"
TABLES_DIR = "tables"
MANIFEST = "MANIFEST.json"
MANIFEST_VERSION = 1

#: Columns that say which run a row belongs to, first in every table.
CONTEXT_COLUMNS = ("campaign_id", "config_name", "run_id")

#: A run's entry whose ``live`` stamp is older than this many seconds has been abandoned by
#: the session that was writing it in parts: a builder rebuilds it whole. Younger, the
#: session owns it and a builder leaves it alone.
LIVE_STALE_S = 30.0


class TableBuffer:
    """One table's rows, accumulated column-wise."""

    def __init__(self, name: str):
        self.name = name
        self.columns: Dict[str, list] = {}
        self.count = 0
        self._plan: tuple = ()
        self._lists: tuple = ()

    def add(self, row: dict) -> None:
        if tuple(row) == self._plan:
            for column, value in zip(self._lists, row.values()):
                column.append(value)
        else:
            columns = self.columns
            for key, value in row.items():
                column = columns.get(key)
                if column is None:
                    column = columns[key] = [None] * self.count
                column.append(value)
            for key, column in columns.items():
                if len(column) == self.count:
                    column.append(None)
            if len(row) == len(columns):
                self._plan = tuple(row)
                self._lists = tuple(columns[k] for k in self._plan)
        self.count += 1

    def to_arrow(self, order: Optional[Callable[[List[str]], List[str]]] = None,
                 context: Optional[dict] = None) -> pa.Table:
        for name in getattr(order, "fieldnames", ()):
            self.columns.setdefault(name, [None] * self.count)
        names = list(self.columns)
        if order is not None:
            names = order(names)
        arrays = {}
        if context:
            for key in CONTEXT_COLUMNS:
                if key in context:
                    arrays[key] = pa.array([context[key]] * self.count)
        for name in names:
            arrays[name] = _column_array(self.columns[name])
        return pa.table(arrays)


def _column_array(values: list) -> pa.Array:
    """One column as an Arrow array; a column mixing types falls back to text."""
    try:
        return pa.array(values)
    except (pa.ArrowInvalid, pa.ArrowTypeError):
        return pa.array([None if v is None else str(v) for v in values], type=pa.string())


def leading_then_sorted(*leading: str) -> Callable[[List[str]], List[str]]:
    """An order: *leading* first (those present), every other column sorted."""
    def order(names: List[str]) -> List[str]:
        first = [n for n in leading if n in names]
        return first + sorted(n for n in names if n not in first)
    return order


def fixed(fieldnames: Iterable[str]) -> Callable[[List[str]], List[str]]:
    """An order: exactly *fieldnames*, in that order, then anything else sorted.

    A table with a fixed schema has those columns even when it has no rows.
    """
    fieldnames = list(fieldnames)

    def order(names: List[str]) -> List[str]:
        first = [n for n in fieldnames if n in names]
        return first + sorted(n for n in names if n not in first)
    order.fieldnames = fieldnames
    return order


# -- where things live ------------------------------------------------------------------------

def cache_root(campaign_dir: str) -> str:
    return os.path.join(campaign_dir, CACHE_DIR)


def run_table_path(campaign_dir: str, table: str, config_name: str, run_id) -> str:
    """The file a run's finished table lives in, relative to the cache root."""
    return os.path.join(TABLES_DIR, table, config_name, f"{run_id}.parquet")


def run_part_path(campaign_dir: str, table: str, config_name: str, run_id, index: int) -> str:
    """One part of a run's table while it is being written, relative to the cache root.

    Parts are numbered in the order they were written; the manifest names the ones that
    make up the table, and :func:`run_table_path` is where they end up merged.
    """
    return os.path.join(TABLES_DIR, table, config_name, str(run_id), f"part-{index:04d}.parquet")


def _incoming(path: str) -> str:
    """A temporary name beside *path* that no other writer of *path* shares.

    Two requests can build the same table at once; each writes its own file and the last
    rename wins, where one shared name would let one writer rename away the other's file.
    """
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                               prefix=os.path.basename(path) + ".", suffix=".incoming")
    os.close(fd)
    return tmp


def write_table(campaign_dir: str, rel_path: str, table: pa.Table) -> int:
    """Write *table* to *rel_path* under the cache root, atomically; its size in bytes."""
    path = os.path.join(cache_root(campaign_dir), rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = _incoming(path)
    try:
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return os.path.getsize(path)


def remove_files(campaign_dir: str, rel_paths: Iterable[str]) -> None:
    """Delete table files the manifest no longer names, and the part directories left empty.

    Called after the manifest that dropped them is written, so a reader that arrives now
    never names a file that is gone; a reader that has one open keeps its handle.
    """
    for rel in rel_paths:
        path = os.path.join(cache_root(campaign_dir), rel)
        try:
            os.unlink(path)
        except FileNotFoundError:
            continue
        parent = os.path.dirname(path)
        if os.path.basename(path).startswith("part-") and not os.listdir(parent):
            os.rmdir(parent)


# -- the manifest ----------------------------------------------------------------------------

@contextmanager
def manifest_lock(campaign_dir: str):
    """Serialise manifest updates across processes (one writer at a time)."""
    root = cache_root(campaign_dir)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, ".lock"), "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def run_lock(campaign_dir: str, run_key: str):
    """Serialise the builds of one run's tables, across threads and processes.

    Two requests that name the same run -- a run view opens several panels at once -- would
    otherwise both decode its recordings and both write its tables. Holding this, the second
    finds what the first wrote current and reads it. Runs lock separately, so building one
    run does not wait for another.
    """
    root = os.path.join(cache_root(campaign_dir), ".locks")
    os.makedirs(root, exist_ok=True)
    name = run_key.replace(os.sep, "__") + ".lock"
    with open(os.path.join(root, name), "a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def read_manifest(campaign_dir: str) -> dict:
    path = os.path.join(cache_root(campaign_dir), MANIFEST)
    if not os.path.isfile(path):
        return {"version": MANIFEST_VERSION, "tables": {}}
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    if manifest.get("version") != MANIFEST_VERSION:
        raise ValueError(
            f"{path} is manifest version {manifest.get('version')}, this decoder reads "
            f"{MANIFEST_VERSION}; clear the campaign's table cache and it is rebuilt")
    return manifest


def write_manifest(campaign_dir: str, manifest: dict) -> None:
    path = os.path.join(cache_root(campaign_dir), MANIFEST)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = _incoming(path)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _schema_id(manifest: dict, schema: pa.Schema) -> str:
    """The id *schema* is stored under in *manifest*: one copy per distinct schema, not per run."""
    fields = [[f.name, str(f.type)] for f in schema]
    key = hashlib.sha1(json.dumps(fields).encode()).hexdigest()[:16]
    manifest.setdefault("schemas", {})[key] = fields
    return key


def schema_of(manifest: dict, entry: dict) -> List[List[str]]:
    """``[[column, type], ...]`` of one run's entry; ``[]`` for a run with nothing for it."""
    key = entry.get("schema")
    return manifest.get("schemas", {}).get(key, []) if key else []


def record_run_table(manifest: dict, table: str, run_key: str, *, files: List[str], rows: int,
                     schema: pa.Schema, sources: dict, complete: bool,
                     live: Optional[float] = None) -> List[str]:
    """Enter one run's contribution to *table* in *manifest* (in memory).

    *files* are every file the run's table is made of: its one finished file, or the parts
    written so far. *live* is the epoch time the session writing those parts last wrote, or
    ``None`` for an entry nobody is appending to. Returns the files the entry named before
    and no longer does, for the caller to remove once the manifest is written.
    """
    entry = manifest["tables"].setdefault(table, {"runs": {}})
    before = entry["runs"].get(run_key) or {}
    record = {
        "files": files,
        "rows": rows,
        "schema": _schema_id(manifest, schema),
        "sources": sources,
        "complete": complete,
        "decoder": __version__,
    }
    if live is not None:
        record["live"] = live
    entry["runs"][run_key] = record
    return [f for f in before.get("files") or [] if f not in files]


def live_owned(entry: Optional[dict], now: Optional[float] = None) -> bool:
    """Whether a session is writing *entry* in parts right now (its ``live`` is fresh).

    Such an entry is left to that session: a build that replaced it whole would race the
    parts it is appending. An entry whose stamp is older than :data:`LIVE_STALE_S` was
    abandoned (the session died) and is anyone's to rebuild.
    """
    if not entry or entry.get("live") is None:
        return False
    return (now if now is not None else time.time()) - entry["live"] < LIVE_STALE_S


def campaign_table_path(table: str) -> str:
    """The file a campaign-level table lives in, relative to the cache root."""
    return os.path.join(TABLES_DIR, table, "_campaign.parquet")


def record_campaign_table(manifest: dict, table: str, *, files: List[str], rows: int,
                          schema: pa.Schema, sources: dict) -> None:
    """Enter a table written for the whole campaign at once in *manifest* (in memory).

    Its rows carry ``config_name`` and ``run_id`` like any table's, so a reader scoped to one
    run reads that run's rows of it.
    """
    entry = manifest["tables"].setdefault(table, {"runs": {}})
    entry["campaign"] = {
        "files": files,
        "rows": rows,
        "schema": _schema_id(manifest, schema),
        "sources": sources,
        "complete": True,
        "decoder": __version__,
    }


def record_run_absent(manifest: dict, table: str, run_key: str, *, sources: dict,
                      complete: bool, reason: Optional[str] = None, known: bool = False,
                      live: Optional[float] = None) -> List[str]:
    """Enter that a run has no rows for *table*: it recorded nothing for it, or *reason*.

    Recorded, not left out, so that asking again for a finished run's table costs a lookup
    rather than another look at its recordings; *reason* is why a build failed, which a
    reader reports beside its answer. *known* says the run's records can give the table and
    it came out empty, as against a table this run never had. *live* is the stamp of a
    watcher that derives the table whole as the run goes and found no rows yet, so the
    entry stays its own. Returns the files the entry named before, for the caller to remove
    once the manifest is written.
    """
    entry = manifest["tables"].setdefault(table, {"runs": {}})
    before = entry["runs"].get(run_key) or {}
    record = {
        "files": [],
        "rows": 0,
        "schema": None,
        "sources": sources,
        "complete": complete,
        "decoder": __version__,
        "reason": reason,
        "known": known or reason is not None,
    }
    if live is not None:
        record["live"] = live
    entry["runs"][run_key] = record
    return list(before.get("files") or [])


__all__ = ["CACHE_DIR", "CONTEXT_COLUMNS", "LIVE_STALE_S", "MANIFEST", "TableBuffer",
           "cache_root", "campaign_table_path", "fixed", "leading_then_sorted", "live_owned",
           "manifest_lock", "read_manifest", "record_campaign_table", "record_run_absent",
           "record_run_table", "remove_files", "run_part_path", "run_table_path", "schema_of",
           "write_manifest", "write_table"]
