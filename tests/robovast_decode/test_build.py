# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A table is built for a run when something names it, once, and says what it could not do."""

import json

import pyarrow.parquet as pq
import pytest

from robovast_decode.build import available_tables, build
from robovast_decode.cli import main

from .conftest import make_campaign


def _manifest(campaign):
    return json.loads((campaign / ".cache" / "MANIFEST.json").read_text())


def test_every_recorded_topic_is_a_table_unless_there_is_a_reason(campaign):
    tables = available_tables(str(campaign))
    for name in ("poses", "nav2_behavior_tree", "costmaps", "rosbag2_collision",
                 "action_navigate_to_pose_feedback", "action_navigate_to_pose_status",
                 "rosout", "clock_map"):
        assert name in tables, name
    assert "rosbag2_scan" not in tables                   # a laser scan is bulk: not rows
    assert all(counts["built"] == 0 for counts in tables.values())


def test_the_recording_report_says_what_was_not_tabulated_and_why(campaign):
    build(str(campaign), tables=["rosbag2_collision"])
    rows = pq.read_table(campaign / ".cache" / "tables" / "_recording" / "cfg" / "0.parquet")
    by_topic = {r["topic"]: r for r in rows.to_pylist()}
    assert by_topic["/scan"]["table"] is None
    assert "bulk" in by_topic["/scan"]["reason"]
    assert by_topic["/collision"]["table"] == "rosbag2_collision"
    assert by_topic["/collision"]["messages"] == 40 and by_topic["/collision"]["bytes"] > 0
    assert by_topic["/rosout"]["recording"] == "logs/rosout_bag"


def test_only_the_named_table_is_built_and_a_second_build_does_nothing(campaign):
    first = build(str(campaign), tables=["poses"])
    assert first.built == {"poses": ["cfg/0"]}
    assert set(_manifest(campaign)["tables"]) == {"poses", "_recording"}
    second = build(str(campaign), tables=["poses"])
    assert second.built == {} and second.skipped == {"poses": ["cfg/0"]}


def test_a_grown_recording_is_built_again(campaign):
    build(str(campaign), tables=["rosbag2_collision"])
    with open(campaign / "cfg" / "0" / "rosbag2" / "rosbag2_0.mcap", "ab") as fh:
        fh.write(b"\x00")                                 # the bytes changed: stale
    assert build(str(campaign), tables=["rosbag2_collision"]).built == {
        "rosbag2_collision": ["cfg/0"]}


def test_a_required_frame_that_never_resolves_fails_its_table_only(campaign):
    config = {"groups": [{"bag_dir": "rosbag2", "plugins": [
        {"type": "tf_to_csv", "frames": "all", "require": ["nowhere"]}]}]}
    report = build(str(campaign), config=config)
    assert "nowhere" in report.failed["poses"]["cfg/0"]
    assert "rosbag2_collision" in report.built
    assert not (campaign / ".cache" / "tables" / "poses").exists()


def test_a_run_that_is_not_there_is_refused(campaign):
    with pytest.raises(KeyError):
        build(str(campaign), runs=["cfg/7"])


def test_a_table_no_recording_gives_is_reported(campaign):
    assert build(str(campaign), tables=["nope"]).unknown == ["nope"]


def test_rows_carry_their_run(tmp_path):
    campaign = make_campaign(tmp_path / "c-2026-01-01-00000000", runs=(("a", 0), ("b", 3)))
    build(str(campaign), tables=["rosbag2_collision"])
    for config, run_id in (("a", 0), ("b", 3)):
        rows = pq.read_table(campaign / ".cache" / "tables" / "rosbag2_collision" / config /
                             f"{run_id}.parquet").to_pylist()
        assert {(r["campaign_id"], r["config_name"], r["run_id"]) for r in rows} == {
            (campaign.name, config, run_id)}


def test_a_job_that_ran_several_runs_keeps_its_rows_unattributed(tmp_path):
    campaign = make_campaign(tmp_path / "c-2026-01-01-00000000", runs=(("a", 0), ("a", 1)),
                             shared_job=True)
    build(str(campaign), tables=["rosout"])
    path = campaign / ".cache" / "tables" / "rosout" / "_jobs" / "job-0.parquet"
    rows = pq.read_table(path).to_pylist()
    assert rows and {(r["config_name"], r["run_id"]) for r in rows} == {(None, None)}
    assert "_jobs/job-0" in _manifest(campaign)["tables"]["rosout"]["runs"]


def test_a_run_without_its_verdict_is_built_but_not_complete(tmp_path):
    campaign = make_campaign(tmp_path / "c-2026-01-01-00000000", verdict=False)
    build(str(campaign), tables=["rosbag2_collision"])
    entry = _manifest(campaign)["tables"]["rosbag2_collision"]["runs"]["cfg/0"]
    assert entry["complete"] is False and entry["rows"] == 40


def test_the_command_line_builds_and_lists(campaign, capsys):
    assert main(["build", str(campaign), "--table", "costmaps"]) == 0
    assert main(["tables", str(campaign)]) == 0
    out = capsys.readouterr().out
    assert "built   costmaps: 1 run(s)" in out
    assert "costmaps" in out and "built for 1 of 1" in out


def test_a_runs_own_data_files_are_tables_typed_by_their_values(campaign):
    run = campaign / "cfg" / "0"
    (run / "out.csv").write_text("# units: m\ndistance,label,missing\n1.5,a,\ninf,007,\n")
    (run / "behaviors.jsonl").write_text(
        '{"format": "behavior_tree_log"}\n'
        '{"id": 1, "name": "root", "status": "RUNNING", "is_active": true}\n')
    report = build(str(campaign), tables=["out", "behaviors"])
    assert not report.failed
    out = pq.read_table(campaign / ".cache" / "tables" / "out" / "cfg" / "0.parquet")
    assert out.schema.field("distance").type == "double"
    assert out.column("label").to_pylist() == ["a", "007"]       # leading zero: text
    assert out.column("distance").to_pylist()[1] == float("inf")
    behaviors = pq.read_table(campaign / ".cache" / "tables" / "behaviors" / "cfg" / "0.parquet")
    row = behaviors.to_pylist()[0]
    assert (row["status"], row["status_name"], row["is_active"]) == (2, "RUNNING", 1)


def test_a_data_file_claiming_a_built_table_is_refused_by_name(campaign):
    (campaign / "cfg" / "0" / "poses.csv").write_text("frame,timestamp\nx,1\n")
    report = build(str(campaign), tables=["poses"])
    assert "poses.csv" in report.failed["poses"]["cfg/0"]


def test_a_ragged_file_fails_its_own_table_only(campaign):
    (campaign / "cfg" / "0" / "bad.csv").write_text("a,b\n1,2,3\n")
    (campaign / "cfg" / "0" / "good.csv").write_text("a,b\n1,2\n")
    report = build(str(campaign), tables=["bad", "good"])
    assert "more fields than its header" in report.failed["bad"]["cfg/0"]
    assert report.built["good"] == ["cfg/0"]


def test_a_pose_table_gets_its_heading(campaign):
    build(str(campaign), tables=["poses"], config={"groups": []})
    poses = pq.read_table(campaign / ".cache" / "tables" / "poses" / "cfg" / "0.parquet")
    assert "orientation.yaw" in poses.column_names
