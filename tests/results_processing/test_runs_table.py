# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``runs`` dimension table, rebuilt in the central index.

``runs`` is what an analysis joins its metrics against on ``(config_name, run_id)``: the
per-run outcome, the host it ran on, and every scenario parameter flattened into a typed
``param_*`` column. It used to be written by the ``data.db`` writer; these tests pin the
behaviour that writer had, against a real Postgres.

Set ``ROBOVAST_TEST_PG_DSN`` to run them; without it they skip.
"""

import csv
import json
import os
import sqlite3

import pytest

from robovast.results_processing import campaign_ingest, index_schema

DSN = os.environ.get("ROBOVAST_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="ROBOVAST_TEST_PG_DSN is not set")

SCHEMA = "runs_table_test"

#: A quantity, not a byte count -- the trap the column exists to close. ``available_mem``
#: is spelled exactly so by ``collect_sysinfo``, and a fixture that invents
#: ``available_mem_gb`` passes while the column is NULL in every real campaign.
SYSINFO = {"instance_type": "n2-standard-8", "node_label": "9f2c1a", "cpu_name": "Xeon",
           "available_cpus": 0.5, "available_mem": "16Gi"}


@pytest.fixture(name="conn")
def _conn():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DSN, autocommit=True) as conn:
        for statement in (f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE",
                          "DROP SCHEMA IF EXISTS campaign CASCADE",
                          f"CREATE SCHEMA {SCHEMA}", f"SET search_path TO {SCHEMA}"):
            conn.execute(statement)
        yield conn
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS campaign CASCADE")


def _store(root, units, runs, *, with_job=True):
    """A ``campaign.db`` with the tables the runs build reads.

    *units* is ``[(config_name, params, objective, status, paramset_id)]``;
    *runs* is ``[(unit_idx, run_id, status, passed, errors, failures, duration, start)]``.
    """
    db = sqlite3.connect(root / "campaign.db")
    db.executescript(
        "CREATE TABLE campaign (id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE unit (id INTEGER PRIMARY KEY, config_name TEXT, params_json TEXT,"
        "                   objective REAL, status TEXT, paramset_id TEXT);"
        "CREATE TABLE job (id INTEGER PRIMARY KEY, sysinfo_json TEXT);"
        "CREATE TABLE run (id INTEGER PRIMARY KEY, unit_id INTEGER, job_id INTEGER,"
        "                  run_id INTEGER, status TEXT, passed INTEGER, errors INTEGER,"
        "                  failures INTEGER, duration_s REAL, start_time TEXT);")
    db.execute("INSERT INTO campaign VALUES (1, 'camp')")
    if with_job:
        db.execute("INSERT INTO job VALUES (1, ?)", (json.dumps(SYSINFO),))
    for idx, (config_name, params, objective, status, paramset_id) in enumerate(units, 1):
        db.execute("INSERT INTO unit VALUES (?, ?, ?, ?, ?, ?)",
                   (idx, config_name, json.dumps(params) if params is not None else None,
                    objective, status, paramset_id))
    for idx, row in enumerate(runs, 1):
        unit_idx, run_id, status, passed, errors, failures, duration, start = row
        db.execute("INSERT INTO run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (idx, unit_idx, 1 if with_job else None, run_id, status, passed,
                    errors, failures, duration, start))
    db.commit()
    db.close()


def _campaign(tmp_path, name="camp", *, units=None, runs=None, with_job=True):
    """A results tree with a campaign record and one run directory per recorded run."""
    units = units if units is not None else [
        ("goal-1", {"speed": 0.5, "map_file": "warehouse.yaml"}, 1.25, "ok", "ps-1"),
    ]
    runs = runs if runs is not None else [
        (1, 0, "passed", 1, 0, 0, 12.5, "2026-08-10T07:15:09"),
        (1, 1, "failed", 0, 0, 1, 9.0, "2026-08-10T07:16:00"),
    ]
    root = tmp_path / name
    (root / "_execution").mkdir(parents=True, exist_ok=True)
    _store(root, units, runs, with_job=with_job)
    for unit_idx, run_id, *_ in runs:
        config_name = units[unit_idx - 1][0]
        run_dir = root / config_name / str(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "nav_metrics.csv").write_text("collided\n0\n")
    return root


def _runs(conn, campaign_id="camp-a"):
    rows = conn.execute(
        "SELECT * FROM runs WHERE campaign_id = %s "
        "ORDER BY config_name, run_id NULLS LAST", (campaign_id,)).fetchall()
    names = [d[0] for d in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
        "AND table_name = 'runs' ORDER BY ordinal_position", (SCHEMA,)).fetchall()]
    return [dict(zip(names, row)) for row in rows]


def test_one_row_per_run_directory_with_the_outcome_from_the_store(conn, tmp_path):
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path)), "camp-a")

    rows = _runs(conn)
    assert [(r["config_name"], r["run_id"]) for r in rows] == [("goal-1", 0), ("goal-1", 1)]
    assert rows[0]["status"] == "passed" and rows[0]["passed"] == 1
    assert rows[1]["failures"] == 1 and rows[1]["duration_s"] == 9.0
    assert rows[0]["objective"] == 1.25


def test_end_time_is_start_plus_duration(conn, tmp_path):
    """Derived, because nothing on disk records when a run stopped."""
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path)), "camp-a")

    assert _runs(conn)[0]["end_time"] == "2026-08-10T07:15:21.500000"


def test_end_time_is_null_when_the_start_is_not_known(conn, tmp_path):
    """A guess would read as authoritative; NULL says "not recorded"."""
    tree = _campaign(tmp_path, runs=[(1, 0, "unknown", 0, 0, 0, None, None)])
    campaign_ingest.ingest_campaign(conn, str(tree), "camp-a")

    assert _runs(conn)[0]["end_time"] is None


def test_available_mem_is_normalised_from_a_kubernetes_quantity(conn, tmp_path):
    """``available_mem`` is "16Gi" when the .vast set a limit and a byte count otherwise.

    Stored raw it would be numeric in some runs and text in others -- the column is
    ``available_mem_bytes`` precisely so a query can compare and average it.
    """
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path)), "camp-a")

    row = _runs(conn)[0]
    assert row["available_mem_bytes"] == 16 * 1024 ** 3
    assert row["instance_type"] == "n2-standard-8"
    assert row["node_label"] == "9f2c1a"


def test_available_cpus_keeps_a_fractional_reservation(conn, tmp_path):
    """INTEGER would truncate 0.5 cores to 0, which reads as "this run had no CPU"."""
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path)), "camp-a")

    assert _runs(conn)[0]["available_cpus"] == 0.5
    types = dict(conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'runs'", (SCHEMA,)).fetchall())
    assert types["available_cpus"] == "double precision"


def test_a_run_without_a_host_record_gets_nulls_not_placeholders(conn, tmp_path):
    campaign_ingest.ingest_campaign(
        conn, str(_campaign(tmp_path, with_job=False)), "camp-a")

    row = _runs(conn)[0]
    assert row["instance_type"] is None and row["node_label"] is None
    assert row["available_mem_bytes"] is None


def test_shm_columns_are_the_peak_and_the_limit_from_resource_usage(conn, tmp_path):
    """The number that explains a SIGBUS, which ``available_mem_bytes`` cannot."""
    tree = _campaign(tmp_path)
    with (tree / "goal-1" / "0" / "resource_usage.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["wall_ts", "shm_used_bytes", "shm_total_bytes"])
        writer.writerow([1.0, 1000, 64000])
        writer.writerow([2.0, "", 64000])       # absent, not zero
        writer.writerow([3.0, -1, 64000])       # negative is absent too
        writer.writerow([4.0, 4000, 64000])
    campaign_ingest.ingest_campaign(conn, str(tree), "camp-a")

    rows = _runs(conn)
    assert rows[0]["shm_peak_bytes"] == 4000 and rows[0]["shm_limit_bytes"] == 64000
    # No resource table for run 1: unmeasured, which is not "used none of it".
    assert rows[1]["shm_peak_bytes"] is None and rows[1]["shm_limit_bytes"] is None


def test_a_run_with_no_clock_map_reports_that_as_a_finding(conn, tmp_path):
    """``none`` is the answer, not an error: its log is wall-time only."""
    from robovast.results_processing import clock_map

    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path)), "camp-a")

    assert _runs(conn)[0]["clock_map_source"] == clock_map.SOURCE_NONE


def test_params_become_typed_param_columns(conn, tmp_path):
    """What makes ``WHERE param_speed > 0.5`` mean what it says instead of comparing text."""
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path)), "camp-a")

    types = dict(conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'runs'", (SCHEMA,)).fetchall())
    assert types["param_speed"] == "double precision"
    assert types["param_map_file"] == "text"
    assert _runs(conn)[0]["param_speed"] == 0.5


def test_a_container_param_is_json_encoded_rather_than_dropped(conn, tmp_path):
    tree = _campaign(tmp_path, units=[
        ("goal-1", {"waypoints": [[1, 2], [3, 4]]}, None, "ok", "ps-1")])
    campaign_ingest.ingest_campaign(conn, str(tree), "camp-a")

    assert json.loads(_runs(conn)[0]["param_waypoints"]) == [[1, 2], [3, 4]]


def test_every_config_gets_every_siblings_param_column(conn, tmp_path):
    """One shape for the whole campaign: NULL where a run has no value for a key."""
    tree = _campaign(
        tmp_path,
        units=[("a", {"speed": 1.0}, None, "ok", "p1"),
               ("b", {"wind": 3.0}, None, "ok", "p2")],
        runs=[(1, 0, "passed", 1, 0, 0, 1.0, None), (2, 0, "passed", 1, 0, 0, 1.0, None)])
    campaign_ingest.ingest_campaign(conn, str(tree), "camp-a")

    rows = _runs(conn)
    assert rows[0]["param_speed"] == 1.0 and rows[0]["param_wind"] is None
    assert rows[1]["param_wind"] == 3.0 and rows[1]["param_speed"] is None


def test_a_param_colliding_with_a_fixed_column_is_skipped(conn, tmp_path):
    """``param_status`` would be a second column named for the run's outcome."""
    tree = _campaign(tmp_path, units=[
        ("goal-1", {"status": "sneaky", "speed": 1.0}, None, "ok", "ps-1")])
    campaign_ingest.ingest_campaign(conn, str(tree), "camp-a")

    row = _runs(conn)[0]
    assert "param_status" not in row
    assert row["status"] == "passed" and row["param_speed"] == 1.0


