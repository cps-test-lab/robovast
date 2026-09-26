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

"""A query, read before it runs: one ``SELECT``, the relations it names, the runs it narrows to.

The statement is parsed by DuckDB itself (``json_serialize_sql``), so what is checked here is
exactly what will run -- no second parser with its own idea of the grammar.

* **One ``SELECT``.** DuckDB serialises nothing else, so a ``COPY``, an ``ATTACH``, a ``SET``
  or a second statement is refused before a connection sees it. This is the second fence; the
  first is the connection's own file-access settings (:mod:`robovast_data.engine`).
* **Two casts are rewritten**, because SQL already written against SQLite means something else
  in DuckDB and nothing raises:

  - ``CAST(x AS REAL)`` is a 4-byte float in DuckDB. An epoch timestamp cast through it loses
    about half a minute, so a 60-second window reads as 128 seconds. It becomes ``DOUBLE``,
    which is what the SQL meant.
  - ``CAST(x AS INTEGER)`` rounds in DuckDB and truncates in SQLite: ``8.6`` is ``9`` in one and
    ``8`` in the other. That sits in every panel's downsampling query
    (``CAST(CAST("timestamp" AS REAL) * <hz> AS INTEGER)``), where it moves every bucket boundary
    by half a bucket and the chart still looks fine. It becomes ``CAST(trunc(x) AS BIGINT)``.

  Nothing else is translated. A spelling DuckDB rejects outright is left to fail, because its
  author sees the error and fixes the query.
* **The relations it names** (tables and views, not CTEs and not subquery aliases), so the
  tables a query needs can be built before it runs.
* **The runs it narrows a table to**: an equality or ``IN`` on ``config_name`` or ``run_id`` in
  the top-level ``WHERE``, conjoined with the rest, and attributable to one table. A query for
  one run of a campaign then builds that run's table and no other. Anything less plain -- an
  ``OR``, a subquery, a comparison on an expression -- narrows nothing, and the whole scope is
  built: the answer is the same either way, only the work differs.
"""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

import duckdb


class QueryError(ValueError):
    """A query that is refused before it runs, or that failed when it did."""


@dataclass
class Narrowing:
    """The run key values a query's ``WHERE`` restricts one table to; ``None`` is unrestricted."""
    config_names: Optional[Set[str]] = None
    run_ids: Optional[Set[int]] = None

    def admits(self, config_name: str, run_id: int) -> bool:
        return ((self.config_names is None or config_name in self.config_names)
                and (self.run_ids is None or run_id in self.run_ids))


@dataclass
class Statement:
    """A parsed ``SELECT``: the SQL to run, what it names, and how it narrows each table."""
    sql: str
    relations: Set[str]
    narrowing: Dict[str, Narrowing] = field(default_factory=dict)


#: One parser connection per thread. A DuckDB connection is not safe to share between threads:
#: two requests parsing at once on one connection read each other's results, or none.
_PARSERS = threading.local()


def _parser() -> duckdb.DuckDBPyConnection:
    """This thread's connection used only to parse and print SQL; it never reads data."""
    connection = getattr(_PARSERS, "connection", None)
    if connection is None:
        connection = _PARSERS.connection = duckdb.connect()
    return connection


def _walk(node, visit):
    if isinstance(node, dict):
        visit(node)
        for value in node.values():
            _walk(value, visit)
    elif isinstance(node, list):
        for value in node:
            _walk(value, visit)


def _relation_name(ref: dict) -> str:
    schema = (ref.get("schema_name") or "").lower()
    name = (ref.get("table_name") or "").lower()
    return f"{schema}.{name}" if schema and schema != "main" else name


def _rewrite_casts(node) -> None:
    """``REAL`` casts to ``DOUBLE``, ``INTEGER`` casts to a truncation, in place."""
    if isinstance(node, list):
        for value in node:
            _rewrite_casts(value)
        return
    if not isinstance(node, dict):
        return
    for value in node.values():
        _rewrite_casts(value)
    if node.get("class") != "CAST":
        return
    cast_type = node.get("cast_type") or {}
    if cast_type.get("id") == "FLOAT":
        node["cast_type"] = {"id": "DOUBLE", "type_info": None}
    elif cast_type.get("id") == "INTEGER":
        child = node["child"]
        node["child"] = {"class": "FUNCTION", "type": "FUNCTION", "alias": "",
                         "query_location": child.get("query_location", 0),
                         "function_name": "trunc", "schema": "", "catalog": "",
                         "children": [child], "filter": None,
                         "order_bys": {"type": "ORDER_MODIFIER", "orders": []},
                         "distinct": False, "is_operator": False, "export_state": False}
        node["cast_type"] = {"id": "BIGINT", "type_info": None}


