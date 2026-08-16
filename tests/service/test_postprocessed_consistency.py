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
    monkeypatch.setattr(
        "robovast.client.project_config.ProjectConfig.load",
        staticmethod(lambda *a, **k: None))
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
