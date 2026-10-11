# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""roqsim's own recording -- one mcap of JSON channels and metadata -- decoded like a bag.

A stepped run has nothing else: no scenario bag, no job, no ``/clock``. Its poses, joints
and clock map, its provenance and its entity roster all come from this one file, whole or
while it is still being written.
"""

import json
import shutil
import struct

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from robovast_decode.build import available_tables, build, find_runs, recording_closed
from robovast_decode.decode import decode_bag
from robovast_decode.framing import MAGIC, OP_DATA_END, has_footer
from robovast_decode.handlers import (SIM_POSE_FIELDNAMES, Handler, HandlerError, SimEntities,
                                      SimPoses, SimRecording)
from robovast_decode.live import Session, Watcher
from robovast_decode.registry import ROQSIM_BAG, plan_for
from robovast_decode.tables import CONTEXT_COLUMNS

from .conftest import (ROQSIM_BODIES, ROQSIM_JOINTS, ROQSIM_SAMPLES, ROQSIM_WALL_START,
                       make_campaign, make_roqsim_campaign, write_roqsim_mcap)

TABLES = ["sim_poses", "joint_states", "clock_map", "sim_recording", "sim_entities"]


def _manifest(campaign):
    return json.loads((campaign / ".cache" / "MANIFEST.json").read_text())


def _table(campaign, table, config="cfg", run_id=0) -> pa.Table:
    return pq.read_table(campaign / ".cache" / "tables" / table / config / f"{run_id}.parquet")


def _without_context(table: pa.Table):
    return sorted(json.dumps(r, sort_keys=True)
                  for r in table.drop_columns(list(CONTEXT_COLUMNS)).to_pylist())


# -- whole ---------------------------------------------------------------------------------

def test_a_stepped_run_gives_its_five_tables_typed(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped")
    report = build(str(campaign))
    for table in TABLES:
        assert report.built[table] == ["cfg/0"], table
    assert not report.failed

    poses = _table(campaign, "sim_poses")
    assert poses.num_rows == ROQSIM_SAMPLES * len(ROQSIM_BODIES)
    assert poses.column_names == [*CONTEXT_COLUMNS, *SIM_POSE_FIELDNAMES, "orientation.yaw"]
    for name in SIM_POSE_FIELDNAMES:
        expected = pa.string() if name == "frame" else pa.float64()
        assert poses.schema.field(name).type == expected, name
    first = poses.slice(0, 1).to_pylist()[0]
    assert first["timestamp"] == 0.0 and first["wall_time"] == ROQSIM_WALL_START
    assert first["frame"] == "robot" and first["orientation.w"] == 1.0
    assert first["twist.linear.x"] == pytest.approx(5.0)
    assert set(poses.column("frame").to_pylist()) == set(ROQSIM_BODIES)

    joints = _table(campaign, "joint_states")
    assert joints.num_rows == ROQSIM_SAMPLES * len(ROQSIM_JOINTS)
    assert joints.column_names == [*CONTEXT_COLUMNS, "timestamp", "wall_time", "joint",
                                   "position"]
    assert joints.schema.field("position").type == pa.float64()
    assert set(joints.column("joint").to_pylist()) == set(ROQSIM_JOINTS)

    clock = _table(campaign, "clock_map")
    assert clock.column_names == [*CONTEXT_COLUMNS, "wall_ts", "sim_ts"]
    rows = clock.to_pylist()
    assert len(rows) >= 3, "a rate change and a pause keep more than the two ends"
    assert rows[0]["wall_ts"] == ROQSIM_WALL_START and rows[0]["sim_ts"] == 0.0
    assert rows[-1]["sim_ts"] == pytest.approx((ROQSIM_SAMPLES - 1) / 50)

    (recording,) = _table(campaign, "sim_recording").to_pylist()
    assert recording["format_version"] == 3 and recording["seed"] == 7
    assert recording["world"] == "worlds/open_space.yaml"
    assert recording["capture_fps"] == "50/1" and recording["timestep"] == 0.002
    assert json.loads(recording["overrides_json"]) == {"robot.speed": 0.5}
    assert json.loads(recording["packages_json"])["roqsim"] == "0.9.0"
    assert json.loads(recording["model_json"])["nq"] == 2, "the closing record wins"

    entities = _table(campaign, "sim_entities").to_pylist()
    assert [(e["name"], e["kind"], e["body"], e["present"]) for e in entities] == [
        ("robot", "robot", "robot", 1), ("box", "prop", "box", 0), ("shelf", "prop", "shelf", 1)]
    assert _table(campaign, "sim_entities").schema.field("present").type == pa.int64()


def test_the_recording_report_names_the_state_channel_and_its_reason(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped")
    build(str(campaign), tables=["sim_poses"])
    by_topic = {r["topic"]: r for r in _table(campaign, "_recording").to_pylist()}
    assert set(by_topic) == {"state", "poses", "joints", "clock"}
    assert by_topic["state"]["reason"] == "raw simulator state, read by roqsim itself"
    assert by_topic["state"]["table"] is None and by_topic["state"]["type"] == "roqsim.state"
    assert by_topic["state"]["messages"] == ROQSIM_SAMPLES and by_topic["state"]["bytes"] > 0
    assert by_topic["poses"]["table"] == "sim_poses"
    assert by_topic["poses"]["type"] == "roqsim.poses" and by_topic["poses"]["messages"] == 50
    assert by_topic["clock"]["table"] == "clock_map"
    assert {r["recording"] for r in by_topic.values()} == {ROQSIM_BAG}


def test_a_run_whose_only_clock_is_the_recording_has_its_clock_map_and_source(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped")
    report = build(str(campaign), tables=["run_clock"])
    assert report.built["clock_map"] == ["cfg/0"], "the derived table's input, built first"
    (clock,) = _table(campaign, "run_clock").to_pylist()
    assert clock["clock_map_source"] == "roqsim"
    assert clock["clock_map_samples"] >= 3
    assert clock["clock_map_wall_span_s"] > 0 and clock["clock_map_sim_span_s"] > 0
    assert not any("no clock map" in note for note in report.notes)


def test_a_run_with_the_infrastructure_clock_keeps_it_and_says_so(tmp_path):
    """A ROS run on roqsim has both: the job's ``/clock`` stays the run's map and the
    recording's ``clock`` channel is reported as not tabulated for that reason."""
    campaign = make_campaign(tmp_path / "nav")
    write_roqsim_mcap(campaign / "cfg" / "0" / "roqsim_bag" / "roqsim.mcap")
    report = build(str(campaign), tables=["clock_map", "sim_poses", "run_clock"])
    assert report.built["clock_map"] == ["cfg/0"] and report.built["sim_poses"] == ["cfg/0"]
    entry = _manifest(campaign)["tables"]["clock_map"]["runs"]["cfg/0"]
    assert list(entry["sources"]) == ["_jobs/job-0/logs/rosout_bag"]
    rows = _table(campaign, "_recording").to_pylist()
    (clock,) = [r for r in rows if r["recording"] == ROQSIM_BAG and r["topic"] == "clock"]
    assert clock["table"] is None and "logs/rosout_bag" in clock["reason"]
    (run_clock,) = _table(campaign, "run_clock").to_pylist()
    assert run_clock["clock_map_source"] == "ros_clock_bag"