def _constants(nodes) -> Optional[list]:
    values = []
    for node in nodes:
        if node.get("class") != "CONSTANT" or node["value"].get("is_null"):
            return None
        values.append(node["value"]["value"])
    return values


def _key_predicate(node) -> Optional[tuple]:
    """``(qualifier, column, values)`` for ``col = const`` or ``col IN (consts)``."""
    if node.get("type") == "COMPARE_EQUAL":
        column, other = node.get("left") or {}, node.get("right") or {}
        if column.get("class") != "COLUMN_REF":
            column, other = other, column
        operands = [other]
    elif node.get("type") == "COMPARE_IN":
        column, *operands = node.get("children") or [{}]
    else:
        return None
    if column.get("class") != "COLUMN_REF":
        return None
    names = column.get("column_names") or []
    if names[-1:] not in (["config_name"], ["run_id"]):
        return None
    values = _constants(operands)
    if values is None:
        return None
    return (names[-2].lower() if len(names) > 1 else None), names[-1], values


def _conjuncts(node) -> list:
    if node is None:
        return []
    if node.get("type") == "CONJUNCTION_AND":
        return [c for child in node.get("children") or [] for c in _conjuncts(child)]
    return [node]


def _from_tables(node, out: dict) -> None:
    """``{alias or name: relation}`` of the base tables joined directly in a ``FROM``."""
    if not node:
        return
    if node.get("type") == "BASE_TABLE":
        relation = _relation_name(node)
        out[(node.get("alias") or node.get("table_name") or "").lower()] = relation
    elif node.get("type") == "JOIN":
        _from_tables(node.get("left"), out)
        _from_tables(node.get("right"), out)


def _narrowing(select: dict, ctes: Set[str]) -> Dict[str, Narrowing]:
    tables: dict = {}
    _from_tables(select.get("from_table"), tables)
    tables = {alias: rel for alias, rel in tables.items() if rel not in ctes}
    out: Dict[str, Narrowing] = {}
    for conjunct in _conjuncts(select.get("where_clause")):
        predicate = _key_predicate(conjunct)
        if predicate is None:
            continue
        qualifier, column, values = predicate
        if qualifier is not None:
            relation = tables.get(qualifier)
        elif len(set(tables.values())) == 1:
            relation = next(iter(tables.values()))
        else:
            relation = None
        if relation is None:
            continue
        narrowing = out.setdefault(relation, Narrowing())
        if column == "config_name":
            given = {str(v) for v in values}
            narrowing.config_names = (given if narrowing.config_names is None
                                      else narrowing.config_names & given)
        else:
            try:
                given = {int(v) for v in values}
            except (TypeError, ValueError):
                continue
            narrowing.run_ids = (given if narrowing.run_ids is None
                                 else narrowing.run_ids & given)
    return out


def parse(sql: str) -> Statement:
    """*sql* checked, rewritten and described; :class:`QueryError` if it is not one ``SELECT``."""
    try:
        raw = _parser().execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
    except duckdb.Error as exc:
        raise QueryError(str(exc)) from exc
    tree = json.loads(raw)
    if tree.get("error"):
        message = tree.get("error_message") or "not a query"
        if "Only SELECT" in message:
            message = "only a SELECT is answered here"
        raise QueryError(message)
    statements = tree.get("statements") or []
    if len(statements) != 1:
        raise QueryError(f"one statement at a time; this has {len(statements)}")

    ctes: Set[str] = set()
    relations: Set[str] = set()

    def visit(node):
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            ctes.update(entry["key"].lower() for entry in cte_map.get("map") or [])
        if node.get("type") == "BASE_TABLE":
            relations.add(_relation_name(node))

    _walk(statements, visit)
    rewritten = copy.deepcopy(tree)
    _rewrite_casts(rewritten["statements"])
    try:
        text = _parser().execute("SELECT json_deserialize_sql(?)",
                                 [json.dumps(rewritten)]).fetchone()[0]
    except duckdb.Error as exc:
        raise QueryError(str(exc)) from exc
    node = statements[0]["node"]
    narrowing = _narrowing(node, ctes) if node.get("type") == "SELECT_NODE" else {}
    return Statement(sql=text, relations=relations - ctes, narrowing=narrowing)


__all__ = ["Narrowing", "QueryError", "Statement", "parse"]
