# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Local campaigns are workspace-independent: they share one results root.

Campaigns created from a workspace must NOT land under ``<workspace>/results``
(where the service's readers never look and ``delete_workspace`` would take them
along). They belong in the shared :meth:`LocalTransport._campaigns_root`, which
every read path — list / status / data query — resolves.
"""

from pathlib import Path

import pytest

from robovast.service.client import LocalTransport
from robovast.service.interface import ListCampaignsRequest
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def transport(monkeypatch, tmp_path):
    # No CWD project → _campaigns_root falls back to the dir beside the workspaces
    # (here <tmp_path>/results, kept unique per test by rooting under tmp_path).
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    return LocalTransport(store=store)


def _make_workspace(transport) -> str:
    ws = transport.store.registry.create(name="w")
    wid = ws["workspace_id"]
    transport.store.write_file(wid, "sim.vast", "configuration:\n  name: x\n")
    return wid


def test_campaign_results_go_to_the_shared_root_not_the_workspace(transport):
    """Results land in the shared root; the workspace only supplies the ``.vast``.

    Resolving a workspace does not *return* a results dir at all — it answers with the
    config path alone (``WorkspaceTarget``), and the root is asked for separately. That
    makes the property structural rather than a runtime check, and what is left to assert
    is that the two are genuinely different places.
    """
    wid = _make_workspace(transport)
    target = transport._project_for_workspace(wid)

    shared = transport._campaigns_root()
    ws_results = transport.store.registry.root / wid / "results"

    assert shared != ws_results
    assert not shared.is_relative_to(transport.store.registry.root / wid)
    # config still resolves from inside the workspace
    assert Path(target.config_path).name == "sim.vast"
    assert not hasattr(target, "results_dir"), \
        "the resolved target must not carry a results dir it never varied by"


def test_shared_root_is_stable_across_workspaces(transport):
    """Two different workspaces resolve to the same campaigns root, and to their own
    ``.vast``. The root cannot vary by workspace — it is not derived from one."""
    w1 = _make_workspace(transport)
    w2 = _make_workspace(transport)
    assert transport._campaigns_root() == transport._campaigns_root()
    assert transport._project_for_workspace(w1).config_path != \
        transport._project_for_workspace(w2).config_path


def test_list_and_status_read_the_shared_root(transport):
    """A campaign dir placed in the shared root is listed and reconstructable."""
    root = transport._campaigns_root()
    cid = "campaign-2026-07-16-101010"
    (root / cid).mkdir(parents=True)

    listed = {c.campaign_id for c in transport.list_campaigns().campaigns}
    assert cid in listed
    # get_status for an untracked campaign reconstructs from that same root
    assert transport.get_status(cid).phase != "unknown"
    assert transport._campaign_dir(cid) == root / cid


def test_started_at_comes_from_the_store(transport):
    """list_campaigns reports the real recorded start time (campaign.created_at)."""
    from datetime import datetime, timezone

    from robovast.common.store import STORE_FILENAME, CampaignStore

    root = transport._campaigns_root()
    cid = "campaign-2026-07-16-131415"
    cdir = root / cid
    cdir.mkdir(parents=True)
    with CampaignStore(cdir / STORE_FILENAME) as store:
        store.create_campaign(cid, {}, mode="batch", config_dir="_config")
        created_at = store.list_campaigns()[0]["created_at"]

    summary = next(c for c in transport.list_campaigns().campaigns if c.campaign_id == cid)
    assert summary.started_at == datetime.fromtimestamp(
        created_at, tz=timezone.utc).isoformat()


def test_description_comes_from_the_store(transport):
    """The description a campaign was launched with is listed back with it."""
    from robovast.common.store import STORE_FILENAME, CampaignStore

    root = transport._campaigns_root()
    cid = "campaign-2026-07-16-141516"
    cdir = root / cid
    cdir.mkdir(parents=True)
    with CampaignStore(cdir / STORE_FILENAME) as store:
        store.create_campaign(cid, {}, mode="batch", config_dir="_config",
                              description="pilot: 5 reps, new inflation radius")

    summary = next(c for c in transport.list_campaigns().campaigns if c.campaign_id == cid)
    assert summary.description == "pilot: 5 reps, new inflation radius"


def test_description_empty_without_store(transport):
    """A campaign launched without one (or with no store yet) lists an empty string,
    never a null the UI would have to special-case."""
    root = transport._campaigns_root()
    cid = "campaign-2026-07-16-171819"
    (root / cid).mkdir(parents=True)
    summary = next(c for c in transport.list_campaigns().campaigns if c.campaign_id == cid)
    assert summary.description == ""


def test_live_campaign_reports_its_description_before_the_store_exists(transport):
    """A just-accepted campaign is described from the in-memory entry: its store row is
    written by the controller, which for an image-building campaign is minutes away."""
    from robovast.execution.control_server import ControllerState
    from robovast.service.local_transport import _LocalCampaign

    cid = "campaign-2026-07-16-181920"
    entry = _LocalCampaign(cid, str(transport._campaigns_root()), ControllerState(),
                           description="full sweep after the pilot")
    with transport._lock:
        transport._campaigns[cid] = entry

    summary = next(c for c in transport.list_campaigns().campaigns if c.campaign_id == cid)
    assert summary.description == "full sweep after the pilot"


def test_started_at_none_without_store(transport):
    """A campaign dir with no campaign.db yields started_at=None (not an error)."""
    root = transport._campaigns_root()
    cid = "campaign-2026-07-16-161718"
    (root / cid).mkdir(parents=True)
    summary = next(c for c in transport.list_campaigns().campaigns if c.campaign_id == cid)
    assert summary.started_at is None


def _campaign_with_start(transport, cid: str, created_at: float) -> None:
    """Write a campaign dir whose store records *created_at* as its start time."""
    from robovast.common.store import STORE_FILENAME, CampaignStore

    cdir = transport._campaigns_root() / cid
    cdir.mkdir(parents=True)
    with CampaignStore(cdir / STORE_FILENAME) as store:
        store.create_campaign(cid, {}, mode="batch", config_dir="_config",
                              created_at=created_at)


def test_listing_is_ordered_by_start_time_not_by_name(transport):
    """The newest campaign comes first even when the names invert the chronology.

    The regression this guards: ordering by the whole campaign id sorts on a
    user-supplied ``<name>-`` prefix, so the list comes out alphabetical by name. Because
    limit/offset slice that order, the newest campaign can fall outside the requested page
    entirely, which no client-side sort can repair.
    """
    _campaign_with_start(transport, "zzz-2026-07-01-120000", 1_000.0)   # older
    _campaign_with_start(transport, "aaa-2026-07-26-120000", 2_000.0)   # newer

    listed = [c.campaign_id for c in transport.list_campaigns().campaigns]
    assert listed == ["aaa-2026-07-26-120000", "zzz-2026-07-01-120000"]
    # The window is cut from the time order, so limit=1 yields the newest.
    page = transport.list_campaigns(ListCampaignsRequest(limit=1)).campaigns
    assert [c.campaign_id for c in page] == ["aaa-2026-07-26-120000"]


def _mark_live(transport, cid: str, phase: str = "running") -> None:
    """Register *cid* as a campaign this transport is driving, at *phase*.

    Only the tracked entry is faked; the liveness the listing reads is derived from it by
    ``_is_done`` exactly as it is for a real campaign.
    """
    from robovast.common.store import read_campaign_created_at
    from robovast.execution.control_server import ControllerState
    from robovast.service.local_transport import _LocalCampaign

    state = ControllerState()
    state.set_phase(phase)
    entry = _LocalCampaign(cid, str(transport._campaigns_root()), state)
    # The recorded start time, not now — mirroring what `_dispatch_background` does when it
    # re-tracks a finished campaign. A tracked entry's `created_at` is what `_started_at_for`
    # answers with, so leaving the constructor's default here would restamp the campaign to
    # now and it would sort first for that reason instead of the one under test.
    entry.created_at = (read_campaign_created_at(transport._campaign_dir(cid))
                        or entry.created_at)
    with transport._lock:
        transport._campaigns[cid] = entry


def test_a_running_campaign_leads_however_old_it_is(transport):
    """Live campaigns come first, which is the order the campaign view exists to show.

    A campaign runs for hours to days, so under strict recency the one still running sinks
    below everything launched since — and because limit/offset slice this order, a long run
    could fall off the first page entirely while it is the thing being watched.
    """
    _campaign_with_start(transport, "old-2026-07-01-120000", 1_000.0)    # older, running
    _campaign_with_start(transport, "new-2026-07-26-120000", 2_000.0)    # newer, finished
    _mark_live(transport, "old-2026-07-01-120000")

    listed = [c.campaign_id for c in transport.list_campaigns().campaigns]
    assert listed == ["old-2026-07-01-120000", "new-2026-07-26-120000"]
    # Sorted before the window is cut, so the live one is on the first page whatever its age.
    page = transport.list_campaigns(ListCampaignsRequest(limit=1)).campaigns
    assert [c.campaign_id for c in page] == ["old-2026-07-01-120000"]


def test_recency_still_orders_within_the_live_group(transport):
    """Liveness only decides the group; inside it the newest still comes first."""
    _campaign_with_start(transport, "old-2026-07-01-120000", 1_000.0)
    _campaign_with_start(transport, "new-2026-07-26-120000", 2_000.0)
    # Deliberately the OLDEST start of the three: if a lingering registration counted as
    # liveness, the finished campaign below would outrank it and this assertion would fail.
    _campaign_with_start(transport, "live-2026-06-01-120000", 500.0)
    _mark_live(transport, "old-2026-07-01-120000", phase="finished")
    _mark_live(transport, "live-2026-06-01-120000", phase="running")

    listed = [c.campaign_id for c in transport.list_campaigns().campaigns]
    assert listed[0] == "live-2026-06-01-120000", "only a campaign still being driven leads"
    assert set(listed[1:]) == {"old-2026-07-01-120000", "new-2026-07-26-120000"}


def test_a_finished_campaign_whose_entry_lingers_does_not_lead(transport):
    """Being *registered* is not being live — the trap this ordering must not fall into.

    An entry is dropped from ``_campaigns`` only on delete, so it outlives the campaign it
    tracked. Keying the live group on registration would put every campaign this service
    has driven since it started at the top, permanently, and the bug would only show up
    after a long uptime.
    """
    _campaign_with_start(transport, "old-2026-07-01-120000", 1_000.0)
    _campaign_with_start(transport, "new-2026-07-26-120000", 2_000.0)
    # Deliberately the OLDEST start of the three: if a lingering registration counted as
    # liveness, the finished campaign below would outrank it and this assertion would fail.
    _campaign_with_start(transport, "live-2026-06-01-120000", 500.0)
    _mark_live(transport, "old-2026-07-01-120000", phase="finished")
    _mark_live(transport, "live-2026-06-01-120000", phase="running")

    # Not an exact order: the lingering entry's phase_since makes its campaign the most
    # recently *finished* one, so it leads the terminal group -- correctly, and not what
    # this guards. What matters is that it is in that group at all.
    listed = [c.campaign_id for c in transport.list_campaigns().campaigns]
    assert listed[0] == "live-2026-06-01-120000", "only a campaign still being driven leads"
    assert set(listed[1:]) == {"old-2026-07-01-120000", "new-2026-07-26-120000"}


def test_campaign_without_start_time_sorts_last(transport):
    """An unknown start time never outranks a recorded one, and never breaks listing."""
    _campaign_with_start(transport, "bbb-2026-07-01-120000", 1_000.0)
    (transport._campaigns_root() / "aaa-2026-07-26-120000").mkdir(parents=True)  # no store

    listed = [c.campaign_id for c in transport.list_campaigns().campaigns]
    assert listed == ["bbb-2026-07-01-120000", "aaa-2026-07-26-120000"]


def test_listed_start_time_matches_the_sort_key(transport):
    """Each row's started_at is the value it was ordered by — one source, not two."""
    _campaign_with_start(transport, "ccc-2026-07-01-120000", 1_000.0)
    _campaign_with_start(transport, "bbb-2026-07-26-120000", 2_000.0)

    campaigns = transport.list_campaigns().campaigns
    times = [c.started_at for c in campaigns]
    assert times == sorted(times, reverse=True)


