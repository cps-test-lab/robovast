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

from robovast.execution.cluster_execution import service_deploy


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
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))
    monkeypatch.setattr(service_deploy, "published_host",
                        lambda *a, **k: "robovast.example.org")
    for name in ("install_kueue_helm", "verify_kueue_admission_ready",
                 "apply_controller_rbac", "apply_kueue_queues",
                 "ensure_nvidia_device_plugin"):
        monkeypatch.setattr(cluster_setup, name, mock.Mock())
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
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))

    def _unreachable(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(service_deploy, "published_host", _unreachable)
    for name in ("install_kueue_helm", "verify_kueue_admission_ready",
                 "apply_controller_rbac", "apply_kueue_queues",
                 "ensure_nvidia_device_plugin"):
        monkeypatch.setattr(cluster_setup, name, mock.Mock())
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
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: (None, None))

    def _must_not_be_called(*_a, **_k):
        raise AssertionError("published_host was called despite an explicit ingress_host")

    monkeypatch.setattr(service_deploy, "published_host", _must_not_be_called)
    for name in ("install_kueue_helm", "verify_kueue_admission_ready",
                 "apply_controller_rbac", "apply_kueue_queues",
                 "ensure_nvidia_device_plugin"):
        monkeypatch.setattr(cluster_setup, name, mock.Mock())
    monkeypatch.setattr(cluster_setup, "get_cluster_config",
                        lambda name: mock.Mock(get_cluster_kwargs=lambda: {}))

    cluster_setup.setup_server(config_name="rke2", namespace="default",
                               service_kwargs={"ingress_host": "given.example.org",
                                               "insecure_http": True})

    assert deploy.call_args.kwargs.get("registry_host") == "given.example.org"


def test_upgrade_reconciles_the_kueue_queues(monkeypatch):
    """`upgrade` is the command operators use to move versions, and the ClusterQueue's
    covered resources are coupled to what the deployed backend requests. Skipping the
    reconcile meant a build that started asking for a new resource kind could be rolled
    onto a queue that does not cover it -- which Kueue answers by suspending every job
    forever rather than failing, so the campaign hangs. Reached through the command that
    looks safe, which is what makes it worth a test.
    """
    from unittest import mock

    from click.testing import CliRunner

    from robovast.execution.cluster_execution import cli as cluster_cli
    from robovast.execution.cluster_execution import cluster_setup, kubernetes_kueue

    apply_queues = mock.Mock()
    monkeypatch.setattr(kubernetes_kueue, "apply_kueue_queues", apply_queues)
    monkeypatch.setattr(cluster_setup, "apply_controller_rbac", mock.Mock())
    monkeypatch.setattr(service_deploy, "read_service_config_from_cluster",
                        lambda *a, **k: ("rke2", {"namespace": "default"}))
    monkeypatch.setattr(service_deploy, "published_host", lambda *a, **k: "")
    monkeypatch.setattr(service_deploy, "deploy_service", mock.Mock())
    monkeypatch.setattr(service_deploy, "wait_for_service_ready", mock.Mock())
    monkeypatch.setattr(service_deploy, "wait_for_rollout", lambda **k: True)
    monkeypatch.setattr(service_deploy, "running_image_digest", lambda *a, **k: "sha256:abc")
    monkeypatch.setattr(service_deploy, "reconcile_registry_ingress_path",
                        lambda **k: False)

    result = CliRunner().invoke(cluster_cli.upgrade, ["-n", "default"])

    assert result.exit_code == 0, result.output
    assert apply_queues.called, "upgrade left the Kueue queues unreconciled"
    assert apply_queues.call_args.kwargs["namespace"] == "default"
