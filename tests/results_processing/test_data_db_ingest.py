# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Tests for CSV -> ``data.db`` ingest typing (``generate_data_db``).

The failure these guard against is silent: with every column stored as TEXT,
``ORDER BY timestamp`` sorts ``"10.022"`` before ``"9.5"``, so a trajectory comes
back shuffled and a path length computed from it is wrong by a factor rather than
raising.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from robovast.common.analysis import DATA_DB_SCHEMA_VERSION
from robovast.results_processing.data_query import describe_data_db
from robovast.results_processing.postprocessing_plugins import generate_data_db


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


def _build(campaign: Path) -> list[str]:
    """Run the ingest, returning its log lines; fails loudly if it did not succeed."""
    log: list[str] = []
    ok, message = generate_data_db(str(campaign), output_callback=log.append)
    assert ok, message
    return log


@pytest.fixture
def campaign(tmp_path: Path) -> Path:
    """A campaign with two runs of one config, written as a simulator would."""
    root = tmp_path / "campaign-1"
    # Timestamps deliberately straddle 10.0 so lexicographic ordering differs from
    # numeric ordering, and pose ids are zero-padded text.
    for run in (0, 1):
        _write_csv(
            root / "cfg-a" / str(run) / "poses.csv",
            "timestamp,position.x,position.y,seq,frame_id,pose_id",
            ["9.5,0.0,0.0,1,odom,007",
             "10.022,1.0,-0.5,2,odom,008",
             "11.75,2.5,1e-3,3,odom,009"],
        )
    return root


