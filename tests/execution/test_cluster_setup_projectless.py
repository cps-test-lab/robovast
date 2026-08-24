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


@pytest.fixture(autouse=True)
def _no_image_warm(monkeypatch):
    """Never pre-pull images at a real cluster from these tests.

    ``setup_server`` finishes by warming the image family onto the nodes, which is a live
    Kubernetes call. Unstubbed it went to whatever context the developer's kubeconfig named
    and blocked until that timed out -- so this file did not merely run slowly, it did not
    finish at all where egress is closed.

    Autouse because pre-pulling is a side effect no test in this file is about, and the eight
    tests here that drive setup would each reintroduce it.
    """
    from robovast.execution.cluster_execution import image_warm
    monkeypatch.setattr(image_warm, "warm_family_images", lambda *a, **k: [])
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
    # Placement now resolves against the live node list before anything is applied.
    _stub_placement(monkeypatch)
    queues = mock.Mock()
    monkeypatch.setattr(cluster_setup, "apply_kueue_queues", queues)
    config = mock.Mock()
    monkeypatch.setattr(cluster_setup, "get_cluster_config", lambda name: config)
    return queues, config


def _stub_placement(monkeypatch, node="node-a"):
    """A decided placement, without a cluster to decide it against.

    `setup_server` resolves which node holds the node-local data before it applies
    anything; unstubbed that lists nodes on whatever context the kubeconfig names.
    """
    from robovast.execution.cluster_execution import node_placement

    monkeypatch.setattr(node_placement, "resolve_placement",
                        lambda core, label, **kw: node_placement.Placement(
                            node, node_placement.label_selector(label), "auto"))
    # Setup also asks whether a build label already exists, to decide whether co-locating
    # the cache is a default or would override a deliberate placement.
    monkeypatch.setattr(node_placement, "labeled_nodes", lambda core, label: [])


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
    # The store pod is still pinned -- that decision comes from the cluster, not from a
    # .vast -- but nothing the ambient project said reached it.
    assert config.setup_cluster.call_args.kwargs["control_node_labels"] == {
        "robovast.io/data-node": "true"}


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
    # ANDed with the placement label, not replaced by it: a node pool alone still lets the
    # store pod float within the pool, which is the same bug at a smaller scale.
    assert config.setup_cluster.call_args.kwargs["control_node_labels"] == {
        **_CONTROL, "robovast.io/data-node": "true"}


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
    _stub_placement(monkeypatch)
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


# -- where the node-local data goes ------------------------------------------


def _placement_spy(monkeypatch, labelled=()):
    """Record every resolve_placement call instead of touching a cluster."""
    from robovast.execution.cluster_execution import node_placement

    calls = []

    def _resolve(core, label, **kwargs):
        calls.append((label, kwargs))
        return node_placement.Placement("node-a", node_placement.label_selector(label),
                                        "auto")

    monkeypatch.setattr(node_placement, "resolve_placement", _resolve)
    monkeypatch.setattr(node_placement, "labeled_nodes",
                        lambda core, label: list(labelled) if label ==
                        node_placement.BUILD_NODE_LABEL else [])
    return calls


def test_the_build_cache_co_locates_with_the_data_when_nothing_says_otherwise(
        monkeypatch, deploy_stubs):
    """Auto-separating would put a 150 GB cache on whichever node was left over."""
    from robovast.execution.cluster_execution import node_placement

    calls = _placement_spy(monkeypatch)
    setup_server(config_name="rke2", namespace="default")

    build = next(kw for label, kw in calls if label == node_placement.BUILD_NODE_LABEL)
    assert build["requested"] == "node-a"


def test_a_build_cache_placed_elsewhere_is_not_dragged_back_every_setup(
        monkeypatch, deploy_stubs):
    """The regression: co-location is a DEFAULT, not a request.

    Feeding the data node in as `requested` unconditionally turns "the operator passed no
    flag" into "the operator asked for this node", and a cache deliberately kept on another
    disk would then be dragged back onto the service's node by a setup that was passed
    nothing at all.
    """
    from robovast.execution.cluster_execution import node_placement

    calls = _placement_spy(monkeypatch, labelled=["node-b"])
    setup_server(config_name="rke2", namespace="default")

    build = next(kw for label, kw in calls if label == node_placement.BUILD_NODE_LABEL)
    assert build["requested"] == ""


def test_an_explicit_data_node_reaches_the_resolver(monkeypatch, deploy_stubs):
    from robovast.execution.cluster_execution import node_placement

    calls = _placement_spy(monkeypatch)
    setup_server(config_name="rke2", namespace="default", data_node="node-c")

    data = next(kw for label, kw in calls if label == node_placement.DATA_NODE_LABEL)
    assert data["requested"] == "node-c"


def test_an_explicit_data_node_takes_the_build_cache_with_it(monkeypatch, deploy_stubs):
    """One name moves the whole of this deployment's on-disk state.

    The cache is the other large tenant, and leaving it on the old node while the registry
    moves is the stranded-bytes surprise `--data-node` exists to make visible -- so an
    existing build label does NOT hold it back the way it does when no flag was passed.
    """
    from robovast.execution.cluster_execution import node_placement

    calls = _placement_spy(monkeypatch, labelled=["node-b"])
    setup_server(config_name="rke2", namespace="default", data_node="node-c")

    build = next(kw for label, kw in calls if label == node_placement.BUILD_NODE_LABEL)
    assert build["requested"] == "node-a"      # the resolved data placement, stubbed


def test_an_explicit_buildkit_node_still_splits_the_two(monkeypatch, deploy_stubs):
    """Where the disk is tight the cache belongs elsewhere, and saying so wins."""
    from robovast.execution.cluster_execution import node_placement

    calls = _placement_spy(monkeypatch)
    setup_server(config_name="rke2", namespace="default", data_node="node-c",
                 buildkit_node="node-d")

    build = next(kw for label, kw in calls if label == node_placement.BUILD_NODE_LABEL)
    assert build["requested"] == "node-d"


def test_a_provisioned_registry_and_workspaces_are_not_pinned(monkeypatch, deploy_stubs):
    """Nothing is on a node, so a nodeSelector would only make the pod harder to place."""
    from robovast.execution.cluster_execution import node_placement

    calls = _placement_spy(monkeypatch)
    setup_server(config_name="rke2", namespace="default",
                 service_kwargs={"registry_storage_class": "fast",
                                 "workspaces_storage_class": "fast"})

    data = next(kw for label, kw in calls if label == node_placement.DATA_NODE_LABEL)
    assert data["node_local"] is False


def test_setup_reports_where_it_put_the_data(monkeypatch, deploy_stubs):
    """The operator runs this with no flags, so a decision nobody sees is how a deployment
    ends up on a node nobody picked."""
    _placement_spy(monkeypatch)
    reported = setup_server(config_name="rke2", namespace="default")
    assert reported["data_node"] == "node-a"
    assert reported["data_source"] == "auto"