def test_a_param_disagreeing_in_type_widens_the_column(conn, tmp_path):
    """One 'n/a' demotes a numeric factor to text rather than losing the value."""
    tree = _campaign(
        tmp_path,
        units=[("a", {"speed": 1.0}, None, "ok", "p1"),
               ("b", {"speed": "n/a"}, None, "ok", "p2")],
        runs=[(1, 0, "passed", 1, 0, 0, 1.0, None), (2, 0, "passed", 1, 0, 0, 1.0, None)])
    campaign_ingest.ingest_campaign(conn, str(tree), "camp-a")

    types = dict(conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'runs'", (SCHEMA,)).fetchall())
    assert types["param_speed"] == "text"


def test_a_second_campaigns_param_widens_the_shared_column_and_is_recorded(conn, tmp_path):
    """Two campaigns share one ``runs`` table, so a disagreement is a cross-campaign fact."""
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path, "a")), "camp-a")
    tree = _campaign(tmp_path, "b", units=[("goal-1", {"speed": "slow"}, None, "ok", "p")],
                     runs=[(1, 0, "passed", 1, 0, 0, 1.0, None)])
    campaign_ingest.ingest_campaign(conn, str(tree), "camp-b")

    types = dict(conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'runs'", (SCHEMA,)).fetchall())
    assert types["param_speed"] == "text"
    note = conn.execute(
        f'SELECT note FROM "{index_schema.COLUMN_NOTES_TABLE}" '
        "WHERE table_name = 'runs' AND column_name = 'param_speed' AND kind = %s",
        (index_schema.NOTE_WIDENING,)).fetchone()
    assert note and "REAL -> TEXT" in note[0]


def test_a_composition_failed_unit_becomes_a_run_less_row(conn, tmp_path):
    """A campaign that could not build half of what it proposed must not read as one
    that proposed less."""
    tree = _campaign(
        tmp_path,
        units=[("goal-1", {"speed": 0.5}, 1.0, "ok", "ps-1"),
               (None, {"speed": 99.0}, None, "composition_failed", "ps-2")],
        runs=[(1, 0, "passed", 1, 0, 0, 1.0, None)])
    campaign_ingest.ingest_campaign(conn, str(tree), "camp-a")

    rows = _runs(conn)
    failed = [r for r in rows if r["status"] == "composition_failed"]
    assert len(failed) == 1
    assert failed[0]["config_name"] == "ps-2", "paramset_id is its only identity"
    assert failed[0]["run_id"] is None and failed[0]["passed"] == 0
    assert failed[0]["duration_s"] is None and failed[0]["probed"] == 0
    assert failed[0]["param_speed"] == 99.0, "the params are the whole point"


def test_probed_is_a_separate_column_and_does_not_touch_the_status(conn, tmp_path):
    """A probed run can still pass; folding a human's action into the outcome is the
    mistake that keeping ``killed`` out of ``num_failed`` avoids."""
    tree = _campaign(tmp_path)
    (tree / "_execution" / "interventions.json").write_text(json.dumps(
        [{"kind": "probed", "job_name": "goal-1/0", "runs": ["goal-1/0"]}]))
    campaign_ingest.ingest_campaign(conn, str(tree), "camp-a")

    rows = _runs(conn)
    assert rows[0]["probed"] == 1 and rows[0]["status"] == "passed"
    assert rows[1]["probed"] == 0


def test_the_probed_column_carries_its_warning(conn, tmp_path):
    """The note the retired writer attached, where someone writing AVG() is looking."""
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path)), "camp-a")

    note = conn.execute(
        f'SELECT note FROM "{index_schema.COLUMN_NOTES_TABLE}" '
        "WHERE table_name = 'runs' AND column_name = 'probed' AND kind = %s",
        (index_schema.NOTE_DOC,)).fetchone()
    assert note and "WHILE IT RAN" in note[0]


