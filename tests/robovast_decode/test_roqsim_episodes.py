# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""roqsim's recording of a run in several episodes: one mcap per episode, ``roqsim.mcap``, then
``roqsim-2.mcap``, ``roqsim-3.mcap``, ... A reset starts the next file and simulated time starts
over in it, so the files are read in episode order and every row carries the episode its file's
provenance names.
"""

import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from robovast_decode.build import build, find_runs
from robovast_decode.decode import segments
from robovast_decode.handlers import HandlerError, SimPoses
from robovast_decode.live import Session
from robovast_decode.tables import CONTEXT_COLUMNS
from robovast_decode.values import ClockDecimator

from .conftest import (ROQSIM_BODIES, ROQSIM_JOINTS, ROQSIM_SAMPLES, make_campaign,
                       make_roqsim_campaign, roqsim_wall_time, write_roqsim_mcap)

TABLES = ["sim_poses", "joint_states", "clock_map", "sim_recording", "sim_entities"]
#: The takes a run's files are numbered by, and the episodes their provenance names: not the
#: same numbers, since a reset before an episode's first sample leaves no file for it.
TAKES = (1, 2, 10)
EPISODES = (1, 2, 4)


def _table(campaign, table) -> pa.Table:
    return pq.read_table(campaign / ".cache" / "tables" / table / "cfg" / "0.parquet")


def _take_name(take: int) -> str:
    return "roqsim.mcap" if take == 1 else f"roqsim-{take}.mcap"


def _episodes_campaign(root, samples=ROQSIM_SAMPLES):
    """A stepped run whose recording is three episodes, each its own file and world."""
    campaign = make_roqsim_campaign(root)
    bag = campaign / "cfg" / "0" / "roqsim_bag"
    for take, episode in zip(TAKES, EPISODES):
        write_roqsim_mcap(bag / _take_name(take), samples=samples,
                          meta={"episode": episode, "world": f"worlds/w{episode}.yaml"})
    return campaign


# -- the order the files are read in -----------------------------------------------------------

def test_a_runs_episodes_are_read_in_the_order_of_their_takes(tmp_path):
    for name in ("x-10.mcap", "x.mcap", "x-2.mcap"):
        (tmp_path / name).write_bytes(b"")
    assert [p.rsplit("/", 1)[1] for p in segments(str(tmp_path))] == [
        "x.mcap", "x-2.mcap", "x-10.mcap"]


def test_a_rosbag2_recordings_segments_keep_their_index_order(tmp_path):
    names = ["rosbag2_2026_01_01-12_00_00_10.mcap", "rosbag2_2026_01_01-12_00_00_0.mcap",
             "rosbag2_2026_01_01-12_00_00_2.mcap", "rosbag2_2026_01_01-12_00_00_1.mcap"]
    for name in names:
        (tmp_path / name).write_bytes(b"")
    assert [p.rsplit("/", 1)[1] for p in segments(str(tmp_path))] == [
        f"rosbag2_2026_01_01-12_00_00_{i}.mcap" for i in (0, 1, 2, 10)]


# -- the episode column ------------------------------------------------------------------------

def test_every_table_carries_the_episode_its_files_provenance_names(tmp_path):
    campaign = _episodes_campaign(tmp_path / "episodes")
    report = build(str(campaign))
    assert not report.failed
    for table in TABLES:
        assert report.built[table] == ["cfg/0"], table

    poses = _table(campaign, "sim_poses").to_pylist()
    per_episode = ROQSIM_SAMPLES * len(ROQSIM_BODIES)
    assert [r["episode"] for r in poses] == [e for e in EPISODES for _ in range(per_episode)]
    for episode in EPISODES:
        times = [r["timestamp"] for r in poses if r["episode"] == episode]
        assert times[0] == 0.0 and times == sorted(times), "each episode's time runs one way"

    joints = _table(campaign, "joint_states").to_pylist()
    assert [r["episode"] for r in joints] == [
        e for e in EPISODES for _ in range(ROQSIM_SAMPLES * len(ROQSIM_JOINTS))]

    clock = _table(campaign, "clock_map").to_pylist()
    assert sorted({r["episode"] for r in clock}) == list(EPISODES)
    for episode in EPISODES:
        sim = [r["sim_ts"] for r in clock if r["episode"] == episode]
        assert sim[0] == 0.0 and sim[-1] == pytest.approx((ROQSIM_SAMPLES - 1) / 50), (
            "each episode keeps its own first and last sample")

    recording = _table(campaign, "sim_recording").to_pylist()
    assert [(r["episode"], r["world"]) for r in recording] == [
        (e, f"worlds/w{e}.yaml") for e in EPISODES]
    assert all(json.loads(r["model_json"])["nq"] == 2 for r in recording), (
        "each episode's closing record wins")

    entities = _table(campaign, "sim_entities").to_pylist()
    assert [(r["episode"], r["name"], r["present"]) for r in entities] == [
        (e, name, int(name != "box")) for e in EPISODES for name in ROQSIM_BODIES]


def test_a_run_of_one_file_reads_as_one_episode_with_the_rows_it_has_always_had(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "one")
    write_roqsim_mcap(campaign / "cfg" / "0" / "roqsim_bag" / "roqsim.mcap",
                      meta={"episode": 5})
    build(str(campaign))
    for table in TABLES:
        assert set(_table(campaign, table).column("episode").to_pylist()) == {5}, table
    assert _table(campaign, "sim_poses").num_rows == ROQSIM_SAMPLES * len(ROQSIM_BODIES)
    assert _table(campaign, "sim_recording").num_rows == 1
    assert _table(campaign, "sim_entities").num_rows == len(ROQSIM_BODIES)

    span = ROQSIM_SAMPLES / 50
    samples = [(roqsim_wall_time(i / 50, span), i / 50) for i in range(ROQSIM_SAMPLES)]
    decimator = ClockDecimator()
    expected = decimator.run(samples)
    clock = _table(campaign, "clock_map").drop_columns([*CONTEXT_COLUMNS, "episode"])
    assert [(r["wall_ts"], r["sim_ts"]) for r in clock.to_pylist()] == expected, (
        "one decimation over the whole file, as before")


def test_a_session_follows_a_run_into_its_next_episode(tmp_path):
    reference = _episodes_campaign(tmp_path / "reference")
    build(str(reference), tables=TABLES)

    campaign = make_roqsim_campaign(tmp_path / "live", verdict=False)
    bag = campaign / "cfg" / "0" / "roqsim_bag"
    write_roqsim_mcap(bag / "roqsim.mcap", meta={"episode": 1, "world": "worlds/w1.yaml"})
    (run,) = find_runs(str(campaign))
    session = Session(str(campaign), run, str(bag), TABLES)
    got = {table: [] for table in TABLES}
    for batch in session.advance():
        got[batch.table].append(batch.rows)
    for take, episode in zip(TAKES[1:], EPISODES[1:]):
        write_roqsim_mcap(bag / _take_name(take),
                          meta={"episode": episode, "world": f"worlds/w{episode}.yaml"})
        for batch in session.advance():
            got[batch.table].append(batch.rows)
    for batch in session.finish():
        got[batch.table].append(batch.rows)

    for table in TABLES:
        union = pa.concat_tables(got[table], promote_options="permissive")
        expected = _table(reference, table)
        assert (union.drop_columns(list(CONTEXT_COLUMNS)).to_pylist()
                == expected.drop_columns(list(CONTEXT_COLUMNS)).to_pylist()), table


# -- where the episode comes from --------------------------------------------------------------

def _provenance(handler, episode):
    handler.metadata("roqsim.recording", {"json": json.dumps({"episode": episode})})


def _sample(handler):
    handler.message("poses", {"t": 0.0, "w": 1.0, "bodies": {"robot": [0.0] * 13}},
                    "roqsim.poses", 0)


def test_a_sample_before_its_files_provenance_fails_the_table():
    handler = SimPoses()
    handler.segment("roqsim.mcap")
    _provenance(handler, 1)
    _sample(handler)
    handler.segment("roqsim-2.mcap")
    with pytest.raises(HandlerError, match="roqsim-2.mcap .* its episode is unknown"):
        _sample(handler)


def test_a_provenance_naming_no_episode_fails_the_table():
    handler = SimPoses()
    handler.segment("roqsim.mcap")
    with pytest.raises(HandlerError, match="names no episode"):
        handler.metadata("roqsim.recording", {"json": json.dumps({"world": "w"})})


def test_ground_truth_keeps_each_episode_apart(tmp_path):
    from robovast_decode.ground_truth import GROUND_TRUTH

    campaign = _episodes_campaign(tmp_path / "episodes")
    build(str(campaign), tables=[GROUND_TRUTH],
          config={"groups": [], "ground_truth": {"table": "sim_poses",
                                                  "entity_kind": "robot"}})
    rows = _table(campaign, GROUND_TRUTH).to_pylist()
    assert [r["episode"] for r in rows] == [e for e in EPISODES for _ in range(ROQSIM_SAMPLES)]
    for episode in EPISODES:
        times = [r["timestamp"] for r in rows if r["episode"] == episode]
        assert times == sorted(times) and times[0] == 0.0


# -- a file that holds several episodes --------------------------------------------------------

def test_a_file_whose_sim_time_goes_back_is_split_into_episodes_in_every_table(tmp_path):
    """An older roqsim wrote every trial of a run into one file, its provenance naming the
    first; each reset starts the next episode."""
    campaign = make_roqsim_campaign(tmp_path / "one_file")
    write_roqsim_mcap(campaign / "cfg" / "0" / "roqsim_bag" / "roqsim.mcap", trials=3,
                      meta={"episode": 2})
    report = build(str(campaign))
    assert not report.failed
    episodes = (2, 3, 4)

    poses = _table(campaign, "sim_poses").to_pylist()
    assert [r["episode"] for r in poses] == [
        e for e in episodes for _ in range(ROQSIM_SAMPLES * len(ROQSIM_BODIES))]
    for episode in episodes:
        times = [r["timestamp"] for r in poses if r["episode"] == episode]
        assert times[0] == 0.0 and times == sorted(times), "each episode's time runs one way"

    joints = _table(campaign, "joint_states").to_pylist()
    assert [r["episode"] for r in joints] == [
        e for e in episodes for _ in range(ROQSIM_SAMPLES * len(ROQSIM_JOINTS))]

    clock = _table(campaign, "clock_map").to_pylist()
    assert sorted({r["episode"] for r in clock}) == list(episodes)
    for episode in episodes:
        sim = [r["sim_ts"] for r in clock if r["episode"] == episode]
        assert sim[0] == 0.0 and sim[-1] == pytest.approx((ROQSIM_SAMPLES - 1) / 50), (
            "each episode is decimated on its own and keeps its first and last sample")

    recording = _table(campaign, "sim_recording").to_pylist()
    assert [r["episode"] for r in recording] == list(episodes), (
        "the process's provenance holds for each of its episodes")
    assert all(json.loads(r["model_json"])["nq"] == 2 for r in recording)

    entities = _table(campaign, "sim_entities").to_pylist()
    assert [(r["episode"], r["name"], r["present"]) for r in entities] == [
        (4, name, int(name != "box")) for name in ROQSIM_BODIES], (
        "the file's last roster is its last episode's, and no earlier one is guessed")


# -- a ROS run has one episode -----------------------------------------------------------------

def _two_episodes(bag, one_file):
    if one_file:
        write_roqsim_mcap(bag / "roqsim.mcap", trials=2, meta={"episode": 0})
    else:
        write_roqsim_mcap(bag / "roqsim.mcap", meta={"episode": 0})
        write_roqsim_mcap(bag / "roqsim-2.mcap", meta={"episode": 1})


@pytest.mark.parametrize("one_file", [False, True], ids=["two files", "one file"])
def test_a_ros_run_whose_simulator_recording_holds_two_episodes_fails_its_tables(tmp_path,
                                                                                  one_file):
    """A ROS run's simulator is a sidecar the run cannot reset, so two episodes in its
    recording are refused by name rather than built into tables that mix the trials."""
    campaign = make_campaign(tmp_path / "ros")
    _two_episodes(campaign / "cfg" / "0" / "roqsim_bag", one_file)
    report = build(str(campaign), tables=["sim_poses", "sim_recording", "sim_entities"])
    for table in ("sim_poses", "sim_recording", "sim_entities"):
        assert "cfg/0" not in report.built.get(table, []), table
        assert "a ROS run, which has one" in report.failed[table]["cfg/0"], table


@pytest.mark.parametrize("one_file", [False, True], ids=["two files", "one file"])
def test_a_stepped_run_whose_simulator_recording_holds_two_episodes_is_built(tmp_path,
                                                                             one_file):
    campaign = make_roqsim_campaign(tmp_path / "stepped")
    _two_episodes(campaign / "cfg" / "0" / "roqsim_bag", one_file)
    report = build(str(campaign), tables=["sim_poses", "sim_recording", "sim_entities"])
    assert not report.failed
    assert sorted(set(_table(campaign, "sim_poses").column("episode").to_pylist())) == [0, 1]


def test_the_campaigns_mode_says_whether_a_run_can_hold_several_episodes(tmp_path):
    """``base`` is the stepped shape whatever else the run recorded; ``ros2`` is a ROS run
    whatever it did not."""
    ros = make_campaign(tmp_path / "ros")
    _two_episodes(ros / "cfg" / "0" / "roqsim_bag", one_file=False)
    assert not build(str(ros), tables=["sim_poses"], config={"mode": "base"}).failed
    stepped = make_roqsim_campaign(tmp_path / "stepped")
    _two_episodes(stepped / "cfg" / "0" / "roqsim_bag", one_file=False)
    report = build(str(stepped), tables=["sim_poses"], config={"mode": "ros2"})
    assert "a ROS run, which has one" in report.failed["sim_poses"]["cfg/0"]
