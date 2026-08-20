# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``cluster setup`` takes its node labels only from an explicitly named ``.vast``.

Setup deploys into a cluster and runs from any directory, so a ``.robovast_project``
must neither be a precondition nor an input: it is found by walking *up* to the
filesystem root, so a project one directory — or ten — above an unrelated CWD would
otherwise decide which nodes a cluster's pods may run on. Only ``vast -V <file>``
names the config; a named config that cannot be read still fails loudly.
"""

import json
from unittest import mock

import pytest

from robovast.execution.cluster_execution import buildkitd_deploy
from robovast.execution.cluster_execution import cluster_setup
from robovast.execution.cluster_execution.cluster_setup import (
    get_kubernetes_node_labels_from_config, setup_server)

_JOBS = {'node-pool': 'primary'}
_CONTROL = {'node-pool': 'extra'}

_VAST = """version: 2
execution:
  containers: {scenario: {image: i}}
  runs: 1
  kubernetes:
    jobs:
      node_labels:
        node-pool: primary
    control:
      node_labels:
        node-pool: extra
"""


@pytest.fixture(name="deploy_stubs")
def _deploy_stubs(monkeypatch):
    """Stub out everything ``setup_server`` touches outside label resolution.

    Returns the ``apply_kueue_queues`` and ``setup_cluster`` mocks, which is where the
    resolved job / control labels land.
    """
    from robovast.execution.cluster_execution import service_deploy

    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))
    monkeypatch.setattr(service_deploy, "deploy_service", mock.Mock())
    # Setup now waits for the pod to be Ready before reporting success; with the
    # deploy stubbed there is no pod to wait for.
    monkeypatch.setattr(service_deploy, "wait_for_service_ready", mock.Mock())
    # Setup now recovers the registry prefix from the live Ingress when no
    # --ingress-host was given, so it reaches the API server where it did not before.
    # Unstubbed, these tests wait out a connection timeout apiece.
    monkeypatch.setattr(service_deploy, "published_host", lambda *a, **k: "")
    for name in ("install_kueue_helm", "verify_kueue_admission_ready",
                 "apply_controller_rbac", "ensure_nvidia_device_plugin"):
        monkeypatch.setattr(cluster_setup, name, mock.Mock())
    # Setup applies the shared build daemon too; without this the test reaches a cluster.
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    queues = mock.Mock()
    monkeypatch.setattr(cluster_setup, "apply_kueue_queues", queues)
    config = mock.Mock()
    monkeypatch.setattr(cluster_setup, "get_cluster_config", lambda name: config)
    return queues, config


def _write_project(directory, config_name, write_config=True):
    """A ``.robovast_project`` in *directory*, optionally with the ``.vast`` it names."""
    if write_config:
        (directory / config_name).write_text(_VAST, encoding="utf-8")
    (directory / ".robovast_project").write_text(json.dumps(
        {"config": config_name, "results_dir": "results"}), encoding="utf-8")


# -- what setup reads --------------------------------------------------------


def test_ambient_project_contributes_no_labels(tmp_path, monkeypatch, deploy_stubs):
    """A project above the CWD must not reach a cluster deploy, even when valid."""
    queues, config = deploy_stubs
    _write_project(tmp_path, "campaign.vast")
    deep = tmp_path / "some" / "other" / "workdir"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)

    setup_server(config_name="rke2", namespace="default")

    assert queues.call_args.kwargs["node_labels"] is None
    assert config.setup_cluster.call_args.kwargs["control_node_labels"] is None


def test_stale_ambient_project_does_not_fail_the_deploy(tmp_path, monkeypatch,
                                                        deploy_stubs):
    """The reported failure: a project naming a .vast that no longer exists.

    Not consulted at all now, so a moved/renamed/deleted ``.vast`` cannot abort a
    deploy that never mentioned it.
    """
    queues, _ = deploy_stubs
    _write_project(tmp_path, "RoboVAST Examples/example.vast", write_config=False)
    monkeypatch.chdir(tmp_path)

    setup_server(config_name="rke2", namespace="default")

    assert queues.call_args.kwargs["node_labels"] is None


def test_named_config_supplies_labels(tmp_path, monkeypatch, deploy_stubs):
    """``vast -V <file>`` is the one way node labels reach the deploy."""
    queues, config = deploy_stubs
    vast = tmp_path / "campaign.vast"
    vast.write_text(_VAST, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cluster_setup, "get_vast_file_override", lambda: str(vast))

    setup_server(config_name="rke2", namespace="default")

    assert queues.call_args.kwargs["node_labels"] == _JOBS
    assert config.setup_cluster.call_args.kwargs["control_node_labels"] == _CONTROL


# -- the reader itself -------------------------------------------------------


def test_no_config_named_yields_no_labels():
    """Nothing named a config: legal, and it means no node selectors."""
    assert get_kubernetes_node_labels_from_config(None) == (None, None)


def test_named_config_is_read(tmp_path):
    vast = tmp_path / "campaign.vast"
    vast.write_text(_VAST, encoding="utf-8")
    assert get_kubernetes_node_labels_from_config(str(vast)) == (_JOBS, _CONTROL)


def test_unreadable_named_config_raises(tmp_path):
    """A named config that cannot be read must abort setup, not mean "no labels"."""
    with pytest.raises(ValueError, match="could not read node labels"):
        get_kubernetes_node_labels_from_config(str(tmp_path / "missing.vast"))


def test_gpus_are_provisioned_before_the_queues_are_sized(monkeypatch):
    """Ordering, and it is load-bearing rather than tidy.

    `apply_kueue_queues` sizes the ClusterQueue's GPU quota from what the nodes advertise,
    and a node advertises nothing until the device plugin's DaemonSet is running. Install it
    afterwards and the quota is sized from zero GPUs by construction -- which Kueue answers
    by suspending every GPU job forever rather than failing, so the campaign hangs while
    setup reports success.
    """

    from robovast.execution.cluster_execution import service_deploy

    order = []
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))
    monkeypatch.setattr(service_deploy, "deploy_service", mock.Mock())
    monkeypatch.setattr(service_deploy, "wait_for_service_ready", mock.Mock())
    monkeypatch.setattr(service_deploy, "published_host", lambda *a, **k: "")
    monkeypatch.setattr(cluster_setup, "ensure_nvidia_device_plugin",
                        lambda **k: order.append("gpu-plugin"))
    monkeypatch.setattr(cluster_setup, "install_kueue_helm",
                        lambda **k: order.append("kueue-helm"))
    monkeypatch.setattr(cluster_setup, "apply_kueue_queues",
                        lambda **k: order.append("kueue-queues"))
    monkeypatch.setattr(cluster_setup, "verify_kueue_admission_ready", mock.Mock())
    monkeypatch.setattr(cluster_setup, "apply_controller_rbac", mock.Mock())
    # Setup applies the shared build daemon too; without this the test reaches a cluster.
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    monkeypatch.setattr(cluster_setup, "get_cluster_config", lambda name: mock.Mock())

    cluster_setup.setup_server(config_name="rke2", namespace="default")

    assert order == ["gpu-plugin", "kueue-helm", "kueue-queues"], (
        "the GPU quota would be sized before the node could advertise any GPUs")


def test_contradictory_gpu_flags_are_refused_before_anything_is_installed(monkeypatch):


    from robovast.execution.cluster_execution import service_deploy

    touched = []
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))
    monkeypatch.setattr(cluster_setup, "ensure_nvidia_device_plugin",
                        lambda **k: touched.append("gpu"))
    monkeypatch.setattr(cluster_setup, "install_kueue_helm",
                        lambda **k: touched.append("kueue"))
    # Setup applies the shared build daemon too; without this the test reaches a cluster.
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    monkeypatch.setattr(cluster_setup, "get_cluster_config", lambda name: mock.Mock())

    with pytest.raises(ValueError, match="contradictory"):
        cluster_setup.setup_server(config_name="rke2", namespace="default",
                                   gpu_replicas=8, no_gpu=True)
    with pytest.raises(ValueError, match="at least 1"):
        cluster_setup.setup_server(config_name="rke2", namespace="default", gpu_replicas=0)
    assert touched == [], "the cluster was modified before the arguments were checked"