def test_a_run_directory_the_store_never_recorded_falls_back_to_test_xml(conn, tmp_path):
    """A store predating the ``run`` table, and a run that crashed before writing one."""
    tree = _campaign(tmp_path)
    orphan = tree / "goal-1" / "7"
    orphan.mkdir(parents=True)
    (orphan / "nav_metrics.csv").write_text("collided\n0\n")
    campaign_ingest.ingest_campaign(conn, str(tree), "camp-a")

    rows = _runs(conn)
    assert [r["run_id"] for r in rows] == [0, 1, 7]
    assert rows[2]["status"] == "unknown", "counted, not silently dropped"


def test_every_row_carries_the_campaign_id(conn, tmp_path):
    """One index holds every campaign; an unscoped join returns the corpus."""
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path)), "camp-a")

    assert {r[0] for r in conn.execute(
        "SELECT DISTINCT campaign_id FROM runs").fetchall()} == {"camp-a"}


def test_another_campaigns_runs_never_appear(conn, tmp_path):
    """Same configuration name and same run ids on purpose: those are the keys that
    collide once every campaign shares one table."""
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path, "a")), "camp-a")
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path, "b")), "camp-b")

    assert len(_runs(conn, "camp-a")) == 2
    assert {r["campaign_id"] for r in _runs(conn, "camp-a")} == {"camp-a"}
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 4