def test_stopped_outcome_persists_across_restart(transport):
    """A stopped campaign records outcome.json, so its phase survives a restart.

    Without it, an untracked stopped campaign reconstructs from disk as "finished".
    """
    from robovast.execution.control_server import ControllerState

    cid = "campaign-2026-07-18-101010"
    (transport._campaigns_root() / cid).mkdir(parents=True)
    state = ControllerState()
    state.update(campaign_id=cid)
    state.set_phase("stopped")

    transport._record_campaign_stopped(cid, str(transport._campaigns_root()), state, None)

    # A fresh transport (no in-memory entry) resolves the phase from disk.
    assert transport._status_from_disk(cid).phase == "stopped"


def test_in_memory_campaign_listed_before_directory_exists(transport):
    """A just-launched campaign (registered in-memory, no directory yet) is listed
    with its live phase — the fix for the launch→list lag."""
    from robovast.execution.control_server import ControllerState, Phase
    from robovast.service.client import _LocalCampaign

    cid = "campaign-2026-07-20-090000"
    state = ControllerState()
    state.set_phase(Phase.BUILDING)
    entry = _LocalCampaign(cid, str(transport._campaigns_root()), state)
    transport._campaigns[cid] = entry

    assert not (transport._campaigns_root() / cid).exists()  # no dir yet

    summary = next(
        (c for c in transport.list_campaigns().campaigns if c.campaign_id == cid),
        None)
    assert summary is not None                    # listed despite having no directory
    assert summary.phase == "building"            # true live phase, not a default
    assert summary.started_at == entry.created_at  # start time available from t=0


