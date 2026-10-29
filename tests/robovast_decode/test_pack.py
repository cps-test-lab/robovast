# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""A finished run's recordings are packed in place: chunked, zstd-compressed and indexed, with
nothing recorded changed, and every reader that held the old file finds out.

The bags here are the fixture's records written through, record by record, as the run's
recorder writes them -- in two segments, with a metadata record and an attachment -- and the
simulator's recording in its own shape, chunked zstd, which is packed already.
"""

import os
import shutil
import time

import pytest
from mcap.reader import make_reader
from mcap.writer import CompressionType, IndexType, Writer

from robovast_decode import framing, pack
from robovast_decode.build import build, find_runs, settled, RECORDING_TABLE
from robovast_decode.bulk import iter_messages, nearest_message
from robovast_decode.frames import FrameIndex
from robovast_decode.framing import (Channel, IndexedFile, McapFormatError, McapTail, Message,
                                     Schema, read_summary)
from robovast_decode.tables import FileCatalog, read_manifest

from .conftest import FIXTURE, NAV_CONFIG, make_campaign, write_roqsim_mcap
from .test_frames import COMPRESSED, TOPIC, compressed, fixture_span, image_bag, jpeg

SEGMENT = FIXTURE / "0" / "rosbag2" / "rosbag2_0.mcap"


def _fixture_records():
    """``(schemas, channels, messages)`` of the fixture recording, in file order."""
    tail = McapTail(str(SEGMENT))
    messages = [r for r in tail.read() if isinstance(r, Message)]
    return tail.schemas, tail.channels, messages


def write_through(path, messages, schemas, channels, metadata=(), attachments=()):
    """*messages* as a write-through recorder leaves a closed segment: no chunks, every
    schema and channel just before its first message, a summary without a chunk index."""
    with open(path, "wb") as fh:
        writer = Writer(fh, use_chunking=False)
        writer.start(profile="ros2", library="libmcap test")
        schema_ids, channel_ids = {0: 0}, {}
        for message in messages:
            channel = channels[message.channel_id]
            if channel.id not in channel_ids:
                if channel.schema_id not in schema_ids:
                    schema = schemas[channel.schema_id]
                    schema_ids[schema.id] = writer.register_schema(schema.name, schema.encoding,
                                                                   schema.data)
                channel_ids[channel.id] = writer.register_channel(
                    channel.topic, channel.message_encoding, schema_ids[channel.schema_id],
                    channel.metadata)
            writer.add_message(channel_ids[channel.id], message.log_time, message.data,
                               message.publish_time, message.sequence)
        for name, values in metadata:
            writer.add_metadata(name, values)
        for name, data in attachments:
            writer.add_attachment(7, 8, name, "application/octet-stream", data)
        writer.finish()
    return path


def written_through_bag(bag_dir):
    """The fixture recording as two closed write-through segments, the second carrying a
    metadata record and an attachment, with the fixture's sidecar and ``metadata.yaml``."""
    schemas, channels, messages = _fixture_records()
    half = len(messages) // 2
    bag_dir.mkdir(parents=True, exist_ok=True)
    write_through(bag_dir / "rosbag2_0.mcap", messages[:half], schemas, channels)
    write_through(bag_dir / "rosbag2_1.mcap", messages[half:], schemas, channels,
                  metadata=[("rosbag2", {"serialized_metadata": "x" * 300})],
                  attachments=[("note.bin", b"\x00\x01" * 500)])
    for name in ("message_definitions.json", "metadata.yaml"):
        shutil.copy(FIXTURE / "0" / "rosbag2" / name, bag_dir / name)
    return bag_dir


def stream(path):
    """Everything a reader can tell of a file's content, ids aside: its header, schemas and
    channels by id, every message in order, metadata and attachments in order."""
    with open(path, "rb") as fh:
        reader = make_reader(fh)
        header = reader.get_header()
        messages = [(channel.topic, schema.name if schema else None, message.sequence,
                     message.log_time, message.publish_time, bytes(message.data))
                    for schema, channel, message in reader.iter_messages(log_time_order=False)]
        metadata = [(m.name, dict(m.metadata)) for m in reader.iter_metadata()]
        attachments = [(a.name, a.media_type, a.log_time, a.create_time, bytes(a.data))
                       for a in reader.iter_attachments()]
        summary = reader.get_summary()
        schemas = {s.id: (s.name, s.encoding, bytes(s.data)) for s in summary.schemas.values()}
        channels = {c.id: (c.topic, c.message_encoding, c.schema_id, dict(c.metadata))
                    for c in summary.channels.values()}
    return {"profile": header.profile, "library": header.library, "schemas": schemas,
            "channels": channels, "messages": messages, "metadata": metadata,
            "attachments": attachments}


