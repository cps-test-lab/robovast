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

"""A run's own data files as tables: any ``*.csv`` or ``*.jsonl`` a run directory holds.

This is how anything that is not a recording contributes data with no registration: a
scenario's metrics file, a simulator's per-row pose stream, a postprocessing plugin's output,
scenario-execution's behaviour-tree log. The table is named after the file (``out.csv`` ->
``out``), its column types are inferred from its values (:mod:`robovast_decode.types`), and a
table carrying a quaternion gets its heading as ``orientation.yaw``.

* A ``#`` preamble before a CSV's header is skipped: it is how a producer states what its
  columns mean.
* A JSONL file is read by the format its first record declares; the behaviour-tree log is the
  one format known (:data:`JSONL_READERS`), and a file of another format is not a table. A
  format may give more than one table from one file: the behaviour-tree log's metadata record
  is the one-row table ``<name>_meta`` beside ``<name>`` (``behaviors_meta`` beside
  ``behaviors``), so what the log says about itself -- the scenario, the clock its stamps are
  in, when it started -- is read by the same query path as its rows.
* A JSONL line is a record once it ends in a newline. The last line of a file still being
  written may be a record half-written, which is dropped for that read and read whole once
  the writer has finished it; a terminated line that is not JSON is a corrupt file and
  refused.
* Two files in one run claiming one table is refused for that table, naming both: appending
  both would double every count through it.
* A CSV row with more fields than its header is refused for that file: the surplus has no
  column to go to.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import pyarrow as pa

from .types import INTEGER, REAL, TEXT, UNKNOWN, infer_column_types, stored_value

#: A table name longer than this is shortened and given a hash of the whole name, so two
#: long names that agree in their first characters stay two tables.
MAX_TABLE_NAME_BYTES = 63
_TABLE_NAME_HASH_LEN = 8

#: py_trees' status names -> the numeric codes the ``behaviors`` table has always carried.
_BT_STATUS_CODES = {"INVALID": 1, "RUNNING": 2, "SUCCESS": 3, "FAILURE": 4}

QUATERNION = ("orientation.x", "orientation.y", "orientation.z", "orientation.w")
YAW = "orientation.yaw"
YAW_NOTE = ("Derived from the quaternion as the heading about z: correct for a body in the "
            "plane, insufficient for one that has left it (read the quaternion then).")


def table_name(filename: str) -> str:
    """A data file's table: its stem, lower-cased, anything but ``[a-z0-9_]`` as ``_``."""
    stem = filename
    for suffix in (".csv", ".jsonl"):
        if stem.lower().endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    name = re.sub(r"[^a-zA-Z0-9_]", "_", stem).lower()
    if name and name[0].isdigit():
        name = "t_" + name
    name = name or "t_unknown"
    if len(name.encode()) <= MAX_TABLE_NAME_BYTES:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:_TABLE_NAME_HASH_LEN]
    head = name[: MAX_TABLE_NAME_BYTES - 1 - _TABLE_NAME_HASH_LEN].rstrip("_")
    return f"{head}_{digest}"


#: The suffix on a file's table naming the one-row table of its metadata record.
META_SUFFIX = "_meta"


def _behaviour_tree_tables(records: list) -> Dict[str, list]:
    """The tables in scenario-execution's behaviour-tree log, by suffix on the file's name.

    ``""`` is ``behaviors``: one row per record after the first, which is already one row per
    status change, gaining the numeric ``status`` beside its name (the columns ``nav2_behaviors``
    shares) and ``seq``, the record's position in the log. ``seq`` is what a fold over the
    table orders by: a later record replaces an earlier one for the same node, and two records
    of one node may share a ``timestamp``, so storage order is not a stand-in.

    ``"_meta"`` is ``behaviors_meta``: the first record, whole, as the one row of a table whose
    columns are its keys -- ``scenario``, ``clock``, ``started_at``, ``tick_period`` and
    whatever else the writer put there.
    """
    rows = []
    for seq, record in enumerate(records[1:], 1):
        row = dict(record)
        status_name = row.pop("status", None)
        row["status"] = _BT_STATUS_CODES.get(status_name)
        row["status_name"] = status_name
        row["seq"] = seq
        rows.append(row)
    return {"": rows, META_SUFFIX: [dict(records[0])]}


