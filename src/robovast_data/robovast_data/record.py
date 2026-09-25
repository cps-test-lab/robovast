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

"""A campaign's own record, ``campaign.db``, as the ``campaign`` schema of a query.

The store is the controller's, written as the campaign runs; this reads it and never writes.
It is small (tens of rows per table for a campaign of hundreds of runs), so it is read whole
per connection, which is also what makes a query see a running campaign's latest rows.

* Every table the record is made of (:data:`TABLES`) keeps its columns as the file declares
  them, with the campaign's string id first as ``campaign_id``. A per-campaign file numbers its
  rows from 1, so ``unit.id = 3`` means something different in every campaign: the ids are kept
  verbatim and read together with ``campaign_id``. The integer ``campaign_id`` the child tables
  carry is dropped, since it is always the one campaign row.
* ``strategy_state`` is an opaque blob only the search reads, and is not offered.
* A ``*_json`` column is re-encoded where Python's ``json`` wrote a non-finite number as
  ``Infinity`` or ``NaN``, which are not JSON and make a JSON cast fail for the whole query.
* ``config_view`` is the campaign's resolved configuration as one row per node, with SQLite's
  ``json_tree`` shape (``fullkey`` ``$.a.b`` or ``$."quoted-key"``, arrays ``[n]``, ``type`` in
  SQLite's vocabulary, a boolean's value ``1``/``0``, containers' values NULL), because queries
  written against that shape select by ``fullkey LIKE '$.execution.%'``.
* ``container_failure_view`` is ``container_failure`` with one row per run the failure named,
  and one row with a NULL ``run_key`` for a failure that named none -- expanding an empty list
  yields no rows, and a failure whose runs could not be resolved must not vanish.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Dict, List

import pyarrow as pa

from robovast_decode.layout import STORE
from robovast_decode.types import json_text

#: The tables the record is made of, parents before children. Named, because which tables are
#: the campaign's record is a decision, not a fact about the file.
TABLES = ("campaign", "batch", "unit", "job", "node", "run", "container_failure")

_DROPPED = frozenset({"campaign_id", "strategy_state"})
_ARROW = {"INTEGER": pa.int64(), "REAL": pa.float64(), "TEXT": pa.string()}
_NON_FINITE_JSON = re.compile(r"-?Infinity|NaN")
_PLAIN_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


def _castable(column: str, value):
    if column.endswith("_json") and isinstance(value, str) and _NON_FINITE_JSON.search(value):
        return json_text(json.loads(value))
    return value


def read_record(campaign_dir: str, campaign_id: str) -> Dict[str, pa.Table]:
    """``{table: rows}`` of *campaign_dir*'s ``campaign.db``; a table the file lacks is absent."""
    path = os.path.join(campaign_dir, STORE)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{campaign_dir} has no {STORE}: not a campaign directory")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out = {}
        for table in TABLES:
            if table not in present:
                continue
            declared = [(r[1], (r[2] or "").upper()) for r in db.execute(
                f'PRAGMA table_info("{table}")') if r[1] not in _DROPPED]
            names = ", ".join(f'"{name}"' for name, _ in declared)
            rows = db.execute(f'SELECT {names} FROM "{table}"').fetchall()
            arrays = {"campaign_id": pa.array([campaign_id] * len(rows), type=pa.string())}
            for i, (name, decl) in enumerate(declared):
                kind = _ARROW.get(decl, pa.string())
                values = [_castable(name, row[i]) for row in rows]
                if kind == pa.string():
                    values = [v if v is None or isinstance(v, str) else str(v) for v in values]
                arrays[name] = pa.array(values, type=kind)
            out[table] = pa.table(arrays)
        return out
    finally:
        db.close()


def _json_type(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "real"
    if isinstance(value, str):
        return "text"
    return "array" if isinstance(value, list) else "object"


def _json_value(value):
    if isinstance(value, bool):
        return "1" if value else "0"
    if value is None or isinstance(value, (list, dict)):
        return None
    if isinstance(value, float):
        return repr(value)
    return str(value)


def config_tree(campaign: pa.Table) -> pa.Table:
    """``config_view``: every node of each campaign row's ``config_json``."""
    rows: List[tuple] = []
    for campaign_id, config in zip(campaign.column("campaign_id").to_pylist(),
                                   campaign.column("config_json").to_pylist()
                                   if "config_json" in campaign.column_names
                                   else [None] * campaign.num_rows):
        if not config:
            continue
        stack = [("$", None, None, json.loads(config))]
        while stack:
            fullkey, key, parent, node = stack.pop(0)
            rows.append((campaign_id, fullkey, key, parent, _json_type(node), _json_value(node)))
            if isinstance(node, dict):
                children = []
                for child_key, child in node.items():
                    spelled = (f".{child_key}" if _PLAIN_KEY.match(child_key)
                               else '."' + child_key.replace('"', '\\"') + '"')
                    children.append((fullkey + spelled, child_key, fullkey, child))
                stack[0:0] = children
            elif isinstance(node, list):
                stack[0:0] = [(f"{fullkey}[{i}]", str(i), fullkey, child)
                              for i, child in enumerate(node)]
    columns = ("campaign_id", "fullkey", "key", "parent", "type", "value")
    return pa.table({name: pa.array([r[i] for r in rows], type=pa.string())
                     for i, name in enumerate(columns)})


def container_failures(failures: pa.Table) -> pa.Table:
    """``container_failure_view``: one row per run a failure named, or one with no run."""
    records = failures.to_pylist()
    out = []
    for record in records:
        try:
            runs = json.loads(record.get("runs_json") or "[]") or []
        except ValueError:
            runs = []
        for run_key in runs or [None]:
            out.append({**record, "run_key": run_key})
    schema = failures.schema.append(pa.field("run_key", pa.string()))
    return pa.Table.from_pylist(out, schema=schema)


__all__ = ["TABLES", "config_tree", "container_failures", "read_record"]
