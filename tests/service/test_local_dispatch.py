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


def _campaign_with_start(transport, cid: str, created_at: float) -> None:
    """A campaign dir whose store records *created_at* as its start time."""
    from robovast.common.store import STORE_FILENAME, CampaignStore

    cdir = transport._campaigns_root() / cid
    cdir.mkdir(parents=True)
    with CampaignStore(cdir / STORE_FILENAME) as store:
        store.create_campaign(cid, {}, mode="batch", config_dir="_config",
                              created_at=created_at)


def _listed(transport) -> list:
    return [c.campaign_id for c in transport.list_campaigns().campaigns]


def test_a_reactivated_campaign_leads_the_listing_then_falls_back(transport):
    """A finished campaign put back to work is live again, and the listing says so.

    ``list_campaigns`` leads with the campaigns something is driving, and a re-triggered
    postprocessing or upload-to-share is exactly that — the campaign the user is watching
    for the duration of the op, however old the run behind it is. Both directions are
    asserted: nothing has to remember to move it back, because the same ``_is_done`` that
    lifted it drops it once the worker ends.
    """
    old, new = "old-2026-07-01-120000", "new-2026-07-26-120000"
    _campaign_with_start(transport, old, 1_000.0)
    _campaign_with_start(transport, new, 2_000.0)
    assert _listed(transport) == [new, old], "recency orders them while both are at rest"

    release = threading.Event()
    assert transport._dispatch_background(
        old, phase=Phase.POSTPROCESSING, work=lambda state: release.wait(2)).ok
    try:
        assert _listed(transport) == [old, new]
    finally:
        release.set()
        transport._campaigns[old].thread.join(2)

    assert _listed(transport) == [new, old], \
        "the entry outlives the op, so only _is_done can put it back"


def test_reactivation_does_not_restamp_the_start_time(transport):
    """It rises because it is live, not because it was made to look new.

    ``_dispatch_background`` deliberately carries the recorded ``created_at`` onto the
    entry it installs, so a re-run cannot re-order a finished campaign by pretending it
    started now. That guard and the live-first ordering answer the same need in two
    different ways, and this pins that the honest one is the one doing the work.
    """
    cid = "camp-2026-07-01-120000"
    _campaign_with_start(transport, cid, 1_000.0)
    before = transport.list_campaigns().campaigns[0].started_at

    release = threading.Event()
    transport._dispatch_background(cid, phase=Phase.SHARING,
                                   work=lambda state: release.wait(2))
    try:
        assert transport.list_campaigns().campaigns[0].started_at == before
    finally:
        release.set()
        transport._campaigns[cid].thread.join(2)

    assert transport.list_campaigns().campaigns[0].started_at == before


def test_a_crashed_reactivation_still_falls_back(transport):
    """The failure path reaches a terminal phase too, so it cannot strand a campaign on top.

    ``_dispatch_background``'s safety-net catches whatever ``work`` raises and sets
    ``FINISHED`` itself; ``_is_done`` independently catches a thread that ran and ended. A
    campaign wedged at the head of every listing by a crashed op would be a bug nobody
    could clear without restarting the service.
    """
    old, new = "old-2026-07-01-120000", "new-2026-07-26-120000"
    _campaign_with_start(transport, old, 1_000.0)
    _campaign_with_start(transport, new, 2_000.0)

    def boom(state):
        raise RuntimeError("postprocessing blew up")

    assert transport._dispatch_background(old, phase=Phase.POSTPROCESSING, work=boom).ok
    transport._campaigns[old].thread.join(2)

    assert _listed(transport) == [new, old]