def _connect(campaign: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(campaign / "_execution" / "data.db")
    conn.row_factory = sqlite3.Row
    return conn


def _types(conn: sqlite3.Connection, table: str) -> dict:
    return {r["name"]: r["type"] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def test_numeric_columns_are_declared_numeric(campaign):
    _build(campaign)
    conn = _connect(campaign)
    types = _types(conn, "poses")
    assert types["timestamp"] == "REAL"
    assert types["position.x"] == "REAL"
    assert types["position.y"] == "REAL"
    assert types["seq"] == "INTEGER"
    assert types["frame_id"] == "TEXT"
    assert types["run_id"] == "INTEGER"


def test_values_are_stored_as_numbers(campaign):
    _build(campaign)
    conn = _connect(campaign)
    row = conn.execute(
        "SELECT timestamp, seq FROM poses ORDER BY timestamp LIMIT 1").fetchone()
    assert isinstance(row["timestamp"], float)
    assert isinstance(row["seq"], int)


def test_order_by_timestamp_is_numeric_not_lexicographic(campaign):
    """The original bug: '10.022' sorted before '9.5' and scrambled the trajectory."""
    _build(campaign)
    conn = _connect(campaign)
    order = [r["timestamp"] for r in conn.execute(
        "SELECT timestamp FROM poses WHERE run_id=0 ORDER BY timestamp")]
    assert order == [9.5, 10.022, 11.75]


def test_aggregates_need_no_cast(campaign):
    _build(campaign)
    conn = _connect(campaign)
    assert conn.execute("SELECT MAX(timestamp) FROM poses").fetchone()[0] == 11.75
    assert conn.execute(
        "SELECT COUNT(*) FROM poses WHERE timestamp > 10").fetchone()[0] == 4


def test_zero_padded_identifier_keeps_its_text(campaign):
    _build(campaign)
    conn = _connect(campaign)
    assert _types(conn, "poses")["pose_id"] == "TEXT"
    assert conn.execute(
        "SELECT pose_id FROM poses ORDER BY timestamp LIMIT 1").fetchone()[0] == "007"


def test_column_added_by_a_later_run_is_typed_too(campaign):
    """The ALTER TABLE path must infer a type, not default the column to text."""
    _write_csv(
        campaign / "cfg-a" / "1" / "poses.csv",
        "timestamp,position.x,position.y,seq,frame_id,pose_id,velocity",
        ["9.5,0.0,0.0,1,odom,007,0.31"],
    )
    _build(campaign)
    conn = _connect(campaign)
    assert _types(conn, "poses")["velocity"] == "REAL"
    assert conn.execute(
        "SELECT velocity FROM poses WHERE run_id=1").fetchone()[0] == pytest.approx(0.31)


def test_column_numeric_in_one_run_and_text_in_another_ends_up_text_everywhere(campaign):
    """The declared type must not outlive the evidence.

    Declaring REAL over a column that also holds ``'n/a'`` is the original bug's
    shape: ``AVG()`` reads the text as 0 and returns a plausible wrong number while
    the schema says the column is numeric. Once every run is in, the table is retyped
    and every value in the column is text.
    """
    _write_csv(
        campaign / "cfg-a" / "1" / "poses.csv",
        "timestamp,position.x,position.y,seq,frame_id,pose_id",
        ["9.5,n/a,0.0,1,odom,007"],
    )
    log = _build(campaign)
    conn = _connect(campaign)
    assert _types(conn, "poses")["position.x"] == "TEXT"
    stored = [r[0] for r in conn.execute('SELECT "position.x" FROM poses')]
    assert stored == ["0.0", "1.0", "2.5", "n/a"]  # nothing dropped, nothing numeric
    assert all(isinstance(v, str) for v in stored)
    assert any("position.x" in line and "WARNING" in line for line in log)


def test_a_retyped_table_keeps_its_lookup_index(campaign):
    """The (config_name, run_id) index must survive the table rebuild."""
    _write_csv(
        campaign / "cfg-a" / "1" / "poses.csv",
        "timestamp,position.x,position.y,seq,frame_id,pose_id",
        ["9.5,n/a,0.0,1,odom,007"],
    )
    _build(campaign)
    conn = _connect(campaign)
    assert [r["name"] for r in conn.execute("PRAGMA index_list(poses)")] == ["idx_poses_ctx"]


def test_mixed_column_is_flagged_to_whoever_writes_the_sql(campaign):
    """A postprocessing-log warning never reaches a SQL caller; describe must."""
    _write_csv(
        campaign / "cfg-a" / "1" / "poses.csv",
        "timestamp,position.x,position.y,seq,frame_id,pose_id",
        ["9.5,n/a,0.0,1,odom,007"],
    )
    _build(campaign)
    desc = describe_data_db(campaign)
    poses = next(t for t in desc["tables"] if t["table"] == "poses")
    assert "position.x TEXT" in poses["columns"]
    assert "numeric in some runs" in poses["column_notes"]["position.x"]
    # The bookkeeping tables are not results and must not be listed as such.
    assert not {t["table"] for t in desc["tables"]} & {"_column_notes", "_table_name_map"}
    assert "column_notes" in desc["note"]


def test_untouched_tables_carry_no_notes_and_are_not_rebuilt(campaign):
    _build(campaign)
    desc = describe_data_db(campaign)
    poses = next(t for t in desc["tables"] if t["table"] == "poses")
    assert "column_notes" not in poses


def test_integer_column_widened_by_a_later_run_is_declared_real(campaign):
    """Case that is harmless for queries but makes the reported schema wrong."""
    _write_csv(campaign / "cfg-a" / "0" / "m.csv", "x", ["1", "2"])
    _write_csv(campaign / "cfg-a" / "1" / "m.csv", "x", ["1.5"])
    _build(campaign)
    conn = _connect(campaign)
    assert _types(conn, "m")["x"] == "REAL"
    stored = [r[0] for r in conn.execute("SELECT x FROM m ORDER BY x")]
    assert stored == [1.0, 1.5, 2.0]
    assert all(isinstance(v, float) for v in stored)


def test_column_empty_in_the_first_run_takes_the_type_of_a_later_one(campaign):
    """A column empty in the first run must not be pinned to TEXT for the rest.

    It is declared with no type while there is no evidence (so nothing coerces the
    later numbers to strings), and the retype pass gives it the type the data proved.
    """
    _write_csv(
        campaign / "cfg-a" / "0" / "poses.csv",
        "timestamp,position.x,position.y,seq,frame_id,pose_id,error",
        ["9.5,0.0,0.0,1,odom,007,"],
    )
    _write_csv(
        campaign / "cfg-a" / "1" / "poses.csv",
        "timestamp,position.x,position.y,seq,frame_id,pose_id,error",
        ["9.5,0.0,0.0,1,odom,007,0.25"],
    )
    _build(campaign)
    conn = _connect(campaign)
    assert _types(conn, "poses")["error"] == "REAL"
    stored = conn.execute("SELECT error FROM poses WHERE run_id=1").fetchone()[0]
    assert isinstance(stored, float) and stored == pytest.approx(0.25)
    assert conn.execute("SELECT error FROM poses WHERE run_id=0").fetchone()[0] is None


def test_runs_table_param_columns_are_typed_from_the_param_values(tmp_path):
    """Scenario params must be comparable: ORDER BY param_speed, WHERE param_speed > x."""
    root = tmp_path / "campaign-2"
    for cfg in ("cfg-a", "cfg-b"):
        _write_csv(root / cfg / "0" / "poses.csv", "timestamp", ["1.0"])
    conn = sqlite3.connect(root / "campaign.db")
    conn.execute("CREATE TABLE unit (config_name TEXT, params_json TEXT, objective REAL)")
    conn.executemany(
        "INSERT INTO unit VALUES (?,?,?)",
        [("cfg-a", json.dumps({"speed": 0.5, "retries": 3, "controller": "dwb",
                               "waypoints": [{"x": 1.0}]}), 1.5),
         ("cfg-b", json.dumps({"speed": 10.0, "retries": 4, "controller": "mppi",
                               "waypoints": [{"x": 9.0}]}), 0.5)],
    )
    conn.commit()
    conn.close()

    _build(root)
    db = _connect(root)
    types = _types(db, "runs")
    assert types["param_speed"] == "REAL"
    assert types["param_retries"] == "INTEGER"
    assert types["param_controller"] == "TEXT"
    assert types["param_waypoints"] == "TEXT"  # non-scalar params stay JSON text
    # The ordering that text columns got wrong: 10.0 must not sort before 0.5.
    assert [r[0] for r in db.execute(
        "SELECT param_speed FROM runs ORDER BY param_speed")] == [0.5, 10.0]


# ---------------------------------------------------------------------------
# JSONL ingest (scenario_execution --bt-log)
# ---------------------------------------------------------------------------

def _bt_record(**overrides) -> dict:
    record = {
        "timestamp": 0.0, "behavior_id": "id-root", "parent_id": None,
        "child_index": None, "behavior_name": "root",
        "class_name": "py_trees.composites.Sequence", "type": "SEQUENCE",
        "additional_detail": "", "status": "INVALID", "feedback_message": "",
        "is_active": False, "tip_id": None,
        "osc_file": "/scenarios/demo.osc", "osc_line": 3, "osc_column": 0,
    }
    record.update(overrides)
    return record


def _write_bt_log(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"format": "behaviour_tree_log", "version": 2, "scenario": "demo",
            "scenario_file": "/scenarios/demo.osc", "scenario_sha256": "abc",
            "tick_period": 0.1, "clock": "SimulationClock", "py_trees": "2.4.0",
            "started_at": "2026-08-06T09:14:22Z"}
    path.write_text(
        "\n".join(json.dumps(r) for r in [meta, *records]) + "\n", encoding="utf-8")


def test_behaviour_tree_log_jsonl_becomes_the_behaviors_table(campaign):
    """--bt-log writes JSONL directly, so non-ROS runs get a behaviors table too."""
    _write_bt_log(campaign / "cfg-a" / "0" / "behaviors.jsonl", [
        _bt_record(),
        _bt_record(behavior_id="id-leaf", parent_id="id-root", child_index=0,
                   behavior_name="drive", class_name="rst.actions.Drive",
                   type="BEHAVIOUR", osc_line=7, osc_column=8),
        _bt_record(timestamp=10.5, status="RUNNING", is_active=True, tip_id="id-leaf"),
        _bt_record(timestamp=10.5, behavior_id="id-leaf", parent_id="id-root",
                   child_index=0, behavior_name="drive", class_name="rst.actions.Drive",
                   type="BEHAVIOUR", status="RUNNING", is_active=True,
                   feedback_message="driving", osc_line=7, osc_column=8),
        _bt_record(timestamp=9.5, behavior_id="id-leaf", parent_id="id-root",
                   child_index=0, behavior_name="drive", class_name="rst.actions.Drive",
                   type="BEHAVIOUR", status="SUCCESS", is_active=True, osc_line=7),
    ])
    _build(campaign)
    conn = _connect(campaign)

    types = _types(conn, "behaviors")
    assert types["timestamp"] == "REAL"
    assert types["status"] == "INTEGER"
    assert types["child_index"] == "INTEGER"
    assert types["osc_line"] == "INTEGER"

    # The metadata record is not a row.
    assert conn.execute("SELECT COUNT(*) FROM behaviors").fetchone()[0] == 5
    # Numeric status codes are re-derived so behaviors and nav2_behaviors stay one schema.
    assert sorted(tuple(r) for r in
                  conn.execute("SELECT DISTINCT status, status_name FROM behaviors")) == \
        [(1, "INVALID"), (2, "RUNNING"), (3, "SUCCESS")]
    # The ordering a TEXT timestamp would get wrong.
    assert [r[0] for r in conn.execute(
        "SELECT DISTINCT timestamp FROM behaviors ORDER BY timestamp")] == [0.0, 9.5, 10.5]
    # Structure survives: parent_id resolves and child_index orders siblings.
    leaf = conn.execute("SELECT parent_id, child_index, osc_line FROM behaviors "
                        "WHERE behavior_name='drive' LIMIT 1").fetchone()
    assert (leaf["parent_id"], leaf["child_index"], leaf["osc_line"]) == ("id-root", 0, 7)


def test_pruned_subtree_record_does_not_lose_its_column(campaign):
    """A removal record carries only three keys; the column must still be created."""
    _write_bt_log(campaign / "cfg-a" / "0" / "behaviors.jsonl", [
        _bt_record(),
        {"timestamp": 2.0, "behavior_id": "id-root", "removed": True},
    ])
    _build(campaign)
    conn = _connect(campaign)
    assert conn.execute(
        "SELECT behavior_id FROM behaviors WHERE removed=1").fetchone()["behavior_id"] == "id-root"


def test_unknown_jsonl_format_is_skipped(campaign):
    """An unrecognised JSONL producer must not create a junk table or fail the build."""
    path = campaign / "cfg-a" / "0" / "mystery.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": "something-else", "a": 1}) + "\n", encoding="utf-8")
    _build(campaign)
    conn = _connect(campaign)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "mystery" not in tables
    assert "poses" in tables