def walked(path):
    """The messages of *path* as the decoder walks it, record by record."""
    return [(m.channel_id, m.sequence, m.log_time, m.publish_time, m.data)
            for m in McapTail(str(path)).read() if isinstance(m, Message)]


@pytest.fixture
def run_campaign(tmp_path):
    """A one-run campaign whose scenario recording is written through, in two segments."""
    root = make_campaign(tmp_path / "nav-2026-01-01-00000000")
    bag = root / "cfg" / "0" / "rosbag2"
    shutil.rmtree(bag)
    written_through_bag(bag)
    return root


# -- one file -----------------------------------------------------------------------------

def test_a_written_through_recording_is_packed_with_nothing_recorded_changed(run_campaign):
    bag = run_campaign / "cfg" / "0" / "rosbag2"
    files = [bag / "rosbag2_0.mcap", bag / "rosbag2_1.mcap"]
    before = {f: (stream(f), walked(f), f.stat().st_size) for f in files}
    assert all(read_summary(f).chunks == () for f in files), "written through: no chunk"

    run = find_runs(str(run_campaign))[0]
    report = pack.pack_run(str(run_campaign), run)

    assert not report.failed and not report.left
    assert {p.path for p in report.packed} >= {str(f) for f in files}
    for f in files:
        content, messages, size = before[f]
        summary = read_summary(f)
        assert summary.packed and summary.message_indexed
        assert all(c.compression == "zstd" for c in summary.chunks)
        assert summary.message_count == len(messages)
        assert f.stat().st_size < size, "smaller"
        assert stream(f) == content, "header, schemas, channels, messages, metadata, " \
            "attachments: the same"
        assert walked(f) == messages, "ids kept: the recorder numbered them from one"
        assert not os.path.exists(str(f) + pack.TMP_SUFFIX), "one copy"


def test_the_chunk_size_bounds_what_one_message_costs(tmp_path):
    """A recording of many frames packs into many chunks, each about the chunk size."""
    t0, _ = fixture_span()
    frames = [(t0 + k * 10**8, compressed(t0 + k * 10**8, os.urandom(4000)))
              for k in range(200)]
    bag = tmp_path / "rosbag2"
    bag.mkdir()
    path = bag / "rosbag2_0.mcap"
    path.write_bytes(image_bag({(TOPIC, COMPRESSED): frames}))
    pack.pack_file(str(path), chunk_size=64 * 1024)
    chunks = read_summary(path).chunks
    assert len(chunks) > 5
    assert all(c.uncompressed_size < 64 * 1024 + 8000 for c in chunks)


def test_a_file_packed_already_is_left_byte_for_byte(tmp_path):
    """The simulator's recording is chunked zstd with a chunk index and no message index;
    one with message indexes too is packed as well. Neither is touched."""
    roqsim = write_roqsim_mcap(tmp_path / "a" / "roqsim.mcap")
    plain = tmp_path / "b" / "roqsim.mcap"
    plain.parent.mkdir()
    with open(plain, "wb") as fh:
        writer = Writer(fh, compression=CompressionType.ZSTD, chunk_size=1024,
                        index_types=IndexType.CHUNK | IndexType.METADATA)
        writer.start(profile="roqsim", library="test")
        channel = writer.register_channel("state", "roqsim.state", 0)
        for i in range(200):
            writer.add_message(channel, i * 10**6, b"%08d" % i * 8, i * 10**6, i)
        writer.finish()
    assert not read_summary(plain).message_indexed
    for path in (roqsim, plain):
        data, mtime = path.read_bytes(), path.stat().st_mtime_ns
        assert pack.is_packed(str(path))
        assert pack.pack_file(str(path)) is None
        assert path.read_bytes() == data and path.stat().st_mtime_ns == mtime


def test_a_packed_file_is_packed_once(run_campaign):
    run = find_runs(str(run_campaign))[0]
    pack.pack_run(str(run_campaign), run)
    path = run_campaign / "cfg" / "0" / "rosbag2" / "rosbag2_0.mcap"
    data, mtime = path.read_bytes(), path.stat().st_mtime_ns
    again = pack.pack_run(str(run_campaign), run)
    assert again.packed == [] and again.already >= 2
    assert path.read_bytes() == data and path.stat().st_mtime_ns == mtime


# -- only what is closed ------------------------------------------------------------------

def test_a_run_without_its_verdict_is_left_as_recorded(run_campaign):
    (run_campaign / "cfg" / "0" / "test.xml").unlink()
    path = run_campaign / "cfg" / "0" / "rosbag2" / "rosbag2_0.mcap"
    data = path.read_bytes()
    report = pack.pack_run(str(run_campaign), find_runs(str(run_campaign))[0])
    assert report.packed == [] and "cfg/0" in report.left
    assert path.read_bytes() == data


