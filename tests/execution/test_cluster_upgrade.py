# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""An upgrade must actually roll the pod, not just report that it did.

``deploy_service`` patches the Deployment. When the image ref is a floating tag that was
re-pushed, or when the only change is inside a Secret, the patched spec is byte-identical
to the live one: Kubernetes creates no new ReplicaSet, ``wait_for_service_ready`` finds
the OLD pod still Ready, and the command prints "✓ upgraded and ready" while the pod goes
on running the previous image and the previous Secret values.

``imagePullPolicy: Always`` is not a fix -- it decides what happens when a container
*starts*, and none did. The env Secrets are worse still: the pod reads them through
``envFrom`` exactly once at container start, so replacing a Secret changes nothing at all
until something forces a restart.

A pod-template annotation that differs on every deploy is what forces it.
"""

import pytest

from robovast.execution.cluster_execution import buildkitd_deploy
from robovast.execution.cluster_execution import service_deploy


@pytest.fixture(autouse=True)
def _no_image_warm(monkeypatch):
    """Never pre-pull images at a real cluster from these tests.

    ``setup_server`` and ``upgrade`` finish by warming the image family onto the nodes, which
    is a live Kubernetes call. Unstubbed it went to whatever context the developer's
    kubeconfig named and blocked until that timed out -- so this file did not merely run
    slowly, it did not finish at all where egress is closed, and the three files with this
    hole were the reason a full ``pytest tests/`` never completed here.

    Autouse because pre-pulling is a side effect no test in this file is about, and the next
    one to drive setup or upgrade would otherwise reintroduce it silently.
    """
    from robovast.execution.cluster_execution import image_warm
    monkeypatch.setattr(image_warm, "warm_family_images", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def _decided_placement(monkeypatch):
    """A decided placement, without a cluster to decide it against.

    Setup resolves which node holds the node-local data before it applies anything, and
    that is a live node list. Autouse for the same reason as the fixture above: it is a
    precondition of driving setup, not a thing any test here is about.
    """
    from robovast.execution.cluster_execution import node_placement
    monkeypatch.setattr(node_placement, "resolve_placement",
                        lambda core, label, **kw: node_placement.Placement(
                            "node-a", node_placement.label_selector(label), "auto"))
    # Setup also asks whether a build label already exists, to decide whether co-locating
    # the cache is a default or would override a deliberate placement.
    monkeypatch.setattr(node_placement, "labeled_nodes", lambda core, label: [])


def _template(manifest):
    return manifest["spec"]["template"]


def _annotations(manifest):
    return _template(manifest)["metadata"].get("annotations", {})


def test_the_pod_template_carries_a_restart_annotation():
    manifest = service_deploy._deployment_manifest("default", "img:latest")
    assert service_deploy.RESTART_ANNOTATION in _annotations(manifest)


def test_two_deploys_of_the_same_image_still_differ():
    """The regression this exists for: same image string, spec must still change."""
    first = service_deploy._deployment_manifest(
        "default", "img:latest", restarted_at="2026-08-15T09:00:00+00:00")
    second = service_deploy._deployment_manifest(
        "default", "img:latest", restarted_at="2026-08-15T09:05:00+00:00")

    assert first["spec"]["template"]["spec"] == second["spec"]["template"]["spec"], (
        "only the annotation should differ -- if the pod spec itself changed, this test "
        "would pass for the wrong reason")
    assert _template(first) != _template(second)


def test_the_stamp_defaults_to_now_so_every_deploy_rolls():
    """No caller passes ``restarted_at``; the default is what makes upgrade work."""
    first = service_deploy._deployment_manifest("default", "img:latest")
    second = service_deploy._deployment_manifest("default", "img:latest")
    stamps = {_annotations(first)[service_deploy.RESTART_ANNOTATION],
              _annotations(second)[service_deploy.RESTART_ANNOTATION]}
    # A clock coarse enough to return the same value twice would silently disable the
    # restart, so require the timestamps to be distinct rather than merely present.
    assert len(stamps) == 2, f"identical stamps would not roll the pod: {stamps}"


def test_it_uses_kubectls_own_annotation():
    """Not a private key: a hand-run ``kubectl rollout restart`` and an upgrade are the
    same event, and tooling already knows how to show this one."""
    assert service_deploy.RESTART_ANNOTATION == "kubectl.kubernetes.io/restartedAt"


def test_service_manifests_stamps_the_deployment_it_builds():
    """The annotation has to survive the path deploy_service actually takes."""
    manifests = service_deploy.service_manifests(namespace="default", image="img:latest")
    deployment = next(m for m in manifests if m["kind"] == "Deployment")
    assert _annotations(deployment).get(service_deploy.RESTART_ANNOTATION)


def test_an_upgrade_keeps_the_build_registry_without_recreating_the_ingress():
    """``registry_host`` and ``ingress_host`` are separate on purpose.

    The registry's prefix is the published host, so an upgrade has to supply it or it
    would rebuild the registry config without one and quietly leave the deployment unable
    to build. But it cannot supply ``ingress_host``: that also *creates* the Ingress, and
    an upgrade has none of the TLS arguments the Ingress was made with, so
    ``validate_ingress_options`` would refuse and the upgrade would fail outright.
    """
    manifests = service_deploy.service_manifests(
        namespace="default", image="img:latest", auth_token="t",
        registry_host="robovast.example.org")

    assert not [m for m in manifests if m["kind"] == "Ingress"], (
        "an upgrade must leave the existing Ingress alone")
    config = next(m for m in manifests if m["kind"] == "Secret"
                  and m["metadata"]["name"] == service_deploy.REGISTRY_CONFIG_SECRET_NAME)
    assert config["stringData"]["ROBOVAST_REGISTRY_PREFIX"] == "robovast.example.org"


def test_setup_still_gets_the_prefix_from_ingress_host():
    """registry_host defaults to ingress_host, so setup passes one thing, not two."""
    manifests = service_deploy.service_manifests(
        namespace="default", image="img:latest", auth_token="t",
        ingress_host="robovast.example.org", issuer="ca")
    config = next(m for m in manifests if m["kind"] == "Secret"
                  and m["metadata"]["name"] == service_deploy.REGISTRY_CONFIG_SECRET_NAME)
    assert config["stringData"]["ROBOVAST_REGISTRY_PREFIX"] == "robovast.example.org"


def test_setup_preserves_the_registry_prefix_of_a_published_deployment(monkeypatch):
    """A `setup` re-run without --ingress-host must not silently disable builds.

    The prefix is baked from the Ingress host, so `_registry_env` returns None without
    one: the Secret goes unlisted from the Deployment's envFrom and the pod loses the
    prefix. The Ingress itself is untouched, so nothing looks wrong -- until a campaign
    is submitted and refused with "nowhere to push it", after a project push, a
    workspace create and a launch.

    `deploy_service` separates registry_host from ingress_host precisely so a caller can
    re-bake the prefix without rebuilding the Ingress. `upgrade` used that; `setup` did
    not.
    """
    from unittest import mock

    from robovast.execution.cluster_execution import cluster_setup

    deploy = mock.Mock()
    monkeypatch.setattr(service_deploy, "deploy_service", deploy)
    monkeypatch.setattr(service_deploy, "wait_for_service_ready", mock.Mock())
    # Setup creates the index Secret before the store pod and then refuses a store pod that
    # predates the registry/index move; both read the API server.
    monkeypatch.setattr(service_deploy, "ensure_index_secret", lambda *a, **k: "pw")
    # Same moment, same reason: the registry's password file is put in the cluster before
    # the store pod that mounts it, and that reads the API server too.
    monkeypatch.setattr(service_deploy, "ensure_registry_htpasswd", lambda *a, **k: "rpw")
    from robovast.execution.cluster_execution import tailnet_deploy
    # Every setup reconciles the optional tailnet node, which reads the API server
    # even when none is configured.
    monkeypatch.setattr(tailnet_deploy, "ensure_tailnet", lambda *a, **k: "")
    monkeypatch.setattr(tailnet_deploy, "reconcile_existing", lambda *a, **k: "")
    monkeypatch.setattr(tailnet_deploy, "remove", lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "verify_store_pod_infrastructure",
                        lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))
    monkeypatch.setattr(service_deploy, "published_host",
                        lambda *a, **k: "robovast.example.org")
    for name in ("apply_controller_rbac", "ensure_nvidia_device_plugin"):
        monkeypatch.setattr(cluster_setup, name, mock.Mock())
    # Returns a dict of what it changed, and setup logs its size -- a bare Mock has no len().
    monkeypatch.setattr(cluster_setup, "apply_node_id_labels", mock.Mock(return_value={}))
    # Setup reports which image and digest the pod came up on, once it is serving. Two more
    # reads against the API server, and reporting-only -- they swallow their own errors, so
    # unstubbed they cost a connect timeout apiece and say nothing.
    monkeypatch.setattr(service_deploy, "deployment_image_ref", lambda *a, **k: ("", False))
    monkeypatch.setattr(service_deploy, "running_image_digest", lambda *a, **k: "")
    # Setup applies the shared build daemon too; without this the test reaches a cluster.
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    # The governor DaemonSet is reconciled on EVERY setup -- installed when asked
    # for and removed when not, so omitting the flag clears a previous one. An
    # unstubbed call reaches a real API server even with no governor requested.
    from robovast.execution.cluster_execution import node_governor
    monkeypatch.setattr(node_governor, "ensure_cpu_governor",
                        mock.Mock(return_value=False))
    monkeypatch.setattr(cluster_setup, "get_cluster_config",
                        lambda name: mock.Mock(get_cluster_kwargs=lambda: {}))

    cluster_setup.setup_server(config_name="rke2", namespace="default")

    assert deploy.call_args.kwargs.get("registry_host") == "robovast.example.org", (
        "setup dropped the registry prefix of a published deployment")


def test_setup_does_not_hang_when_the_api_server_cannot_be_reached(monkeypatch):
    """The lookup is a convenience, not a requirement. Setup must not die -- or wait out
    a connection timeout -- because it could not read something it is only preserving."""
    from unittest import mock

    from robovast.execution.cluster_execution import cluster_setup

    deploy = mock.Mock()
    monkeypatch.setattr(service_deploy, "deploy_service", deploy)
    monkeypatch.setattr(service_deploy, "wait_for_service_ready", mock.Mock())
    # Setup creates the index Secret before the store pod and then refuses a store pod that
    # predates the registry/index move; both read the API server.
    monkeypatch.setattr(service_deploy, "ensure_index_secret", lambda *a, **k: "pw")
    # Same moment, same reason: the registry's password file is put in the cluster before
    # the store pod that mounts it, and that reads the API server too.
    monkeypatch.setattr(service_deploy, "ensure_registry_htpasswd", lambda *a, **k: "rpw")
    from robovast.execution.cluster_execution import tailnet_deploy
    # Every setup reconciles the optional tailnet node, which reads the API server
    # even when none is configured.
    monkeypatch.setattr(tailnet_deploy, "ensure_tailnet", lambda *a, **k: "")
    monkeypatch.setattr(tailnet_deploy, "reconcile_existing", lambda *a, **k: "")
    monkeypatch.setattr(tailnet_deploy, "remove", lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "verify_store_pod_infrastructure",
                        lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))

    def _unreachable(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(service_deploy, "published_host", _unreachable)
    for name in ("apply_controller_rbac", "ensure_nvidia_device_plugin"):
        monkeypatch.setattr(cluster_setup, name, mock.Mock())
    # Returns a dict of what it changed, and setup logs its size -- a bare Mock has no len().
    monkeypatch.setattr(cluster_setup, "apply_node_id_labels", mock.Mock(return_value={}))
    # Setup reports which image and digest the pod came up on, once it is serving. Two more
    # reads against the API server, and reporting-only -- they swallow their own errors, so
    # unstubbed they cost a connect timeout apiece and say nothing.
    monkeypatch.setattr(service_deploy, "deployment_image_ref", lambda *a, **k: ("", False))
    monkeypatch.setattr(service_deploy, "running_image_digest", lambda *a, **k: "")
    # Setup applies the shared build daemon too; without this the test reaches a cluster.
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    # The governor DaemonSet is reconciled on EVERY setup -- installed when asked
    # for and removed when not, so omitting the flag clears a previous one. An
    # unstubbed call reaches a real API server even with no governor requested.
    from robovast.execution.cluster_execution import node_governor
    monkeypatch.setattr(node_governor, "ensure_cpu_governor",
                        mock.Mock(return_value=False))
    monkeypatch.setattr(cluster_setup, "get_cluster_config",
                        lambda name: mock.Mock(get_cluster_kwargs=lambda: {}))

    cluster_setup.setup_server(config_name="rke2", namespace="default")

    assert "registry_host" not in deploy.call_args.kwargs, (
        "an unreachable API server must leave registry_host unset, not guessed")


def test_an_explicit_ingress_host_still_wins(monkeypatch):
    """The lookup exists for the case where none was given. Passing one must not trigger
    an API call at all."""
    from unittest import mock

    from robovast.execution.cluster_execution import cluster_setup

    deploy = mock.Mock()
    monkeypatch.setattr(service_deploy, "deploy_service", deploy)
    monkeypatch.setattr(service_deploy, "wait_for_service_ready", mock.Mock())
    # Setup creates the index Secret before the store pod and then refuses a store pod that
    # predates the registry/index move; both read the API server.
    monkeypatch.setattr(service_deploy, "ensure_index_secret", lambda *a, **k: "pw")
    # Same moment, same reason: the registry's password file is put in the cluster before
    # the store pod that mounts it, and that reads the API server too.
    monkeypatch.setattr(service_deploy, "ensure_registry_htpasswd", lambda *a, **k: "rpw")
    from robovast.execution.cluster_execution import tailnet_deploy
    # Every setup reconciles the optional tailnet node, which reads the API server
    # even when none is configured.
    monkeypatch.setattr(tailnet_deploy, "ensure_tailnet", lambda *a, **k: "")
    monkeypatch.setattr(tailnet_deploy, "reconcile_existing", lambda *a, **k: "")
    monkeypatch.setattr(tailnet_deploy, "remove", lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "verify_store_pod_infrastructure",
                        lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))

    def _must_not_be_called(*_a, **_k):
        raise AssertionError("published_host was called despite an explicit ingress_host")

    monkeypatch.setattr(service_deploy, "published_host", _must_not_be_called)
    for name in ("apply_controller_rbac", "ensure_nvidia_device_plugin"):
        monkeypatch.setattr(cluster_setup, name, mock.Mock())
    # Returns a dict of what it changed, and setup logs its size -- a bare Mock has no len().
    monkeypatch.setattr(cluster_setup, "apply_node_id_labels", mock.Mock(return_value={}))
    # Setup reports which image and digest the pod came up on, once it is serving. Two more
    # reads against the API server, and reporting-only -- they swallow their own errors, so
    # unstubbed they cost a connect timeout apiece and say nothing.
    monkeypatch.setattr(service_deploy, "deployment_image_ref", lambda *a, **k: ("", False))
    monkeypatch.setattr(service_deploy, "running_image_digest", lambda *a, **k: "")
    # Setup applies the shared build daemon too; without this the test reaches a cluster.
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    # The governor DaemonSet is reconciled on EVERY setup -- installed when asked
    # for and removed when not, so omitting the flag clears a previous one. An
    # unstubbed call reaches a real API server even with no governor requested.
    from robovast.execution.cluster_execution import node_governor
    monkeypatch.setattr(node_governor, "ensure_cpu_governor",
                        mock.Mock(return_value=False))
    monkeypatch.setattr(cluster_setup, "get_cluster_config",
                        lambda name: mock.Mock(get_cluster_kwargs=lambda: {}))

    cluster_setup.setup_server(config_name="rke2", namespace="default",
                               service_kwargs={"ingress_host": "given.example.org",
                                               "insecure_http": True})

    assert deploy.call_args.kwargs.get("registry_host") == "given.example.org"


def test_upgrade_reconciles_the_controller_rbac(monkeypatch):
    """`upgrade` is the command operators use to move versions, and the RBAC a deployed
    backend needs is coupled to what that version does. It is also the half of an upgrade
    the RUNNING pod picks up with no roll, which is what makes `--no-restart` possible --
    so skipping it would leave a service missing a permission it never regains without a
    full redeploy. Reached through the command that looks safe, which makes it worth a test.

    Nothing cluster-scoped is reconciled here.
    """
    from unittest import mock

    from click.testing import CliRunner

    from robovast.execution.cluster_execution import cli as cluster_cli
    from robovast.execution.cluster_execution import cluster_setup

    apply_rbac = mock.Mock()
    monkeypatch.setattr(cluster_setup, "apply_controller_rbac", apply_rbac)
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: ("rke2", {"namespace": "default"}))
    monkeypatch.setattr(service_deploy, "published_url", lambda *a, **k: "")
    monkeypatch.setattr(service_deploy, "deploy_service", mock.Mock())
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    # The governor DaemonSet is reconciled on EVERY setup -- installed when asked
    # for and removed when not, so omitting the flag clears a previous one. An
    # unstubbed call reaches a real API server even with no governor requested.
    from robovast.execution.cluster_execution import node_governor
    monkeypatch.setattr(node_governor, "ensure_cpu_governor",
                        mock.Mock(return_value=False))
    monkeypatch.setattr(buildkitd_deploy, "buildkitd_storage_from_cluster", lambda *a, **k: {})
    monkeypatch.setattr(service_deploy, "wait_for_service_ready", mock.Mock())
    # Setup creates the index Secret before the store pod and then refuses a store pod that
    # predates the registry/index move; both read the API server.
    monkeypatch.setattr(service_deploy, "ensure_index_secret", lambda *a, **k: "pw")
    # Same moment, same reason: the registry's password file is put in the cluster before
    # the store pod that mounts it, and that reads the API server too.
    monkeypatch.setattr(service_deploy, "ensure_registry_htpasswd", lambda *a, **k: "rpw")
    from robovast.execution.cluster_execution import tailnet_deploy
    # Every setup reconciles the optional tailnet node, which reads the API server
    # even when none is configured.
    monkeypatch.setattr(tailnet_deploy, "ensure_tailnet", lambda *a, **k: "")
    monkeypatch.setattr(tailnet_deploy, "reconcile_existing", lambda *a, **k: "")
    monkeypatch.setattr(tailnet_deploy, "remove", lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "verify_store_pod_infrastructure",
                        lambda *a, **k: None)
    # Returns None on success; it raises on every non-convergence.
    monkeypatch.setattr(service_deploy, "wait_for_rollout", lambda **k: None)
    monkeypatch.setattr(service_deploy, "running_image_digest", lambda *a, **k: "sha256:abc")
    monkeypatch.setattr(service_deploy, "reconcile_registry_ingress_path",
                        lambda **k: False)
    # Refuses an upgrade whose store pod predates the registry/index move; it reads the
    # live pod, so an unstubbed call reaches a real API server.
    monkeypatch.setattr(service_deploy, "verify_store_pod_infrastructure",
                        lambda *a, **k: None)

    result = CliRunner().invoke(cluster_cli.upgrade, ["-n", "default"])

    assert result.exit_code == 0, result.output
    assert apply_rbac.called, "upgrade left the controller RBAC unreconciled"
    assert apply_rbac.call_args.kwargs["namespace"] == "default"


def test_upgrade_reconciles_a_tailnet_node_that_already_exists(monkeypatch):
    """It carries a rotated key or a changed serve config into a cluster that is already on
    a tailnet -- and creates nothing.

    `setup --tailnet` is what decides. Creating here would let an operator upgrading two
    clusters from one shell publish the second by accident, which is the whole reason the
    decision is a flag rather than an environment variable.
    """
    from unittest import mock

    from click.testing import CliRunner

    from robovast.execution.cluster_execution import cli as cluster_cli
    from robovast.execution.cluster_execution import cluster_setup, tailnet_deploy

    ensure = mock.Mock(return_value="robovast")
    monkeypatch.setattr(tailnet_deploy, "reconcile_existing", ensure)
    monkeypatch.setattr(cluster_setup, "apply_controller_rbac", mock.Mock())
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: ("rke2", {"namespace": "default"}))
    monkeypatch.setattr(service_deploy, "published_url", lambda *a, **k: "")
    monkeypatch.setattr(service_deploy, "deploy_service", mock.Mock())
    monkeypatch.setattr(service_deploy, "wait_for_service_ready", mock.Mock())
    monkeypatch.setattr(service_deploy, "wait_for_rollout", lambda **k: None)
    monkeypatch.setattr(service_deploy, "running_image_digest", lambda *a, **k: "sha256:a")
    monkeypatch.setattr(service_deploy, "reconcile_registry_ingress_path", lambda **k: False)
    monkeypatch.setattr(service_deploy, "verify_store_pod_infrastructure",
                        lambda *a, **k: None)
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    monkeypatch.setattr(buildkitd_deploy, "buildkitd_storage_from_cluster",
                        lambda *a, **k: {})

    result = CliRunner().invoke(cluster_cli.upgrade, ["-n", "default"])

    assert result.exit_code == 0, result.output
    assert ensure.called, "an environment-configured route needs the environment command"
    assert ensure.call_args.kwargs["namespace"] == "default"
    assert "robovast" in result.output


def test_an_upgrade_declares_the_origin_it_read_from_the_ingress(monkeypatch):
    """An upgrade on its own is enough to publish the service's origin.

    It is the command an operator already runs to move a version, and it is the only one
    that can do this without being told anything: the origin needs a scheme as well as a
    host, `setup` knows that from its own flags but `upgrade` has none of them, so it reads
    the whole URL off the live Ingress -- where the TLS block decides the scheme -- and
    states it. The alternative shipped briefly and was worse than nothing: rendering from
    `ingress_host`, which an upgrade must never pass, evaluated to empty and would have
    erased a correct origin on every upgrade of a published deployment.
    """
    from unittest import mock

    from click.testing import CliRunner

    from robovast.execution.cluster_execution import cli as cluster_cli
    from robovast.execution.cluster_execution import cluster_setup

    deploy = mock.Mock()
    monkeypatch.setattr(cluster_setup, "apply_controller_rbac", mock.Mock())
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: ("rke2", {"namespace": "default"}))
    monkeypatch.setattr(service_deploy, "published_url",
                        lambda *a, **k: "http://robovast.example.org")
    monkeypatch.setattr(service_deploy, "deploy_service", deploy)
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    # The governor DaemonSet is reconciled on EVERY setup -- installed when asked
    # for and removed when not, so omitting the flag clears a previous one. An
    # unstubbed call reaches a real API server even with no governor requested.
    from robovast.execution.cluster_execution import node_governor
    monkeypatch.setattr(node_governor, "ensure_cpu_governor",
                        mock.Mock(return_value=False))
    monkeypatch.setattr(buildkitd_deploy, "buildkitd_storage_from_cluster", lambda *a, **k: {})
    monkeypatch.setattr(service_deploy, "wait_for_service_ready", mock.Mock())
    # Setup creates the index Secret before the store pod and then refuses a store pod that
    # predates the registry/index move; both read the API server.
    monkeypatch.setattr(service_deploy, "ensure_index_secret", lambda *a, **k: "pw")
    # Same moment, same reason: the registry's password file is put in the cluster before
    # the store pod that mounts it, and that reads the API server too.
    monkeypatch.setattr(service_deploy, "ensure_registry_htpasswd", lambda *a, **k: "rpw")
    from robovast.execution.cluster_execution import tailnet_deploy
    # Every setup reconciles the optional tailnet node, which reads the API server
    # even when none is configured.
    monkeypatch.setattr(tailnet_deploy, "ensure_tailnet", lambda *a, **k: "")
    monkeypatch.setattr(tailnet_deploy, "reconcile_existing", lambda *a, **k: "")
    monkeypatch.setattr(tailnet_deploy, "remove", lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "verify_store_pod_infrastructure",
                        lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "wait_for_rollout", lambda **k: None)
    monkeypatch.setattr(service_deploy, "running_image_digest", lambda *a, **k: "sha256:abc")
    monkeypatch.setattr(service_deploy, "reconcile_registry_ingress_path", lambda **k: False)
    monkeypatch.setattr(service_deploy, "verify_store_pod_infrastructure",
                        lambda *a, **k: None)

    result = CliRunner().invoke(cluster_cli.upgrade, ["-n", "default"])
    assert result.exit_code == 0, result.output

    kwargs = deploy.call_args.kwargs
    # The scheme is the point: read from the Ingress, not assumed from the host.
    assert kwargs.get("public_origin") == "http://robovast.example.org"
    # And the host still reaches the registry config, which is what it was read for first.
    assert kwargs.get("registry_host") == "robovast.example.org"


# -- the campaign job node pool ----------------------------------------------------------

def _stub_upgrade(monkeypatch, deploy):
    """Everything `upgrade` reaches for except `deploy_service`, which the test inspects."""
    from unittest import mock

    from robovast.execution.cluster_execution import cluster_setup, tailnet_deploy

    monkeypatch.setattr(cluster_setup, "apply_controller_rbac", mock.Mock())
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: ("rke2", {"namespace": "default"}))
    monkeypatch.setattr(service_deploy, "published_url", lambda *a, **k: "")
    monkeypatch.setattr(service_deploy, "deploy_service", deploy)
    monkeypatch.setattr(service_deploy, "wait_for_service_ready", mock.Mock())
    monkeypatch.setattr(service_deploy, "wait_for_rollout", lambda **k: None)
    monkeypatch.setattr(service_deploy, "running_image_digest", lambda *a, **k: "sha256:a")
    monkeypatch.setattr(service_deploy, "reconcile_registry_ingress_path", lambda **k: False)
    monkeypatch.setattr(service_deploy, "verify_store_pod_infrastructure",
                        lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "ensure_registry_htpasswd", lambda *a, **k: "rpw")
    monkeypatch.setattr(tailnet_deploy, "reconcile_existing", lambda *a, **k: "")
    monkeypatch.setattr(buildkitd_deploy, "apply_buildkitd", mock.Mock())
    monkeypatch.setattr(buildkitd_deploy, "buildkitd_storage_from_cluster",
                        lambda *a, **k: {})


def _upgrade(monkeypatch, *args):
    from unittest import mock

    from click.testing import CliRunner

    from robovast.execution.cluster_execution import cli as cluster_cli

    deploy = mock.Mock()
    _stub_upgrade(monkeypatch, deploy)
    result = CliRunner().invoke(cluster_cli.upgrade, ["-n", "default", "--yes", *args])
    return result, deploy


def test_an_upgrade_without_the_flag_asks_deploy_service_to_keep_the_pool(monkeypatch):
    """`None` is "recover": the pool is recorded nowhere but the live Deployment."""
    result, deploy = _upgrade(monkeypatch)
    assert result.exit_code == 0, result.output
    assert deploy.call_args.kwargs["job_node_labels"] is None


def test_an_upgrade_may_change_the_pool(monkeypatch):
    result, deploy = _upgrade(monkeypatch, "--jobs-node-label", "node-pool=primary",
                              "--jobs-node-label", "tier=batch")
    assert result.exit_code == 0, result.output
    assert deploy.call_args.kwargs["job_node_labels"] == {"node-pool": "primary",
                                                          "tier": "batch"}


def test_an_upgrade_clears_the_pool_with_an_empty_flag(monkeypatch):
    result, deploy = _upgrade(monkeypatch, "--jobs-node-label", "")
    assert result.exit_code == 0, result.output
    assert deploy.call_args.kwargs["job_node_labels"] == {}


def test_an_empty_flag_beside_a_label_is_refused(monkeypatch):
    result, deploy = _upgrade(monkeypatch, "--jobs-node-label", "",
                              "--jobs-node-label", "node-pool=primary")
    assert result.exit_code != 0
    assert not deploy.called


def test_a_pool_change_without_a_restart_is_refused(monkeypatch):
    """The pool is in the pod's env, so --no-restart could only ignore the argument."""
    result, deploy = _upgrade(monkeypatch, "--no-restart", "--jobs-node-label", "a=b")
    assert result.exit_code != 0
    assert not deploy.called


