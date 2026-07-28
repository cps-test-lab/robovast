# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Routing tests for MultiBackendService (no Docker / no cluster needed).

The dual-lane service subclasses LocalTransport (the local lane) and holds one
ClusterService (the cluster lane). These cover which lane each call is routed to —
by ``request.backend`` for a new campaign, by ``_lane_for`` for an existing one —
plus lane advertisement and the shared-store invariant. The lane implementations
themselves are stubbed, so nothing touches Docker or Kubernetes.
"""

import tempfile

import pytest

from robovast.service.cluster_service import ClusterService
from robovast.service.interface import (CampaignRef, CreateCampaignRequest,
                                        ResourceUsage)
from robovast.service.local_transport import LocalTransport
from robovast.service.multi_backend import MultiBackendService
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


def _make(tmp_path):
    """A MultiBackendService whose two lanes share one store rooted at ``tmp_path``.

    The campaigns root is pinned to ``tmp_path`` as well. It is *not* derived from the
    store: :meth:`LocalTransport._campaigns_root` prefers an initialized CWD project's
    ``results_dir``, so on a developer machine that has one, these tests wrote their
    marker files (``camp-2026-01-01-000000/_execution/backend``) into that real results
    directory — polluting it with a fake campaign that then showed up in ``vast``
    listings, and failing on the second run with ``FileExistsError`` because the
    leftover was still there.
    """
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path)))
    cluster = ClusterService(namespace="ns", cluster_config_name="x",
                             cluster_config_kwargs={}, store=store,
                             reap_on_start=False)
    svc = MultiBackendService(cluster, store=store)
    root = tmp_path / "campaigns"
    root.mkdir(exist_ok=True)
    svc._campaigns_root = lambda: root
    cluster._campaigns_root = lambda: root
    # Seed the cluster lane's campaign-index cache empty: discovery reads the object
    # store, and off-cluster that opens a kubectl port-forward no test has. Tests about
    # index-based routing set it explicitly.
    import time as _time
    cluster._index_cache = (_time.monotonic(), {})
    return svc


def test_requires_shared_store(tmp_path):
    """A split store would hide each lane's campaigns from the other's list/status."""
    store_a = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "a")))
    store_b = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path / "b")))
    cluster = ClusterService(namespace="ns", cluster_config_name="x",
                             cluster_config_kwargs={}, store=store_b,
                             reap_on_start=False)
    with pytest.raises(ValueError, match="share this service's store"):
        MultiBackendService(cluster, store=store_a)


def test_version_advertises_both_lanes(tmp_path):
    assert _make(tmp_path).version().backends == ["local", "cluster"]


def test_create_defaults_to_cluster(tmp_path, monkeypatch):
    svc = _make(tmp_path)
    seen = []
    monkeypatch.setattr(LocalTransport, "create_campaign",
                        lambda self, r: seen.append("local") or CampaignRef(campaign_id="loc"))
    monkeypatch.setattr(ClusterService, "create_campaign",
                        lambda self, r: seen.append("cluster") or CampaignRef(campaign_id="clu"))
    ref = svc.create_campaign(CreateCampaignRequest(workspace_id=""))
    assert seen == ["cluster"] and ref.campaign_id == "clu"
    assert svc._lane_for("clu") == "cluster"


def test_create_explicit_local(tmp_path, monkeypatch):
    svc = _make(tmp_path)
    monkeypatch.setattr(LocalTransport, "create_campaign",
                        lambda self, r: CampaignRef(campaign_id="loc"))
    monkeypatch.setattr(ClusterService, "create_campaign",
                        lambda self, r: CampaignRef(campaign_id="clu"))
    ref = svc.create_campaign(CreateCampaignRequest(workspace_id="", backend="local"))
    assert ref.campaign_id == "loc"
    assert svc._lane_for("loc") == "local"


def test_create_rejects_unknown_backend(tmp_path):
    with pytest.raises(ValueError, match="unknown backend"):
        _make(tmp_path).create_campaign(
            CreateCampaignRequest(workspace_id="", backend="gpu"))


def test_lane_for_unknown_defaults_local(tmp_path):
    assert _make(tmp_path)._lane_for("never-seen") == "local"


def test_lane_for_reads_disk_marker_after_map_cleared(tmp_path):
    """A campaign from before a restart resolves via its persisted _execution/backend."""
    svc = _make(tmp_path)
    cid = "camp-2026-01-01-000000"
    marker = svc._marker_path(cid)
    marker.parent.mkdir(parents=True)
    marker.write_text("cluster", encoding="utf-8")
    # Nothing in the in-memory map (simulating a fresh process) -> read the marker.
    assert svc._lane_for(cid) == "cluster"


def test_per_campaign_call_routes_to_owning_lane(tmp_path, monkeypatch):
    svc = _make(tmp_path)
    svc._lane_map.update({"clu": "cluster", "loc": "local"})
    seen = []
    monkeypatch.setattr(LocalTransport, "get_status",
                        lambda self, cid: seen.append(("local", cid)))
    monkeypatch.setattr(ClusterService, "get_status",
                        lambda self, cid: seen.append(("cluster", cid)))
    svc.get_status("clu")
    svc.get_status("loc")
    assert seen == [("cluster", "clu"), ("local", "loc")]


def test_data_status_routes_so_each_lane_answers_for_itself(tmp_path, monkeypatch):
    """Whether a query transfers anything *is* the per-lane difference.

    Answering it in the router would report one lane's cost for both: a local-lane campaign
    in this service fetches nothing, while a cluster-lane one may move hundreds of MB.
    """
    svc = _make(tmp_path)
    svc._lane_map.update({"clu": "cluster", "loc": "local"})
    monkeypatch.setattr(ClusterService, "campaign_data_status",
                        lambda self, cid: "cluster-answer")

    assert svc.campaign_data_status("clu") == "cluster-answer"
    local = svc.campaign_data_status("loc")
    assert local.fetch_required is False and local.transfer == "none"


def test_resource_usage_routes_and_defaults_cluster(tmp_path, monkeypatch):
    svc = _make(tmp_path)
    seen = []

    def usage(backend_name):
        def _u(self, backend=None):
            seen.append(backend_name)
            return ResourceUsage(backend=backend_name, cpu_capacity=1, cpu_used=0,
                                 memory_capacity_bytes=1, memory_used_bytes=0,
                                 parallel_runs=False)
        return _u
    monkeypatch.setattr(LocalTransport, "resource_usage", usage("docker"))
    monkeypatch.setattr(ClusterService, "resource_usage", usage("kubernetes"))

    svc.resource_usage("local")
    svc.resource_usage()  # default -> cluster
    assert seen == ["docker", "kubernetes"]
    with pytest.raises(ValueError, match="unknown backend"):
        svc.resource_usage("gpu")


def test_lists_a_cluster_campaign_before_it_has_a_directory(tmp_path):
    """A cluster campaign is listable from the instant it is accepted.

    ``list_campaigns`` builds its id set from the *local* lane's "disk ∪ in-memory"
    view, so a campaign registered in the cluster lane's registry used to be missing
    from every listing until its results directory appeared on disk — the whole length
    of the lane's pre-flight (project push, image build). A caller whose start call
    timed out in that window polled every read path, was told the campaign did not
    exist, and retried into a duplicate.
    """
    svc = _make(tmp_path)
    cid = "camp-2026-02-02-000000"

    class _Entry:
        created_at = None
        description = ""

        class state:
            @staticmethod
            def snapshot():
                from robovast.common.status import Status
                return Status(phase="initializing", campaign_id=cid)

    with svc._cluster._lock:
        svc._cluster._campaigns[cid] = _Entry()

    listed = svc.list_campaigns()
    assert [c.campaign_id for c in listed.campaigns] == [cid]
    assert listed.campaigns[0].phase == "initializing"
    assert not (tmp_path / "campaigns" / cid).exists()


def test_pre_flight_cluster_campaign_is_listed_first(tmp_path):
    """A just-accepted cluster campaign heads the list, before its store exists.

    Ordering is by recorded start time, which for a pre-flight campaign lives only in
    the *cluster* lane's registry — the local lane knows neither an in-memory entry nor
    a campaign.db. Without ``_started_at_for`` consulting that lane, the newest campaign
    would sort last, behind every finished one.
    """
    from datetime import datetime, timezone

    from robovast.common.store import STORE_FILENAME, CampaignStore

    svc = _make(tmp_path)
    older = "zzz-2026-02-01-000000"
    cdir = svc._campaigns_root() / older
    cdir.mkdir(parents=True)
    with CampaignStore(cdir / STORE_FILENAME) as store:
        store.create_campaign(older, {}, mode="batch", created_at=1_000.0)

    cid = "aaa-2026-02-02-000000"

    class _Entry:
        created_at = datetime.now(timezone.utc).isoformat()
        description = ""

        class state:
            @staticmethod
            def snapshot():
                from robovast.common.status import Status
                return Status(phase="initializing", campaign_id=cid)

    with svc._cluster._lock:
        svc._cluster._campaigns[cid] = _Entry()

    assert [c.campaign_id for c in svc.list_campaigns().campaigns] == [cid, older]


def test_an_indexed_campaign_routes_to_the_cluster_lane(tmp_path):
    """A cluster campaign whose local scratch is gone has neither an in-memory entry nor
    an ``_execution/backend`` marker on disk, so lane resolution used to fall through to
    the historical local default — the one lane that cannot read its records, which then
    reported ``unknown`` with no counts. Its presence in the object store's index is the
    durable answer.
    """
    import time as _time
    from robovast.service.multi_backend import _CLUSTER, _LOCAL

    svc = _make(tmp_path)
    cid = "camp-2026-01-01-000000"
    assert svc._lane_for(cid) == _LOCAL          # not indexed: unchanged behaviour

    svc._cluster._index_cache = (_time.monotonic(), {cid: "2026-01-01T00:00:00+00:00"})
    assert svc._lane_for(cid) == _CLUSTER


def test_an_indexed_campaign_is_ordered_by_its_recorded_start_time(tmp_path):
    """The counterpart of routing: the inherited helper knows only the local registry, so
    an index-only campaign would have no start time and sort last — exactly the campaign
    a caller is looking for."""
    import time as _time

    svc = _make(tmp_path)
    cid = "camp-2026-01-01-000000"
    svc._cluster._index_cache = (_time.monotonic(), {cid: "2026-01-01T00:00:00+00:00"})
    assert svc._started_at_for(cid) == "2026-01-01T00:00:00+00:00"


def test_the_routers_listing_includes_the_cluster_lanes_stored_campaigns(tmp_path):
    import time as _time

    svc = _make(tmp_path)
    cid = "camp-2026-01-01-000000"
    svc._cluster._index_cache = (_time.monotonic(), {cid: "2026-01-01T00:00:00+00:00"})
    assert cid in svc._durable_campaign_ids()
