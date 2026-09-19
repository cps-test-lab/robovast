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


def test_the_stream_reader_hands_pushed_chunks_to_a_blocking_reader():
    reader = tar_io.StreamReader(max_chunks=4)
    reader.push(b"abc")
    reader.push(b"def")
    reader.finish()
    assert reader.read(4) == b"abcd"
    assert reader.read() == b"ef"
    assert reader.read(10) == b""
