# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the no-fallback contract of the cluster execution path.

Each case pins a spot that used to silently degrade (swallow an error and proceed
with a quietly-wrong configuration) and asserts it now fails loudly instead.
"""

from unittest import mock

import pytest


# -- A1: aux-container discovery ---------------------------------------------

def test_aux_discovery_subprocess_failure_raises():
    """A plugin-discovery subprocess that exits non-zero must abort, not yield []."""
    from robovast.execution.cluster_execution import container_runner
    from robovast.execution.cluster_execution.container_runner import \
        AuxDiscoveryError

    completed = mock.Mock(returncode=1, stdout="boom", stderr="traceback here")
    with mock.patch.object(container_runner.subprocess, "run", return_value=completed):
        with pytest.raises(AuxDiscoveryError, match="exit 1"):
            container_runner._discover_specs_subprocess("/nonexistent/campaign.vast")


def test_aux_discovery_subprocess_no_result_raises():
    """A worker that exits 0 but writes no result file must abort, not yield []."""
    from robovast.execution.cluster_execution import container_runner
    from robovast.execution.cluster_execution.container_runner import \
        AuxDiscoveryError

    completed = mock.Mock(returncode=0, stdout="", stderr="")
    with mock.patch.object(container_runner.subprocess, "run", return_value=completed):
        with pytest.raises(AuxDiscoveryError, match="no readable result"):
            container_runner._discover_specs_subprocess("/nonexistent/campaign.vast")


def test_aux_discovery_variation_error_propagates():
    """A variation whose container requirement can't be computed aborts discovery."""
    from robovast.execution.cluster_execution import container_runner

    class _Boom:
        __name__ = "BoomVariation"

        @staticmethod
        def get_required_container(_params):
            raise RuntimeError("cannot compute container spec")

    # load_config / ensure_workspace_plugins / _get_variation_classes are imported
    # inside _discover_specs, so patch them at their defining modules.
    with mock.patch(
        "robovast.common.common.load_config",
        return_value={"configuration": [{"variations": {}}]},
    ), mock.patch(
        "robovast.common.config_plugins.ensure_workspace_plugins"
    ), mock.patch(
        "robovast.common.config_generation._get_variation_classes",
        return_value=[(_Boom, {})],
    ):
        with pytest.raises(RuntimeError, match="cannot compute container spec"):
            container_runner._discover_specs("/tmp/campaign.vast")


# -- A2: Kueue quota ----------------------------------------------------------

def test_kueue_quota_raises_when_no_allocatable_cpu():
    """Zero allocatable CPU must raise, not silently provision a tiny default quota."""
    from robovast.execution.cluster_execution import kubernetes_kueue

    node = mock.Mock()
    node.status.allocatable = {"cpu": "0", "memory": "0"}
    node_list = mock.Mock(items=[node])

    with mock.patch("robovast.common.kube.load_kube_config"), \
         mock.patch.object(kubernetes_kueue.client, "CoreV1Api") as api:
        api.return_value.list_node.return_value = node_list
        with pytest.raises(RuntimeError, match="No allocatable CPU"):
            kubernetes_kueue.get_cluster_allocatable_resources()


def test_kueue_quota_raises_when_query_fails():
    """A failed node query must raise, not fall back to a hard-coded quota."""
    from robovast.execution.cluster_execution import kubernetes_kueue

    with mock.patch("robovast.common.kube.load_kube_config"), \
         mock.patch.object(kubernetes_kueue.client, "CoreV1Api") as api:
        api.return_value.list_node.side_effect = RuntimeError("api unreachable")
        with pytest.raises(RuntimeError, match="Failed to query cluster resources"):
            kubernetes_kueue.get_cluster_allocatable_resources()


def test_kueue_default_quota_constants_removed():
    """The silent fallback quotas must not exist anymore."""
    from robovast.execution.cluster_execution import kubernetes_kueue

    assert not hasattr(kubernetes_kueue, "DEFAULT_CPU_QUOTA")
    assert not hasattr(kubernetes_kueue, "DEFAULT_MEMORY_QUOTA")


# -- A3: CPU manager policy ---------------------------------------------------

def test_cpu_manager_policy_unknown_on_query_failure():
    """A configz read failure is *unknown* (None), never silently reported as "none"."""
    from robovast.common import execution

    client = mock.Mock()
    client.connect_get_node_proxy_with_path.side_effect = RuntimeError("no configz")
    assert execution._check_static_cpu_manager(client, "node-1") is None


