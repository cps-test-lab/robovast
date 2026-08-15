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