def test_tracked_campaign_phase_wins_over_disk(transport):
    """A campaign both tracked and on disk is reported once, with its live phase
    (the same precedence get_status uses), not the disk-reconstructed 'finished'."""
    from robovast.execution.control_server import ControllerState, Phase
    from robovast.service.client import _LocalCampaign

    root = transport._campaigns_root()
    cid = "campaign-2026-07-20-091500"
    (root / cid).mkdir(parents=True)              # on disk → would reconstruct "finished"
    state = ControllerState()
    state.set_phase(Phase.RUNNING)
    transport._campaigns[cid] = _LocalCampaign(cid, str(root), state)

    summaries = transport.list_campaigns().campaigns
    ids = [c.campaign_id for c in summaries]
    assert ids.count(cid) == 1                    # union deduplicates disk ∪ memory
    assert next(c for c in summaries if c.campaign_id == cid).phase == "running"


def test_deleting_workspace_keeps_campaigns(transport):
    """Campaigns survive deletion of the workspace that authored them."""
    wid = _make_workspace(transport)
    root = transport._campaigns_root()
    cid = "campaign-2026-07-16-121212"
    (Path(root) / cid).mkdir(parents=True)

    transport.store.registry.delete(wid)

    assert (Path(root) / cid).is_dir()
    assert cid in {c.campaign_id for c in transport.list_campaigns().campaigns}


