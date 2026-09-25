# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""An mcap file is read record by record, including while it is written and after a cut."""

import os

from mcap.writer import CompressionType, Writer

from robovast_decode.framing import McapTail, Message


def _writer(fh, chunked=False):
    w = Writer(fh, use_chunking=chunked, compression=CompressionType.ZSTD if chunked else
               CompressionType.NONE)
    w.start(profile="", library="test")
    sid = w.register_schema("test/msg/Raw", "", b"")
    cid = w.register_channel("/raw", "raw", sid)
    return w, cid


def _messages(tail):
    return [r for r in tail.read() if isinstance(r, Message)]


def test_a_growing_file_is_read_as_far_as_it_is_written(tmp_path):
    path = tmp_path / "grow.mcap"
    with open(path, "wb") as fh:
        w, cid = _writer(fh)
        tail = McapTail(path)
        seen = []
        for batch in range(3):
            for i in range(10):
                w.add_message(cid, log_time=batch * 10 + i, publish_time=0, data=b"x" * i,
                              sequence=0)
            fh.flush()
            seen.extend(m.log_time for m in _messages(tail))
        w.finish()
    assert seen == list(range(30))
    assert not tail.finished
    _messages(tail)
    assert tail.finished


def test_a_file_cut_mid_record_yields_everything_before_the_cut(tmp_path):
    path = tmp_path / "whole.mcap"
    with open(path, "wb") as fh:
        w, cid = _writer(fh)
        for i in range(100):
            w.add_message(cid, log_time=i, publish_time=0, data=b"payload", sequence=0)
        w.finish()
    data = path.read_bytes()
    cut = tmp_path / "cut.mcap"
    cut.write_bytes(data[: len(data) // 2 + 3])
    tail = McapTail(cut)
    got = [m.log_time for m in _messages(tail)]
    assert got == list(range(len(got))) and 0 < len(got) < 100
    assert tail.offset < os.path.getsize(cut)


def test_reading_resumes_at_the_recorded_offset(tmp_path):
    path = tmp_path / "resume.mcap"
    with open(path, "wb") as fh:
        w, cid = _writer(fh)
        for i in range(10):
            w.add_message(cid, log_time=i, publish_time=0, data=b"a", sequence=0)
        fh.flush()
        first = McapTail(path)
        assert len(_messages(first)) == 10
        for i in range(10, 15):
            w.add_message(cid, log_time=i, publish_time=0, data=b"b", sequence=0)
        w.finish()
    again = McapTail(path, offset=first.offset, schemas=first.schemas, channels=first.channels)
    assert [m.log_time for m in _messages(again)] == list(range(10, 15))


def test_compressed_chunks_are_opened(tmp_path):
    path = tmp_path / "chunked.mcap"
    with open(path, "wb") as fh:
        w, cid = _writer(fh, chunked=True)
        for i in range(500):
            w.add_message(cid, log_time=i, publish_time=0, data=b"z" * 100, sequence=0)
        w.finish()
    assert [m.log_time for m in _messages(McapTail(path))] == list(range(500))
