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

    with mock.patch.object(kubernetes_kueue.config, "load_incluster_config"), \
         mock.patch.object(kubernetes_kueue.client, "CoreV1Api") as api:
        api.return_value.list_node.return_value = node_list
        with pytest.raises(RuntimeError, match="No allocatable CPU"):
            kubernetes_kueue.get_cluster_allocatable_resources()


def test_kueue_quota_raises_when_query_fails():
    """A failed node query must raise, not fall back to a hard-coded quota."""
    from robovast.execution.cluster_execution import kubernetes_kueue

    with mock.patch.object(kubernetes_kueue.config, "load_incluster_config"), \
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
