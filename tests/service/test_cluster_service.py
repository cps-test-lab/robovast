# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ClusterService pod-manifest generation (no cluster needed)."""

import json
import tempfile

import pytest

from robovast.service.cluster_service import ClusterService
from robovast.service.interface import CreateCampaignRequest
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def cs():
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tempfile.mkdtemp()))
    return ClusterService(namespace="ns1", cluster_config_name="rke2",
                          cluster_config_kwargs={"foo": "bar"},
                          image="example/robovast:test", store=store)


def test_version_reports_kubernetes_backend(cs):
    assert cs.version().backend == "kubernetes"


def test_controller_pod_manifest_shape(cs):
    req = CreateCampaignRequest(workspace_id="ws-x", config_filter="h*", runs=3)
    m = cs._controller_pod_manifest("nav-2026-07-16-120000", "bkt",
                                    "pre/_staging_inputs", req)
    assert m["kind"] == "Pod"
    assert m["metadata"]["namespace"] == "ns1"
    assert m["metadata"]["labels"]["app"] == "robovast-controller"
    spec = m["spec"]
    assert spec["restartPolicy"] == "Never"
    assert spec["serviceAccountName"] == "robovast-controller"
    c = spec["containers"][0]
    assert c["image"] == "example/robovast:test"
    assert c["command"] == ["python", "-m", "robovast.execution.cluster_bootstrap"]
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env["ROBOVAST_CAMPAIGN_ID"] == "nav-2026-07-16-120000"
    assert env["ROBOVAST_STAGING_BUCKET"] == "bkt"
    assert env["ROBOVAST_STAGING_PREFIX"] == "pre/_staging_inputs"
    assert env["ROBOVAST_RUNS"] == "3"
    assert env["ROBOVAST_CONFIG_FILTER"] == "h*"
    assert json.loads(env["ROBOVAST_CLUSTER_CONFIG_KWARGS"]) == {"foo": "bar"}


def test_optional_env_omitted_when_defaulted(cs):
    req = CreateCampaignRequest(workspace_id="ws-x")  # runs=1, no filter
    m = cs._controller_pod_manifest("c-1", "b", "p", req)
    env = {e["name"] for e in m["spec"]["containers"][0]["env"]}
    assert "ROBOVAST_CONFIG_FILTER" not in env


def test_cluster_config_requires_name():
    cs = ClusterService(namespace="ns", cluster_config_name=None,
                        cluster_config_kwargs={})
    with pytest.raises(ValueError, match="cluster config not configured"):
        cs._cluster_config()