@dataclass(frozen=True)
class JsonlFormat:
    """One JSONL layout: the tables a file of it gives, and how its records become their rows.

    *tables* are suffixes on the file's table name, ``""`` for the file's own; *read* maps the
    records to ``{suffix: rows}`` for every suffix in *tables*.
    """
    tables: Tuple[str, ...]
    read: Callable[[list], Dict[str, list]]


#: JSONL ``format`` -> its layout. Both spellings of the behaviour-tree log's format name one
#: layout: scenario-execution has written either, and a run recorded with either is read.
_BEHAVIOUR_TREE_LOG = JsonlFormat(("", META_SUFFIX), _behaviour_tree_tables)
JSONL_READERS = {"behaviour_tree_log": _BEHAVIOUR_TREE_LOG,
                 "behavior_tree_log": _BEHAVIOUR_TREE_LOG}


class RaggedFile(ValueError):
    """A CSV row has more fields than its header."""


def _jsonl_records(path: str) -> list:
    """Every record in a JSONL file, the last line held back while it is being written.

    A line is a record once it ends in a newline. The file's writer appends a record per line
    and a reader may arrive mid-write, so an unterminated last line is a record in progress
    unless it already parses whole: then it is the last record of a file written without a
    trailing newline. A terminated line that does not parse is a corrupt file, and raises.
    """
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.split("\n")
    tail = lines.pop()
    records = [json.loads(line) for line in lines if line.strip()]
    if tail.strip():
        try:
            records.append(json.loads(tail))
        except ValueError:
            pass
    return records


def jsonl_format(path: str) -> Optional[str]:
    """The ``format`` a JSONL file's first record declares, or ``None`` when it declares none.

    Reads one line, so a run directory's files can be listed by the tables they give without
    reading them.
    """
    with open(path, encoding="utf-8") as fh:
        line = fh.readline()
    if not line.endswith("\n"):
        return None
    try:
        first = json.loads(line)
    except ValueError:
        return None
    return first.get("format") if isinstance(first, dict) else None


def read_tables(path: str) -> Dict[str, list]:
    """A data file's tables, ``{table: rows}``; ``{}`` for a JSONL file of an unknown format.

    A CSV gives the one table named after it. A JSONL file gives the tables its format
    declares (:data:`JSONL_READERS`), each named by the file's table and the format's suffix.

    Raises :class:`RaggedFile` for a CSV with a row longer than its header, and ``OSError``
    or ``ValueError`` for a file that cannot be read at all.
    """
    table = table_name(os.path.basename(path))
    if path.lower().endswith(".jsonl"):
        records = _jsonl_records(path)
        if not records or not isinstance(records[0], dict):
            return {}
        layout = JSONL_READERS.get(records[0].get("format"))
        if layout is None:
            return {}
        return {table + suffix: rows for suffix, rows in layout.read(records).items()}
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(line for line in fh if not line.startswith("#"))
        rows = list(reader)
        if any(None in row for row in rows):
            raise RaggedFile(f"a row has more fields than its header "
                             f"({len(reader.fieldnames or ())} columns)")
    return {table: rows}


def read_rows(path: str, table: Optional[str] = None) -> list:
    """The rows of *table* in a data file -- the file's own table when none is named.

    ``[]`` for a JSONL file of an unknown format, or for a table the file's format does not
    give. Raises as :func:`read_tables` does.
    """
    tables = read_tables(path)
    return tables.get(table if table is not None else table_name(os.path.basename(path)), [])


