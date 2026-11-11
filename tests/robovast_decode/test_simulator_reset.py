# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A run's simulator must not be reset once it records: a simulator recording that was reset
fails the run's tables built from it, whole or followed live, and leaves everything else as it was.
"""

import json
import shutil

import pyarrow.parquet as pq
import pytest

from robovast_decode.build import available_tables, build, find_runs
from robovast_decode.live import Session, Watcher

from .conftest import (ROQSIM_BODIES, ROQSIM_SAMPLES, make_campaign, make_roqsim_campaign,
                       string_cdr, write_bag, write_roqsim_mcap)

TABLES = ["sim_poses", "joint_states", "clock_map", "sim_recording", "sim_entities"]
ONE_RUN_ROWS = ROQSIM_SAMPLES * len(ROQSIM_BODIES)
#: A file reset once: its sim time goes back at the fourth sample.
RESET_ONCE = [0.0, 0.02, 0.04, 0.0, 0.02, 0.04]
TWO_RUNS = (("cfg", 0), ("cfg", 1))


def _bag(campaign, run_id=0):
    return campaign / "cfg" / str(run_id) / "roqsim_bag"


def _manifest(campaign):
    return json.loads((campaign / ".cache" / "MANIFEST.json").read_text())


def _second_file(campaign, run_id=0):
    write_roqsim_mcap(_bag(campaign, run_id) / "roqsim-2.mcap")


def _sim_time_goes_back(campaign, run_id=0):
    write_roqsim_mcap(_bag(campaign, run_id) / "roqsim.mcap", times=RESET_ONCE)


def _assert_failed(report, campaign, says, run="cfg/0"):
    """Every simulator table of *run* failed in the report and in what a query reads."""
    tables = available_tables(str(campaign))
    for table in TABLES:
        reason = report.failed[table][run]
        assert "a run's simulator must not be reset once it records" in reason and says in reason, table
        assert tables[table]["failed"][run] == reason, table
        entry = _manifest(campaign)["tables"][table]["runs"][run]
        assert entry["rows"] == 0 and entry["files"] == [] and entry["reason"] == reason, table


@pytest.mark.parametrize("names", [("roqsim.mcap", "roqsim-2.mcap"), ("x.mcap", "x-2.mcap")])
def test_a_run_of_several_recording_files_fails_its_simulator_tables(tmp_path, names):
    campaign = make_roqsim_campaign(tmp_path / "stepped")
    bag = _bag(campaign)
    (bag / "roqsim.mcap").unlink()
    for name in names:
        write_roqsim_mcap(bag / name)
    report = build(str(campaign), tables=TABLES)
    _assert_failed(report, campaign, f"several files: {names[0]}, {names[1]}")
    assert not report.built.get("sim_poses")


def test_a_file_whose_sim_time_goes_back_fails_its_simulator_tables(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped")
    _sim_time_goes_back(campaign)
    report = build(str(campaign), tables=TABLES)
    _assert_failed(report, campaign, "sim time in roqsim.mcap goes back, from 0.04 s to 0 s")


def test_a_whole_build_replaces_what_the_run_gave_before_the_reset_with_the_failure(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped", verdict=False)
    assert build(str(campaign)).built["sim_poses"] == ["cfg/0"]
    _second_file(campaign)
    report = build(str(campaign))
    _assert_failed(report, campaign, "several files")


def test_another_run_of_the_campaign_and_its_one_file_still_build(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped", runs=TWO_RUNS)
    _second_file(campaign)
    report = build(str(campaign), tables=TABLES)
    _assert_failed(report, campaign, "several files")
    for table in TABLES:
        assert report.built[table] == ["cfg/1"], table
        assert "cfg/1" not in report.failed[table], table
    poses = pq.read_table(campaign / ".cache" / "tables" / "sim_poses" / "cfg" / "1.parquet")
    assert poses.num_rows == ONE_RUN_ROWS
    assert available_tables(str(campaign))["sim_poses"]["built"] == 1


def test_a_repeated_stamp_is_not_a_going_back(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped", runs=TWO_RUNS)
    _second_file(campaign)
    write_roqsim_mcap(_bag(campaign, 1) / "roqsim.mcap", times=[0.0, 0.02, 0.02, 0.04])
    report = build(str(campaign), tables=TABLES)
    _assert_failed(report, campaign, "several files")
    assert "cfg/1" not in report.failed["sim_poses"]
    assert "cfg/1" in report.built["sim_poses"]


def test_a_ros_bags_segments_are_one_recording(tmp_path):
    """The numbered segments of a recording are one recording, each with its own stamps, in the
    scenario bag and under any recording's own names; the sibling run is what fails."""
    campaign = make_roqsim_campaign(tmp_path / "stepped", runs=(*TWO_RUNS, ("cfg", 2)))
    _second_file(campaign)
    scenario = campaign / "cfg" / "1" / "rosbag2"
    write_bag(scenario, {"/fine": ("std_msgs/msg/String", [string_cdr("a"), string_cdr("b")])})
    shutil.copy(scenario / "rosbag2_0.mcap", scenario / "rosbag2_1.mcap")
    bag = _bag(campaign, 2)
    (bag / "roqsim.mcap").unlink()
    write_roqsim_mcap(bag / "x_0.mcap")
    write_roqsim_mcap(bag / "x_1.mcap")

    report = build(str(campaign), tables=[*TABLES, "rosbag2_fine"])
    _assert_failed(report, campaign, "several files")
    assert report.built["rosbag2_fine"] == ["cfg/1"]
    rows = pq.read_table(campaign / ".cache" / "tables" / "rosbag2_fine" / "cfg" / "1.parquet")
    assert rows.num_rows == 4, "both segments"
    for table in TABLES:
        assert sorted(report.built[table]) == ["cfg/1", "cfg/2"], table


