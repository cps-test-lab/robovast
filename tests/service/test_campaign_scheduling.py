# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for a campaign's standing with the admission queue: its rank and its hold.

The point of the feature is to get a short campaign through while a long one is running,
without touching the long one's results. So the checks here are the ways that could go
wrong quietly:

- a lane with no queue accepting a rank it cannot act on, leaving the campaign to run at
  the ordinary time with nothing saying the rank did nothing;
- a call that asked for nothing being answered "done";
- setting one half of the pair silently resetting the other;
- the change reaching the queue but not the launch record, so the next service restart
  returns the campaign to what it was launched with.
"""

import pytest

from robovast.common.campaign_data import read_launch_record, write_launch_record
from robovast.execution.cluster_execution.cluster_service import ClusterService
from robovast.service.client import LocalTransport
from robovast.service.interface import PRIORITY_LIMIT, CreateCampaignRequest
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def _store(tmp_path):
    return WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))


@pytest.fixture
def local(_store):
    return LocalTransport(store=_store)


class _NoCluster:
    """A BudgetProvider over an empty cluster. Nothing here places a job; these tests are
    about what the queue is *told*, which needs no capacity at all."""

    def budget(self):
        from robovast.execution.cluster_execution.node_admission import Budget
        return Budget(nodes=(), counted_jobs=frozenset(), growable=False)

    def capacities(self):
        return []


@pytest.fixture
def cluster(_store):
    # reap_on_start would talk to a cluster; nothing here needs a live one.
    service = ClusterService(store=_store, reap_on_start=False)
    # Built by hand so the real factory, which reads the cluster for capacity, is not
    # reached: what these tests exercise is the queue's bookkeeping, not its placement.
    from robovast.execution.cluster_execution.node_admission import AdmissionController
    service._admission = AdmissionController(_NoCluster())
    return service


# -- the lane that has no queue -----------------------------------------------------------

def test_the_local_lane_refuses_a_rank_at_launch(local):
    """Accepting it is the silent failure: the campaign would run at the ordinary time and
    nothing would ever say the rank did nothing."""
    with pytest.raises(ValueError, match="one campaign at a time"):
        local.create_campaign(CreateCampaignRequest(workspace_id="ws-x", priority=-1))


def test_the_local_lane_refuses_a_hold_at_launch(local):
    with pytest.raises(ValueError, match="no queue"):
        local.create_campaign(CreateCampaignRequest(workspace_id="ws-x", paused=True))


def test_the_local_lane_admits_a_launch_that_asks_for_nothing(local):
    """The default must stay launchable on both lanes, or every ordinary local run breaks."""
    local._admit_scheduling(CreateCampaignRequest(workspace_id="ws-x"))


def test_the_local_lane_refuses_the_operation_even_at_the_default(local):
    """Unlike a launch: a launch that never mentions scheduling is an ordinary launch, but
    *asking* for a rank here asks for something this lane cannot do, whatever the value."""
    with pytest.raises(ValueError, match="no queue"):
        local.set_campaign_scheduling("camp-1", priority=0)


def test_the_cluster_lane_admits_a_rank(cluster):
    cluster._admit_scheduling(CreateCampaignRequest(workspace_id="ws-x", priority=5))


# -- a call that asked for nothing --------------------------------------------------------

@pytest.mark.parametrize("impl", ["local", "cluster"])
def test_setting_neither_half_is_refused(impl, request):
    """Answering it "done" would report a change that never happened."""
    service = request.getfixturevalue(impl)
    with pytest.raises(ValueError, match="nothing to set"):
        service.set_campaign_scheduling("camp-1")


# -- the bound ----------------------------------------------------------------------------

def test_a_rank_outside_the_range_is_refused_where_it_is_typed():
    with pytest.raises(ValueError):
        CreateCampaignRequest(workspace_id="ws-x", priority=PRIORITY_LIMIT + 1)


# -- the cluster lane, which has a queue --------------------------------------------------

def test_an_unknown_campaign_is_reported_not_raised(cluster):
    result = cluster.set_campaign_scheduling("camp-nope", priority=1)
    assert result.ok is False and "not running here" in result.message


def _live(cluster, campaign_id, results_dir):
    """Register a campaign the way a launch does, without running one."""
    from robovast.execution.control_server import ControllerState
    from robovast.service.local_transport import _LocalCampaign
    entry = _LocalCampaign(campaign_id, str(results_dir), ControllerState(campaign_id=campaign_id))
    with cluster._lock:
        cluster._campaigns[campaign_id] = entry
    return entry


def test_a_rank_reaches_the_queue_and_the_launch_record(cluster, tmp_path):
    campaign_id = "camp-2026-01-01-120000"
    _live(cluster, campaign_id, tmp_path)
    root = tmp_path / campaign_id
    root.mkdir()
    write_launch_record(root, CreateCampaignRequest(workspace_id="ws-x"))

    result = cluster.set_campaign_scheduling(campaign_id, priority=-3)

    assert result.ok is True
    assert cluster._admission_controller().scheduling(campaign_id) == (-3, False)
    # Without this the next service restart re-launches the campaign at what it was
    # launched with, and it goes back to taking capacity somebody already took away.
    assert read_launch_record(root)["priority"] == -3


def test_holding_a_campaign_leaves_the_rank_it_resumes_at(cluster, tmp_path):
    campaign_id = "camp-2026-01-01-130000"
    _live(cluster, campaign_id, tmp_path)
    root = tmp_path / campaign_id
    root.mkdir()
    write_launch_record(root, CreateCampaignRequest(workspace_id="ws-x"))

    cluster.set_campaign_scheduling(campaign_id, priority=4)
    cluster.set_campaign_scheduling(campaign_id, paused=True)

    assert cluster._admission_controller().scheduling(campaign_id) == (4, True)
    record = read_launch_record(root)
    assert record["priority"] == 4 and record["paused"] is True


def test_a_launch_seeds_the_queue_before_the_first_batch(cluster, tmp_path):
    """Seeding later would let one batch be admitted at the ordinary rank first."""
    campaign_id = "camp-2026-01-01-140000"
    cluster._register_scheduling(
        campaign_id, CreateCampaignRequest(workspace_id="ws-x", priority=-2, paused=True))
    assert cluster._admission_controller().scheduling(campaign_id) == (-2, True)


def test_a_listing_reports_what_the_queue_holds(cluster, tmp_path):
    campaign_id = "camp-2026-01-01-150000"
    _live(cluster, campaign_id, tmp_path)
    cluster._admission_controller().set_scheduling(campaign_id, priority=6, paused=True)
    assert cluster._scheduling_for(campaign_id, live=True) == {"priority": 6, "paused": True}


def test_a_finished_campaign_reports_no_standing(cluster, tmp_path):
    """Its entry is dropped when it ends, so a rank reported for it would describe
    something nothing can act on."""
    assert cluster._scheduling_for("camp-old", live=False) == {"priority": 0, "paused": False}


# -- over HTTP ----------------------------------------------------------------------------
#
# The route and the transport are a pair: an omitted half must stay off the wire, and
# ``paused=False`` must reach the service as a value rather than being dropped with the
# Nones -- otherwise resuming a campaign would do nothing at all.

class _RecordingImpl(LocalTransport):
    """Records the call instead of touching a queue."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.seen = None

    def set_campaign_scheduling(self, campaign_id, priority=None, paused=None):
        from robovast.service.interface import ActionResult
        self.seen = (campaign_id, priority, paused)
        return ActionResult(ok=True, message="recorded")


