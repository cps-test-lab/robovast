# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A stepped-run campaign -- roqsim's recording per run, no rosbag -- answers SQL like any
other, and its tables carry their notes."""

import pytest

from robovast.common.analysis.metrics import calculate_speeds_from_poses
from robovast_data import Engine, Scope
from robovast_data.notes import notes_for
from robovast_decode.handlers import SIM_POSE_FIELDNAMES
from tests.robovast_decode.conftest import (ROQSIM_BODIES, ROQSIM_SAMPLES, make_roqsim_campaign,
                                            write_roqsim_mcap)

from .conftest import write_store


def _stepped(root):
    make_roqsim_campaign(root, runs=(("cfg", 0), ("cfg", 1)))
    write_store(root, {"cfg": {"params": {"speed": 0.5}, "runs": {0: "passed", 1: "passed"}}})
    return root


def test_a_stepped_campaign_answers_over_its_simulator_recording(tmp_path):
    engine = Engine([Scope(str(_stepped(tmp_path / "stepped")))], workers=1)
    count = engine.arrow("SELECT count(*) AS n FROM sim_poses").column("n")[0].as_py()
    assert count == 2 * ROQSIM_SAMPLES * len(ROQSIM_BODIES)
    joints = engine.arrow("SELECT count(DISTINCT joint) AS n FROM joint_states")
    assert joints.column("n")[0].as_py() == 2
    clock = engine.arrow("SELECT run_id, clock_map_source FROM run_clock ORDER BY run_id")
    assert clock.to_pylist() == [{"run_id": 0, "clock_map_source": "roqsim"},
                                 {"run_id": 1, "clock_map_source": "roqsim"}]
    recording = engine.arrow("SELECT run_id, capture_fps, seed FROM sim_recording ORDER BY 1")
    assert recording.to_pylist() == [{"run_id": 0, "capture_fps": "50/1", "seed": 7},
                                     {"run_id": 1, "capture_fps": "50/1", "seed": 7}]
    entities = engine.arrow("SELECT name FROM sim_entities WHERE present = 0 AND run_id = 0")
    assert entities.column("name").to_pylist() == ["box"]


def test_the_simulator_poses_are_tracks(tmp_path):
    engine = Engine([Scope(str(_stepped(tmp_path / "stepped")))], workers=1)
    rows = engine.arrow("SELECT source, frame, points FROM pose_track_view "
                        "WHERE run_id = 0 ORDER BY frame").to_pylist()
    assert rows == [{"source": "sim_poses", "frame": name, "points": ROQSIM_SAMPLES}
                    for name in sorted(ROQSIM_BODIES)]
    catalog = engine.catalog()
    for table in ("sim_poses", "joint_states", "sim_recording", "sim_entities", "clock_map"):
        assert catalog[table]["runs"] == 2, table
    assert catalog["sim_poses"]["built"] == 2, "built by the query that named it"
    assert catalog["joint_states"]["built"] == 0, "not yet named by anything"


def _episodes(root):
    """Run 0 holds three episodes, its robot driving 0.1, 0.2 and 0.3 m per sample in them;
    run 1 holds one, at 0.1 m per sample."""
    _stepped(root)
    write_roqsim_mcap(root / "cfg" / "0" / "roqsim_bag" / "roqsim.mcap", trials=3,
                      meta={"episode": 1})
    return Engine([Scope(str(root))], workers=1)


#: The robot's length in one episode at 0.1 m per sample.
_LENGTH = 0.1 * (ROQSIM_SAMPLES - 1)


def test_each_episode_of_a_run_is_its_own_track(tmp_path):
    engine = _episodes(tmp_path / "episodes")
    rows = engine.arrow("SELECT run_id, episode, points, length_m, duration_s "
                        "FROM pose_track_view WHERE frame = 'robot' "
                        "ORDER BY run_id, episode").to_pylist()
    duration = (ROQSIM_SAMPLES - 1) / 50
    assert rows == [
        {"run_id": 0, "episode": e, "points": ROQSIM_SAMPLES,
         "length_m": pytest.approx(_LENGTH * e), "duration_s": pytest.approx(duration)}
        for e in (1, 2, 3)
    ] + [{"run_id": 1, "episode": 0, "points": ROQSIM_SAMPLES,
          "length_m": pytest.approx(_LENGTH), "duration_s": pytest.approx(duration)}]


def test_speeds_are_differentiated_within_each_episode(tmp_path):
    engine = _episodes(tmp_path / "episodes")
    poses = engine.arrow("SELECT * FROM sim_poses WHERE frame = 'robot'").to_pandas()
    speeds = calculate_speeds_from_poses(poses)
    per = speeds.groupby(["run_id", "episode"])["linear_speed"]
    assert per.size().to_dict() == {(0, 1): ROQSIM_SAMPLES - 1, (0, 2): ROQSIM_SAMPLES - 1,
                                    (0, 3): ROQSIM_SAMPLES - 1, (1, 0): ROQSIM_SAMPLES - 1}
    for (run_id, episode), group in per:
        factor = episode if run_id == 0 else 1
        assert group.to_numpy() == pytest.approx(0.1 * 50 * factor), (run_id, episode)


def test_the_simulator_tables_carry_their_notes():
    poses = notes_for("sim_poses", SIM_POSE_FIELDNAMES + ["orientation.yaw"])
    assert "SIMULATED seconds" in poses["timestamp"] and "Difference it freely" in poses["timestamp"]
    assert "Do NOT difference it" in poses["wall_time"]
    assert "planar projection" in poses["orientation.yaw"]
    joints = notes_for("joint_states", ["timestamp", "wall_time", "joint", "position"])
    assert set(joints) == {"timestamp", "wall_time", "position"}
    recording = notes_for("sim_recording", ["format_version", "world", "overrides_json", "seed",
                                            "packages_json", "capture_fps", "timestep",
                                            "model_json"])
    assert set(recording) == {"overrides_json", "packages_json", "capture_fps", "timestep",
                              "model_json"}
    assert "num/den" in recording["capture_fps"]
    entities = notes_for("sim_entities", ["name", "kind", "body", "present"])
    assert set(entities) == {"present", "body"} and "LAST" in entities["present"]
