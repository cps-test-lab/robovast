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


def _mark_live(transport, cid: str) -> None:
    """Register *cid* as a campaign this transport is driving, so it is genuinely live."""
    from robovast.common.store import read_campaign_created_at
    from robovast.execution.control_server import ControllerState
    from robovast.service.local_transport import _LocalCampaign

    state = ControllerState()
    state.set_phase("running")
    entry = _LocalCampaign(cid, str(transport._campaigns_root()), state)
    entry.created_at = (read_campaign_created_at(transport._campaign_dir(cid))
                        or entry.created_at)
    with transport._lock:
        transport._campaigns[cid] = entry


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

    # The crashed op ended the campaign, so it is the most recently *finished* one and leads
    # the terminal group -- which is correct and is not what this guards. What matters is that
    # it left the LIVE group: a genuinely live campaign, with the oldest start of the three,
    # outranks it. Wedged in the live group, `old` would come first instead.
    _campaign_with_start(transport, "live-2026-06-01-120000", 500.0)
    _mark_live(transport, "live-2026-06-01-120000")
    assert _listed(transport)[0] == "live-2026-06-01-120000"
    assert set(_listed(transport)[1:]) == {old, new}


def _finished_campaign(transport, cid: str, **outcome) -> None:
    """A campaign at rest whose durable record carries *outcome*."""
    from robovast.common.campaign_data import write_execution_outcome
    from robovast.common.store import STORE_FILENAME, CampaignStore
    from robovast.execution.control_server import Status

    cdir = transport._campaigns_root() / cid
    cdir.mkdir(parents=True)
    with CampaignStore(cdir / STORE_FILENAME) as store:
        store.create_campaign(cid, {}, mode="batch")
    write_execution_outcome(cdir, Status(campaign_id=cid, **outcome))


def _summary(transport, cid: str):
    return next(c for c in transport.list_campaigns().campaigns if c.campaign_id == cid)


def test_dispatch_keeps_a_recorded_postprocessing_failure_visible(transport):
    """Re-triggering the upload must not blank the error the campaign already carries.

    The tracked entry answers for the campaign while the op runs, so an empty
    ControllerState reports a failed postprocessing as error-free -- and, because
    ``_derive_postprocessed``'s only guard against promoting ``postprocessed`` over a
    failure is that same error field, it also flips the flag the web UI gates its
    Results views on. A failed build would offer its results for as long as an upload
    somebody triggered kept running.
    """
    cid = "camp-2026-07-17-140000"
    _finished_campaign(transport, cid, phase=Phase.FINISHED,
                       postprocessing_error="rosbags_costmap_to_csv: killed (exit 137)")

    release = threading.Event()
    assert transport._dispatch_background(cid, phase=Phase.SHARING,
                                          work=lambda state: release.wait(2)).ok
    try:
        live = _summary(transport, cid)
        assert live.postprocessing_error == "rosbags_costmap_to_csv: killed (exit 137)"
        assert live.postprocessed is False
    finally:
        release.set()
        transport._campaigns[cid].thread.join(2)


def test_dispatch_keeps_a_recorded_share_failure_visible(transport):
    """The same, the other way round: postprocessing must not hide a failed upload.

    ``run_postprocessing``'s worker writes back only the postprocessing fields, so a
    ``share_error`` the entry did not carry is not restored when the op ends either --
    it stays blank for as long as the entry is tracked, which outlives the op.
    """
    cid = "camp-2026-07-17-150000"
    _finished_campaign(transport, cid, phase=Phase.FINISHED,
                       share_error="the share refused the credentials")

    release = threading.Event()
    assert transport._dispatch_background(cid, phase=Phase.POSTPROCESSING,
                                          work=lambda state: release.wait(2)).ok
    try:
        assert _summary(transport, cid).share_error == "the share refused the credentials"
    finally:
        release.set()
        transport._campaigns[cid].thread.join(2)


def test_dispatch_keeps_how_the_campaign_ended(transport):
    """A campaign that failed still reads as failed while an op runs on it."""
    cid = "camp-2026-07-17-160000"
    _finished_campaign(transport, cid, phase=Phase.FAILED, error="the sweep died")

    release = threading.Event()
    assert transport._dispatch_background(cid, phase=Phase.SHARING,
                                          work=lambda state: release.wait(2)).ok
    try:
        assert _summary(transport, cid).error == "the sweep died"
    finally:
        release.set()
        transport._campaigns[cid].thread.join(2)


def test_the_busy_refusal_names_what_is_running(transport):
    """"An operation" is not actionable: waiting out a postprocessing run and waiting
    out the sweep itself are different waits, and the phase is what tells them apart."""
    cid = "camp-2026-07-17-170000"
    release = threading.Event()
    transport._dispatch_background(cid, phase=Phase.POSTPROCESSING,
                                   work=lambda state: release.wait(2))
    try:
        refused = transport._dispatch_background(cid, phase=Phase.SHARING,
                                                 work=lambda s: None)
        assert not refused.ok
        assert Phase.POSTPROCESSING in refused.message
    finally:
        release.set()
        transport._campaigns[cid].thread.join(2)


def test_a_re_triggered_share_leaves_a_stopped_campaign_stopped(transport):
    """End to end, on the real ``run_share`` worker rather than a stand-in.

    Two records have to agree afterwards and both used to say ``finished``: the durable
    ``outcome.json``, which a restart and every importer read, and the tracked entry,
    which answers until one happens. A sweep the user stopped and then uploaded must not
    read as one that ran to the end in either.
    """
    from robovast.execution.status_recovery import reconstruct_status_from_disk
    from robovast.service.interface import RunShareRequest

    cid = "camp-2026-07-17-190000"
    _finished_campaign(transport, cid, phase=Phase.STOPPED, error="stopped by user")

    shared = {}
    transport._build_backend = lambda state: type(
        "B", (), {"preflight_upload_to_share": lambda s: None,
                  "share_campaign": lambda s, root, opts, progress_callback=None:
                      shared.setdefault("root", root)})()

    assert transport.run_share(RunShareRequest(campaign_id=cid)).ok
    transport._campaigns[cid].thread.join(5)

    assert shared["root"].endswith(cid), "the upload ran"
    assert transport.get_status(cid).phase == Phase.STOPPED
    assert reconstruct_status_from_disk(transport._campaign_dir(cid)).phase == Phase.STOPPED
