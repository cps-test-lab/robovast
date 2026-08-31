# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A campaign's results directory read into the index, replacing ``generate_data_db``.

Against a real Postgres and a results tree built here, because what is under test is the
ingest contract as a whole: one file becomes one table, a stem collision is refused, the
scenario verdict is derived once, and a second ingest replaces rather than doubles.

Set ``ROBOVAST_TEST_PG_DSN`` to run them; without it they skip.
"""

import os
import sqlite3

import pytest

from robovast.results_processing import campaign_ingest, index_schema
from robovast.results_processing.row_sink import PostgresRowSink

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")


@pytest.fixture(name="conn")
def _conn():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN, autocommit=True) as conn:
        # ``campaign`` is a fixed top-level schema an ingest creates, so it is dropped
        # here too -- otherwise one test's campaign record answers another test's query.
        for statement in ("DROP SCHEMA IF EXISTS ing_test CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          "CREATE SCHEMA ing_test", "SET search_path TO ing_test"):
            conn.execute(statement)
        yield conn
        conn.execute("DROP SCHEMA IF EXISTS ing_test CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


def _campaign(tmp_path, *, runs=(("goal-1", 0), ("goal-1", 1)), extra_file=None,
              with_store=True):
    """A results tree shaped like a real campaign's."""
    root = tmp_path / "camp"
    root.mkdir(parents=True, exist_ok=True)
    if with_store:
        db = sqlite3.connect(root / "campaign.db")
        db.executescript(
            "CREATE TABLE campaign (id INTEGER PRIMARY KEY, name TEXT, mode TEXT);"
            "CREATE TABLE run (id INTEGER PRIMARY KEY, unit_id INTEGER, run_id INTEGER,"
            "                  status TEXT);")
        db.execute("INSERT INTO campaign VALUES (1, 'camp', 'batch')")
        db.commit()
        db.close()
    for config_name, run_id in runs:
        run_dir = root / config_name / str(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "poses.csv").write_text(
            "timestamp,frame,position.x\n0.5,base_link,1.0\n1.5,base_link,2.0\n")
        (run_dir / "nav_metrics.csv").write_text(
            "duration_s,collided\n12.5,0\n")
        if extra_file:
            target = run_dir / extra_file[0]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(extra_file[1])
    return str(root)


def test_one_file_becomes_one_table_named_after_its_stem(conn, tmp_path):
    """The extension mechanism, unchanged: nothing registers a table."""
    totals = campaign_ingest.ingest_campaign(conn, _campaign(tmp_path), "camp-a")

    assert totals["poses"] == 4, "two runs, two rows each"
    assert totals["nav_metrics"] == 2
    assert conn.execute("SELECT COUNT(*) FROM poses").fetchone()[0] == 4


def test_rows_are_scoped_by_campaign_config_and_run(conn, tmp_path):
    campaign_ingest.ingest_campaign(conn, _campaign(tmp_path), "camp-a")

    got = conn.execute(
        "SELECT DISTINCT campaign_id, config_name, run_id FROM poses ORDER BY run_id"
    ).fetchall()
    assert got == [("camp-a", "goal-1", 0), ("camp-a", "goal-1", 1)]


