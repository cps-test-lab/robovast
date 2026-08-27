# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``cluster setup`` reads no ``.vast`` at all.

A ``.vast`` describes a campaign. Which machines a cluster's pods may run on is a property
of the CLUSTER, and carrying it in a campaign file put a deploy's lasting, cluster-wide
decisions somewhere that travels with an experiment. It also forced an awkward guard --
read only from a config named with ``vast -V``, never an ambient project -- because a
``.robovast_project`` is found by walking *up* to the filesystem root, so one ten
directories above an unrelated CWD could otherwise decide a cluster's node pools.

Both are now settled by construction: the labels are command-line options, so there is no
file to consult and no ambient project to guard against. These tests pin that the
configuration reaches what enforces it, and that omitting an option CLEARS rather than
preserves -- which is what keeps the command the whole truth about the cluster.
"""

import json
from unittest import mock

import pytest

from robovast.execution.cluster_execution import buildkitd_deploy
from robovast.execution.cluster_execution import cluster_setup
from robovast.execution.cluster_execution.cluster_setup import setup_server


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

_JOBS = {'node-pool': 'primary'}
_CONTROL = {'node-pool': 'extra'}

_VAST = """version: 3
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

    Returns the ``setup_cluster`` mock, which is where the resolved control labels land.
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
    for name in ("apply_controller_rbac", "ensure_nvidia_device_plugin"):
        monkeypatch.setattr(cluster_setup, name, mock.Mock())
    # Returns a dict of what it changed, and setup logs its size -- a bare Mock has no len().
    monkeypatch.setattr(cluster_setup, "apply_node_id_labels", mock.Mock(return_value={}))
    # Setup applies the shared build daemon too; without this the test reaches a cluster.
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    # The governor DaemonSet is reconciled on EVERY setup -- installed when asked
    # for, removed when not, so that omitting the flag clears a previous one. An
    # unstubbed call therefore reaches a real API server even though no test here
    # asks for a governor.
    from robovast.execution.cluster_execution import node_governor
    monkeypatch.setattr(node_governor, "ensure_cpu_governor",
                        mock.Mock(return_value=False))
    # Placement now resolves against the live node list before anything is applied.
    _stub_placement(monkeypatch)
    config = mock.Mock()
    monkeypatch.setattr(cluster_setup, "get_cluster_config", lambda name: config)
    return config


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


def test_the_job_node_pool_reaches_the_service_that_enforces_them(deploy_stubs):
    """The pool travels in the service's env because the admission controller enforces it and
    the controller runs there.

    Asserted on ``job_node_labels``, NOT on ``env``. That distinction is the bug this once
    shipped: ``env`` is the WHOLE environment rather than an addition to it, so writing the
    pool through it replaced ROBOVAST_CLUSTER_CONFIG_NAME and deployed a service that could
    run nothing while setup reported success. See test_service_env_is_complete.
    """
    from robovast.execution.cluster_execution import service_deploy

    setup_server(config_name="rke2", namespace="default", jobs_node_labels=_JOBS)

    kwargs = service_deploy.deploy_service.call_args.kwargs
    assert kwargs["job_node_labels"] == _JOBS
    assert kwargs.get("env") is None, (
        "the pool must not be written through `env`, which would replace the cluster env")


def test_omitting_the_option_clears_a_previously_configured_pool(deploy_stubs):
    """Written on every setup, empty included -- otherwise dropping the option would leave the
    old pool in force and the command would stop being the whole truth about the cluster."""
    from robovast.execution.cluster_execution import service_deploy

    setup_server(config_name="rke2", namespace="default", vast_path=str(vast))

    assert service_deploy.deploy_service.call_args.kwargs["job_node_labels"] is None


def test_control_node_labels_reach_the_placement_resolver(deploy_stubs):
    """They narrow rather than decide: ANDed with the node-local data placement setup picks."""
    setup_server(config_name="rke2", namespace="default", control_node_labels=_CONTROL)
    passed = deploy_stubs.setup_cluster.call_args.kwargs["control_node_labels"]
    assert passed["node-pool"] == "extra"


# -- the reader itself -------------------------------------------------------


def test_gpus_are_provisioned_before_the_service_can_run_a_campaign(monkeypatch):
    """Ordering, and it is load-bearing rather than tidy.

    A node advertises no ``nvidia.com/gpu`` until the device plugin's DaemonSet is running,
    and admission reads exactly that field to decide whether a GPU request could ever be
    satisfied. The plugin therefore has to be in place before the service is deployed and
    can accept a campaign; otherwise the first GPU campaign measures a cluster with no GPUs
    and is refused outright.
    """

    from robovast.execution.cluster_execution import service_deploy

    order = []
    _stub_placement(monkeypatch)
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))
    monkeypatch.setattr(service_deploy, "wait_for_service_ready", mock.Mock())
    monkeypatch.setattr(service_deploy, "published_host", lambda *a, **k: "")
    monkeypatch.setattr(cluster_setup, "apply_node_id_labels", mock.Mock(return_value={}))
    monkeypatch.setattr(cluster_setup, "ensure_nvidia_device_plugin",
                        lambda **k: order.append("gpu-plugin"))
    monkeypatch.setattr(service_deploy, "deploy_service",
                        lambda *a, **k: order.append("service"))
    monkeypatch.setattr(cluster_setup, "apply_controller_rbac",
                        lambda **k: order.append("rbac"))
    # Setup applies the shared build daemon too; without this the test reaches a cluster.
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    # The governor DaemonSet is reconciled on EVERY setup -- installed when asked
    # for, removed when not, so that omitting the flag clears a previous one. An
    # unstubbed call therefore reaches a real API server even though no test here
    # asks for a governor.
    from robovast.execution.cluster_execution import node_governor
    monkeypatch.setattr(node_governor, "ensure_cpu_governor",
                        mock.Mock(return_value=False))
    monkeypatch.setattr(cluster_setup, "get_cluster_config", lambda name: mock.Mock())

    cluster_setup.setup_server(config_name="rke2", namespace="default")

    assert order[0] == "gpu-plugin", (
        "a campaign could be accepted before any node could advertise a GPU")
    assert "service" in order


def test_contradictory_gpu_flags_are_refused_before_anything_is_installed(monkeypatch):


    from robovast.execution.cluster_execution import service_deploy

    touched = []
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))
    monkeypatch.setattr(cluster_setup, "apply_node_id_labels", mock.Mock(return_value={}))
    monkeypatch.setattr(cluster_setup, "ensure_nvidia_device_plugin",
                        lambda **k: touched.append("gpu"))
    monkeypatch.setattr(cluster_setup, "apply_controller_rbac",
                        lambda **k: touched.append("rbac"))
    # Setup applies the shared build daemon too; without this the test reaches a cluster.
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    # The governor DaemonSet is reconciled on EVERY setup -- installed when asked
    # for, removed when not, so that omitting the flag clears a previous one. An
    # unstubbed call therefore reaches a real API server even though no test here
    # asks for a governor.
    from robovast.execution.cluster_execution import node_governor
    monkeypatch.setattr(node_governor, "ensure_cpu_governor",
                        mock.Mock(return_value=False))
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
