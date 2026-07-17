# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ClusterService's launch behaviour (no cluster needed).

The service drives cluster campaigns **in-process** (one worker thread each) over a
KubernetesBackend; there is no per-campaign controller pod any more, so these cover
the launch *hooks* it overrides on LocalTransport plus the aux-pod manifest that
replaced the old controller-pod sidecar.
"""

import tempfile

import pytest

from robovast.execution.cluster_execution.container_runner import (
    AUX_LABEL, DEFAULT_AUX_DEADLINE_SECONDS, aux_pod_name,
    build_aux_pod_manifest)
from robovast.common.variation.container_runner import ContainerSpec
from robovast.service.cluster_service import ClusterService
from robovast.service.interface import CreateCampaignRequest
from robovast.service.workspaces import WorkspaceRegistry, WorkspaceStore


@pytest.fixture
def cs():
    store = WorkspaceStore(registry=WorkspaceRegistry(root=tempfile.mkdtemp()))
    # reap_on_start=False: the reaper talks to the kube API, which no test has.
    return ClusterService(namespace="ns1", cluster_config_name="rke2",
                          cluster_config_kwargs={"foo": "bar"},
                          image="example/robovast:test", store=store,
                          reap_on_start=False)


def test_version_reports_kubernetes_backend(cs):
    assert cs.version().backend == "kubernetes"


def test_cluster_config_requires_name():
    cs = ClusterService(namespace="ns", cluster_config_name=None,
                        cluster_config_kwargs={}, reap_on_start=False)
    with pytest.raises(ValueError, match="cluster config not configured"):
        cs._cluster_config()


# -- launch hooks -----------------------------------------------------------

def test_campaigns_run_in_parallel(cs):
    """Unlike local Docker, the cluster has no single-flight guard."""
    assert cs._guard_new_campaign() is None


def test_run_options_carry_postprocess_out_of_band(cs):
    """postprocess travels in the options, not the process env.

    One service process drives many campaigns, so an env var could not tell them
    apart — that is why RunOptions gained these fields.
    """
    opts = cs._run_options(CreateCampaignRequest(workspace_id="ws-x", postprocess=True))
    assert opts.postprocess is True
    assert opts.namespace == "ns1"
    assert opts.controller_image == "example/robovast:test"

    off = cs._run_options(CreateCampaignRequest(workspace_id="ws-x", postprocess=False))
    assert off.postprocess is False


def test_postprocessing_is_chained_by_the_builder_not_the_worker(cs):
    """So data.db rides the campaign's existing upload rather than a second one."""
    assert cs._postprocess_in_process() is False


def test_unknown_campaign_status_falls_back_to_object_store(cs, monkeypatch):
    """A campaign this process is not driving is explained from the durable home."""
    monkeypatch.setattr(ClusterService, "_read_outcome", lambda self, cid: None)
    status = cs._status_from_disk("nope-2026-07-17-120000")
    assert status.phase == "unknown"
    assert status.campaign_id == "nope-2026-07-17-120000"


# -- aux pod (replaces the controller-pod sidecar) --------------------------

def _spec():
    return ContainerSpec(image="ghcr.io/secorolab/scenery_builder:1.2",
                         command_prefix=["/entry.sh"],
                         keep_alive_command=["sleep", "infinity"],
                         env={"A": "1"}, run_as_user="1000:1000")


def test_aux_pod_manifest_shape():
    m = build_aux_pod_manifest("nav-2026-07-17-120000", [_spec()], "ns1")
    assert m["kind"] == "Pod"
    assert m["metadata"]["name"] == aux_pod_name("nav-2026-07-17-120000")
    assert m["metadata"]["namespace"] == "ns1"
    assert m["metadata"]["labels"]["app"] == "robovast-aux"
    assert AUX_LABEL == "app=robovast-aux"
    spec = m["spec"]
    assert spec["restartPolicy"] == "Never"
    # Backstop so a leaked aux pod always dies by itself.
    assert spec["activeDeadlineSeconds"] == DEFAULT_AUX_DEADLINE_SECONDS
    c = spec["containers"][0]
    assert c["image"] == "ghcr.io/secorolab/scenery_builder:1.2"
    # The image's one-shot entrypoint is overridden so it stays up for the campaign.
    assert c["command"] == ["sleep", "infinity"]
    assert c["env"] == [{"name": "A", "value": "1"}]
    assert c["securityContext"] == {"runAsUser": 1000}


def test_aux_pod_is_labelled_per_campaign():
    """So concurrent campaigns' aux pods never collide and cleanup can target one."""
    a = build_aux_pod_manifest("camp-a-2026-07-17-120000", [_spec()], "ns")
    b = build_aux_pod_manifest("camp-b-2026-07-17-120000", [_spec()], "ns")
    assert a["metadata"]["name"] != b["metadata"]["name"]
    assert (a["metadata"]["labels"]["campaign-id"]
            != b["metadata"]["labels"]["campaign-id"])


def test_aux_pod_owner_reference_ties_it_to_the_service_pod():
    """K8s then GCs it if the service is replaced — the sidecar's old guarantee."""
    owner = {"apiVersion": "v1", "kind": "Pod", "name": "robovast-service-x",
             "uid": "abc", "controller": False, "blockOwnerDeletion": False}
    m = build_aux_pod_manifest("c-2026-07-17-120000", [_spec()], "ns", owner_ref=owner)
    assert m["metadata"]["ownerReferences"] == [owner]