def test_re_ingesting_reproduces_the_same_rows(conn, tmp_path):
    """The reproducibility invariant: re-ingest replaces, it does not double."""
    tree = str(_campaign(tmp_path))
    campaign_ingest.ingest_campaign(conn, tree, "camp-a")
    first = _runs(conn)
    campaign_ingest.ingest_campaign(conn, tree, "camp-a")

    assert _runs(conn) == first


def test_re_ingesting_one_campaign_leaves_the_others_runs_alone(conn, tmp_path):
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path, "a")), "camp-a")
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path, "b")), "camp-b")
    campaign_ingest.ingest_campaign(conn, str(_campaign(tmp_path, "a2")), "camp-a")

    counts = dict(conn.execute(
        "SELECT campaign_id, COUNT(*) FROM runs GROUP BY 1 ORDER BY 1").fetchall())
    assert counts == {"camp-a": 2, "camp-b": 2}


def test_a_campaign_without_a_record_still_gets_its_runs(conn, tmp_path):
    """A campaign that ended badly is exactly the one worth reading -- with no params."""
    root = tmp_path / "bare"
    (root / "_execution").mkdir(parents=True)
    run_dir = root / "goal-1" / "0"
    run_dir.mkdir(parents=True)
    (run_dir / "nav_metrics.csv").write_text("collided\n0\n")

    campaign_ingest.ingest_campaign(conn, str(root), "camp-a")

    rows = _runs(conn)
    assert [(r["config_name"], r["run_id"], r["status"]) for r in rows] == [
        ("goal-1", 0, "unknown")]
    assert not [c for c in rows[0] if c.startswith("param_")]
