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
    """A MultiBackendService whose two lanes share one store rooted at ``tmp_path``."""
    store = WorkspaceStore(registry=WorkspaceRegistry(root=str(tmp_path)))
    cluster = ClusterService(namespace="ns", cluster_config_name="x",
                             cluster_config_kwargs={}, store=store,
                             reap_on_start=False)
    return MultiBackendService(cluster, store=store)


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