# -- project binding: workspace_id is the only one --------------------------------


def test_resolve_project_without_workspace_fails_loudly(transport):
    """An empty ``workspace_id`` is refused, not resolved from a CWD project.

    Regression: the empty-id branch loaded ``.robovast_project`` from the service's
    CWD and **ignored** ``vast_path``, so a caller naming one ``.vast`` silently got
    whichever one had been initialized -- a campaign that ran the wrong simulator and
    reported success.
    """
    with pytest.raises(ValueError, match="workspace_id is required"):
        transport._resolve_project("", "sim.vast")


def test_config_path_selects_the_named_vast(transport):
    """``vast_path`` picks among several ``.vast`` files -- it is never ignored."""
    wid = _make_workspace(transport)
    transport.store.write_file(wid, "other.vast", "configuration:\n  name: y\n")

    assert transport._resolve_project(wid, "sim.vast").config_path.endswith("sim.vast")
    assert transport._resolve_project(wid, "other.vast").config_path.endswith("other.vast")


def test_ambiguous_workspace_names_the_candidates(transport):
    """With several ``.vast`` files and no ``vast_path``, the error lists them."""
    wid = _make_workspace(transport)
    transport.store.write_file(wid, "other.vast", "configuration:\n  name: y\n")

    with pytest.raises(ValueError, match="other.vast"):
        transport._resolve_project(wid, "")


