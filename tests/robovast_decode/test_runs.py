# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The ``runs`` table: one row per run and per run-less unit, from ``campaign.db``."""

import json
import sqlite3

import pyarrow as pa
import pytest

from robovast_decode.runs import StoreError, build_runs, channel_column_names

#: A quantity, not a byte count: ``available_mem`` is spelled exactly so by the sysinfo
#: collector, and a fixture inventing another key passes while the column is NULL everywhere.
SYSINFO = {"instance_type": "n2-standard-8", "node_label": "9f2c1a", "cpu_name": "Xeon",
           "available_cpus": 0.5, "available_mem": "16Gi"}


def _store(root, units, runs, *, with_job=True):
    """*units*: ``[(config, params, objective, status, paramset_id[, channels])]``;
    *runs*: ``[(unit_idx, run_id, status, passed, errors, failures, duration, start)]``."""
    db = sqlite3.connect(root / "campaign.db")
    db.executescript(
        "CREATE TABLE unit (id INTEGER PRIMARY KEY, config_name TEXT, params_json TEXT,"
        "                   objective REAL, status TEXT, paramset_id TEXT, channels_json TEXT);"
        "CREATE TABLE job (id INTEGER PRIMARY KEY, sysinfo_json TEXT);"
        "CREATE TABLE run (id INTEGER PRIMARY KEY, unit_id INTEGER, job_id INTEGER,"
        "                  run_id INTEGER, status TEXT, passed INTEGER, errors INTEGER,"
        "                  failures INTEGER, duration_s REAL, start_time TEXT);")
    if with_job:
        db.execute("INSERT INTO job VALUES (1, ?)", (json.dumps(SYSINFO),))
    for idx, (config_name, params, objective, status, paramset_id, *channels) in enumerate(
            units, 1):
        db.execute("INSERT INTO unit VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (idx, config_name, json.dumps(params) if params is not None else None,
                    objective, status, paramset_id,
                    json.dumps(channels[0]) if channels else None))
    for idx, (unit_idx, *rest) in enumerate(runs, 1):
        db.execute("INSERT INTO run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (idx, unit_idx, 1 if with_job else None, *rest))
    db.commit()
    db.close()


def _campaign(tmp_path, name="camp-a", *, units=None, runs=None, with_job=True):
    units = units if units is not None else [
        ("goal-1", {"speed": 0.5, "map_file": "warehouse.yaml"}, 1.25, "ok", "ps-1")]
    runs = runs if runs is not None else [
        (1, 0, "passed", 1, 0, 0, 12.5, "2026-08-10T07:15:09"),
        (1, 1, "failed", 0, 0, 1, 9.0, "2026-08-10T07:16:00")]
    root = tmp_path / name
    (root / "_execution").mkdir(parents=True)
    _store(root, units, runs, with_job=with_job)
    for unit_idx, run_id, *_ in runs:
        (root / units[unit_idx - 1][0] / str(run_id)).mkdir(parents=True)
    return root


def _rows(root):
    return build_runs(str(root)).to_pylist()


def _types(root):
    return {f.name: f.type for f in build_runs(str(root)).schema}


def test_one_row_per_run_with_the_outcome_from_the_store(tmp_path):
    rows = _rows(_campaign(tmp_path))
    assert [(r["config_name"], r["run_id"]) for r in rows] == [("goal-1", 0), ("goal-1", 1)]
    assert rows[0]["status"] == "passed" and rows[0]["passed"] == 1
    assert rows[1]["failures"] == 1 and rows[1]["duration_s"] == 9.0
    assert rows[0]["objective"] == 1.25
    assert {r["campaign_id"] for r in rows} == {"camp-a"}


def test_a_directory_without_a_campaign_store_is_refused(tmp_path):
    with pytest.raises(StoreError, match="not a campaign directory"):
        build_runs(str(tmp_path))


def test_end_time_is_start_plus_duration(tmp_path):
    assert _rows(_campaign(tmp_path))[0]["end_time"] == "2026-08-10T07:15:21.500000"


def test_end_time_is_null_when_the_start_is_not_known(tmp_path):
    tree = _campaign(tmp_path, runs=[(1, 0, "unknown", 0, 0, 0, None, None)])
    assert _rows(tree)[0]["end_time"] is None


def test_available_mem_is_normalised_from_a_kubernetes_quantity(tmp_path):
    row = _rows(_campaign(tmp_path))[0]
    assert row["available_mem_bytes"] == 16 * 1024 ** 3
    assert row["instance_type"] == "n2-standard-8" and row["node_label"] == "9f2c1a"


def test_available_cpus_keeps_a_fractional_reservation(tmp_path):
    tree = _campaign(tmp_path)
    assert _rows(tree)[0]["available_cpus"] == 0.5
    assert _types(tree)["available_cpus"] == pa.float64()


def test_a_run_without_a_host_record_gets_nulls(tmp_path):
    row = _rows(_campaign(tmp_path, with_job=False))[0]
    assert row["instance_type"] is None and row["node_label"] is None
    assert row["available_mem_bytes"] is None


def test_params_become_typed_param_columns(tmp_path):
    tree = _campaign(tmp_path)
    assert _types(tree)["param_speed"] == pa.float64()
    assert _types(tree)["param_map_file"] == pa.string()
    assert _rows(tree)[0]["param_speed"] == 0.5


def test_a_container_param_is_json_encoded(tmp_path):
    tree = _campaign(tmp_path, units=[("goal-1", {"waypoints": [[1, 2], [3, 4]]}, None, "ok",
                                       "ps-1")])
    assert json.loads(_rows(tree)[0]["param_waypoints"]) == [[1, 2], [3, 4]]


def test_a_censored_param_stays_readable(tmp_path):
    """A non-finite value is the float it is alone, and its text spelling inside JSON."""
    tree = _campaign(tmp_path, units=[
        ("goal-1", {"clearance": float("inf"), "gaps": [1.0, float("nan")]}, None, "ok",
         "ps-1")])
    row = _rows(tree)[0]
    assert row["param_clearance"] == float("inf")
    assert json.loads(row["param_gaps"]) == [1.0, "nan"]


def test_every_config_gets_every_siblings_param_column(tmp_path):
    tree = _campaign(tmp_path,
                     units=[("a", {"speed": 1.0}, None, "ok", "p1"),
                            ("b", {"wind": 3.0}, None, "ok", "p2")],
                     runs=[(1, 0, "passed", 1, 0, 0, 1.0, None),
                           (2, 0, "passed", 1, 0, 0, 1.0, None)])
    rows = _rows(tree)
    assert rows[0]["param_speed"] == 1.0 and rows[0]["param_wind"] is None
    assert rows[1]["param_wind"] == 3.0 and rows[1]["param_speed"] is None


def test_a_param_named_like_a_fixed_column_stays_apart(tmp_path):
    tree = _campaign(tmp_path, units=[("goal-1", {"status": "sneaky"}, None, "ok", "ps-1")])
    row = _rows(tree)[0]
    assert row["status"] == "passed" and row["param_status"] == "sneaky"


def test_a_param_disagreeing_in_type_widens_the_column(tmp_path):
    tree = _campaign(tmp_path,
                     units=[("a", {"speed": 1.0}, None, "ok", "p1"),
                            ("b", {"speed": "n/a"}, None, "ok", "p2")],
                     runs=[(1, 0, "passed", 1, 0, 0, 1.0, None),
                           (2, 0, "passed", 1, 0, 0, 1.0, None)])
    assert _types(tree)["param_speed"] == pa.string()
    assert [r["param_speed"] for r in _rows(tree)] == ["1.0", "n/a"]


def test_sim_and_sut_destinations_become_param_columns(tmp_path):
    channels = {"sim": {"floor": {"friction": 0.8}, "wall": {"friction": 0.2}},
                "sut": {"nav2.controller.max_vel": 0.4}}
    tree = _campaign(tmp_path, units=[("goal-1", {"speed": 1.0}, None, "ok", "ps-1",
                                       channels)])
    row = _rows(tree)[0]
    assert row["param_sim_floor_friction"] == 0.8
    assert row["param_sim_wall_friction"] == 0.2
    assert row["param_sut_max_vel"] == 0.4
    assert row["param_speed"] == 1.0


def test_a_scenario_param_wins_a_name_clash(tmp_path):
    channels = {"sim": {"speed": 9.0}}
    tree = _campaign(tmp_path, units=[("goal-1", {"sim_speed": 1.0}, None, "ok", "ps-1",
                                       channels)])
    assert _rows(tree)[0]["param_sim_speed"] == 1.0


def test_column_names_grow_leftwards_only_as_far_as_they_must():
    names = channel_column_names([("sim", "floor.friction"), ("sim", "wall.friction"),
                                  ("sut", "a.b.radius")])
    assert names == {("sim", "floor.friction"): "sim_floor_friction",
                     ("sim", "wall.friction"): "sim_wall_friction",
                     ("sut", "a.b.radius"): "sut_radius"}


def test_a_composition_failed_unit_becomes_a_run_less_row(tmp_path):
    tree = _campaign(tmp_path,
                     units=[("goal-1", {"speed": 0.5}, 1.0, "ok", "ps-1"),
                            (None, {"speed": 99.0}, None, "composition_failed", "ps-2")],
                     runs=[(1, 0, "passed", 1, 0, 0, 1.0, None)])
    failed = [r for r in _rows(tree) if r["status"] == "composition_failed"]
    assert len(failed) == 1
    assert failed[0]["config_name"] == "ps-2", "the parameter-set id is its only identity"
    assert failed[0]["run_id"] is None and failed[0]["passed"] == 0
    assert failed[0]["duration_s"] is None and failed[0]["probed"] == 0
    assert failed[0]["param_speed"] == 99.0


def test_a_missing_configuration_becomes_a_run_less_row(tmp_path):
    tree = _campaign(tmp_path,
                     units=[("cfg-a", {"speed": 0.5}, 1.0, "ok", "cfg-a"),
                            ("cfg-b", {}, None, "missing", "cfg-b")],
                     runs=[(1, 0, "passed", 1, 0, 0, 1.0, None)])
    missing = [r for r in _rows(tree) if r["status"] == "missing"]
    assert [(r["config_name"], r["run_id"], r["passed"]) for r in missing] == [
        ("cfg-b", None, 0)]


def test_probed_is_a_separate_column_and_leaves_the_status_alone(tmp_path):
    tree = _campaign(tmp_path)
    (tree / "_execution" / "interventions.json").write_text(json.dumps(
        [{"kind": "probed", "job_name": "goal-1/0", "runs": ["goal-1/0"]},
         {"kind": "killed", "job_name": "goal-1/1", "runs": ["goal-1/1"]}]))
    rows = _rows(tree)
    assert rows[0]["probed"] == 1 and rows[0]["status"] == "passed"
    assert rows[1]["probed"] == 0


def test_a_probed_cluster_job_marks_its_runs_through_the_job_links(tmp_path):
    tree = _campaign(tmp_path)
    (tree / "_transient").mkdir()
    (tree / "_transient" / "job_links.yaml").write_text(
        "goal-1/0/job: ../../_jobs/batch-0/job-0\ngoal-1/1/job: ../../_jobs/batch-0/job-1\n")
    (tree / "_execution" / "interventions.json").write_text(json.dumps(
        [{"kind": "probed", "job_name": "k8s-job", "job_dir": "_jobs/batch-0/job-1"}]))
    assert [r["probed"] for r in _rows(tree)] == [0, 1]


def test_a_run_directory_the_store_has_not_recorded_is_a_run_still_going(tmp_path):
    tree = _campaign(tmp_path)
    (tree / "goal-1" / "7").mkdir()
    rows = _rows(tree)
    assert [r["run_id"] for r in rows] == [0, 1, 7]
    assert rows[2]["status"] is None and rows[2]["param_speed"] == 0.5


def test_building_twice_gives_the_same_table(tmp_path):
    tree = _campaign(tmp_path)
    assert build_runs(str(tree)).equals(build_runs(str(tree)))
