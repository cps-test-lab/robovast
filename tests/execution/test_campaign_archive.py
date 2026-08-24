# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the campaign archive engine (file + streamed tar.gz).

Covers the symlink-preserving member walk, ``.cache`` exclusion, the local-file
tarball, the streamed generator/CM (``pigz`` pipe), object-store-style member
injection via a custom ``add_members``, and error propagation from the writer.
"""

import io
import os
import tarfile

import pytest

from robovast.execution import campaign_archive


def _make_campaign(root):
    """Build a small campaign tree with a job symlink and a .cache dir."""
    os.makedirs(os.path.join(root, "config1", "1"))
    os.makedirs(os.path.join(root, "_jobs", "batch-0", "job-0"))
    os.makedirs(os.path.join(root, ".cache"))
    with open(os.path.join(root, "config1", "1", "test.xml"), "w") as fh:
        fh.write("<ok/>")
    with open(os.path.join(root, "campaign.db"), "w") as fh:
        fh.write("db")
    with open(os.path.join(root, ".cache", "hash.json"), "w") as fh:
        fh.write("{}")
    # <config>/<run>/job -> ../../_jobs/batch-0/job-0 (a dir symlink)
    os.symlink("../../_jobs/batch-0/job-0", os.path.join(root, "config1", "1", "job"))


def _members(tar_bytes):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tf:
        return {m.name: m for m in tf.getmembers()}


def test_make_campaign_tarball_includes_files_excludes_cache_keeps_symlink(tmp_path):
    root = tmp_path / "camp-2026-01-01-000000"
    _make_campaign(str(root))
    archive_dir = tmp_path / "_archives"
    out = campaign_archive.make_campaign_tarball(str(root), str(archive_dir))
    assert out == str(archive_dir / "camp-2026-01-01-000000.tar.gz")

    members = _members(open(out, "rb").read())
    cid = "camp-2026-01-01-000000"
    assert f"{cid}/config1/1/test.xml" in members
    assert f"{cid}/campaign.db" in members
    # .cache is excluded entirely.
    assert not any("/.cache/" in n for n in members)
    # The job link is stored as a symlink, not recursed into (no _jobs duplicated under it).
    job = members[f"{cid}/config1/1/job"]
    assert job.issym()
    assert job.linkname == "../../_jobs/batch-0/job-0"
    assert not any(n.startswith(f"{cid}/config1/1/job/") for n in members)


def test_iter_campaign_tar_matches_the_file_tarball(tmp_path):
    root = tmp_path / "camp-2026-01-01-000000"
    _make_campaign(str(root))
    streamed = b"".join(campaign_archive.iter_campaign_tar(str(root)))
    members = _members(streamed)
    cid = "camp-2026-01-01-000000"
    assert f"{cid}/config1/1/test.xml" in members
    assert members[f"{cid}/config1/1/job"].issym()
    assert not any("/.cache/" in n for n in members)


def test_campaign_tar_stream_cm_yields_readable(tmp_path):
    root = tmp_path / "camp-2026-01-01-000000"
    _make_campaign(str(root))
    with campaign_archive.campaign_tar_stream(str(root)) as stream:
        data = stream.read()
    assert b"".join([data])  # non-empty
    assert f"camp-2026-01-01-000000/campaign.db" in _members(data)


def test_iter_tar_with_custom_add_members_injects_objects():
    """Object-store-style source: add_members writes members directly (no local dir)."""
    def add_members(tar):
        for name, payload in [("cid/a.txt", b"aaa"), ("cid/sub/b.txt", b"bbbb")]:
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

    data = b"".join(campaign_archive.iter_tar(add_members))
    members = _members(data)
    assert members["cid/a.txt"].size == 3
    assert members["cid/sub/b.txt"].size == 4


def test_share_refuses_a_campaign_no_import_could_take_back_in(tmp_path, monkeypatch):
    """An unimportable archive must not be written, let alone uploaded.

    A campaign that dies before its ``_config/`` is frozen still has an ``_execution/``
    full of logs, so it archives, uploads, lists and downloads exactly like a good one --
    and fails only at the far end, on somebody else's service, with an ingest refusal and
    no way to repair the source. That happened: a share ended up holding an archive whose
    every future import was a refusal, and the message the reader got named the ingest
    stages rather than the missing directory.

    Refusing here costs a directory listing. Refusing at the far end costs a transfer and
    tells the wrong person.
    """
    from robovast.common.errors import CampaignConfigError
    from robovast.execution.backends import DockerBackend

    root = tmp_path / "camp-2026-01-01-000000"
    _make_campaign(str(root))
    os.makedirs(os.path.join(root, "_execution"))
    archive_dir = tmp_path / "_archives"

    with pytest.raises(CampaignConfigError, match="_config/"):
        DockerBackend().share_campaign(str(root), None)
    assert not archive_dir.exists(), "and nothing is written on the way to the refusal"

    # The same campaign with its frozen config exports normally: the guard is about what
    # is missing, not about the shape of a campaign that never ran anything.
    os.makedirs(os.path.join(root, "_config"))
    with open(os.path.join(root, "_config", "nav.vast"), "w") as fh:
        fh.write("version: 1\n")
    monkeypatch.setenv("ROBOVAST_ARCHIVE_DIR", str(archive_dir))
    DockerBackend().share_campaign(str(root), None)
    assert list(archive_dir.iterdir()), "a complete campaign still exports"


def test_writer_error_propagates_on_close():
    def boom(_tar):
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        list(campaign_archive.iter_tar(boom))