def test_a_recording_still_open_leaves_the_run_as_recorded(run_campaign):
    """A rosbag2 recording is closed once its ``metadata.yaml`` is written."""
    (run_campaign / "cfg" / "0" / "rosbag2" / "metadata.yaml").unlink()
    path = run_campaign / "cfg" / "0" / "rosbag2" / "rosbag2_0.mcap"
    data = path.read_bytes()
    report = pack.pack_run(str(run_campaign), find_runs(str(run_campaign))[0])
    assert report.packed == [] and "still open" in report.left["cfg/0"]
    assert path.read_bytes() == data


def test_a_run_a_live_session_still_reads_is_left_for_the_next_pass(run_campaign):
    with FileCatalog(str(run_campaign)).update() as manifest:
        manifest["tables"]["poses"] = {"runs": {"cfg/0": {"live": time.time()}}}
    path = run_campaign / "cfg" / "0" / "rosbag2" / "rosbag2_0.mcap"
    data = path.read_bytes()
    report = pack.pack_run(str(run_campaign), find_runs(str(run_campaign))[0])
    assert report.packed == [] and "live session" in report.left["cfg/0"]
    assert path.read_bytes() == data


def test_a_file_without_its_footer_is_read_as_it_is(run_campaign):
    path = run_campaign / "cfg" / "0" / "rosbag2" / "rosbag2_1.mcap"
    cut = path.read_bytes()[:-200]
    path.write_bytes(cut)
    report = pack.pack_run(str(run_campaign), find_runs(str(run_campaign))[0])
    assert not report.failed
    assert path.read_bytes() == cut
    assert "no footer" in report.left[os.path.relpath(path, run_campaign)]
    assert read_summary(path.with_name("rosbag2_0.mcap")).packed, "its closed sibling is"
    with pytest.raises(pack.PackError, match="no footer"):
        pack.pack_file(str(path))


# -- a failure leaves the original --------------------------------------------------------

def test_a_rewrite_that_fails_leaves_the_original_and_says_why(run_campaign, monkeypatch):
    path = run_campaign / "cfg" / "0" / "rosbag2" / "rosbag2_0.mcap"
    data = path.read_bytes()

    def broken(src, dst, chunk_size):
        dst.write(b"\x89MCAP0\r\n half a file")
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(pack, "_rewrite", broken)
    report = pack.pack_run(str(run_campaign), find_runs(str(run_campaign))[0])

    rel = os.path.relpath(path, run_campaign)
    assert "No space left" in report.failed[rel]
    assert path.read_bytes() == data
    assert not os.path.exists(str(path) + pack.TMP_SUFFIX)


def test_a_rewrite_that_loses_a_message_is_refused(run_campaign, monkeypatch):
    path = run_campaign / "cfg" / "0" / "rosbag2" / "rosbag2_0.mcap"
    data = path.read_bytes()
    real = pack._rewrite
    monkeypatch.setattr(pack, "_rewrite", lambda src, dst, size: real(src, dst, size) + 1)
    with pytest.raises(pack.PackError, match="does not hold"):
        pack.pack_file(str(path))
    assert path.read_bytes() == data
    assert not os.path.exists(str(path) + pack.TMP_SUFFIX)


# -- reading through the index ------------------------------------------------------------

def _frames_campaign(root, count=40):
    """A campaign whose run recorded *count* camera frames, written through."""
    make_campaign(root)
    bag = root / "cfg" / "0" / "rosbag2"
    t0, _ = fixture_span()
    stamps = [t0 + k * 10**8 for k in range(count)]
    data = image_bag({(TOPIC, COMPRESSED): [
        (t, compressed(t, jpeg((5 * k, 5 * k, 5 * k)) + os.urandom(3000)))
        for k, t in enumerate(stamps)]})
    path = bag / "rosbag2_0.mcap"
    path.write_bytes(data)
    schemas, channels, messages = {}, {}, []
    for record in McapTail(str(path)).read():
        if isinstance(record, Schema):
            schemas[record.id] = record
        elif isinstance(record, Channel):
            channels[record.id] = record
        elif isinstance(record, Message):
            messages.append(record)
    write_through(path, messages, schemas, channels)
    return root, stamps


@pytest.fixture
def decompressions(monkeypatch):
    """How many chunks were decompressed since the fixture was taken."""
    seen = []
    real = framing._decompress

    def counting(compression, data, size):
        seen.append(size)
        return real(compression, data, size)
    monkeypatch.setattr(framing, "_decompress", counting)
    return seen


