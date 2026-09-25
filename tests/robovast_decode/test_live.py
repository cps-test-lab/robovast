# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A recording is decoded while it is written: batches as it grows, parts a query reads,
one file once the run is done."""

import io
import json
import os
import shutil
import struct
import threading
import time
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from mcap.writer import Writer

from robovast_decode.build import build, find_runs
from robovast_decode.framing import (MAGIC, OP_CHANNEL, OP_DATA_END, OP_SCHEMA, Channel, McapTail,
                                     Message, Schema, _parse)
from robovast_decode.handlers import Videos
from robovast_decode.live import Batch, LostOwnership, PartWriter, Session, Watcher
from robovast_decode.registry import SCENARIO_BAG, plan_for
from robovast_decode.tables import CONTEXT_COLUMNS

from .conftest import FIXTURE, NAV_CONFIG, make_campaign

SEGMENT = FIXTURE / "0" / "rosbag2" / "rosbag2_0.mcap"
TABLES = ["poses", "rosbag2_collision", "nav2_behavior_tree", "costmaps",
          "action_navigate_to_pose_feedback", "action_navigate_to_pose_status"]


def _manifest(campaign):
    return json.loads((campaign / ".cache" / "MANIFEST.json").read_text())


def _entry(campaign, table, key="cfg/0"):
    return _manifest(campaign)["tables"][table]["runs"][key]


def unchunked(segment=SEGMENT) -> bytes:
    """The fixture segment's records framed without chunks, so growth shows per record.

    The fixture is one chunk, which a reader can only open once it is whole; the same
    records written one by one make a file that is readable at every cut. A channel is
    registered with its first message, as rosbag2 writes it, so a topic appears when it
    starts publishing.
    """
    out = io.BytesIO()
    writer = Writer(out, use_chunking=False)
    writer.start(profile="ros2", library="test")
    schemas, channels, schema_ids, channel_ids = {}, {}, {}, {}
    for record in McapTail(str(segment)).read():
        if isinstance(record, Schema):
            schemas[record.id] = record
        elif isinstance(record, Channel):
            channels[record.id] = record
        elif isinstance(record, Message):
            if record.channel_id not in channel_ids:
                channel = channels[record.channel_id]
                if channel.schema_id not in schema_ids:
                    schema = schemas[channel.schema_id]
                    schema_ids[schema.id] = writer.register_schema(schema.name, schema.encoding,
                                                                   schema.data)
                channel_ids[channel.id] = writer.register_channel(
                    channel.topic, channel.message_encoding, schema_ids[channel.schema_id],
                    channel.metadata)
            writer.add_message(channel_ids[record.channel_id], record.log_time, record.data,
                               record.publish_time, record.sequence)
    writer.finish()
    return out.getvalue()


def top_level(data: bytes):
    """``(start offset, opcode, body)`` of every top-level record of an mcap file."""
    pos = len(MAGIC)
    while pos + 9 <= len(data):
        op = data[pos]
        (n,) = struct.unpack_from("<Q", data, pos + 1)
        yield pos, op, data[pos + 9:pos + 9 + n]
        pos += 9 + n


class GrowingBag:
    """A run's recording written in slices, closed with the fixture's ``metadata.yaml``."""

    def __init__(self, run_dir, data=None):
        self.bag_dir = run_dir / "rosbag2"
        self.bag_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURE / "0" / "rosbag2" / "message_definitions.json", self.bag_dir)
        self.data = data if data is not None else unchunked()
        self.segment = self.bag_dir / "rosbag2_0.mcap"
        self.written = 0

    def grow(self, upto: int) -> None:
        with open(self.segment, "ab") as fh:
            fh.write(self.data[self.written:upto])
        self.written = max(self.written, upto)

    def close(self) -> None:
        self.grow(len(self.data))
        shutil.copy(FIXTURE / "0" / "rosbag2" / "metadata.yaml", self.bag_dir)


def open_campaign(root):
    """A campaign whose one run has no verdict and an empty recording directory."""
    campaign = make_campaign(root, verdict=False)
    shutil.rmtree(campaign / "cfg" / "0" / "rosbag2")
    return campaign, GrowingBag(campaign / "cfg" / "0")


def reference(tmp_path, tables=tuple(TABLES), config=None):
    """What a whole build gives for the fixture, ``{table: arrow}``."""
    campaign = make_campaign(tmp_path / "reference")
    build(str(campaign), tables=list(tables), config=NAV_CONFIG if config is None else config)
    root = campaign / ".cache" / "tables"
    return {t: pq.read_table(root / t / "cfg" / "0.parquet") for t in tables}


def cuts(data: bytes, n: int):
    return [len(data) * i // n for i in range(1, n)]


def _sorted_rows(table: pa.Table):
    """The rows without the context columns, comparable across campaigns."""
    table = table.drop_columns([c for c in CONTEXT_COLUMNS if c in table.column_names])
    return sorted(json.dumps(r, sort_keys=True, default=str) for r in table.to_pylist())


# -- the session --------------------------------------------------------------------------

def test_batches_arrive_as_the_file_grows_and_add_up_to_a_whole_build(tmp_path):
    campaign, bag = open_campaign(tmp_path / "c")
    (run,) = find_runs(str(campaign))
    bag.grow(cuts(bag.data, 8)[0])
    session = Session(str(campaign), run, str(bag.bag_dir), TABLES, NAV_CONFIG)
    assert session.tables == TABLES and not session.unknown
    got = {t: [] for t in TABLES}
    arrivals = []
    for cut in cuts(bag.data, 8)[1:]:
        bag.grow(cut)
        batches = session.advance()
        arrivals.append({b.table: b.rows.num_rows for b in batches})
        for b in batches:
            got[b.table].append(b.rows)
        assert session.advance() == [], "nothing new: nothing repeated"
    assert not session.closed
    bag.close()
    assert session.closed
    for b in session.finish():
        got[b.table].append(b.rows)
    assert session.finished
    assert sum(1 for a in arrivals if a.get("poses")) >= 3, "poses came in several batches"

    expected = reference(tmp_path)
    for table in TABLES:
        union = pa.concat_tables(got[table], promote_options="permissive")
        assert union.num_rows == expected[table].num_rows, table
    # The transform buffer persists across flushes: the same map-relative poses, no fewer.
    assert _sorted_rows(pa.concat_tables(got["poses"], promote_options="permissive")) == _sorted_rows(
        expected["poses"])
    assert session.sources() == {"cfg/0/rosbag2": os.path.getsize(bag.segment)}


def test_the_session_knows_what_it_read_and_reads_only_what_is_new(tmp_path):
    campaign, bag = open_campaign(tmp_path / "c")
    (run,) = find_runs(str(campaign))
    session = Session(str(campaign), run, str(bag.bag_dir), ["rosbag2_collision"], NAV_CONFIG)
    assert session.advance() == [] and session.bytes_read == {}
    bag.grow(len(bag.data) // 2)
    first = session.advance()
    read = session.bytes_read[str(bag.segment)]
    assert 0 < read <= len(bag.data) // 2
    assert session.sources() == {"cfg/0/rosbag2": read}
    bag.close()
    second = session.finish()
    assert session.bytes_read[str(bag.segment)] > read and session.segments_closed == 1
    rows = sum(b.rows.num_rows for b in first + second if b.table == "rosbag2_collision")
    assert rows == 40
    with pytest.raises(RuntimeError, match="finished"):
        session.advance()


def test_a_session_refuses_to_finish_an_open_bag(tmp_path):
    campaign, bag = open_campaign(tmp_path / "c")
    (run,) = find_runs(str(campaign))
    session = Session(str(campaign), run, str(bag.bag_dir), ["rosbag2_collision"], NAV_CONFIG)
    bag.grow(len(bag.data))
    with pytest.raises(RuntimeError, match="metadata.yaml"):
        session.finish()


def test_a_table_the_topics_cannot_give_is_unknown_not_silent(tmp_path):
    campaign, bag = open_campaign(tmp_path / "c")
    (run,) = find_runs(str(campaign))
    session = Session(str(campaign), run, str(bag.bag_dir), ["rosbag2_nothing"], NAV_CONFIG)
    assert session.unknown == ["rosbag2_nothing"] and session.tables == []


def test_a_failed_handler_is_named_per_table_and_the_rest_carry_on(tmp_path):
    campaign, bag = open_campaign(tmp_path / "c")
    (run,) = find_runs(str(campaign))
    config = {"groups": [{"bag_dir": "rosbag2", "plugins": [
        {"type": "tf_to_csv", "frames": "all", "require": ["nowhere"]},
        {"type": "to_csv", "topics": ["/collision"]}]}]}
    session = Session(str(campaign), run, str(bag.bag_dir), ["poses", "rosbag2_collision"],
                      config)
    bag.close()
    final = session.finish()
    assert "nowhere" in session.failed["poses"]
    assert {b.table for b in final} == {"rosbag2_collision"}


# -- parts ----------------------------------------------------------------------------------

def test_parts_are_named_in_the_manifest_and_merged_into_one_file_at_the_end(tmp_path):
    campaign, bag = open_campaign(tmp_path / "c")
    (run,) = find_runs(str(campaign))
    session = Session(str(campaign), run, str(bag.bag_dir), ["poses"], NAV_CONFIG)
    writer = PartWriter(str(campaign), run)
    bag.grow(len(bag.data) // 3)
    for b in session.advance():
        writer.append(b)
    first = writer.write(session.sources())
    bag.grow(2 * len(bag.data) // 3)
    for b in session.advance():
        writer.append(b)
    second = writer.write(session.sources())
    assert first == ["tables/poses/cfg/0/part-0000.parquet"]
    assert second == ["tables/poses/cfg/0/part-0001.parquet"]
    entry = _entry(campaign, "poses")
    assert entry["files"] == first + second and entry["complete"] is False
    assert time.time() - entry["live"] < 5
    assert entry["sources"] == session.sources()
    assert entry["rows"] == sum(pq.read_table(campaign / ".cache" / f).num_rows
                                for f in entry["files"])
    stamp = entry["live"]
    time.sleep(0.01)
    assert writer.write(session.sources()) == [], "nothing accumulated: no part"
    assert _entry(campaign, "poses")["live"] > stamp, "but the entry is stamped again"

    bag.close()
    for b in session.finish():
        writer.append(b)
    files = writer.finalise(session.sources())
    assert files == ["tables/poses/cfg/0.parquet"]
    entry = _entry(campaign, "poses")
    assert entry["files"] == files and entry["complete"] is True and "live" not in entry
    assert not (campaign / ".cache" / "tables" / "poses" / "cfg" / "0").exists()
    merged = pq.read_table(campaign / ".cache" / "tables" / "poses" / "cfg" / "0.parquet")
    assert merged.num_rows == entry["rows"] == reference(tmp_path, ["poses"])["poses"].num_rows
    assert "orientation.yaw" in merged.column_names
    # A whole build now finds the entry current: same bytes, this decoder.
    assert build(str(campaign), tables=["poses"], config=NAV_CONFIG).skipped["poses"] == ["cfg/0"]
    with pytest.raises(RuntimeError, match="finalised"):
        writer.write()


def test_a_table_that_came_out_empty_or_failed_is_recorded_at_the_end(tmp_path):
    campaign, _ = open_campaign(tmp_path / "c")
    (run,) = find_runs(str(campaign))
    writer = PartWriter(str(campaign), run)
    writer.append(Batch("empty", pa.table({"a": pa.array([], pa.int64())})))
    assert writer.write() == []
    writer.finalise({}, failed={"broken": "HandlerError: no frames"})
    assert _entry(campaign, "empty")["rows"] == 0 and _entry(campaign, "empty")["files"]
    broken = _entry(campaign, "broken")
    assert broken["files"] == [] and "no frames" in broken["reason"] and broken["known"]


def _abandon(campaign, table, key="cfg/0"):
    """Age the entry's stamp past staleness, as a session that died leaves it."""
    manifest = _manifest(campaign)
    manifest["tables"][table]["runs"][key]["live"] -= 1000
    (campaign / ".cache" / "MANIFEST.json").write_text(json.dumps(manifest))


