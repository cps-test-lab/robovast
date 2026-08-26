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
    from robovast.execution.cluster_execution.container_runner import AuxDiscoveryError

    completed = mock.Mock(returncode=1, stdout="boom", stderr="traceback here")
    with mock.patch.object(container_runner.subprocess, "run", return_value=completed):
        with pytest.raises(AuxDiscoveryError, match="exit 1"):
            container_runner._discover_specs_subprocess("/nonexistent/campaign.vast")


def test_aux_discovery_subprocess_no_result_raises():
    """A worker that exits 0 but writes no result file must abort, not yield []."""
    from robovast.execution.cluster_execution import container_runner
    from robovast.execution.cluster_execution.container_runner import AuxDiscoveryError

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

    from robovast.execution.cluster_execution.kube_client import load_kube_config
    with mock.patch.object(kc, "load_incluster_config",
                           side_effect=kc.ConfigException("not in cluster")), \
         mock.patch.object(kc, "load_kube_config",
                           side_effect=Exception("no kubeconfig")):
        with pytest.raises(RuntimeError, match="no Kubernetes configuration available"):
            load_kube_config(context="x")


def test_load_kube_config_prefers_in_cluster():
    from kubernetes import config as kc

    from robovast.execution.cluster_execution.kube_client import load_kube_config
    with mock.patch.object(kc, "load_incluster_config", return_value=None):
        assert load_kube_config() == "in-cluster"
# -- A6: postprocessing survives an unreachable cluster ----------------------
#
# The Kueue admission preflight that used to guard this path is gone, but the obligation
# it happened to carry is not: postprocessing chains AFTER the runs are published, so a
# cluster that has gone away must be a reported, re-runnable failure rather than an
# exception out of the conversion step. The load-bearing detail is that the very first
# call touching the API server is the one that has to translate the transport error.

def test_unreachable_cluster_only_ends_postprocessing():
    """The runs are already published when postprocessing chains, so an unreachable
    cluster is a reported, re-runnable postprocessing failure -- never an exception out
    of the conversion step."""
    import urllib3.exceptions

    from robovast.execution.cluster_execution import postprocess_job

    cluster_config = mock.Mock()
    cluster_config.get_s3_credentials.return_value = ("key", "secret")
    boom = urllib3.exceptions.MaxRetryError(
        pool=mock.Mock(), url="/api/v1/namespaces/ns/configmaps",
        reason=urllib3.exceptions.ConnectTimeoutError("connect timed out"))
    core = mock.Mock()
    core.create_namespaced_config_map.side_effect = boom

    with mock.patch("robovast.execution.cluster_execution.in_pod_storage."
                    "campaign_storage_location", return_value=("bucket", "prefix/")), \
         mock.patch("robovast.execution.cluster_execution.kube_client.load_kube_config"), \
         mock.patch("kubernetes.client.CoreV1Api", return_value=core), \
         mock.patch("kubernetes.client.BatchV1Api"), \
         mock.patch("robovast.execution.cluster_execution.cluster_execution."
                    "resolve_pull_secret", return_value=""):
        ok, message = postprocess_job.run_conversion_job(
            cluster_config, "camp", "ns", "img", [{"plugins": []}])

    assert ok is False
    assert "unreachable" in message
    # One sentence, not a urllib3 traceback: the transport failing IS the whole fact.
    assert "MaxRetryError" not in message
