# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""LocalTransport._dispatch_background — post-run ops as tracked, monitorable campaigns.

A re-run (postprocessing / share) is dispatched to a daemon thread and returns at once;
while it runs the campaign is tracked with the operation's phase (so the Monitor shows
it), and a second dispatch is refused by the busy guard until the first finishes.
"""

import threading

import pytest

from robovast.execution.control_server import Phase
from robovast.service.client import LocalTransport
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def transport(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "robovast.client.project_config.ProjectConfig.load",
        staticmethod(lambda *a, **k: None))
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return LocalTransport(store=store)


def test_dispatch_tracks_phase_then_finishes(transport):
    cid = "camp-2026-07-17-120000"
    started, release, done = threading.Event(), threading.Event(), threading.Event()

    def work(state):
        started.set()
        assert release.wait(2)
        state.set_phase(Phase.FINISHED)
        done.set()

    res = transport._dispatch_background(cid, phase=Phase.POSTPROCESSING, work=work)
    assert res.ok and "started" in res.message
    assert started.wait(2)

    # While the op runs, the campaign is tracked with the op's phase — this is what the
    # Monitor renders as a live postprocessing/sharing phase.
    assert transport.get_status(cid).phase == Phase.POSTPROCESSING

    # Busy guard: a second dispatch is refused while the first is live.
    busy = transport._dispatch_background(cid, phase=Phase.SHARING, work=lambda s: None)
    assert not busy.ok and "busy" in busy.message

    release.set()
    assert done.wait(2)
    transport._campaigns[cid].thread.join(2)
    assert transport.get_status(cid).phase == Phase.FINISHED

    # Once the op is done the guard clears — a new dispatch is accepted.
    again = transport._dispatch_background(cid, phase=Phase.POSTPROCESSING,
                                           work=lambda s: s.set_phase(Phase.FINISHED))
    assert again.ok
    transport._campaigns[cid].thread.join(2)


def test_dispatch_keeps_the_campaign_description(transport):
    """The tracked entry a re-run installs answers for the campaign while it is live, so
    it must carry the recorded description — otherwise re-triggering postprocessing blanks
    it out of every listing for the duration of the op."""
    from robovast.common.store import STORE_FILENAME, CampaignStore

    cid = "camp-2026-07-17-130000"
    cdir = transport._campaigns_root() / cid
    cdir.mkdir(parents=True)
    with CampaignStore(cdir / STORE_FILENAME) as store:
        store.create_campaign(cid, {}, mode="batch", description="the full sweep")

    release = threading.Event()
    transport._dispatch_background(
        cid, phase=Phase.POSTPROCESSING,
        work=lambda state: release.wait(2))
    try:
        summary = next(c for c in transport.list_campaigns().campaigns
                       if c.campaign_id == cid)
        assert summary.description == "the full sweep"
    finally:
        release.set()
        transport._campaigns[cid].thread.join(2)