def test_cpu_manager_policy_reads_value_when_available():
    from robovast.common import execution

    client = mock.Mock()
    client.connect_get_node_proxy_with_path.return_value = \
        '{"kubeletconfig": {"cpuManagerPolicy": "static"}}'
    assert execution._check_static_cpu_manager(client, "node-1") == "static"


# -- A5: single kube-config loader -------------------------------------------

def test_load_kube_config_raises_when_no_source():
    """Neither in-cluster nor host config available must raise, not proceed silently."""
    from kubernetes import config as kc

    from robovast.common.kube import load_kube_config
    with mock.patch.object(kc, "load_incluster_config",
                           side_effect=kc.ConfigException("not in cluster")), \
         mock.patch.object(kc, "load_kube_config",
                           side_effect=Exception("no kubeconfig")):
        with pytest.raises(RuntimeError, match="no Kubernetes configuration available"):
            load_kube_config(context="x")


def test_load_kube_config_prefers_in_cluster():
    from kubernetes import config as kc

    from robovast.common.kube import load_kube_config
    with mock.patch.object(kc, "load_incluster_config", return_value=None):
        assert load_kube_config() == "in-cluster"


# -- A6: Kueue admission path ------------------------------------------------
#
# Every scenario/postprocess Job is labelled into a Kueue LocalQueue, so a broken
# admission path does not fail the submit — Kueue just suspends the jobs, with no pod,
# forever. activeDeadlineSeconds cannot rescue them (its timer does not run while
# suspended), so the batch used to spin "still running" indefinitely.

def _kueue_api(local_queue=None, cluster_queue=None, missing=(), forbidden=()):
    """A CustomObjectsApi double returning the given objects.

    Names in *missing* raise 404, names in *forbidden* raise 403.
    """
    from kubernetes.client import rest

    def _maybe_raise(name):
        if name in forbidden:
            raise rest.ApiException(status=403, reason="Forbidden")
        if name in missing:
            raise rest.ApiException(status=404, reason="Not Found")

    api = mock.Mock()

    def get_namespaced(group, version, plural, namespace, name):
        _maybe_raise(name)
        return local_queue

    def get_cluster(group, version, plural, name):
        _maybe_raise(name)
        return cluster_queue

    api.get_namespaced_custom_object.side_effect = get_namespaced
    api.get_cluster_custom_object.side_effect = get_cluster
    return api


def _verify(api, **kwargs):
    from robovast.execution.cluster_execution import kubernetes_kueue
    with mock.patch.object(kubernetes_kueue.client, "CustomObjectsApi",
                           return_value=api), \
         mock.patch("robovast.common.kube.load_kube_config"):
        return kubernetes_kueue.verify_kueue_admission_ready(namespace="ns", **kwargs)


_LQ = {"spec": {"clusterQueue": "robovast-cluster-queue"}}
_CQ_OK = {"spec": {}, "status": {"conditions": [{"type": "Active", "status": "True"}]}}


def test_missing_cluster_queue_fails_loudly():
    """The observed incident: LocalQueue present, ClusterQueue gone. Jobs sat forever
    and only `kubectl get workloads` revealed why."""
    from robovast.common.errors import CampaignConfigError

    api = _kueue_api(local_queue=_LQ, missing=("robovast-cluster-queue",))
    with pytest.raises(CampaignConfigError, match="robovast-cluster-queue"):
        _verify(api)


def test_missing_local_queue_fails_loudly():
    from robovast.common.errors import CampaignConfigError

    api = _kueue_api(missing=("robovast",))
    with pytest.raises(CampaignConfigError, match="LocalQueue"):
        _verify(api)


def test_held_cluster_queue_fails_loudly():
    """A cleanup that died mid-way used to leave stopPolicy=Hold behind, which suspends
    every later campaign exactly like a missing queue."""
    from robovast.common.errors import CampaignConfigError

    api = _kueue_api(local_queue=_LQ, cluster_queue={"spec": {"stopPolicy": "Hold"}})
    with pytest.raises(CampaignConfigError, match="stopped"):
        _verify(api)


def test_inactive_cluster_queue_fails_loudly():
    """Kueue reports an unusable queue (e.g. missing ResourceFlavor) as Active=False
    rather than by deleting anything, so every object still looks present."""
    from robovast.common.errors import CampaignConfigError

    cq = {"spec": {}, "status": {"conditions": [
        {"type": "Active", "status": "False", "reason": "FlavorNotFound",
         "message": "flavor default-flavor not found"}]}}
    api = _kueue_api(local_queue=_LQ, cluster_queue=cq)
    with pytest.raises(CampaignConfigError, match="FlavorNotFound"):
        _verify(api)