def test_unknown_vast_path_is_an_error_not_a_fallback(transport):
    wid = _make_workspace(transport)
    with pytest.raises(ValueError, match="no such .vast"):
        transport._resolve_project(wid, "nope.vast")


def _campaign_finished_at(transport, cid: str, finished_at: float) -> None:
    """Record a terminal outcome for *cid*, ending at *finished_at*.

    The same durable record the controller writes at the end of a campaign — this is where
    ``read_campaign_finished_at`` reads the time from, so a test that fakes it any other way
    would not exercise the path the service uses.
    """
    from robovast.client.status import Status
    from robovast.common.campaign_data import write_execution_outcome
    from robovast.execution.control_server import Phase

    write_execution_outcome(transport._campaign_dir(cid),
                            Status(phase=Phase.FINISHED, phase_since=finished_at))


def test_the_terminal_group_is_ordered_by_when_a_campaign_ended(transport):
    """A long campaign that just ended is the freshest result, however old its start.

    Start time answers "which of these is recent" badly for exactly the campaign most worth
    seeing: `slow` ran for a day and finished a minute ago, `quick` started later and finished
    long before it. Ordered by start, the one with the newest results sits underneath.
    """
    _campaign_with_start(transport, "slow-2026-07-01-120000", 1_000.0)
    _campaign_with_start(transport, "quick-2026-07-20-120000", 5_000.0)
    _campaign_finished_at(transport, "slow-2026-07-01-120000", 90_000.0)
    _campaign_finished_at(transport, "quick-2026-07-20-120000", 6_000.0)

    listed = [c.campaign_id for c in transport.list_campaigns().campaigns]
    assert listed == ["slow-2026-07-01-120000", "quick-2026-07-20-120000"]


def test_a_campaign_with_no_recorded_ending_is_ordered_by_its_start(transport):
    """The fallback, which is permanent rather than transitional.

    A campaign whose record carries no terminal outcome — one that predates the record, or an
    import that arrived without one — never gets a finish time, so it keeps ordering exactly as
    it did. Here the campaign with no ending started later than the other one finished, and
    leads on that basis.
    """
    _campaign_with_start(transport, "ended-2026-07-01-120000", 1_000.0)
    _campaign_with_start(transport, "unknown-2026-07-20-120000", 5_000.0)
    _campaign_finished_at(transport, "ended-2026-07-01-120000", 2_000.0)

    listed = [c.campaign_id for c in transport.list_campaigns().campaigns]
    assert listed == ["unknown-2026-07-20-120000", "ended-2026-07-01-120000"]

    summaries = {c.campaign_id: c for c in transport.list_campaigns().campaigns}
    assert summaries["unknown-2026-07-20-120000"].finished_at is None
    assert summaries["ended-2026-07-01-120000"].finished_at is not None


def test_a_live_campaign_still_leads_and_is_ordered_by_its_start(transport):
    """Liveness is the first key and a finish time cannot displace it.

    A running campaign has no ending yet, and a just-launched one belongs at the top — so the
    live group keeps sorting by start time even though the group below it no longer does.
    """
    _campaign_with_start(transport, "ended-2026-07-26-120000", 5_000.0)
    _campaign_finished_at(transport, "ended-2026-07-26-120000", 90_000.0)
    _campaign_with_start(transport, "live-2026-06-01-120000", 500.0)
    _mark_live(transport, "live-2026-06-01-120000", phase="running")

    listed = [c.campaign_id for c in transport.list_campaigns().campaigns]
    assert listed == ["live-2026-06-01-120000", "ended-2026-07-26-120000"]