class _Captured(Exception):
    """Raised by a stubbed `service_manifests`, carrying what `deploy_service` handed it."""


def _deploy_capturing_manifests(monkeypatch, live_pool, **kwargs):
    """Drive `deploy_service` up to rendering, against a cluster whose pool is *live_pool*."""
    from unittest.mock import MagicMock

    from kubernetes import client as kclient

    from robovast.execution.cluster_execution import kube_client

    for api in ("CoreV1Api", "RbacAuthorizationV1Api", "AppsV1Api", "NetworkingV1Api"):
        monkeypatch.setattr(kclient, api, lambda *a, **k: MagicMock())
    monkeypatch.setattr(kube_client, "load_kube_config", lambda *a, **k: None)
    monkeypatch.setattr(service_deploy, "service_storage_from_cluster", lambda *a, **k: {})
    monkeypatch.setattr(service_deploy, "_resolve_data_node", lambda *a, **k: {})
    monkeypatch.setattr(service_deploy, "existing_auth_token", lambda *a, **k: "token")
    monkeypatch.setattr(service_deploy, "existing_index_password", lambda *a, **k: "pw")
    monkeypatch.setattr(service_deploy, "job_node_pool_from_cluster",
                        lambda *a, **k: dict(live_pool))

    def _capture(**kw):
        raise _Captured(kw)

    monkeypatch.setattr(service_deploy, "service_manifests", _capture)
    with pytest.raises(_Captured) as caught:
        service_deploy.deploy_service(namespace="default", config_name="rke2", **kwargs)
    return caught.value.args[0]