def test_healthy_queue_passes():
    assert _verify(_kueue_api(local_queue=_LQ, cluster_queue=_CQ_OK)) is None


def test_queue_with_no_status_yet_passes():
    """Right after `cluster setup` the Active condition may not be published yet; an
    absent condition must not be read as a broken queue."""
    assert _verify(_kueue_api(local_queue=_LQ, cluster_queue={"spec": {}})) is None


def test_forbidden_read_is_not_reported_as_missing():
    """A missing RBAC grant must never masquerade as a broken queue — the two demand
    opposite responses, and refusing to run on a 403 would be worse than the hang."""
    from robovast.execution.cluster_execution.kubernetes_kueue import \
        KueueCheckUnavailable

    api = _kueue_api(local_queue=_LQ, forbidden=("robovast",))
    with pytest.raises(KueueCheckUnavailable):
        _verify(api)


def test_quota_exhaustion_is_not_a_failure():
    """Quota exhaustion is Kueue's normal state — every cluster user meets it — so a
    healthy but busy queue must keep waiting, not fail the campaign."""
    busy = {"spec": {"resourceGroups": [{"flavors": [{"resources": [
        {"name": "cpu", "nominalQuota": "1"}]}]}]},
        "status": {"conditions": [{"type": "Active", "status": "True"}],
                   "pendingWorkloads": 5, "admittedWorkloads": 0}}
    assert _verify(_kueue_api(local_queue=_LQ, cluster_queue=busy)) is None


# -- A7: the shared ClusterQueue is not held for one campaign's cleanup -------

def _run_cleanup(campaign):
    """Run cleanup_cluster_campaign with every deletion stubbed; returns the
    stopPolicy values written to the shared ClusterQueue."""
    from robovast.execution.cluster_execution import cluster_execution
    written = []
    with mock.patch.object(cluster_execution, "_cleanup_cluster_campaign_resources"), \
         mock.patch("robovast.execution.cluster_execution.kubernetes_kueue."
                    "set_cluster_queue_stop_policy",
                    side_effect=lambda p, **kw: written.append(p)), \
         mock.patch("robovast.execution.cluster_execution.kubernetes_kueue."
                    "_queue_object", return_value={"spec": {}}):
        cluster_execution.cleanup_cluster_campaign(namespace="ns", campaign=campaign)
    return written


def test_per_campaign_cleanup_leaves_the_shared_queue_alone():
    """stopPolicy lives on ONE cluster-scoped ClusterQueue shared by every campaign, so
    holding it to delete one campaign's jobs stops every *other* campaign being
    admitted for the length of the cleanup."""
    assert _run_cleanup("camp-a") == []


def test_cluster_wide_cleanup_holds_and_restores():
    assert _run_cleanup(None) == ["Hold", None]


def test_hold_is_restored_even_when_cleanup_raises():
    """A queue left held is worse than a failed cleanup: the failure is visible, the
    held queue is not — it just suspends every future campaign forever."""
    from robovast.execution.cluster_execution import cluster_execution
    written = []
    with mock.patch.object(cluster_execution, "_cleanup_cluster_campaign_resources",
                           side_effect=RuntimeError("deletion blew up")), \
         mock.patch("robovast.execution.cluster_execution.kubernetes_kueue."
                    "set_cluster_queue_stop_policy",
                    side_effect=lambda p, **kw: written.append(p)), \
         mock.patch("robovast.execution.cluster_execution.kubernetes_kueue."
                    "_queue_object", return_value={"spec": {}}):
        with pytest.raises(RuntimeError, match="deletion blew up"):
            cluster_execution.cleanup_cluster_campaign(namespace="ns", campaign=None)
    assert written == ["Hold", None]


def test_pre_existing_hold_is_preserved():
    """A concurrent teardown (or a deliberate manual hold) must survive: restore what
    was there, don't force None."""
    from robovast.execution.cluster_execution import cluster_execution
    written = []
    with mock.patch.object(cluster_execution, "_cleanup_cluster_campaign_resources"), \
         mock.patch("robovast.execution.cluster_execution.kubernetes_kueue."
                    "set_cluster_queue_stop_policy",
                    side_effect=lambda p, **kw: written.append(p)), \
         mock.patch("robovast.execution.cluster_execution.kubernetes_kueue."
                    "_queue_object", return_value={"spec": {"stopPolicy": "Hold"}}):
        cluster_execution.cleanup_cluster_campaign(namespace="ns", campaign=None)
    assert written == ["Hold", "Hold"]
