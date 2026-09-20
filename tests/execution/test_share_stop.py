# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Cancelling an upload to share: it stops, it is not a failure, and it leaves no archive.

The last clause is the one with teeth. A truncated archive lists and downloads exactly like
a complete one and only fails at the far end -- on somebody else's service, after a full
transfer -- which is the shape ``DockerBackend._refuse_unimportable`` exists to keep off a
share. A cancellation must not manufacture it.
"""

import os
import tarfile

import pytest

from robovast.execution import campaign_archive
from robovast.execution.backends import DockerBackend, ShareStopped
from robovast.execution.control_server import STOP_RUNS, STOP_SHARE, ControllerState
from robovast.execution.controller import make_upload_progress_cb, share_cancelled_detail


def _campaign(tmp_path, name="camp-1", nfiles=6):
    """A campaign tree complete enough for ``share_campaign`` to accept it."""
    root = tmp_path / name
    (root / "_config").mkdir(parents=True)
    (root / "_config" / "campaign.vast").write_text("execution:\n  runs: 1\n")
    for i in range(nfiles):
        (root / f"file{i}.bin").write_bytes(b"x" * 4096)
    return root


def test_both_live_loops_raise_when_the_upload_is_cancelled():
    """The archiver's writer thread drives ``on_member``, the sending thread ``__call__``.

    Polling in only one would leave the other running: a local archive write never reaches
    the wire, and a path-based upload never reaches ``on_member``.
    """
    state = ControllerState()
    state.request_stop(STOP_SHARE)
    cb = make_upload_progress_cb(state)

    with pytest.raises(ShareStopped):
        cb.on_member(1024)
    with pytest.raises(ShareStopped):
        cb(1024, 0)


def test_a_run_scoped_stop_does_not_cancel_the_upload():
    """The scopes are independent here too, or stopping a campaign's runs would abort an
    upload that was asked for separately."""
    state = ControllerState()
    state.request_stop(STOP_RUNS)
    cb = make_upload_progress_cb(state)

    cb.on_member(1024)
    cb(1024, 0)


def test_a_cancelled_local_share_leaves_no_archive(tmp_path):
    """No ``.tar.gz`` under the real name, complete or otherwise.

    The writer builds into a temporary name and renames only once the archive is whole, so
    an interrupted write leaves nothing that can be mistaken for one -- and it removes its
    own partial on the way out.
    """
    root = _campaign(tmp_path)
    archives = tmp_path / "_archives"
    os.environ["ROBOVAST_ARCHIVE_DIR"] = str(archives)
    try:
        state = ControllerState()
        cb = make_upload_progress_cb(state)
        # Cancel once the archiver is already consuming members, which is the real shape:
        # a stop lands mid-write, not before it.
        real_on_member = cb.on_member
        seen = {"n": 0}

        def cancel_after_two(nbytes):
            seen["n"] += 1
            if seen["n"] == 2:
                state.request_stop(STOP_SHARE)
            real_on_member(nbytes)

        cb.on_member = cancel_after_two
        with pytest.raises(ShareStopped):
            DockerBackend().share_campaign(str(root), None, progress_callback=cb)
    finally:
        os.environ.pop("ROBOVAST_ARCHIVE_DIR", None)

    left = sorted(p.name for p in archives.iterdir()) if archives.is_dir() else []
    assert left == [], f"a cancelled share left {left}"


def test_a_completed_local_share_still_lands_under_its_final_name(tmp_path):
    """The property the temp-and-rename must not break: a successful share is unchanged,
    readable, and named what it was always named."""
    root = _campaign(tmp_path)
    archives = tmp_path / "_archives"
    os.environ["ROBOVAST_ARCHIVE_DIR"] = str(archives)
    try:
        DockerBackend().share_campaign(str(root), None, progress_callback=None)
    finally:
        os.environ.pop("ROBOVAST_ARCHIVE_DIR", None)

    written = sorted(p.name for p in archives.iterdir())
    assert len(written) == 1 and written[0].endswith(".tar.gz")
    assert not written[0].endswith(".part")
    with tarfile.open(archives / written[0]) as tar:      # not truncated
        assert tar.getnames()


def test_an_interrupted_tarball_write_removes_its_partial(tmp_path):
    """Not only cancellation: the writer owns this, so a crashed or killed write is covered
    by the same rename rather than by the caller remembering to clean up."""
    root = _campaign(tmp_path)
    out = tmp_path / "_archives"

    def boom(nbytes):
        raise RuntimeError("disk went away")

    with pytest.raises(RuntimeError):
        campaign_archive.make_campaign_tarball(str(root), str(out), name="c.tar.gz",
                                               on_member=boom)

    assert sorted(p.name for p in out.iterdir()) == []


def test_the_upload_is_not_started_when_it_is_already_cancelled(tmp_path):
    """Reading a campaign to build the archive can mean reading a terabyte; a share already
    cancelled must not begin one."""
    root = _campaign(tmp_path)
    archives = tmp_path / "_archives"
    os.environ["ROBOVAST_ARCHIVE_DIR"] = str(archives)
    state = ControllerState()
    state.request_stop(STOP_SHARE)
    cb = make_upload_progress_cb(state)
    touched = []
    cb.set_source_total = lambda total: touched.append(total)
    try:
        with pytest.raises(ShareStopped):
            DockerBackend().share_campaign(str(root), None, progress_callback=cb)
    finally:
        os.environ.pop("ROBOVAST_ARCHIVE_DIR", None)

    assert touched == [], "the campaign was walked for a share that was already cancelled"


class _Backend:
    """A backend double standing in for the lane's partial-artifact cleanup."""

    def __init__(self, note=None, error=None):
        self.note, self.error, self.asked = note, error, []

    def discard_partial_share(self, object_name):
        self.asked.append(object_name)
        if self.error is not None:
            raise self.error
        return self.note


def test_a_removed_partial_is_reported_as_removed():
    backend = _Backend(note="the partial 'c.tar.gz' was removed from the share.")
    detail = share_cancelled_detail(backend, ShareStopped("cancelled", "c.tar.gz"))

    assert backend.asked == ["c.tar.gz"]
    assert "cancelled" in detail and "was removed" in detail


def test_a_cleanup_that_failed_names_the_object_and_says_it_may_remain():
    """Worded as uncertainty because that is what it is: ``remove_archive`` raises both for
    a provider that will not delete and for an object that was never created, and asserting
    either would be the kind of wrong answer that looks right."""
    backend = _Backend(error=RuntimeError("403 Forbidden"))
    detail = share_cancelled_detail(backend, ShareStopped("cancelled", "c.tar.gz"))

    assert "c.tar.gz" in detail and "may be left" in detail and "403" in detail


def test_a_cleanup_failure_never_turns_a_cancellation_into_an_error():
    """The cancellation is the news. A cleanup that raised must not propagate, or a
    deliberate stop would be filed as a fault."""
    backend = _Backend(error=RuntimeError("boom"))
    assert share_cancelled_detail(backend, ShareStopped("cancelled", "c.tar.gz"))


def test_nothing_is_discarded_when_no_object_was_named():
    """The local lane names none: its writer leaves no partial to discard."""
    backend = _Backend()
    detail = share_cancelled_detail(backend, ShareStopped("cancelled"))

    assert backend.asked == []
    assert detail == "cancelled"