def test_a_ros_runs_own_tables_stay_when_its_simulator_recording_is_several(tmp_path):
    campaign = make_campaign(tmp_path / "nav")
    write_roqsim_mcap(_bag(campaign) / "roqsim.mcap")
    _second_file(campaign)
    report = build(str(campaign), tables=["poses", "clock_map", "sim_poses"])
    assert "several files" in report.failed["sim_poses"]["cfg/0"]
    assert report.built["poses"] == ["cfg/0"]
    assert report.built["clock_map"] == ["cfg/0"], "the job's /clock, not the simulator's"


# -- live --------------------------------------------------------------------------------------

def test_a_session_fails_its_tables_when_the_next_file_appears(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "live", verdict=False)
    (run,) = find_runs(str(campaign))
    session = Session(str(campaign), run, str(_bag(campaign)), TABLES)
    assert any(b.table == "sim_poses" for b in session.advance())
    assert session.failed == {}
    _second_file(campaign)
    assert session.advance() == []
    assert set(session.failed) == set(TABLES)
    assert all("several files" in reason for reason in session.failed.values())


def test_a_session_fails_its_tables_when_sim_time_goes_back(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "live", verdict=False)
    _sim_time_goes_back(campaign)
    (run,) = find_runs(str(campaign))
    session = Session(str(campaign), run, str(_bag(campaign)), TABLES)
    session.advance()
    assert set(session.failed) == set(TABLES)
    assert all("goes back" in reason for reason in session.failed.values())


def test_the_watcher_names_the_reason_as_it_comes_and_ends_with_no_rows(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "live", verdict=False)
    watcher = Watcher(str(campaign), {}, part_s=0.0)
    watcher.subscribe("cfg/0", ["sim_poses"], lambda batch: None)
    first = _manifest(campaign)["tables"]["sim_poses"]["runs"]["cfg/0"]
    assert first["rows"] > 0 and not first.get("reason")

    _second_file(campaign)
    watcher.changed([str(_bag(campaign) / "roqsim-2.mcap")])
    running = _manifest(campaign)["tables"]["sim_poses"]["runs"]["cfg/0"]
    assert running["rows"] == first["rows"] and "several files" in running["reason"]

    verdict = campaign / "cfg" / "0" / "test.xml"
    verdict.write_text("<testsuite/>")
    watcher.changed([str(verdict)])
    assert watcher.following("cfg/0") == set()
    final = _manifest(campaign)["tables"]["sim_poses"]["runs"]["cfg/0"]
    assert final["rows"] == 0 and final["files"] == [] and final["complete"] is True
    assert "several files" in final["reason"]
    report = build(str(campaign), tables=["sim_poses"])
    assert report.skipped["sim_poses"] == ["cfg/0"], "a build finds the failure current"
