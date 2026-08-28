# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``postprocessed`` must not depend on how long the service has been up.

It is read as "is there derived data here": the web UI gates its Results and Run views on
it, and both read ``_execution/data.db``. But two paths produced it, and they disagreed.

* the **live** ``ControllerState`` — ``_postprocess`` records ``True`` only when the ``.vast``
  declared ``results_processing.postprocessing`` *entries*, which is the narrower question of
  whether the stored archive is the postprocessed one;
* the **disk recovery** path — derives it from ``data.db`` existing, and says so in its own
  comment: *"postprocessed is a fact about the campaign, not about who last drove it."*

A campaign declaring no postprocessing entries still builds ``data.db``. So for as long as
the service tracked it, it reported ``False`` and the UI hid the two views that read that
file; after a restart dropped the entry, the same campaign on the same bytes reported
``True`` and the views appeared. These pin the two answers together.
"""

import pytest

from robovast.execution.control_server import ControllerState, Phase
from robovast.service.local_transport import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def svc(tmp_path, monkeypatch):
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "ws")))
    transport = LocalTransport(store=store)
    results = tmp_path / "results"
    results.mkdir()
    transport._campaigns_root = lambda: results        # noqa: SLF001
    return transport


CID = "pilot-2026-08-09-120000"


def _campaign(svc, *, with_data_db: bool):
    """A finished campaign on disk, with or without its derived data."""
    campaign = svc._campaigns_root() / CID              # noqa: SLF001
    (campaign / "_execution").mkdir(parents=True)
    if with_data_db:
        (campaign / "_execution" / "data.db").write_bytes(b"")
    return campaign


def _track(svc, *, postprocessed: bool):
    """Register a live entry whose state reports *postprocessed*, as the worker leaves it."""
    from robovast.service.local_transport import _LocalCampaign
    state = ControllerState(campaign_id=CID)
    state.set_phase(Phase.FINISHED)
    state.update(postprocessed=postprocessed)
    entry = _LocalCampaign(CID, str(svc._campaigns_root()), state)   # noqa: SLF001
    with svc._lock:                                                  # noqa: SLF001
        svc._campaigns[CID] = entry                                  # noqa: SLF001
    return entry


def test_a_tracked_campaign_with_derived_data_reports_postprocessed(svc):
    """The bug: the .vast declared no postprocessing entries, so the live state says False —
    but data.db is there, which is what a reader is actually asking about."""
    _campaign(svc, with_data_db=True)
    _track(svc, postprocessed=False)
    assert svc.get_status(CID).postprocessed is True


def test_the_listing_agrees_with_the_status(svc):
    """The web UI gates its buttons on the *listing*, so the two must not diverge."""
    _campaign(svc, with_data_db=True)
    _track(svc, postprocessed=False)
    assert svc._summary_for(CID).postprocessed is True        # noqa: SLF001
    assert svc.get_status(CID).postprocessed is True


def test_a_restart_does_not_change_the_answer(svc):
    """Same campaign, same bytes, entry dropped — the disk path already said True, and the
    tracked path now agrees. This is the inconsistency that made the buttons come and go."""
    _campaign(svc, with_data_db=True)
    _track(svc, postprocessed=False)
    tracked = svc.get_status(CID).postprocessed
    with svc._lock:                                          # noqa: SLF001
        svc._campaigns.pop(CID)                              # noqa: SLF001
    assert tracked is svc.get_status(CID).postprocessed is True


def test_without_derived_data_it_stays_false(svc):
    """Only ``data.db`` promotes it, so a campaign that produced none is still not
    postprocessed — the views would open on nothing."""
    _campaign(svc, with_data_db=False)
    _track(svc, postprocessed=False)
    assert svc.get_status(CID).postprocessed is False


def test_a_state_that_already_says_true_is_left_alone(svc):
    """Promotion is one-way: what ``_postprocess`` recorded is never contradicted, so the
    archive decision that reads it is unaffected."""
    _campaign(svc, with_data_db=False)
    _track(svc, postprocessed=True)
    assert svc.get_status(CID).postprocessed is True


# -- a build in progress is not a finished one -------------------------------
#
# The rule above was "``data.db`` exists", and existence is not evidence that the build
# finished: ``build_data_db`` unlinks any previous database and then connects, so the file is
# there from 0%. A 9 GB build across 1870 runs reported ``postprocessed: true`` for the twenty
# minutes it was being written — and because the web UI gates its Results views on this flag,
# it offered them over a database still being appended to. A *re*-postprocess is worse: the
# previous results are unlinked first, so the window covers a database that is not merely
# unfinished but has replaced one that was.
#
# ``campaign_has_derived_data`` is the shared predicate now, so these pin the live path and
# the disk path to the same answer, exactly as the tests above do for existence.


def _building(svc):
    """A campaign whose ``data.db`` has a live writer: SQLite's WAL sidecars beside it."""
    campaign = _campaign(svc, with_data_db=True)
    (campaign / "_execution" / "data.db-wal").write_bytes(b"")
    (campaign / "_execution" / "data.db-shm").write_bytes(b"")
    return campaign


def test_a_data_db_still_being_built_is_not_postprocessed(svc):
    _building(svc)
    _track(svc, postprocessed=False)
    assert svc.get_status(CID).postprocessed is False


def test_the_listing_agrees_while_the_build_runs(svc):
    """The UI gates its Results views on the *listing*, so it must not offer them mid-build."""
    _building(svc)
    _track(svc, postprocessed=False)
    assert svc._summary_for(CID).postprocessed is False       # noqa: SLF001


def test_it_flips_once_the_writer_closes(svc):
    """Closing the last connection checkpoints the WAL and removes both sidecars — which is
    the moment the database becomes readable as a whole. ``build_data_db`` closes from a
    ``finally``, so this is the transition every completed build makes."""
    campaign = _building(svc)
    _track(svc, postprocessed=False)
    assert svc.get_status(CID).postprocessed is False
    (campaign / "_execution" / "data.db-wal").unlink()
    (campaign / "_execution" / "data.db-shm").unlink()
    assert svc.get_status(CID).postprocessed is True


def test_a_recorded_failure_is_never_promoted(svc):
    """``build_data_db`` closes from a ``finally``, so a build that raised part-way leaves a
    sidecar-free database behind. Promoting on it would put "results are ready" over the top
    of the ``postprocessing_error`` that says they are not."""
    _campaign(svc, with_data_db=True)
    entry = _track(svc, postprocessed=False)
    entry.state.update(postprocessing_error="conversion failed")
    assert svc.get_status(CID).postprocessed is False


def test_a_restart_mid_build_does_not_change_the_answer(svc):
    """The same pairing the tests above make for a finished database: one predicate, so the
    tracked and the disk-recovered answers cannot drift apart while the build runs."""
    _building(svc)
    _track(svc, postprocessed=False)
    tracked = svc.get_status(CID).postprocessed
    with svc._lock:                                           # noqa: SLF001
        svc._campaigns.pop(CID)                               # noqa: SLF001
    assert tracked is svc.get_status(CID).postprocessed is False