def test_a_build_leaves_a_live_entry_alone_and_takes_a_stale_one_whole(tmp_path):
    campaign = make_campaign(tmp_path / "c", verdict=False)
    (run,) = find_runs(str(campaign))
    rows = pa.table({"campaign_id": ["c"], "config_name": ["cfg"], "run_id": [0], "x": [1]})
    writer = PartWriter(str(campaign), run)
    writer.append(Batch("rosbag2_collision", rows))
    part = writer.write({"cfg/0/rosbag2": 1})
    report = build(str(campaign), tables=["rosbag2_collision"])
    assert report.skipped["rosbag2_collision"] == ["cfg/0"]
    assert "rosbag2_collision" not in report.built
    assert _entry(campaign, "rosbag2_collision")["files"] == part

    _abandon(campaign, "rosbag2_collision")
    build(str(campaign), tables=["rosbag2_collision"])
    entry = _entry(campaign, "rosbag2_collision")
    assert entry["files"] == ["tables/rosbag2_collision/cfg/0.parquet"] and "live" not in entry
    assert entry["rows"] == 40
    assert not (campaign / ".cache" / part[0]).exists(), "the parts a whole build supersedes go"
    writer.append(Batch("rosbag2_collision", rows))
    with pytest.raises(LostOwnership):
        writer.write({"cfg/0/rosbag2": 2})


