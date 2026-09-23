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
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
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
    """One column as an Arrow array; a column mixing types falls back to text, as today."""
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


def write_table(campaign_dir: str, rel_path: str, table: pa.Table) -> int:
    """Write *table* to *rel_path* under the cache root, atomically; its size in bytes."""
    path = os.path.join(cache_root(campaign_dir), rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".incoming"
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, path)
    return os.path.getsize(path)


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
    tmp = path + ".incoming"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


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
                     schema: pa.Schema, sources: dict, complete: bool) -> None:
    """Enter one run's contribution to *table* in *manifest* (in memory)."""
    entry = manifest["tables"].setdefault(table, {"runs": {}})
    entry["runs"][run_key] = {
        "files": files,
        "rows": rows,
        "schema": _schema_id(manifest, schema),
        "sources": sources,
        "complete": complete,
        "decoder": __version__,
    }


def record_run_absent(manifest: dict, table: str, run_key: str, *, sources: dict,
                      complete: bool, reason: Optional[str] = None) -> None:
    """Enter that a run has no rows for *table*: it recorded nothing for it, or *reason*.

    Recorded, not left out, so that asking again for a finished run's table costs a lookup
    rather than another look at its recordings; *reason* is why a build failed, which a
    reader reports beside its answer.
    """
    entry = manifest["tables"].setdefault(table, {"runs": {}})
    entry["runs"][run_key] = {
        "files": [],
        "rows": 0,
        "schema": None,
        "sources": sources,
        "complete": complete,
        "decoder": __version__,
        "reason": reason,
    }


__all__ = ["CACHE_DIR", "CONTEXT_COLUMNS", "MANIFEST", "TableBuffer", "cache_root", "fixed",
           "leading_then_sorted", "manifest_lock", "read_manifest", "record_run_absent",
           "record_run_table", "run_table_path", "schema_of", "write_manifest", "write_table"]