def test_types_are_inferred_so_ordering_is_numeric(conn, tmp_path):
    """The failure csv_types exists to prevent: '10.022' sorting before '9.5'."""
    campaign_ingest.ingest_campaign(conn, _campaign(tmp_path), "camp-a")

    types = dict(conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'ing_test' AND table_name = 'poses'").fetchall())
    assert types["timestamp"] == "double precision"
    assert types["frame"] == "text"


def test_re_ingesting_replaces_rather_than_doubles(conn, tmp_path):
    """Idempotence is what makes the reproducibility invariant checkable.

    Without the clear, a re-postprocess would leave two copies of every row and every
    aggregate over the campaign would silently double.
    """
    tree = _campaign(tmp_path)
    first = campaign_ingest.ingest_campaign(conn, tree, "camp-a")
    second = campaign_ingest.ingest_campaign(conn, tree, "camp-a")

    assert first["poses"] == second["poses"]
    assert conn.execute("SELECT COUNT(*) FROM poses").fetchone()[0] == 4


def test_re_ingesting_one_campaign_leaves_the_others_alone(conn, tmp_path):
    """The clear is scoped; re-running one postprocess must not empty the index."""
    campaign_ingest.ingest_campaign(conn, _campaign(tmp_path / "a"), "camp-a")
    campaign_ingest.ingest_campaign(conn, _campaign(tmp_path / "b"), "camp-b")
    campaign_ingest.ingest_campaign(conn, _campaign(tmp_path / "a2"), "camp-a")

    counts = dict(conn.execute(
        "SELECT campaign_id, COUNT(*) FROM poses GROUP BY 1 ORDER BY 1").fetchall())
    assert counts == {"camp-a": 4, "camp-b": 4}


def test_a_duplicate_stem_in_one_run_is_refused(conn, tmp_path):
    """Two files claiming one table means one silently wins, by directory order."""
    tree = _campaign(tmp_path, runs=(("goal-1", 0),),
                     extra_file=("sub/poses.csv", "timestamp\n1.0\n"))

    with pytest.raises(ValueError, match="Duplicate table name 'poses'"):
        campaign_ingest.ingest_campaign(conn, tree, "camp-a")


def test_the_campaign_record_is_mirrored_alongside_the_data(conn, tmp_path):
    """A campaign is findable and its runs joinable, not just its metrics present."""
    totals = campaign_ingest.ingest_campaign(conn, _campaign(tmp_path), "camp-a")

    assert totals["campaign"] == 1
    assert conn.execute(
        "SELECT name FROM campaign.campaign WHERE campaign_id = 'camp-a'").fetchone()[0] == "camp"


def test_a_missing_campaign_db_still_ingests_the_data(conn, tmp_path):
    """A campaign that ended badly is exactly the one worth reading."""
    totals = campaign_ingest.ingest_campaign(
        conn, _campaign(tmp_path, with_store=False), "camp-a")

    assert "campaign" not in totals
    assert totals["poses"] == 4


def test_the_name_map_records_the_sanitised_table_name(conn, tmp_path):
    """A reader has to find the table a file became after the name was sanitised."""
    tree = _campaign(tmp_path, runs=(("goal-1", 0),),
                     extra_file=("action-nav.csv", "a\n1\n"))
    campaign_ingest.ingest_campaign(conn, tree, "camp-a")

    got = dict(conn.execute(
        "SELECT display_name, sql_name FROM _table_name_map").fetchall())
    assert got["action-nav"] == "action_nav"


def test_the_scenario_verdict_is_derived_once_from_the_run_log(conn, tmp_path):
    """One answer to "when did the trial end", rather than a text match per reader."""
    tree = _campaign(tmp_path, runs=(("goal-1", 0),))
    run_dir = os.path.join(tree, "goal-1", "0")
    with open(os.path.join(run_dir, "run_log.csv"), "w", encoding="utf-8") as handle:
        handle.write("sim_time,wall_ts,node,message\n")
        handle.write("1.0,1000.0,other,nothing to see\n")
        # The real marker: anchored on a *quoted* scenario name, so a line merely quoting
        # a verdict is not read as one (see scenario_markers._SUCCEEDED_RE).
        handle.write("12.5,1012.5,scenario_execution,Scenario 'funnel' succeeded.\n")

    campaign_ingest.ingest_campaign(conn, tree, "camp-a")

    row = conn.execute(
        'SELECT "timestamp", wall_ts, status FROM scenario_timestamps').fetchone()
    assert row is not None, "a run_log carrying a verdict must produce a row"
    assert row[0] == 12.5 and row[1] == 1012.5


def test_an_empty_file_produces_no_table_rather_than_an_empty_one(conn, tmp_path):
    """A header-only CSV is written on purpose; it should not invent a table."""
    tree = _campaign(tmp_path, runs=(("goal-1", 0),),
                     extra_file=("empty.csv", "a,b\n"))
    campaign_ingest.ingest_campaign(conn, tree, "camp-a")

    assert "empty" not in index_schema.known_tables(conn)


def test_a_later_run_adding_a_column_widens_the_table(conn, tmp_path):
    """The one-file-one-table rule across runs that disagree."""
    tree = _campaign(tmp_path, runs=(("goal-1", 0), ("goal-1", 1)))
    with open(os.path.join(tree, "goal-1", "1", "nav_metrics.csv"), "w",
              encoding="utf-8") as handle:
        handle.write("duration_s,collided,recovery_count\n9.0,1,3\n")

    campaign_ingest.ingest_campaign(conn, tree, "camp-a")

    got = conn.execute(
        "SELECT run_id, recovery_count FROM nav_metrics ORDER BY run_id").fetchall()
    assert got == [(0, None), (1, 3)]


def test_clear_campaign_reports_what_it_removed(conn, tmp_path):
    """Scoped deletion is the mechanism; it should be inspectable."""
    campaign_ingest.ingest_campaign(conn, _campaign(tmp_path), "camp-a")

    deleted = index_schema.clear_campaign(conn, "camp-a")

    assert deleted["poses"] == 4
    assert conn.execute("SELECT COUNT(*) FROM poses").fetchone()[0] == 0


def test_clear_campaign_on_an_empty_index_is_not_an_error(conn):
    """Called before anything was ingested, e.g. on a first-ever run."""
    assert index_schema.clear_campaign(conn, "never-seen") == {}


def test_a_sink_written_row_and_an_ingested_row_share_one_table(conn, tmp_path):
    """The RowSink seam and the CSV glob are two sources, not two schemas."""
    from robovast.results_processing.csv_types import REAL, TEXT

    campaign_ingest.ingest_campaign(conn, _campaign(tmp_path), "camp-a")
    PostgresRowSink(conn, campaign_id="camp-b").write(
        "poses", [{"timestamp": 9.0, "frame": "base_link", "position.x": 3.0}],
        context={"config_name": "goal-9", "run_id": 0},
        types={"timestamp": REAL, "frame": TEXT, "position.x": REAL})

    counts = dict(conn.execute(
        "SELECT campaign_id, COUNT(*) FROM poses GROUP BY 1 ORDER BY 1").fetchall())
    assert counts == {"camp-a": 4, "camp-b": 1}
