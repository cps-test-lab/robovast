# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The flat views, held to what the SQLite ones returned.

``config_view`` gets the most attention because it is the one with no Postgres equivalent:
SQLite's ``json_tree`` had to be rebuilt as a recursive CTE, and the documented way to use
it is ``WHERE fullkey LIKE '$.execution%'`` -- so a path spelled differently returns nothing
rather than erroring, which is the worst way for a port to be wrong.

Set ``ROBOVAST_TEST_PG_DSN`` to run these; without it they skip.
"""

import json
import os
import sqlite3

import pytest

from robovast.results_processing import campaign_ingest, index_query, index_views

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

SCHEMA = "v_test"

#: Deliberately awkward: keys with underscores and dashes (which SQLite quotes), a nested
#: array, an integer, a real, a boolean and a null -- one of each thing the two engines
#: spell differently.
_CONFIG = {
    "version": 3,
    "metadata": {"name": "demo", "keywords": ["a", "b"]},
    "results_processing": {"health_checks": None, "postprocessing": [{"run-log": {}}]},
    "execution": {"runs": 5, "ratio": 1.5, "enabled": True},
}


@pytest.fixture(name="index")
def _index(monkeypatch, tmp_path):
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db

    with psycopg.connect(DSN, autocommit=True) as setup:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}"):
            setup.execute(statement)
    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")

    root = tmp_path / "camp"
    run = root / "goal-1" / "0"
    run.mkdir(parents=True)
    (run / "poses.csv").write_text("timestamp,x\n0.5,1.0\n")
    db = sqlite3.connect(root / "campaign.db")
    db.executescript(
        "CREATE TABLE campaign (id INTEGER PRIMARY KEY, name TEXT, config_json TEXT);"
        "CREATE TABLE batch (id INTEGER PRIMARY KEY, campaign_id INTEGER, idx INTEGER);"
        "CREATE TABLE unit (id INTEGER PRIMARY KEY, batch_id INTEGER, config_name TEXT,"
        "                   paramset_id TEXT, params_json TEXT, objective REAL, status TEXT);"
        "CREATE TABLE job (id INTEGER PRIMARY KEY, campaign_id INTEGER, job_dir TEXT,"
        "                  sysinfo_json TEXT);"
        "CREATE TABLE run (id INTEGER PRIMARY KEY, unit_id INTEGER, run_id INTEGER,"
        "                  status TEXT, passed INTEGER, duration_s REAL, errors INTEGER,"
        "                  failures INTEGER, tests INTEGER, start_time TEXT,"
        "                  failure_message TEXT, job_id INTEGER);")
    db.execute("INSERT INTO campaign VALUES (1, 'camp', ?)", (json.dumps(_CONFIG),))
    db.execute("INSERT INTO batch VALUES (1, 1, 0)")
    db.execute("INSERT INTO unit VALUES (1, 1, 'goal-1', 'ps-1', '{}', 0.5, 'evaluated')")
    db.execute("INSERT INTO unit VALUES (2, 1, '', 'ps-2', '{}', NULL, 'composition_failed')")
    db.execute("INSERT INTO job VALUES (1, 1, '_jobs/job-0', '{}')")
    db.execute("INSERT INTO run VALUES (1, 1, 0, 'passed', 1, 9.5, 0, 0, 1, 't', NULL, 1)")
    db.commit()
    db.close()

    with index_query.open_index(readonly=False) as conn:
        campaign_ingest.ingest_campaign(conn, str(root), "camp-a")
        index_views.create_views(conn)
    yield
    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


def _sqlite_tree():
    """What SQLite's json_tree returns for the same config -- the oracle."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE c (config_json TEXT)")
    conn.execute("INSERT INTO c VALUES (?)", (json.dumps(_CONFIG),))
    rows = conn.execute(
        "SELECT t.fullkey, t.key, t.type, t.atom FROM c, json_tree(c.config_json) t"
    ).fetchall()
    conn.close()
    return sorted((r[0], str(r[1]) if r[1] is not None else None, r[2],
                   str(r[3]) if r[3] is not None else None) for r in rows)


def test_config_view_matches_json_tree_exactly(index):
    """Every row, on fullkey/key/type/value. Not "close enough"."""
    result = index_query.query_index(
        "SELECT fullkey, key, type, value FROM config_view", max_rows=5000)
    got = sorted((r["fullkey"], r["key"], r["type"],
                  str(r["value"]) if r["value"] is not None else None)
                 for r in result["rows"])

    assert got == _sqlite_tree()


def test_config_view_quotes_keys_the_way_sqlite_does(index):
    """SQLite emits a key bare only when purely alphanumeric; else it quotes.

    Uniform quoting (or none) would break every ``fullkey LIKE`` a user has written, and
    silently -- a path that does not match returns no rows rather than an error.
    """
    keys = {r["fullkey"] for r in index_query.query_index(
        "SELECT fullkey FROM config_view", max_rows=5000)["rows"]}

    assert "$.metadata" in keys, "a plain key is bare"
    assert '$."results_processing"' in keys, "an underscored key is quoted"
    assert '$."results_processing"."health_checks"' in keys


def test_config_view_uses_sqlites_type_names(index):
    """Postgres says string/number; SQLite says text/integer/real.

    ``WHERE type = 'text'`` is a query someone has written.
    """
    types = {r["fullkey"]: r["type"] for r in index_query.query_index(
        "SELECT fullkey, type FROM config_view", max_rows=5000)["rows"]}

    assert types["$.version"] == "integer"
    assert types["$.execution.ratio"] == "real"
    assert types["$.metadata.name"] == "text"
    assert types["$.execution.enabled"] == "true"
    assert types['$."results_processing"."health_checks"'] == "null"


def test_config_view_leaves_containers_valueless(index):
    """A container returning a serialized subtree would be truncated by the cell cap
    into a config that looks complete and is not."""
    rows = {r["fullkey"]: r["value"] for r in index_query.query_index(
        "SELECT fullkey, value FROM config_view", max_rows=5000)["rows"]}

    assert rows["$.metadata"] is None
    assert rows["$.metadata.keywords"] is None
    assert rows["$.metadata.name"] == "demo"


def test_run_view_joins_the_config_name_onto_every_run(index):
    """run_id is unique only within a configuration; a forgotten join returns the wrong
    rows rather than raising."""
    result = index_query.query_index(
        "SELECT config_name, run_id, status, batch, job_dir FROM run_view "
        "WHERE run_id IS NOT NULL")

    assert result["rows"] == [{"config_name": "goal-1", "run_id": 0, "status": "passed",
                               "batch": 0, "job_dir": "_jobs/job-0"}]


def test_run_view_keeps_a_draw_that_never_ran(index):
    """A composition-failed unit has no run rows, so the join alone drops it.

    Without the UNION ALL a search campaign silently reports only the draws that worked.
    """
    result = index_query.query_index(
        "SELECT config_name, run_id, status FROM run_view WHERE run_id IS NULL")

    assert result["rows"] == [{"config_name": "ps-2", "run_id": None,
                               "status": "composition_failed"}]


def test_run_view_carries_the_campaign_so_it_can_span_them(index):
    """The column that makes an arm comparison one query."""
    result = index_query.query_index("SELECT DISTINCT campaign_id FROM run_view")

    assert result["rows"] == [{"campaign_id": "camp-a"}]


def test_views_are_created_for_what_the_index_actually_has(index):
    """A view over a missing table is created happily and fails at query time."""
    with index_query.open_index() as conn:
        names = set(index_views.campaign_view_sql(conn))

    assert {"run_view", "config_view"} <= names