# -- the scenario verdict ----------------------------------------------------
#
# Recorded here and read everywhere else (the playback clock, the log views,
# `search_run_logs`), so these guard the one place the verdict is found in text.

_RUN_LOG_HEADER = "sim_time,wall_ts,time_source,container,node,source,level,severity,message"


def _write_run_log(campaign: Path, run: int, rows: list[str]) -> None:
    _write_csv(campaign / "cfg-a" / str(run) / "run_log.csv", _RUN_LOG_HEADER, rows)


def _verdict(campaign: Path, run: int = 0) -> sqlite3.Row:
    return _connect(campaign).execute(
        "SELECT * FROM scenario_timestamps WHERE config_name='cfg-a' AND run_id=?",
        (run,)).fetchone()


def test_the_verdict_is_recorded_on_both_clocks(campaign):
    """`wall_ts` is not redundant with `timestamp`: the log is ordered by wall, and
    every surface that stops at the end of the trial cuts on it."""
    _write_run_log(campaign, 0, [
        "11.75,1785092243.5,stamp,sut,nav2,rosout,INFO,other,goal reached",
        "12.00,1785092244.0,stamp,sut,scenario_execution_ros,rosout,INFO,other,"
        "Scenario 'test_scenario' succeeded.",
    ])
    _build(campaign)
    row = _verdict(campaign)
    assert row["status"] == "succeeded"
    assert row["timestamp"] == pytest.approx(12.0)
    assert row["wall_ts"] == pytest.approx(1785092244.0)


