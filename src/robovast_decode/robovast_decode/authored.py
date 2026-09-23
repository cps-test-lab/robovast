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
  one format known (:data:`JSONL_READERS`), and a file of another format is not a table.
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
from typing import Dict, List, Optional

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


def _behaviour_tree_rows(records: list) -> list:
    """``behaviors`` rows from scenario-execution's behaviour-tree log.

    The first record is metadata; the rest are one row per status change already, and gain
    the numeric ``status`` beside its name, the columns ``nav2_behaviors`` shares.
    """
    rows = []
    for record in records[1:]:
        row = dict(record)
        status_name = row.pop("status", None)
        row["status"] = _BT_STATUS_CODES.get(status_name)
        row["status_name"] = status_name
        rows.append(row)
    return rows


#: JSONL ``format`` -> the function turning its records into rows. Both spellings: the log's
#: format was renamed from ``behaviour_tree_log`` to ``behavior_tree_log``, and a run
#: recorded with either is read.
JSONL_READERS = {"behaviour_tree_log": _behaviour_tree_rows,
                 "behavior_tree_log": _behaviour_tree_rows}


class RaggedFile(ValueError):
    """A CSV row has more fields than its header."""


def read_rows(path: str) -> list:
    """A data file's rows as dicts; ``[]`` for a JSONL file of an unknown format.

    Raises :class:`RaggedFile` for a CSV with a row longer than its header, and ``OSError``
    or ``ValueError`` for a file that cannot be read at all.
    """
    if path.lower().endswith(".jsonl"):
        with open(path, encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        if not records or not isinstance(records[0], dict):
            return []
        reader = JSONL_READERS.get(records[0].get("format"))
        return reader(records) if reader else []
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(line for line in fh if not line.startswith("#"))
        rows = list(reader)
        if any(None in row for row in rows):
            raise RaggedFile(f"a row has more fields than its header "
                             f"({len(reader.fieldnames or ())} columns)")
    return rows


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


def run_files(run_dir: str, reserved=()) -> RunFiles:
    """Every ``*.csv`` and ``*.jsonl`` below *run_dir*, by table; conflicts refused.

    *reserved* are tables something else builds for this run (a recording's tables, the
    tables RoboVAST derives): a file claiming one is refused, because its rows and the built
    ones would be the same table twice.
    """
    found = RunFiles()
    paths = []
    for root, dirs, files in os.walk(run_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        paths.extend(os.path.join(root, f) for f in sorted(files)
                     if f.lower().endswith((".csv", ".jsonl")))
    for path in sorted(paths):
        table = table_name(os.path.basename(path))
        rel = os.path.relpath(path, run_dir)
        if table in reserved:
            found.refused[table] = (f"{rel} would be the table '{table}', which is built from "
                                    f"the run's records; rename the file")
        elif table in found.tables:
            first = os.path.relpath(found.tables.pop(table), run_dir)
            found.refused[table] = f"two files claim the table: {first} and {rel}"
        elif table not in found.refused:
            found.tables[table] = path
    return found


__all__ = ["JSONL_READERS", "MAX_TABLE_NAME_BYTES", "QUATERNION", "RaggedFile", "RunFiles",
           "YAW", "YAW_NOTE", "read_rows", "run_files", "table_name", "to_arrow", "with_yaw"]
