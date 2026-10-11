# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``ground_truth_poses``: one table of where the robot truly was, whichever producer gave it.

A simulator that records itself answers from its own recording (``sim_poses``, the bodies its
roster calls robots); one that does not publishes a ``*_gt`` frame on ``/tf``, which ``poses``
holds. Both come out with the same columns, in world frame, on sim seconds.
"""

import json

import pyarrow.parquet as pq
import pytest

from robovast_decode.build import available_tables, build
from robovast_decode.ground_truth import COLUMNS, GROUND_TRUTH, TF_SOURCE, source_of
from robovast_decode.tables import CONTEXT_COLUMNS

from .conftest import (NAV_CONFIG, ROQSIM_SAMPLES, make_campaign, make_roqsim_campaign,
                       write_roqsim_mcap)

ROQSIM_TRUTH = {"groups": [], "ground_truth": {"table": "sim_poses", "entity_kind": "robot"}}


def _table(campaign, table, config="cfg", run_id=0):
    return pq.read_table(campaign / ".cache" / "tables" / table / config / f"{run_id}.parquet")


def _entry(campaign, key="cfg/0"):
    manifest = json.loads((campaign / ".cache" / "MANIFEST.json").read_text())
    return manifest["tables"][GROUND_TRUTH]["runs"][key]


def test_a_simulator_recording_gives_the_robot_entitys_poses(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped")
    report = build(str(campaign), tables=[GROUND_TRUTH], config=ROQSIM_TRUTH)
    assert report.built[GROUND_TRUTH] == ["cfg/0"] and not report.failed
    assert not report.unknown, "the inputs it read are not the caller's tables"

    truth = _table(campaign, GROUND_TRUTH)
    assert truth.column_names == [*CONTEXT_COLUMNS, *COLUMNS, "orientation.yaw"]
    rows = truth.to_pylist()
    assert len(rows) == ROQSIM_SAMPLES, "the robot only: the props are not ground truth"
    assert {r["frame"] for r in rows} == {"robot"}
    assert {r["source_table"] for r in rows} == {"sim_poses"}
    sim = [r for r in _table(campaign, "sim_poses").to_pylist() if r["frame"] == "robot"]
    for mine, theirs in zip(rows, sim):
        for column in ("timestamp", "position.x", "position.y", "orientation.w",
                       "twist.linear.x"):
            assert mine[column] == theirs[column], column
    assert _entry(campaign)["complete"] is True


def test_a_tf_ground_truth_frame_gives_the_same_shape(tmp_path):
    campaign = make_campaign(tmp_path / "nav")
    report = build(str(campaign), tables=[GROUND_TRUTH], config=NAV_CONFIG)
    assert report.built[GROUND_TRUTH] == ["cfg/0"] and not report.failed

    truth = _table(campaign, GROUND_TRUTH)
    assert truth.column_names == [*CONTEXT_COLUMNS, *COLUMNS, "orientation.yaw"]
    rows = truth.to_pylist()
    assert rows and {r["frame"] for r in rows} == {"robot_gt"}
    assert {r["source_table"] for r in rows} == {"poses"}
    poses = [r for r in _table(campaign, "poses").to_pylist() if r["frame"] == "robot_gt"]
    # The time the pose was true: the transform's stamp, not when it arrived.
    assert [r["timestamp"] for r in rows] == sorted(r["stamp"] for r in poses
                                                    if r["stamp"] is not None)
    assert all(r["twist.linear.x"] is None for r in rows), "TF carries no velocity"


def test_the_tf_convention_is_the_default(tmp_path):
    assert source_of({"groups": []}) == TF_SOURCE
    campaign = make_campaign(tmp_path / "nav")
    build(str(campaign), tables=[GROUND_TRUTH], config=NAV_CONFIG)
    assert {r["source_table"] for r in _table(campaign, GROUND_TRUTH).to_pylist()} == {"poses"}


def test_a_ros_run_on_a_recording_simulator_reads_the_recording(tmp_path):
    """Both producers present: the configured one answers, and only it."""
    campaign = make_campaign(tmp_path / "nav")
    write_roqsim_mcap(campaign / "cfg" / "0" / "roqsim_bag" / "roqsim.mcap")
    config = {**NAV_CONFIG, "ground_truth": ROQSIM_TRUTH["ground_truth"]}
    build(str(campaign), tables=[GROUND_TRUTH], config=config)
    rows = _table(campaign, GROUND_TRUTH).to_pylist()
    assert {(r["frame"], r["source_table"]) for r in rows} == {("robot", "sim_poses")}


def test_a_run_with_no_ground_truth_frame_says_so(tmp_path):
    campaign = make_campaign(tmp_path / "nav")
    base_only = {"groups": [{"bag_dir": "rosbag2",
                             "plugins": [{"type": "tf_to_csv", "frames": ["base_link"]}]}]}
    report = build(str(campaign), tables=[GROUND_TRUTH], config=base_only)
    reason = report.failed[GROUND_TRUTH]["cfg/0"]
    assert "ends in '_gt'" in reason and "base_link" in reason
    assert _entry(campaign)["reason"] == reason
    assert not (campaign / ".cache" / "tables" / GROUND_TRUTH).exists()


def test_a_roster_with_no_robot_says_so(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped")
    config = {"ground_truth": {"table": "sim_poses", "entity_kind": "pedestrian"}}
    report = build(str(campaign), tables=[GROUND_TRUTH], config=config)
    assert "no entity of kind 'pedestrian'" in report.failed[GROUND_TRUTH]["cfg/0"]


def test_it_is_listed_where_its_source_is_and_built_again_for_another_source(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped")
    assert available_tables(str(campaign), ROQSIM_TRUTH)[GROUND_TRUTH]["runs"] == 1
    assert GROUND_TRUTH not in available_tables(str(campaign)), "no poses, no TF ground truth"

    build(str(campaign), tables=[GROUND_TRUTH], config=ROQSIM_TRUTH)
    again = build(str(campaign), tables=[GROUND_TRUTH], config=ROQSIM_TRUTH)
    assert again.skipped[GROUND_TRUTH] == ["cfg/0"]
    other = build(str(campaign), tables=[GROUND_TRUTH], config={})
    assert "poses has no rows" in other.failed[GROUND_TRUTH]["cfg/0"]


@pytest.mark.parametrize("source", [{"table": "poses"}, {"frame_suffix": "_gt"},
                                    {"table": "poses", "frame_suffix": "_gt",
                                     "entity_kind": "robot"}, "poses"])
def test_a_malformed_source_is_refused(source):
    with pytest.raises(ValueError, match="ground_truth"):
        source_of({"ground_truth": source})
