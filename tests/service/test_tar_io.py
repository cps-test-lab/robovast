# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``tar_io.extract_stream`` — what a pod's tar may and may not put on disk.

A tar is one caller-supplied path per member, so every escape the file routes refuse has
to be refused here too, per member, without losing the members around it. And a stream
that stops halfway must leave nothing that looks like a finished file: a run's evidence
is read by name, and a truncated ``test.xml`` under its real name is a wrong verdict.
"""

import io
import os
import tarfile

import pytest

from robovast.service import tar_io


def _tar(members):
    """A gzip tar of ``(name, payload | None, mode)`` -- ``None`` payload is a directory;
    a ``str`` payload starting with ``->`` is a symlink to what follows."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload, mode in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if payload is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif isinstance(payload, str) and payload.startswith("->"):
                info.type = tarfile.SYMTYPE
                info.linkname = payload[2:]
                tar.addfile(info)
            else:
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
    return io.BytesIO(buf.getvalue())


def test_members_land_with_their_mode_and_directories(tmp_path):
    out = tar_io.extract_stream(_tar([
        ("cfg", None, 0o755),
        ("cfg/run.sh", b"#!/bin/sh\n", 0o755),
        ("cfg/params.yaml", b"a: 1\n", 0o644),
    ]), tmp_path)
    assert out.files == 2 and out.refused == []
    assert (tmp_path / "cfg" / "params.yaml").read_bytes() == b"a: 1\n"
    # The executable bit is the whole reason a tar carries a mode.
    assert os.access(tmp_path / "cfg" / "run.sh", os.X_OK)


def test_a_member_leaving_the_tree_is_refused_and_the_rest_kept(tmp_path):
    out = tar_io.extract_stream(_tar([
        ("../escape.txt", b"x", 0o644),
        ("/abs.txt", b"x", 0o644),
        ("kept.txt", b"y", 0o644),
    ]), tmp_path)
    assert (tmp_path / "kept.txt").exists()
    assert not (tmp_path.parent / "escape.txt").exists()
    # An absolute name is taken relative to the tree, as tar itself does.
    assert (tmp_path / "abs.txt").exists()
    assert out.refused == ["../escape.txt"]


def test_a_symlink_inside_the_tree_is_kept_and_one_outside_refused(tmp_path):
    out = tar_io.extract_stream(_tar([
        ("_jobs", None, 0o755),
        ("_jobs/job-1", None, 0o755),
        ("run/job", "->../_jobs/job-1", 0o777),
        ("run/etc", "->/etc", 0o777),
    ]), tmp_path)
    assert os.readlink(tmp_path / "run" / "job") == "../_jobs/job-1"
    assert not (tmp_path / "run" / "etc").exists()
    assert out.refused == ["run/etc"]


