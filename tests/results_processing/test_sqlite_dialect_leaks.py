# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""No SQLite-only JSON function survives in SQL the product ships or teaches.

The sibling guard in ``test_advice.py`` covers the ``*_SQL`` constants of
``advice.py``. It did not cover the two other places the same strings live, and both
broke the product after the move to the central Postgres index: the web UI's own query
constants (the run view and the Explorer both failed with "function json_extract(text,
unknown) does not exist"), and the table descriptions ``describe_campaign_data`` serves
verbatim -- which are how an agent learns to query a campaign, so a stale example there
does not fail once, it teaches every agent to write SQL that fails.

Strings, not a database: these are what a caller is handed before any connection exists,
and a check needing a live index would not run where it matters (the frontend has no
Python-reachable database at all).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from robovast.mcp_server.plugins import prompts
from robovast.results_processing import data_query

#: A *call* of one of SQLite's JSON functions -- the name followed by its argument list.
#: Prose naming them to say Postgres has them not ("not SQLite's json_extract / json_each")
#: is the correction, so only the call shape counts as a leak.
_SQLITE_JSON_CALL = re.compile(r"\bjson_(?:extract|each|tree|quote)\s*\(", re.IGNORECASE)

#: Postgres has no ``sqlite_master``; a schema probe against the index has to read
#: ``information_schema``.
_SQLITE_CATALOG = re.compile(r"\bsqlite_master\b", re.IGNORECASE)

_ADVICE = ("Use -> / ->> for a field (0-based for an array element) and "
           "jsonb_array_elements for a fan-out; information_schema for a schema probe.")

_UI_SRC = Path(__file__).resolve().parents[2] / "frontend" / "ui" / "src"

#: The UI files holding hand-written SQL. Named rather than globbed: a new file of query
#: constants should be added here deliberately, and a glob over the whole tree would also
#: sweep up prose in unrelated comments.
_UI_SQL_FILES = ("lib/resultsTree.ts", "lib/campaignDetails.ts",
                 "lib/panels/dataProvider.ts", "components/runLog/useRunLog.ts")

#: `const NAME_SQL = <one or more adjacent string literals joined by +>`.
#: A backtick template literal that is SQL rather than prose -- it names a statement's own
#: keywords. Comments are stripped first, so a comment *about* SQL is not mistaken for it.
_TS_TEMPLATE = re.compile(r"`([^`]*)`", re.DOTALL)
_LOOKS_LIKE_SQL = re.compile(r"\b(SELECT|FROM|ORDER\s+BY|GROUP\s+BY|WHERE)\b")
_TS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)

#: SQLite's implicit row id, which Postgres does not have. ``lastrowid`` is excluded: that
#: is the Python driver's cursor attribute and has nothing to do with a query.
_SQLITE_ROWID = re.compile(r"(?<!last)\browid\b", re.IGNORECASE)

_TS_SQL_CONST = re.compile(
    r"const\s+(\w*SQL\w*)\s*=\s*((?:\s*(?:'[^']*'|\"[^\"]*\")\s*\+?)+)")


def _ts_sql_constants() -> dict:
    found = {}
    for name in _UI_SQL_FILES:
        path = _UI_SRC / name
        assert path.exists(), f"{path} moved; this guard now checks nothing"
        text = path.read_text(encoding="utf-8")
        for const, body in _TS_SQL_CONST.findall(text):
            found[f"{name}:{const}"] = "".join(
                lit[1:-1] for lit in re.findall(r"'[^']*'|\"[^\"]*\"", body))
        code = _TS_COMMENT.sub(" ", text)
        for i, lit in enumerate(_TS_TEMPLATE.findall(code)):
            if _LOOKS_LIKE_SQL.search(lit):
                found[f"{name}:template#{i}"] = lit
    return found


_TS_STATEMENTS = _ts_sql_constants()

#: Every table description plus the note, keyed by what a reader would call it.
_SERVED_TEXT = {
    f"_TABLE_DESCRIPTIONS[{schema}.{table}]": text
    for (schema, table), text in data_query._TABLE_DESCRIPTIONS.items()  # noqa: SLF001
}
_SERVED_TEXT["_DESCRIBE_NOTE"] = data_query._DESCRIBE_NOTE  # noqa: SLF001
_SERVED_TEXT["mcp_server.plugins.prompts"] = prompts.__doc__ or ""
_SERVED_TEXT.update({
    f"prompts.{name}": value for name, value in vars(prompts).items()
    if isinstance(value, str) and not name.startswith("__")})


def test_the_guard_has_something_to_check():
    """A rename emptying either set would pass every test below vacuously."""
    assert _TS_STATEMENTS, f"no *_SQL constants found under {_UI_SRC}"
    assert len(_SERVED_TEXT) > 5


@pytest.mark.parametrize("name", sorted(_TS_STATEMENTS))
def test_no_ui_query_uses_sqlite_only_sql(name):
    sql = _TS_STATEMENTS[name]
    assert not _SQLITE_JSON_CALL.search(sql), f"{name} calls a SQLite JSON function. {_ADVICE}"
    assert not _SQLITE_CATALOG.search(sql), f"{name} reads sqlite_master. {_ADVICE}"
    assert not _SQLITE_ROWID.search(sql), (
        f"{name} names rowid, which Postgres does not have. It was only ever a stand-in for "
        f"the order rows were written in -- order by a column that records that order "
        f"(run_log has `seq`), because a table is a set and no engine owes a reader "
        f"insertion order. {_ADVICE}")


@pytest.mark.parametrize("name", sorted(_SERVED_TEXT))
def test_no_documented_example_uses_a_sqlite_only_json_function(name):
    text = _SERVED_TEXT[name]
    assert not _SQLITE_JSON_CALL.search(text), (
        f"{name} is served to agents and hands out a SQLite JSON function, which the "
        f"index rejects outright. {_ADVICE}")
