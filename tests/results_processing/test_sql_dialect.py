# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""No Postgres-only SQL survives in what the product ships or teaches.

Queries are answered by DuckDB over the campaign directory. The same SQL strings live in
three places, and each fails differently when it carries another engine's spelling: the web
UI's own query constants (a panel that errors on every campaign), the SQL the MCP tools and
the advice build (a tool that answers "error" instead of the question), and the table
descriptions and prompts served to agents verbatim -- which are how an agent learns to
query a campaign, so a stale example there does not fail once, it teaches every agent to
write SQL that fails.

Strings, not a database: these are what a caller is handed before any query runs, and the
frontend has no Python-reachable database at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from robovast.mcp_server.plugins import prompts
from robovast.results_processing import advice, data_query

_REPO = Path(__file__).resolve().parents[2]
_UI_SRC = _REPO / "frontend" / "ui" / "src"

#: Spellings DuckDB rejects or reads differently, each with what to write instead.
_POSTGRES_ONLY = {
    re.compile(r"::\s*jsonb\b", re.IGNORECASE): "`::JSON` (the column is TEXT holding JSON)",
    re.compile(r"\bjsonb_\w+", re.IGNORECASE):
        "`unnest(from_json(col, '[\"JSON\"]'))` for a fan-out, `->`/`->>` for a field",
    re.compile(r"\bdouble\s+precision\b", re.IGNORECASE): "`DOUBLE`",
    re.compile(r"\btemp\.", re.IGNORECASE):
        "the view unqualified: views and tables are in the default schema `main`",
}

#: A driver placeholder, which the engine does not take: it binds `?`.
_PERCENT_PLACEHOLDER = re.compile(r"%s\b")

#: The UI files holding hand-written SQL. Named rather than globbed: a new file of query
#: constants should be added here deliberately, and a glob over the whole tree would also
#: sweep up prose in unrelated comments.
_UI_SQL_FILES = ("lib/resultsTree.ts", "lib/campaignDetails.ts", "lib/dataTables.ts",
                 "lib/panels/dataProvider.ts", "components/runLog/useRunLog.ts")

#: The Python modules that build SQL at run time, read whole: a spelling in a comment is as
#: much a guide to the next edit as one in a statement.
_PY_SQL_FILES = ("src/robovast/results_processing/advice.py",
                 "src/robovast/results_processing/data_query.py",
                 "src/robovast/mcp_server/plugins/results.py",
                 "src/robovast/mcp_server/plugins/run_logs.py",
                 "src/robovast/mcp_server/plugins/prompts.py")

_TS_TEMPLATE = re.compile(r"`([^`]*)`", re.DOTALL)
_LOOKS_LIKE_SQL = re.compile(r"\b(SELECT|FROM|ORDER\s+BY|GROUP\s+BY|WHERE)\b")
_TS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_TS_SQL_CONST = re.compile(
    r"const\s+(\w*SQL\w*)\s*=\s*((?:\s*(?:'[^']*'|\"[^\"]*\")\s*\+?)+)")

#: SQLite's implicit row id, which DuckDB does not have. It only ever stood in for the order
#: rows were written in; ``lastrowid`` is a Python driver attribute and is not SQL.
_ROWID = re.compile(r"(?<!last)\browid\b", re.IGNORECASE)


def _ts_statements() -> dict:
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


_TS_STATEMENTS = _ts_statements()

_ADVICE_STATEMENTS = {f"advice.{name}": value for name, value in vars(advice).items()
                      if name.endswith("_SQL") and isinstance(value, str)}

#: Every table description plus the note and the prompts, keyed by what a reader would call it.
_SERVED_TEXT = {
    f"_TABLE_DESCRIPTIONS[{schema}.{table}]": text
    for (schema, table), text in data_query._TABLE_DESCRIPTIONS.items()  # noqa: SLF001
}
_SERVED_TEXT["_DESCRIBE_NOTE"] = data_query._DESCRIBE_NOTE  # noqa: SLF001
_SERVED_TEXT["mcp_server.plugins.prompts"] = prompts.__doc__ or ""
_SERVED_TEXT.update({
    f"prompts.{name}": value for name, value in vars(prompts).items()
    if isinstance(value, str) and not name.startswith("__")})

_SOURCES = {name: (_REPO / name).read_text(encoding="utf-8") for name in _PY_SQL_FILES}


def _postgres_only(text: str) -> list:
    return [f"{m.group(0)!r} (write {fix})" for pattern, fix in _POSTGRES_ONLY.items()
            for m in pattern.finditer(text)]


def test_the_guard_has_something_to_check():
    """A rename emptying any set would pass every test below vacuously."""
    assert _TS_STATEMENTS, f"no SQL found under {_UI_SRC}"
    assert _ADVICE_STATEMENTS
    assert len(_SERVED_TEXT) > 5


def test_the_patterns_catch_what_they_name():
    for spelling in ("x::jsonb ->> 'a'", "jsonb_array_elements(x)",
                     "CAST(x AS double precision)", "FROM temp.run_view"):
        assert _postgres_only(spelling), spelling
    assert not _postgres_only("x::JSON ->> 'a' FROM run_view CAST(x AS DOUBLE)")


_STATEMENTS = {**_TS_STATEMENTS, **_ADVICE_STATEMENTS}


@pytest.mark.parametrize("name", sorted(_STATEMENTS))
def test_no_shipped_query_uses_postgres_only_sql(name):
    sql = _STATEMENTS[name]
    assert not _postgres_only(sql), f"{name}: {', '.join(_postgres_only(sql))}"
    assert not _PERCENT_PLACEHOLDER.search(sql), f"{name} uses %s; the engine binds ?"
    assert not _ROWID.search(sql), (
        f"{name} names rowid, which the engine does not have: order by a column that records "
        f"the order (run_log has `seq`), because a table is a set.")


@pytest.mark.parametrize("name", sorted(_SERVED_TEXT))
def test_no_served_example_uses_postgres_only_sql(name):
    text = _SERVED_TEXT[name]
    assert not _postgres_only(text), (
        f"{name} is served to agents and teaches {', '.join(_postgres_only(text))}")
    assert not _PERCENT_PLACEHOLDER.search(text), f"{name} teaches %s; the engine binds ?"


@pytest.mark.parametrize("name", sorted(_SOURCES))
def test_no_module_that_builds_sql_spells_it_for_postgres(name):
    found = _postgres_only(_SOURCES[name])
    assert not found, f"{name}: {', '.join(found)}"