def test_a_run_aborted_at_shutdown_is_still_a_verdict(campaign):
    """The matcher this replaced knew only `succeeded.` and `: execution failed.`, so a
    run that ended `Aborted` / `Setup failed` / `Run failed` recorded no end at all --
    and is exactly the run with the most shutdown noise to hide."""
    _write_run_log(campaign, 0, [
        "12.00,1785092244.0,stamp,sut,scenario_execution_ros,rosout,ERROR,error,"
        "test_scenario: Aborted",
    ])
    _build(campaign)
    assert _verdict(campaign)["status"] == "failed"


def test_a_verdict_the_clock_map_cannot_place_still_records_its_wall_time(campaign):
    """The clock map does not extrapolate, so a run whose /clock stopped first has no
    sim time for its own verdict. Recording NULL there and a real `wall_ts` is what
    keeps the log cut working on precisely that run."""
    _write_run_log(campaign, 0, [
        ",1785092244.0,stamp,sut,scenario_execution_ros,rosout,INFO,other,"
        "Scenario 'test_scenario' succeeded.",
    ])
    _build(campaign)
    row = _verdict(campaign)
    assert row["timestamp"] is None
    assert row["wall_ts"] == pytest.approx(1785092244.0)
    assert row["status"] == "succeeded"


def test_a_run_that_logged_no_verdict_records_none(campaign):
    """Not an error: a run killed by its deadline never reached one. The UI reads this
    as 'nothing to trim' rather than trimming to an invented moment."""
    _write_run_log(campaign, 0, [
        "11.75,1785092243.5,stamp,sut,nav2,rosout,INFO,other,goal reached",
    ])
    _build(campaign)
    row = _verdict(campaign)
    assert row["status"] is None and row["wall_ts"] is None


def test_the_layout_is_stamped_so_a_reader_can_recognise_an_old_database(campaign):
    """``PRAGMA user_version`` records which layout built this file.

    No migrations go with it, deliberately: data.db is derived, so the upgrade path for an
    old one is to run postprocessing again. The stamp exists so a reader can say "this
    database predates the column you asked for" instead of "no such column".
    """
    _build(campaign)
    conn = sqlite3.connect(campaign / "_execution" / "data.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DATA_DB_SCHEMA_VERSION
    finally:
        conn.close()
