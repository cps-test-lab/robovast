# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Two data files never share one index table by accident.

Postgres truncates an identifier past 63 bytes without a word, so a table name over the
limit is not a long name -- it is a different one. A bag-derived data file is named for
the recording's directory plus the topic, which agree in exactly the leading characters a
truncation keeps, so two topics under one long prefix landed in a single table holding
both files' rows appended. Nothing raised, nothing was logged, and every count through
that table was the sum of two topics.

The same hazard reached the index by a second route that has nothing to do with length:
two filenames that sanitise to one identifier (``run.clock_map.csv`` and
``run_clock_map.csv``). The duplicate guard keyed on the filename, so it saw two names and
let both through.
"""

import csv
import json
import os
import sqlite3

import pytest

from robovast.results_processing.postprocessing_plugins import _csv_to_table_name

#: Stated here rather than read from the module under test. It is Postgres's limit, not a
#: choice RoboVAST gets to make, and a test that imported the constant would pass whatever
#: value it had -- including one over the limit, which is the bug.
LIMIT = 63

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
SCHEMA = "table_name_bounds_test"

pg = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

#: A recording directory plus a topic, which is how the generic bag handler names its
#: output. These two differ only past the 63rd byte, which is the whole point.
BAG = "rosbag2_2026_09_07-11_22_33"
LONG_A = f"{BAG}_local_costmap_costmap_updates_footprint_alpha.csv"
LONG_B = f"{BAG}_local_costmap_costmap_updates_footprint_beta.csv"


def test_a_short_name_is_unchanged():
    assert _csv_to_table_name("behaviors.csv") == "behaviors"
    assert _csv_to_table_name("action-nav.csv") == "action_nav"
    assert _csv_to_table_name("1_metric.csv") == "t_1_metric"


def test_a_name_that_does_not_fit_is_shortened_to_something_that_does():
    for name in (LONG_A, LONG_B):
        assert len(name) > LIMIT + 1, "the fixture must exceed the limit"
        assert len(_csv_to_table_name(name).encode()) <= LIMIT


def test_two_over_long_names_that_agree_up_to_the_cut_stay_apart():
    """The defect itself, asserted on what the *database* sees.

    Comparing the derived names whole would pass while the bug was live: they differ, in
    the bytes Postgres is about to throw away. So the comparison is of the first LIMIT
    bytes of each, which is the identifier the table actually gets.
    """
    assert LONG_A[:LIMIT] == LONG_B[:LIMIT], \
        "the fixture must be two names a truncation would merge"
    assert _csv_to_table_name(LONG_A)[:LIMIT] != _csv_to_table_name(LONG_B)[:LIMIT]


def test_the_shortened_name_is_stable():
    """It reaches the index and ``_table_name_map``, so it cannot move between ingests."""
    assert _csv_to_table_name(LONG_A) == _csv_to_table_name(LONG_A)
    assert _csv_to_table_name(LONG_A) == _csv_to_table_name(LONG_A.replace(".csv", ".jsonl"))


def test_the_head_of_the_shortened_name_still_reads():
    """Enough of the original survives to recognise in a table listing."""
    assert _csv_to_table_name(LONG_A).startswith("rosbag2_2026_09_07_11_22_33_local_costmap")


def _campaign(root, files):
    cdir = root / "camp-a-2026-08-20-00000001"
    (cdir / "_execution").mkdir(parents=True)
    run_dir = cdir / "nominal" / "0"
    run_dir.mkdir(parents=True)
    for name, value in files.items():
        with (run_dir / name).open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["v"])
            writer.writerow([value])

    store = sqlite3.connect(cdir / "campaign.db")
    store.executescript(
        "CREATE TABLE campaign (id INTEGER PRIMARY KEY, name TEXT, config_json TEXT);"
        "CREATE TABLE unit (id INTEGER PRIMARY KEY, batch_id INTEGER, config_name TEXT,"
        "  paramset_id TEXT, params_json TEXT, objective REAL, status TEXT);"
        "CREATE TABLE run (id INTEGER PRIMARY KEY, unit_id INTEGER, run_id INTEGER,"
        "  status TEXT, passed INTEGER, duration_s REAL, errors INTEGER, failures INTEGER,"
        "  tests INTEGER, start_time TEXT, failure_message TEXT, job_id INTEGER);")
    store.execute("INSERT INTO campaign VALUES (1, ?, ?)",
                  (cdir.name, json.dumps({})))
    store.execute("INSERT INTO unit VALUES (1,1,'nominal','ps-1','{}',0.5,'evaluated')")
    store.execute("INSERT INTO run VALUES (1,1,0,'passed',1,1.0,0,0,1,'t',NULL,NULL)")
    store.commit()
    store.close()
    return cdir


@pytest.fixture(name="index")
def _index(monkeypatch):
    if not DSN:
        pytest.skip("ROBOVAST_TEST_PG_DSN is not set")
    psycopg = pytest.importorskip("psycopg")
    from robovast.common import index_db

    monkeypatch.setenv(index_db.DSN_ENV, f"{DSN} options=-csearch_path={SCHEMA}")
    with psycopg.connect(DSN, autocommit=True) as setup:
        setup.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        setup.execute("DROP SCHEMA IF EXISTS campaign CASCADE")
        setup.execute(f"CREATE SCHEMA {SCHEMA}")
    yield
    with psycopg.connect(DSN, autocommit=True) as teardown:
        teardown.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        teardown.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


@pg
def test_two_over_long_files_reach_two_tables_in_the_index(index, tmp_path):
    """End to end: the rows of one topic are not the rows of another."""
    del index
    from robovast.results_processing import campaign_ingest, index_query

    cdir = _campaign(tmp_path, {LONG_A: 1.0, LONG_B: 2.0})
    with index_query.open_index(readonly=False) as conn:
        campaign_ingest.ingest_campaign(conn, str(cdir), cdir.name)

        table_a = _csv_to_table_name(LONG_A)
        table_b = _csv_to_table_name(LONG_B)
        assert conn.execute(f'SELECT v FROM "{table_a}"').fetchall() == [(1.0,)]
        assert conn.execute(f'SELECT v FROM "{table_b}"').fetchall() == [(2.0,)]

        # And a reader can get from the file's name to the table it became.
        mapped = dict(conn.execute(
            "SELECT display_name, sql_name FROM _table_name_map").fetchall())
        assert mapped[LONG_A[:-len(".csv")]] == table_a
        assert mapped[LONG_B[:-len(".csv")]] == table_b


@pg
def test_two_filenames_that_sanitise_to_one_table_are_refused(index, tmp_path):
    """Not a length problem: one destination, two names, and the rows would be appended."""
    del index
    from robovast.results_processing import campaign_ingest, index_query

    cdir = _campaign(tmp_path, {"run.clock_map.csv": 1.0, "run_clock_map.csv": 2.0})
    with index_query.open_index(readonly=False) as conn:
        with pytest.raises(ValueError) as excinfo:
            campaign_ingest.ingest_campaign(conn, str(cdir), cdir.name)
    message = str(excinfo.value)
    assert "run_clock_map" in message
    assert "run.clock_map.csv" in message and "run_clock_map.csv" in message