# -- the watcher --------------------------------------------------------------------------

def test_the_watcher_follows_a_run_pushes_batches_and_finalises_it(tmp_path):
    campaign, bag = open_campaign(tmp_path / "c")
    watcher = Watcher(str(campaign), NAV_CONFIG, part_s=0.0)
    seen = []
    unsubscribe = watcher.subscribe("cfg/0", ["poses", "rosbag2_collision"], seen.append)
    assert watcher.following("cfg/0") == {"poses", "rosbag2_collision"}
    for cut in cuts(bag.data, 6):
        bag.grow(cut)
        watcher.changed([str(bag.segment)])
    assert {b.table for b in seen} == {"poses", "rosbag2_collision"}
    assert all(b.rows.num_rows for b in seen), "a subscriber gets rows, never an empty batch"
    entry = _entry(campaign, "poses")
    assert len(entry["files"]) >= 2 and entry["complete"] is False and "live" in entry

    bag.close()
    watcher.changed([str(bag.bag_dir / "metadata.yaml")])
    assert watcher.following("cfg/0") == {"poses", "rosbag2_collision"}, "no verdict yet"
    (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
    watcher.changed([str(campaign / "cfg" / "0" / "test.xml")])
    assert watcher.following("cfg/0") == set()
    for table in ("poses", "rosbag2_collision"):
        entry = _entry(campaign, table)
        assert entry["complete"] is True and entry["files"] == [f"tables/{table}/cfg/0.parquet"]
    expected = reference(tmp_path, ["poses", "rosbag2_collision"])
    for table, rows in expected.items():
        pushed = pa.concat_tables([b.rows for b in seen if b.table == table],
                                  promote_options="permissive")
        assert pushed.num_rows == rows.num_rows == _entry(campaign, table)["rows"]
    unsubscribe()


def test_a_run_demanded_before_it_exists_is_followed_once_it_appears(tmp_path):
    campaign, _ = open_campaign(tmp_path / "c")
    watcher = Watcher(str(campaign), NAV_CONFIG, part_s=0.0)
    watcher.demand("cfg/1", ["rosbag2_collision"])
    assert watcher.following("cfg/1") == set()
    bag = GrowingBag(campaign / "cfg" / "1")
    bag.close()
    watcher.changed([str(bag.segment)])
    assert watcher.following("cfg/1") == {"rosbag2_collision"}
    (campaign / "cfg" / "1" / "test.xml").write_text("<testsuite/>")
    watcher.changed([str(campaign / "cfg" / "1" / "test.xml")])
    assert _entry(campaign, "rosbag2_collision", "cfg/1")["rows"] == 40


def test_a_table_demanded_later_is_decoded_from_the_beginning(tmp_path):
    campaign, bag = open_campaign(tmp_path / "c")
    watcher = Watcher(str(campaign), NAV_CONFIG, part_s=0.0)
    bag.grow(len(bag.data) // 2)
    watcher.demand("cfg/0", ["rosbag2_collision"])
    watcher.changed([str(bag.segment)])
    assert watcher.following("cfg/0") == {"rosbag2_collision"}
    late = []
    watcher.subscribe("cfg/0", ["poses"], late.append)
    assert watcher.following("cfg/0") == {"rosbag2_collision", "poses"}
    assert late and late[0].table == "poses", "started from offset 0: rows already there"
    bag.close()
    (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
    watcher.changed([str(bag.segment)])
    assert _entry(campaign, "poses")["rows"] == reference(tmp_path, ["poses"])["poses"].num_rows
    assert sum(b.rows.num_rows for b in late) == _entry(campaign, "poses")["rows"]


def test_a_table_a_later_topic_gives_starts_when_the_topic_appears(tmp_path):
    """On the default plan a topic is a table once it is recorded, so a table demanded
    before its topic's first message waits, then is decoded from the recording's start."""
    campaign, bag = open_campaign(tmp_path / "c")
    watcher = Watcher(str(campaign), {}, part_s=0.0)
    # The last topic to start whose channel record adds tables to the default plan.
    recorded, schemas, cut, late = {}, {}, None, []
    for pos, op, body in top_level(bag.data):
        if op == OP_DATA_END:
            break                                   # the summary repeats every channel
        if op == OP_SCHEMA:
            schema = _parse(op, body)
            schemas[schema.id] = schema.name
        if op != OP_CHANNEL:
            continue
        channel = _parse(op, body)
        before = set(plan_for(SCENARIO_BAG, recorded).tables)
        recorded[channel.topic] = schemas[channel.schema_id]
        added = set(plan_for(SCENARIO_BAG, recorded).tables) - before
        if added:
            cut, late = pos, sorted(added)
    assert late and "poses" not in late
    bag.grow(cut)
    watcher.demand("cfg/0", ["poses", *late])
    watcher.changed([str(bag.segment)])
    assert watcher.following("cfg/0") == {"poses"}
    bag.close()
    watcher.changed([str(bag.segment)])
    assert watcher.following("cfg/0") == {"poses", *late}
    (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
    watcher.changed([str(campaign / "cfg" / "0" / "test.xml")])
    assert watcher.following("cfg/0") == set()
    expected = reference(tmp_path, ["poses", *late], config={})
    for table, rows in expected.items():
        assert _entry(campaign, table)["complete"] is True
        assert _entry(campaign, table)["rows"] == rows.num_rows


def test_the_watcher_starts_over_when_a_build_took_its_table(tmp_path, caplog):
    campaign, bag = open_campaign(tmp_path / "c")
    watcher = Watcher(str(campaign), NAV_CONFIG, part_s=0.0)
    watcher.demand("cfg/0", ["rosbag2_collision"])
    bag.grow(len(bag.data) // 2)
    watcher.changed([str(bag.segment)])
    assert len(_entry(campaign, "rosbag2_collision")["files"]) == 1
    _abandon(campaign, "rosbag2_collision")
    build(str(campaign), tables=["rosbag2_collision"])
    assert _entry(campaign, "rosbag2_collision")["files"] == [
        "tables/rosbag2_collision/cfg/0.parquet"]
    bag.close()
    with caplog.at_level("WARNING", logger="robovast_decode.live"):
        watcher.changed([str(bag.segment)])
    assert "again from the start" in caplog.text
    assert watcher.following("cfg/0") == {"rosbag2_collision"}
    entry = _entry(campaign, "rosbag2_collision")
    assert entry["files"] == ["tables/rosbag2_collision/cfg/0/part-0000.parquet"]
    assert entry["rows"] == 40, "decoded again from the recording's start"
    (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
    watcher.changed([str(campaign / "cfg" / "0" / "test.xml")])
    assert _entry(campaign, "rosbag2_collision")["complete"] is True


def test_run_forever_is_driven_by_an_inotify_watch(tmp_path):
    file_agent = pytest.importorskip("robovast.execution.data.file_agent")
    campaign, bag = open_campaign(tmp_path / "c")
    watcher = Watcher(str(campaign), NAV_CONFIG, part_s=0.1)
    seen = []
    watcher.subscribe("cfg/0", ["rosbag2_collision"], seen.append)
    with file_agent.Inotify() as inotify:
        thread = threading.Thread(target=watcher.run_forever, args=(inotify,), daemon=True)
        thread.start()
        try:
            for cut in cuts(bag.data, 4):
                bag.grow(cut)
                time.sleep(0.3)
            bag.close()
            (campaign / "cfg" / "0" / "test.xml").write_text("<testsuite/>")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and watcher.following("cfg/0"):
                time.sleep(0.1)
        finally:
            watcher.stop()
            thread.join(5)
    assert not thread.is_alive()
    assert _entry(campaign, "rosbag2_collision")["complete"] is True
    assert sum(b.rows.num_rows for b in seen) == 40


# -- videos ---------------------------------------------------------------------------------

TOPIC = "/camera/image_raw/compressed"


def _jpeg(shade: int) -> bytes:
    image = pytest.importorskip("PIL.Image")
    out = io.BytesIO()
    image.new("RGB", (32, 24), (shade, shade, shade)).save(out, format="JPEG")
    return out.getvalue()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_videos_flush_nothing_and_encode_at_finish(tmp_path):
    handler = Videos([(TOPIC, 30.0)])
    handler.output_dir, handler.bag_name = str(tmp_path), "rosbag2"
    for i, shade in enumerate((0, 80, 160, 240)):
        handler.message(TOPIC, SimpleNamespace(data=_jpeg(shade)),
                        "sensor_msgs/msg/CompressedImage", 1_000_000_000 * (10 + i))
        assert handler.flush() == {}, "a video is an end-only table"
    handler.end({TOPIC: "sensor_msgs/msg/CompressedImage"})
    (row,) = handler.flush()["videos"].to_pylist()
    assert row["frames"] == 4 and (tmp_path / row["file"]).stat().st_size > 0
    assert handler.flush()["videos"].num_rows == 0, "flushed once"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_a_session_gives_the_video_table_at_finish_only(tmp_path):
    import numpy as np
    from rosbags.typesys import Stores, get_typestore

    store = get_typestore(Stores.ROS2_JAZZY)
    typename = "sensor_msgs/msg/CompressedImage"
    image, header, stamp = (store.types[n] for n in (
        typename, "std_msgs/msg/Header", "builtin_interfaces/msg/Time"))
    out = io.BytesIO()
    writer = Writer(out, use_chunking=False)
    writer.start(profile="ros2", library="test")
    sid = writer.register_schema(typename, "ros2msg", b"")
    cid = writer.register_channel(TOPIC, "cdr", sid)
    for i, shade in enumerate((0, 120, 240)):
        msg = image(header=header(stamp=stamp(sec=10 + i, nanosec=0), frame_id="cam"),
                    format="jpeg", data=np.frombuffer(_jpeg(shade), dtype=np.uint8))
        writer.add_message(cid, 1_000_000_000 * (10 + i), store.serialize_cdr(msg, typename),
                           1_000_000_000 * (10 + i), i)
    writer.finish()
    campaign = make_campaign(tmp_path / "c", verdict=False)
    shutil.rmtree(campaign / "cfg" / "0" / "rosbag2")
    bag = GrowingBag(campaign / "cfg" / "0", data=out.getvalue())
    (run,) = find_runs(str(campaign))
    config = {"groups": [{"bag_dir": "rosbag2", "plugins": [{"type": "to_webm", "topic": TOPIC}]}]}
    bag.grow(len(bag.data) // 2)
    session = Session(str(campaign), run, str(bag.bag_dir), ["videos"], config)
    assert session.advance() == []
    bag.grow(len(bag.data))
    assert session.advance() == []
    (bag.bag_dir / "metadata.yaml").write_text("rosbag2_bagfile_information: {}\n")
    (batch,) = session.finish()
    assert batch.table == "videos" and batch.rows.num_rows == 1
    assert (campaign / "cfg" / "0" / batch.rows.to_pylist()[0]["file"]).exists()