def test_a_hard_link_is_refused(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("a.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
        link = tarfile.TarInfo("b.txt")
        link.type = tarfile.LNKTYPE
        link.linkname = "a.txt"
        tar.addfile(link)
    out = tar_io.extract_stream(io.BytesIO(buf.getvalue()), tmp_path)
    assert (tmp_path / "a.txt").exists() and not (tmp_path / "b.txt").exists()
    assert out.refused == ["b.txt"]


def test_the_campaign_store_is_never_written_and_a_deny_list_is_honoured(tmp_path):
    out = tar_io.extract_stream(_tar([
        ("campaign.db", b"sqlite", 0o644),
        ("sub/campaign.db-wal", b"sqlite", 0o644),
        ("_execution/controller.log", b"mine", 0o644),
        ("_execution/other.log", b"ok", 0o644),
    ]), tmp_path, deny=("_execution/controller.log",))
    assert sorted(out.refused) == ["_execution/controller.log", "campaign.db",
                                   "sub/campaign.db-wal"]
    assert not (tmp_path / "campaign.db").exists()
    assert (tmp_path / "_execution" / "other.log").exists()


def test_the_last_writer_wins(tmp_path):
    tar_io.extract_stream(_tar([("log.txt", b"first", 0o644),
                                ("log.txt", b"second", 0o644)]), tmp_path)
    assert (tmp_path / "log.txt").read_bytes() == b"second"


def test_a_stream_that_stops_mid_member_leaves_no_file_under_the_real_name(tmp_path):
    full = _tar([("a.txt", b"a" * 10000, 0o644), ("b.txt", b"b" * 10000, 0o644)]).getvalue()
    cut = io.BytesIO(full[: len(full) // 2])
    with pytest.raises(Exception):
        tar_io.extract_stream(cut, tmp_path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert "b.txt" not in names
    # Whatever the cut left is under the incoming suffix, and the sweep names it.
    assert all(n.endswith(tar_io.INCOMING_SUFFIX) or n == "a.txt" for n in names)
    assert tar_io.sweep_incoming(tmp_path) == len([n for n in names if n != "a.txt"])
    assert sorted(p.name for p in tmp_path.iterdir()) in (["a.txt"], [])


def _ranges(members):
    """A plain pax tar of ``(name, payload, offset | None)`` -- an offset is sent as the
    :data:`tar_io.OFFSET_HEADER` pax header, ``None`` sends the file whole."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, payload, offset in members:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            if offset is not None:
                info.pax_headers = {tar_io.OFFSET_HEADER: str(offset)}
            tar.addfile(info, io.BytesIO(payload))
    return io.BytesIO(buf.getvalue())


def test_a_range_at_the_files_end_is_appended(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "system.log").write_bytes(b"one\n")
    out = tar_io.extract_stream(_ranges([("logs/system.log", b"two\nthree\n", 4)]), tmp_path)
    assert (tmp_path / "logs" / "system.log").read_bytes() == b"one\ntwo\nthree\n"
    assert (out.files, out.bytes, out.refused, out.resync) == (1, 10, [], [])
    assert not list(tmp_path.rglob(f"*{tar_io.INCOMING_SUFFIX}"))


def test_offset_zero_creates_a_missing_file(tmp_path):
    out = tar_io.extract_stream(_ranges([("a/b/data.csv", b"x,y\n", 0)]), tmp_path)
    assert (tmp_path / "a" / "b" / "data.csv").read_bytes() == b"x,y\n"
    assert out.files == 1 and out.resync == []


def test_a_range_already_present_is_a_no_op(tmp_path):
    (tmp_path / "data.jsonl").write_bytes(b"{}\n{}\n{}\n")
    out = tar_io.extract_stream(_ranges([("data.jsonl", b"{}\n", 3),
                                         ("data.jsonl", b"{}\n{}\n", 3)]), tmp_path)
    assert (tmp_path / "data.jsonl").read_bytes() == b"{}\n{}\n{}\n"
    assert (out.files, out.bytes, out.refused, out.resync) == (0, 0, [], [])


@pytest.mark.parametrize("existing, offset", [
    (None, 5),            # a gap before a file that is not here
    (b"abc", 5),          # a gap after the file's end
    (b"abcdef", 4),       # the file is longer than the offset but short of the range's end
    (b"ab", 1),           # a range starting inside the file and running past it
], ids=["missing", "gap", "longer", "overlap"])
def test_a_range_that_does_not_continue_the_file_is_a_resync(tmp_path, existing, offset):
    if existing is not None:
        (tmp_path / "f.log").write_bytes(existing)
    out = tar_io.extract_stream(_ranges([("f.log", b"0123456789", offset)]), tmp_path)
    assert out.resync == ["f.log"]
    assert (out.files, out.bytes, out.refused) == (0, 0, [])
    if existing is None:
        assert not (tmp_path / "f.log").exists()
    else:
        assert (tmp_path / "f.log").read_bytes() == existing


def test_a_denied_or_escaping_range_is_refused(tmp_path):
    out = tar_io.extract_stream(_ranges([
        ("campaign.db", b"x", 0),
        ("sub/campaign.db-wal", b"x", 0),
        ("_execution/controller.log", b"x", 0),
        ("../outside.log", b"x", 0),
    ]), tmp_path, deny=("_execution/controller.log",))
    assert sorted(out.refused) == ["../outside.log", "_execution/controller.log",
                                   "campaign.db", "sub/campaign.db-wal"]
    assert out.files == 0 and out.resync == []
    assert not (tmp_path / "campaign.db").exists()
    assert not (tmp_path.parent / "outside.log").exists()


@pytest.mark.parametrize("value", ["-1", "abc", "", "1.5", " 3"])
def test_an_offset_that_is_not_a_non_negative_integer_is_refused(tmp_path, value):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("f.log")
        info.size = 1
        info.pax_headers = {tar_io.OFFSET_HEADER: value}
        tar.addfile(info, io.BytesIO(b"x"))
    out = tar_io.extract_stream(io.BytesIO(buf.getvalue()), tmp_path)
    assert out.refused == ["f.log"]
    assert not (tmp_path / "f.log").exists()


def test_a_directory_at_the_path_wins_over_a_range(tmp_path):
    (tmp_path / "f.log").mkdir()
    out = tar_io.extract_stream(_ranges([("f.log", b"x", 0)]), tmp_path)
    assert (tmp_path / "f.log").is_dir()
    assert (out.files, out.refused, out.resync) == (0, [], [])


def test_a_range_is_not_appended_through_a_symlink(tmp_path):
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"")
    tree = tmp_path / "tree"
    tree.mkdir()
    os.symlink(outside, tree / "f.log")
    out = tar_io.extract_stream(_ranges([("f.log", b"x", 0)]), tree)
    assert out.resync == ["f.log"]
    assert outside.read_bytes() == b""


def test_a_whole_file_after_ranges_replaces_the_file(tmp_path):
    out = tar_io.extract_stream(_ranges([("f.log", b"one\n", 0),
                                         ("f.log", b"two\n", 4),
                                         ("f.log", b"whole\n", None)]), tmp_path)
    assert (tmp_path / "f.log").read_bytes() == b"whole\n"
    assert out.files == 3 and out.resync == []


def test_the_stream_reader_hands_pushed_chunks_to_a_blocking_reader():
    reader = tar_io.StreamReader(max_chunks=4)
    reader.push(b"abc")
    reader.push(b"def")
    reader.finish()
    assert reader.read(4) == b"abcd"
    assert reader.read() == b"ef"
    assert reader.read(10) == b""
