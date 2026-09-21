# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Deleting a campaign removes everything it owns on the service host, or says what it left.

A campaign's bytes are not only its directory: the local lane writes each archive it shares
beside the campaign dirs, and a failed import keeps the copy it fetched from the share. A
delete that left those behind, or that swallowed a path it could not remove, would answer
"deleted" while the space stayed taken.
"""

import os

import pytest

CID = "gone-2026-09-01-101500"


class _NullIndexConn:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture(name="env")
def _env(tmp_path, monkeypatch):
    from robovast.service.local_transport import LocalTransport
    from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore

    monkeypatch.delenv("ROBOVAST_ARCHIVE_DIR", raising=False)
    monkeypatch.setattr("robovast.results_processing.index_schema.forget_campaign",
                        lambda conn, cid: {})
    monkeypatch.setattr("robovast.common.index_db.connect",
                        lambda *a, **k: _NullIndexConn())
    results = tmp_path / "results"
    (results / CID / "cfg" / "0").mkdir(parents=True)
    (results / CID / "cfg" / "0" / "out.bag").write_bytes(b"x" * 16)
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    transport = LocalTransport(store=store)
    transport._campaigns_root = lambda: results        # noqa: SLF001
    return transport, results


def test_the_local_archives_go_with_the_campaign(env):
    transport, results = env
    archives = results / "_archives"
    archives.mkdir()
    mine = [archives / f"{CID}.raw.tar.gz", archives / f"{CID}.postprocessed.tar.gz",
            archives / f"{CID}.raw.tar.gz.part"]
    other = archives / "other-2026-09-01-101500.raw.tar.gz"
    for path in [*mine, other]:
        path.write_bytes(b"a" * 8)

    result = transport.delete_campaign(CID)

    assert result.ok, result.message
    assert "3 archive(s)" in result.message
    assert not (results / CID).exists()
    assert not any(p.exists() for p in mine)
    assert other.exists(), "another campaign's archive is not this delete's to remove"


def test_the_archive_dir_override_is_honoured(env, tmp_path, monkeypatch):
    transport, _results = env
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setenv("ROBOVAST_ARCHIVE_DIR", str(elsewhere))
    archive = elsewhere / f"{CID}.raw.tar.gz"
    archive.write_bytes(b"a")

    assert transport.delete_campaign(CID).ok
    assert not archive.exists()


def test_a_share_copy_a_failed_import_kept_is_removed(env):
    transport, results = env
    staged = results / "_imports" / f"{CID}.tar.gz"
    staged.parent.mkdir()
    staged.write_bytes(b"a")
    upload = results / "_imports" / "sometoken.tar.gz"
    upload.write_bytes(b"a")

    assert transport.delete_campaign(CID).ok
    assert not staged.exists()
    assert upload.exists(), "an upload is keyed by its token and left to the age sweep"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root can unlink anything, so nothing is left to report")
def test_a_path_that_cannot_be_removed_fails_the_delete_and_is_named(env):
    """The shape of run output written by a container user other than the service's."""
    transport, results = env
    locked = results / CID / "cfg" / "0"
    locked.chmod(0o555)
    try:
        result = transport.delete_campaign(CID)
    finally:
        locked.chmod(0o755)

    assert not result.ok
    assert "out.bag" in result.message
    assert "run_as_user" in result.message
    # Once the owner has made it removable, deleting again finishes the job.
    assert transport.delete_campaign(CID).ok
    assert not (results / CID).exists()


def test_a_delete_drops_what_is_cached_about_the_campaign(env):
    transport, _results = env
    transport._started_at_cache[CID] = "2026-09-01T10:15:00"       # noqa: SLF001
    transport._description_cache[CID] = "old"                      # noqa: SLF001
    transport._summary_cache[CID] = ("key", object())              # noqa: SLF001

    assert transport.delete_campaign(CID).ok
    assert CID not in transport._started_at_cache                  # noqa: SLF001
    assert CID not in transport._description_cache                 # noqa: SLF001
    assert CID not in transport._summary_cache                     # noqa: SLF001


def test_a_campaign_with_nothing_left_deletes_idempotently(env):
    transport, _results = env
    assert transport.delete_campaign(CID).ok
    again = transport.delete_campaign(CID)
    assert again.ok
    assert "nothing to delete" in again.message