def header(path: str) -> Optional[List[str]]:
    """A CSV file's column names from its header line alone; ``None`` for a JSONL file.

    What a file's table holds, before building it: enough to tell whether it follows a
    contract such as the pose one.
    """
    if path.lower().endswith(".jsonl"):
        return None
    with open(path, encoding="utf-8", newline="") as fh:
        for line in fh:
            if not line.startswith("#"):
                return next(csv.reader([line]), [])
    return []


_ARROW = {INTEGER: pa.int64(), REAL: pa.float64(), TEXT: pa.string(), UNKNOWN: pa.null()}


def to_arrow(rows: List[dict], context: Optional[dict] = None) -> pa.Table:
    """Rows as a typed table: each column typed by every value it holds, in file order."""
    columns: List[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    types = infer_column_types(rows, columns)
    arrays: Dict[str, pa.Array] = {}
    for key, value in (context or {}).items():
        arrays[key] = pa.array([value] * len(rows))
    for column in columns:
        verdict = types[column]
        values = [stored_value(row.get(column), verdict) for row in rows]
        arrays[column] = pa.array(values, type=_ARROW[verdict])
    return with_yaw(pa.table(arrays))


def with_yaw(table: pa.Table) -> pa.Table:
    """*table* with ``orientation.yaw`` derived from its quaternion, when it has one and no yaw."""
    names = table.column_names
    if YAW in names or any(c not in names for c in QUATERNION):
        return table
    x, y, z, w = (table.column(c).to_pylist() for c in QUATERNION)
    yaw = []
    for qx, qy, qz, qw in zip(x, y, z, w):
        if None in (qx, qy, qz, qw):
            yaw.append(None)
        else:
            yaw.append(math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz)))
    return table.append_column(YAW, pa.array(yaw, type=pa.float64()))


@dataclass
class RunFiles:
    """A run directory's data files, by the table each would be."""
    tables: Dict[str, str] = field(default_factory=dict)      # table -> path
    refused: Dict[str, str] = field(default_factory=dict)     # table -> reason


def _tables_of(path: str) -> List[str]:
    """The tables *path* gives, by name: the file's own, and its format's companions."""
    table = table_name(os.path.basename(path))
    if not path.lower().endswith(".jsonl"):
        return [table]
    layout = JSONL_READERS.get(jsonl_format(path))
    if layout is None:
        return [table]
    return [table + suffix for suffix in layout.tables]


def run_files(run_dir: str, reserved=()) -> RunFiles:
    """Every ``*.csv`` and ``*.jsonl`` below *run_dir*, by table; conflicts refused.

    A file gives one table, or the several its format declares (:func:`read_tables`): each
    is listed against the file. *reserved* are tables something else builds for this run (a
    recording's tables, the tables RoboVAST derives): a file claiming one is refused, because
    its rows and the built ones would be the same table twice.
    """
    found = RunFiles()
    paths = []
    for root, dirs, files in os.walk(run_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        paths.extend(os.path.join(root, f) for f in sorted(files)
                     if f.lower().endswith((".csv", ".jsonl")))
    for path in sorted(paths):
        rel = os.path.relpath(path, run_dir)
        for table in _tables_of(path):
            if table in reserved:
                found.refused[table] = (f"{rel} would be the table '{table}', which is built "
                                        f"from the run's records; rename the file")
            elif table in found.tables:
                first = os.path.relpath(found.tables.pop(table), run_dir)
                found.refused[table] = f"two files claim the table: {first} and {rel}"
            elif table not in found.refused:
                found.tables[table] = path
    return found


__all__ = ["JSONL_READERS", "JsonlFormat", "MAX_TABLE_NAME_BYTES", "META_SUFFIX", "QUATERNION",
           "RaggedFile", "RunFiles", "YAW", "YAW_NOTE", "header", "jsonl_format", "read_rows",
           "read_tables", "run_files", "table_name", "to_arrow", "with_yaw"]
