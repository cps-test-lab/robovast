# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""An aux container's ``family:`` image is resolved before it reaches a Pod.

`family:<member>` is symbolic: core resolves it to `<project>/<member>:<tag>` once a campaign
exists. The local lane does that when it builds the runner
(``config_generation._make_container_runner``), so a family ref has always worked there. The
cluster lane bakes the image into a Pod manifest instead, and passed it through unresolved --
kubelet then read it as a Docker Hub library image:

    failed to resolve reference "docker.io/library/family:robovast-roqsim":
    pull access denied ... insufficient_scope

which reads like a credentials problem rather than an unresolved reference, and cost 300 s of
readiness timeout per attempt to say so.

Latent until an aux image could be a family member at all: aux pods were only created for
variations, and the ones in the wild name concrete refs. An `execution.generate` entry naming
`family:robovast-roqsim` -- the whole point being that the deployment chooses the project -- is
what first exercised it.
"""

import pytest

from robovast.common.variation.container_runner import ContainerSpec
from robovast.execution.cluster_execution.container_runner import (_aux_image,
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


def test_the_pod_manifest_carries_the_resolved_image(project):
    """THE regression: this put `family:robovast-roqsim` into the Pod and the pull failed."""
    manifest = build_aux_pod_manifest(
        "aux-pod", [ContainerSpec(image=FAMILY)], "default"
    )
    # initContainers only exist when the store mirror is wired; the aux containers are the ones
    # this is about, and mc-tools is the sidecar that already resolved its own image.
    pod = manifest["spec"]
    images = [c["image"] for c in list(pod.get("initContainers", [])) + list(pod["containers"])
              if c["name"] != "mc-tools"]
    assert FAMILY not in images, "an unresolved family ref reached the Pod manifest"
    assert "registry.example.com/robovast/robovast-roqsim:2026-08-28" in images


def test_the_pull_secret_reaches_the_manifest_when_given(project):
    """A resolved family image lives in the deployment's private registry, so the aux pod needs
    the same credential the run Jobs use."""
    manifest = build_aux_pod_manifest(
        "aux-pod", [ContainerSpec(image=FAMILY)], "default", pull_secret="regcred"
    )
    assert manifest["spec"]["imagePullSecrets"] == [{"name": "regcred"}]


def test_no_pull_secret_means_none_is_declared(project):
    """Referencing a Secret that does not exist keeps the pod from starting, so an absent
    credential must stay absent rather than be invented."""
    manifest = build_aux_pod_manifest("aux-pod", [ContainerSpec(image=FAMILY)], "default")
    assert "imagePullSecrets" not in manifest["spec"]