def test_deploy_service_carries_the_live_pool_forward_when_not_told(monkeypatch):
    """The regression: an unstated pool was rendered as an explicitly empty variable, so
    every upgrade widened campaigns onto every node."""
    rendered = _deploy_capturing_manifests(monkeypatch, {"node-pool": "primary"})
    assert rendered["job_node_labels"] == {"node-pool": "primary"}


def test_deploy_service_obeys_a_stated_empty_pool(monkeypatch):
    rendered = _deploy_capturing_manifests(monkeypatch, {"node-pool": "primary"},
                                           job_node_labels={})
    assert rendered["job_node_labels"] == {}


def _cluster_with(monkeypatch, *, dep=None, error=None):
    from kubernetes import client as kclient

    from robovast.execution.cluster_execution import kube_client

    class _Apps:
        def read_namespaced_deployment(self, name, namespace):
            if error is not None:
                raise error
            return dep

    monkeypatch.setattr(kube_client, "load_kube_config", lambda *a, **k: None)
    monkeypatch.setattr(kclient, "AppsV1Api", lambda *a, **k: _Apps())


def _live_deployment(pool):
    """A deployed service Deployment carrying *pool*, as the API would return it."""
    import json

    from kubernetes.client import ApiClient

    env = service_deploy._cluster_env("default", "rke2", {}, job_node_labels=pool)
    manifest = service_deploy._deployment_manifest("default", "img:latest", env=env)

    class _Response:
        data = json.dumps(manifest)

    return ApiClient().deserialize(_Response(), "V1Deployment")


def test_the_pool_is_read_back_from_the_live_deployment(monkeypatch):
    _cluster_with(monkeypatch, dep=_live_deployment({"node-pool": "primary"}))
    assert service_deploy.job_node_pool_from_cluster("default") == {"node-pool": "primary"}


def test_no_deployment_yet_has_no_pool(monkeypatch):
    from kubernetes.client.exceptions import ApiException
    _cluster_with(monkeypatch, error=ApiException(status=404))
    assert service_deploy.job_node_pool_from_cluster("default") == {}


def test_a_failed_read_is_not_mistaken_for_no_pool(monkeypatch):
    """Defaulting here would be the silent widening this reader exists to prevent."""
    from kubernetes.client.exceptions import ApiException
    _cluster_with(monkeypatch, error=ApiException(status=500))
    with pytest.raises(ApiException):
        service_deploy.job_node_pool_from_cluster("default")