def test_one_message_by_time_decompresses_one_chunk(tmp_path, decompressions):
    root, stamps = _frames_campaign(tmp_path / "c")
    bag = root / "cfg" / "0" / "rosbag2"
    walked_answer = nearest_message(str(bag), TOPIC, stamps[25] / 1e9 + 0.01)
    pack.pack_file(str(bag / "rosbag2_0.mcap"), chunk_size=16 * 1024)
    chunks = read_summary(bag / "rosbag2_0.mcap").chunks
    assert len(chunks) > 4

    decompressions.clear()
    sample = nearest_message(str(bag), TOPIC, stamps[25] / 1e9 + 0.01)
    assert sample.t_ns == stamps[25] == walked_answer.t_ns
    assert bytes(sample.msg.data) == bytes(walked_answer.msg.data)
    assert len(decompressions) == 1, "the one chunk that holds it"

    decompressions.clear()
    index = FrameIndex(str(bag), TOPIC)
    assert index.times == [t / 1e9 for t in stamps]
    assert decompressions == [], "the index is read from the message indexes alone"
    index.read_frame(index.nearest(stamps[30] / 1e9))
    assert len(decompressions) == 1


def test_a_span_reads_only_the_chunks_it_overlaps(tmp_path, decompressions):
    root, stamps = _frames_campaign(tmp_path / "c")
    bag = root / "cfg" / "0" / "rosbag2"
    span = (stamps[10] / 1e9, stamps[14] / 1e9)
    walked_answer = [(s.t_ns, bytes(s.msg.data)) for s in iter_messages(str(bag), TOPIC, *span)]
    pack.pack_file(str(bag / "rosbag2_0.mcap"), chunk_size=16 * 1024)
    total = len(read_summary(bag / "rosbag2_0.mcap").chunks)

    decompressions.clear()
    got = [(s.t_ns, bytes(s.msg.data)) for s in iter_messages(str(bag), TOPIC, *span)]
    assert got == walked_answer and len(got) == 5
    assert 0 < len(decompressions) < total / 2


def test_a_corrupt_summary_is_an_error_not_a_walk(run_campaign):
    path = run_campaign / "cfg" / "0" / "rosbag2" / "rosbag2_0.mcap"
    pack.pack_file(str(path))
    data = bytearray(path.read_bytes())
    summary_start = int.from_bytes(data[-8 - 20:-8 - 12], "little")
    data[summary_start + 20] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(McapFormatError, match="checksum"):
        read_summary(path)
    with pytest.raises(McapFormatError):
        nearest_message(str(path.parent), "/scan")


# -- what held the old file ---------------------------------------------------------------

def test_a_frame_index_of_the_old_file_says_so_and_refuses_it(tmp_path):
    root, stamps = _frames_campaign(tmp_path / "c")
    bag = root / "cfg" / "0" / "rosbag2"
    index = FrameIndex(str(bag), TOPIC)
    ref = index.nearest(stamps[3] / 1e9)
    assert ref.inner is None, "walked: the file is written through"
    assert not index.replaced()

    pack.pack_run(str(root), find_runs(str(root))[0])

    assert index.replaced()
    with pytest.raises(McapFormatError, match="replaced"):
        index.read_frame(ref)


def test_a_tail_of_the_old_file_refuses_the_new_one(run_campaign):
    path = run_campaign / "cfg" / "0" / "rosbag2" / "rosbag2_0.mcap"
    tail = McapTail(str(path))
    list(tail.read())
    pack.pack_run(str(run_campaign), find_runs(str(run_campaign))[0])
    with pytest.raises(McapFormatError, match="replaced"):
        list(tail.read())


def test_an_indexed_read_refuses_a_file_replaced_after_its_index(run_campaign):
    path = run_campaign / "cfg" / "0" / "rosbag2" / "rosbag2_0.mcap"
    pack.pack_file(str(path))
    indexed = IndexedFile.open(str(path))
    entry = next(indexed.entries(indexed.channel("/scan").id))
    shutil.copy(path, str(path) + ".copy")
    os.replace(str(path) + ".copy", path)
    with pytest.raises(McapFormatError, match="replaced"):
        indexed.message(entry)


def test_tables_built_before_packing_stay_current(run_campaign):
    """Packing changes a recording's size and none of its messages: what was built from it
    is not built again."""
    first = build(str(run_campaign), config=NAV_CONFIG)
    assert first.built and not first.failed
    pack.pack_run(str(run_campaign), find_runs(str(run_campaign))[0])

    again = build(str(run_campaign), config=NAV_CONFIG)
    assert again.built == {}, "nothing built again"
    manifest = read_manifest(str(run_campaign))
    entry = manifest["tables"][RECORDING_TABLE]["runs"]["cfg/0"]
    assert settled(str(run_campaign), RECORDING_TABLE, entry)