def test_every_table_of_the_recording_is_listed_without_building(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped", runs=(("cfg", 0), ("cfg", 1)))
    tables = available_tables(str(campaign))
    for table in TABLES:
        assert tables[table] == {"runs": 2, "built": 0, "failed": {}}, table
    assert "run_clock" in tables and "poses" not in tables


# -- cut off ---------------------------------------------------------------------------------

def _cut_summary(path) -> None:
    """Drop everything from the data-end record on: the chunks stay, the footer goes."""
    data = path.read_bytes()
    pos = len(MAGIC)
    while pos + 9 <= len(data):
        op = data[pos]
        (n,) = struct.unpack_from("<Q", data, pos + 1)
        if op == OP_DATA_END:
            path.write_bytes(data[:pos])
            return
        pos += 9 + n
    raise AssertionError("no data-end record")


def test_a_recording_without_its_footer_is_open_and_still_decodes(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped")
    whole = build(str(campaign), tables=["sim_poses", "sim_entities"])
    assert whole.built["sim_poses"] == ["cfg/0"]
    expected = _table(campaign, "sim_poses")
    shutil.rmtree(campaign / ".cache")

    bag = campaign / "cfg" / "0" / "roqsim_bag"
    _cut_summary(bag / "roqsim.mcap")
    assert not has_footer(bag / "roqsim.mcap")
    assert not recording_closed(ROQSIM_BAG, str(bag))
    build(str(campaign), tables=["sim_poses", "sim_entities"])
    assert _without_context(_table(campaign, "sim_poses")) == _without_context(expected)
    assert _table(campaign, "sim_entities").num_rows == 3, "metadata before the cut is read"
    entry = _manifest(campaign)["tables"]["sim_poses"]["runs"]["cfg/0"]
    assert entry["complete"] is False, "test.xml is there, but the recorder never closed"


def test_a_killed_writer_leaves_its_closed_chunks(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "stepped", finish=False)
    assert not has_footer(campaign / "cfg" / "0" / "roqsim_bag" / "roqsim.mcap")
    build(str(campaign), tables=["sim_poses"])
    rows = _table(campaign, "sim_poses").num_rows
    assert 0 < rows < ROQSIM_SAMPLES * len(ROQSIM_BODIES), "up to the last closed chunk"


# -- live ------------------------------------------------------------------------------------

def test_a_session_streams_the_poses_as_the_recording_grows(tmp_path):
    reference = make_roqsim_campaign(tmp_path / "reference")
    build(str(reference), tables=["sim_poses", "clock_map", "sim_recording"])
    expected = _table(reference, "sim_poses")

    campaign = make_roqsim_campaign(tmp_path / "live", verdict=False)
    bag = campaign / "cfg" / "0" / "roqsim_bag"
    whole = (bag / "roqsim.mcap").read_bytes()
    cuts = [len(whole) * i // 8 for i in range(1, 9)]
    # The first chunk is there when the session is planned: as with a ROS bag, a table a
    # channel gives is unknown until the channel's record has been written.
    (bag / "roqsim.mcap").write_bytes(whole[:cuts[1]])
    (run,) = find_runs(str(campaign))
    session = Session(str(campaign), run, str(bag), TABLES)
    assert session.role == ROQSIM_BAG and session.tables == TABLES and not session.unknown
    got, arrivals = [], 0
    for i, cut in enumerate(cuts[1:], 2):
        with open(bag / "roqsim.mcap", "ab") as fh:
            fh.write(whole[cuts[i - 2] if i > 2 else cuts[1]:cut])
        batches = session.advance()
        assert not session.closed or i == 8
        assert not any(b.table in ("sim_recording", "sim_entities") for b in batches), (
            "a table from the last metadata record is known only at the end")
        for batch in batches:
            if batch.table == "sim_poses":
                got.append(batch.rows)
                arrivals += 1
        assert session.advance() == []
    assert arrivals >= 3, "the poses came in batches"
    assert session.closed
    final = session.finish()
    assert {b.table for b in final} >= {"sim_recording", "sim_entities"}
    got += [b.rows for b in final if b.table == "sim_poses"]
    union = pa.concat_tables(got, promote_options="permissive")
    assert union.num_rows == expected.num_rows
    assert _without_context(union) == _without_context(expected)
    assert session.sources() == {"cfg/0/roqsim_bag": len(whole)}


def test_a_session_refuses_to_finish_a_recording_without_its_footer(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "live", verdict=False, finish=False)
    (run,) = find_runs(str(campaign))
    session = Session(str(campaign), run, str(campaign / "cfg" / "0" / "roqsim_bag"), TABLES)
    with pytest.raises(RuntimeError, match="no footer"):
        session.finish()


def test_the_watcher_follows_a_stepped_run_and_finalises_it(tmp_path):
    campaign = make_roqsim_campaign(tmp_path / "live", verdict=False)
    bag = campaign / "cfg" / "0" / "roqsim_bag" / "roqsim.mcap"
    whole = bag.read_bytes()
    bag.write_bytes(whole[:len(whole) // 3])
    watcher = Watcher(str(campaign), {}, part_s=0.0)
    seen = []
    watcher.subscribe("cfg/0", ["sim_poses", "clock_map"], seen.append)
    assert watcher.following("cfg/0") == {"sim_poses", "clock_map"}
    assert seen and seen[0].table in ("sim_poses", "clock_map")
    bag.write_bytes(whole)
    (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
    watcher.changed([str(bag), str(campaign / "cfg" / "0" / "test.xml")])
    assert watcher.following("cfg/0") == set()
    entry = _manifest(campaign)["tables"]["sim_poses"]["runs"]["cfg/0"]
    assert entry["complete"] is True and entry["rows"] == ROQSIM_SAMPLES * len(ROQSIM_BODIES)
    assert sum(b.rows.num_rows for b in seen if b.table == "sim_poses") == entry["rows"]


# -- the handlers ---------------------------------------------------------------------------

def test_the_metadata_hook_is_a_no_op_unless_a_handler_wants_it():
    class Nothing(Handler):
        def topics(self):
            return []

        def tables(self):
            return []
    Nothing().metadata("roqsim.recording", {"json": "{}"})


def test_a_metadata_table_is_flushed_at_the_end_and_the_last_record_wins():
    handler = SimEntities()
    handler.metadata("roqsim.entities", {"json": json.dumps({"entities": [
        {"name": "a", "kind": "prop", "body": "a", "present": True}]})})
    assert handler.flush() == {}
    handler.metadata("roqsim.entities", {"json": json.dumps({"entities": [
        {"name": "a", "kind": "prop", "body": "a", "present": False},
        {"name": "b", "kind": "prop", "body": "b", "present": True}]})})
    handler.end({})
    rows = handler.flush()["sim_entities"].to_pylist()
    assert [(r["name"], r["present"]) for r in rows] == [("a", 0), ("b", 1)]


def test_a_metadata_record_without_its_document_fails_loudly():
    with pytest.raises(HandlerError, match="no 'json' key"):
        SimRecording().metadata("roqsim.recording", {"yaml": "x: 1"})


def test_a_body_vector_of_the_wrong_length_fails_the_table():
    handler = SimPoses()
    with pytest.raises(HandlerError, match="12 values"):
        handler.message("poses", {"t": 0.0, "w": 1.0, "bodies": {"robot": [0.0] * 12}},
                        "roqsim.poses", 0)


def test_a_channel_without_a_table_is_reported_not_dropped(tmp_path):
    plan = plan_for(ROQSIM_BAG, {"poses": "roqsim.poses", "camera": "roqsim.camera"})
    assert "no table is defined" in plan.untabulated["camera"]
    path = write_roqsim_mcap(tmp_path / "roqsim_bag" / "roqsim.mcap", samples=3)
    report = decode_bag(str(path.parent), plan.handlers)
    assert report.topics["state"].messages == 3 and not report.failed
