# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""An aux container's ``family:`` image is resolved before it reaches a Pod.

`family:<member>` is symbolic: core resolves it to `<project>/<member>:<tag>` once a campaign
exists. The aux Pod manifest bakes the image in, so an unresolved ref would reach kubelet, which
reads it as a Docker Hub library image and fails with what looks like a credentials error.
"""

import pathlib

import pytest

from robovast.common.variation.container_runner import ContainerSpec
from robovast.execution.cluster_execution.container_runner import (TRANSFER_CONTAINER,
                                                                   _aux_image,
                                                                   build_aux_pod_manifest)

FAMILY = "family:robovast-roqsim"


@pytest.fixture
def project(monkeypatch):
    """A deployment's project and tag, as the service pod carries them."""
    monkeypatch.setenv("ROBOVAST_PROJECT", "registry.example.com/robovast")
    monkeypatch.setenv("ROBOVAST_PROJECT_TAG", "2026-08-28")


def test_a_family_ref_is_resolved(project):
    assert _aux_image(FAMILY) == "registry.example.com/robovast/robovast-roqsim:2026-08-28"


def test_a_concrete_ref_is_left_exactly_as_written(project):
    """An image a campaign names is used verbatim everywhere else; an aux container is no place
    to start rewriting one."""
    for ref in ("registry.example.org/team/tool:1.2.3",
                "tool@sha256:" + "0" * 64,
                "alpine:latest"):
        assert _aux_image(ref) == ref


def _manifest(*specs, **kwargs):
    return build_aux_pod_manifest(
        "aux-pod", list(specs), "default",
        stage_dir=lambda slot: pathlib.Path("/results/_staged") / slot,
        token_for=lambda scope: "tok", **kwargs)


def test_the_pod_manifest_carries_the_resolved_image(project):
    """THE regression: this put `family:robovast-roqsim` into the Pod and the pull failed."""
    manifest = _manifest(ContainerSpec(image=FAMILY))
    # The aux containers are the ones this is about; the transfer container is the sidecar,
    # which resolved its own image.
    pod = manifest["spec"]
    images = [c["image"] for c in pod["containers"] if c["name"] != TRANSFER_CONTAINER]
    assert FAMILY not in images, "an unresolved family ref reached the Pod manifest"
    assert "registry.example.com/robovast/robovast-roqsim:2026-08-28" in images


def test_the_pull_secret_reaches_the_manifest_when_given(project):
    """A resolved family image lives in the deployment's private registry, so the aux pod needs
    the same credential the run Jobs use."""
    manifest = _manifest(ContainerSpec(image=FAMILY), pull_secret="regcred")
    assert manifest["spec"]["imagePullSecrets"] == [{"name": "regcred"}]


def test_no_pull_secret_means_none_is_declared(project):
    """Referencing a Secret that does not exist keeps the pod from starting, so an absent
    credential must stay absent rather than be invented."""
    manifest = _manifest(ContainerSpec(image=FAMILY))
    assert "imagePullSecrets" not in manifest["spec"]


# -- and the policy that decides whether that image is re-checked ---------------------


def _policies(manifest):
    """``{container name: imagePullPolicy}`` over every container in *manifest*."""
    pod = manifest["spec"]
    return {c["name"]: c["imagePullPolicy"]
            for c in list(pod.get("initContainers", [])) + list(pod["containers"])}


def test_a_tagged_aux_image_is_re_checked(project):
    """A tag can be re-pushed under us, and a `family:` member is the deployment's floating one.

    Hard-coded ``IfNotPresent`` made the aux pod run whatever copy its node happened to hold:
    on a node that had pulled the tag once, a republished image never arrived, and the world
    query and the override pre-check then answered from a build the campaign's own pods --
    pinned to a digest before they are written -- were not running.
    """
    manifest = _manifest(ContainerSpec(image=FAMILY))

    policies = _policies(manifest)
    assert policies["aux-robovast-roqsim"] == "Always"
    # The sidecar moves the workspace in and out and is resolved the same floating way.
    assert policies[TRANSFER_CONTAINER] == "Always"


def test_a_digest_pinned_aux_image_is_not(project):
    """A digest names its bytes, so "if not present" cannot serve anything stale."""
    ref = "registry.example.org/team/tool@sha256:" + "0" * 64
    manifest = _manifest(ContainerSpec(image=ref))

    assert _policies(manifest)[ContainerSpec(image=ref).container_name()] == "IfNotPresent"
