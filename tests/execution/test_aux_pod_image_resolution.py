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


# -- a campaign's aux pod runs the digests its launch fixed ----------------------------

_AUX_DIGEST = "registry.example.com/robovast/robovast-roqsim@sha256:" + "a" * 64
_SIDECAR_DIGEST = "registry.example.com/robovast/robovast-sidecar@sha256:" + "b" * 64


def test_a_campaign_family_ref_resolves_from_its_own_project(project):
    """``--image-project`` reaches the aux helper of the campaign that asked for it."""
    assert _aux_image(FAMILY, project="registry.example.com/dev", tag="feature-x") == \
        "registry.example.com/dev/robovast-roqsim:feature-x"


def test_the_manifest_runs_the_images_it_is_handed(project):
    """The campaign's pins, not the environment: both the aux container and the transfer."""
    spec = ContainerSpec(image=FAMILY)
    manifest = _manifest(spec, images={spec.container_name(): _AUX_DIGEST},
                         sidecar_image=_SIDECAR_DIGEST)

    images = {c["name"]: c["image"] for c in manifest["spec"]["containers"]}
    assert images == {spec.container_name(): _AUX_DIGEST, TRANSFER_CONTAINER: _SIDECAR_DIGEST}
    assert set(_policies(manifest).values()) == {"IfNotPresent"}


def _options(**fields):
    from robovast.execution.backends import RunOptions
    return RunOptions(**fields)


def _launched(tmp_path):
    """A campaign directory whose launch record a launch has just written."""
    from robovast.common.campaign_data import write_launch_record
    from robovast.service.interface import CreateCampaignRequest
    write_launch_record(tmp_path, CreateCampaignRequest(workspace_id="ws"))
    return tmp_path


def test_pins_fix_and_record_each_image_once(project, tmp_path):
    """Fixed on the first ask, recorded in the launch record before the pod exists, and the
    same digest on every later ask -- so a replaced pod runs the bytes its predecessor did."""
    from robovast.common.campaign_data import read_launch_record
    from robovast.execution.cluster_execution.container_runner import CampaignImagePins

    asked = []

    def read(ref):
        asked.append(ref)
        return ref.rsplit(":", 1)[0] + "@sha256:" + "c" * 64, ""

    options = _options(image_project="registry.example.com/dev", image_project_tag="feature-x")
    pins = CampaignImagePins(_launched(tmp_path), options, read)
    spec = ContainerSpec(image=FAMILY)

    aux, sidecar = pins.aux(spec), pins.sidecar()
    assert (pins.aux(spec), pins.sidecar()) == (aux, sidecar)
    assert asked == ["registry.example.com/dev/robovast-roqsim:feature-x",
                     "registry.example.com/dev/robovast-sidecar:feature-x"]

    record = read_launch_record(tmp_path)
    assert record["aux_images"] == {spec.container_name(): aux}
    assert record["sidecar_image"] == sidecar
    assert options.aux_images == {spec.container_name(): aux}
    assert options.sidecar_image == sidecar


def test_an_unreadable_aux_digest_refuses_the_launch(project, tmp_path):
    from robovast.common.errors import CampaignConfigError
    from robovast.execution.cluster_execution.container_runner import CampaignImagePins

    pins = CampaignImagePins(_launched(tmp_path), _options(),
                             lambda ref: ("", "the registry does not have it"))
    with pytest.raises(CampaignConfigError) as e:
        pins.aux(ContainerSpec(image=FAMILY))
    assert "registry.example.com/robovast/robovast-roqsim:2026-08-28" in str(e.value)
    assert "does not have it" in str(e.value)


def test_a_replay_runs_the_recorded_digests_and_asks_nobody(project, tmp_path, monkeypatch):
    """The environment moved; the replay does not. And a helper its source never asked for is
    refused rather than resolved."""
    from robovast.common.errors import CampaignConfigError
    from robovast.execution.cluster_execution.container_runner import CampaignImagePins

    monkeypatch.setenv("ROBOVAST_PROJECT", "registry.example.com/elsewhere")
    spec = ContainerSpec(image=FAMILY)
    options = _options(images_fixed=True, sidecar_image=_SIDECAR_DIGEST,
                       aux_images={spec.container_name(): _AUX_DIGEST})
    pins = CampaignImagePins(_launched(tmp_path), options,
                             lambda ref: pytest.fail(f"a replay asked the registry for {ref}"))

    assert pins.aux(spec) == _AUX_DIGEST
    assert pins.sidecar() == _SIDECAR_DIGEST
    with pytest.raises(CampaignConfigError, match="aux-tool"):
        pins.aux(ContainerSpec(image="registry.example.org/team/tool:1"))


def test_a_replay_without_a_recorded_sidecar_is_refused(project, tmp_path):
    from robovast.common.errors import CampaignConfigError
    from robovast.execution.cluster_execution.container_runner import CampaignImagePins

    pins = CampaignImagePins(_launched(tmp_path), _options(images_fixed=True),
                             lambda ref: pytest.fail("a replay asked the registry"))
    with pytest.raises(CampaignConfigError, match="sidecar"):
        pins.sidecar()