@pytest.fixture
def http(tmp_path):
    from starlette.testclient import TestClient

    from robovast.service.app import build_app
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tmp_path / "workspaces"))
    impl = _RecordingImpl(store=store)
    app = build_app(impl, mount_mcp=False, auth_token="t")
    return impl, TestClient(app, headers={"Authorization": "Bearer t"})


def test_the_route_parses_a_rank(http):
    impl, client = http
    resp = client.post("/campaigns/camp-1/scheduling", params={"priority": -2})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert impl.seen == ("camp-1", -2, None), "the untouched half must stay untouched"


def test_the_route_parses_a_resume_rather_than_ignoring_it(http):
    """``false`` must arrive as a value, not be read as an omission, or resume does nothing."""
    impl, client = http
    client.post("/campaigns/camp-1/scheduling", params={"paused": "false"})
    assert impl.seen == ("camp-1", None, False)


def test_the_route_parses_a_pause(http):
    impl, client = http
    client.post("/campaigns/camp-1/scheduling", params={"paused": "true"})
    assert impl.seen == ("camp-1", None, True)


def test_the_transport_leaves_an_omitted_half_off_the_wire():
    """``_post`` drops the ``None``s and keeps ``False`` -- the contract this relies on."""
    from robovast.service.http_client import HTTPTransport

    sent = {}

    class _Transport(HTTPTransport):
        def __init__(self):  # no session, no base url: only the query is under test
            pass

        def _post(self, route, json=None, *, timeout=None, **params):
            sent["route"] = route
            sent["query"] = {k: v for k, v in params.items() if v is not None}
            return {"ok": True, "message": ""}

    _Transport().set_campaign_scheduling("camp-1", paused=False)
    assert sent["route"] == "/campaigns/camp-1/scheduling"
    assert sent["query"] == {"paused": False}
