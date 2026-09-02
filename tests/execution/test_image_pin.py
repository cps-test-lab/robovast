# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Pinning the SUT image to the immutable digest the run pods used."""

import types

import pytest

from robovast.execution.cluster_execution.kubernetes_backend import (pullable_digest,
                                                                     resolve_image_digest)
from robovast.execution.cluster_execution.postprocess_job import campaign_execution_image


@pytest.mark.parametrize("image_id, expected", [
    ("ghcr.io/o/sut@sha256:abc", "ghcr.io/o/sut@sha256:abc"),
    ("docker-pullable://ghcr.io/o/sut@sha256:def", "ghcr.io/o/sut@sha256:def"),
    ("sha256:localdockerid", None),   # not pullable → rejected
    ("ghcr.io/o/sut:latest", None),   # a tag, no digest
    ("", None),
    (None, None),
])
def test_pullable_digest(image_id, expected):
    assert pullable_digest(image_id) == expected


def test_resolve_image_digest_prefers_matching_container():
    statuses = [
        types.SimpleNamespace(image="sidecar:1", image_id="ghcr.io/o/side@sha256:bbb"),
        types.SimpleNamespace(image="sut:latest", image_id="ghcr.io/o/sut@sha256:aaa"),
    ]
    assert resolve_image_digest(statuses, "sut:latest") == "ghcr.io/o/sut@sha256:aaa"


def test_resolve_image_digest_falls_back_to_any_digest():
    statuses = [types.SimpleNamespace(image="x", image_id="ghcr.io/o/x@sha256:ccc")]
    assert resolve_image_digest(statuses, "sut:latest") == "ghcr.io/o/x@sha256:ccc"


def test_resolve_image_digest_none_when_unpinnable():
    statuses = [types.SimpleNamespace(image="sut:latest", image_id="sha256:localid")]
    assert resolve_image_digest(statuses, "sut:latest") is None
    assert resolve_image_digest([], "sut:latest") is None


def _write_execution_yaml(tmp_path, **fields):
    import yaml
    exec_dir = tmp_path / "_execution"
    exec_dir.mkdir()
    (exec_dir / "execution.yaml").write_text(yaml.safe_dump(fields))


def test_campaign_execution_image_prefers_pinned_digest(tmp_path):
    _write_execution_yaml(tmp_path, image="ghcr.io/o/sut:latest",
                          image_revision="ghcr.io/o/sut@sha256:aaa")
    # The pinned digest wins so a re-postprocess uses the exact image the runs used.
    assert campaign_execution_image(str(tmp_path)) == "ghcr.io/o/sut@sha256:aaa"


def test_campaign_execution_image_falls_back_to_tag(tmp_path):
    # A local-docker id (the off-cluster/unpinned case) is not a digest ref → use tag.
    _write_execution_yaml(tmp_path, image="ghcr.io/o/sut:latest",
                          image_revision="unknown")
    assert campaign_execution_image(str(tmp_path)) == "ghcr.io/o/sut:latest"


def test_every_planned_container_reaches_the_launch_record(tmp_path):
    """Keyed by container name AND by every role it backs, roles first so a name wins.

    A role is how a reader asks the question ("which simulator ran?"); the name is what the
    pod calls it. A stepped simulator makes one container answer to both, so recording only
    names loses the role the campaign was actually configured by.
    """
    from robovast.common.campaign_data import read_launch_record, write_launch_record
    from robovast.execution.cluster_execution.kubernetes_backend import KubernetesBackend
    from robovast.service.interface import CreateCampaignRequest

    write_launch_record(tmp_path, CreateCampaignRequest(
        workspace_id="ws", config_path="p.vast", config_filter="", campaign_name="c",
        runs=1, postprocess=True, upload_to_share=False, show_gui=False, description=""))

    runner = types.SimpleNamespace(plan=types.SimpleNamespace(containers=(
        types.SimpleNamespace(name="sut", image="r.example.com/sut@sha256:a", roles=("sut",)),
        # One container backing two roles -- the stepped-simulator shape.
        types.SimpleNamespace(name="scenario", image="r.example.com/sc@sha256:b",
                              roles=("scenario", "simulation")),
        # No image: nothing to record, and it must not write a null.
        types.SimpleNamespace(name="sidecar", image=None, roles=()),
    )))

    KubernetesBackend._record_launch_images(str(tmp_path), runner)

    images = read_launch_record(tmp_path)["images"]
    assert images["sut"] == "r.example.com/sut@sha256:a"
    assert images["simulation"] == "r.example.com/sc@sha256:b"
    assert images["scenario"] == "r.example.com/sc@sha256:b"
    assert "sidecar" not in images
    # Idempotent: a search re-runs this per batch against an unchanged plan.
    before = dict(images)
    KubernetesBackend._record_launch_images(str(tmp_path), runner)
    assert read_launch_record(tmp_path)["images"] == before


# -- what the record names before the batch has run -------------------------------------

def _runner(containers, resolved=None):
    """A stand-in for the batch runner: a pinned plan, and nothing read off a pod yet."""
    plan = types.SimpleNamespace(containers=tuple(containers))
    return types.SimpleNamespace(plan=plan, _resolved_image_digests=resolved or {})


def _container(name, image, roles=()):
    return types.SimpleNamespace(name=name, image=image, roles=tuple(roles))


def test_planned_images_answers_by_role_and_by_name():
    """A role is how a reader asks; the name is what the pod calls it."""
    from robovast.execution.cluster_execution.kubernetes_backend import _planned_images

    images = _planned_images(_runner([
        _container("roqsim", "ghcr.io/o/roqsim@sha256:aaa", roles=("simulation",)),
        _container("robovast", "ghcr.io/o/rv@sha256:bbb", roles=("scenario",)),
    ]))

    assert images["simulation"] == "ghcr.io/o/roqsim@sha256:aaa"
    assert images["roqsim"] == "ghcr.io/o/roqsim@sha256:aaa"
    assert images["scenario"] == "ghcr.io/o/rv@sha256:bbb"


def test_the_plan_names_the_bytes_before_any_pod_has_run():
    """The regression: an early record that named its images by TAG.

    ``_resolved_image_digests`` is read back off a pod, so before the batch it is empty — and
    the record is now written before the batch. Reading only that source left the campaign
    saying what it had ASKED for and not what it would run, which is enough to refuse building
    any artifact from it: an artifact that cannot be attributed to the bytes that produced it
    is not attributable at all. The plan carries those bytes already.
    """
    from robovast.execution.cluster_execution.kubernetes_backend import _planned_images

    runner = _runner([_container("roqsim", "ghcr.io/o/roqsim@sha256:aaa", roles=("simulation",))])
    assert runner._resolved_image_digests == {}
    assert _planned_images(runner)["simulation"] == "ghcr.io/o/roqsim@sha256:aaa"


def test_an_unpinnable_ref_is_left_out_rather_than_recorded_as_a_revision():
    """Pinning is fail-soft, so a ref that could not be resolved is still a tag.

    A tag in ``image_revisions`` would claim an identity it does not have — the same name can
    be re-pushed between one batch and the next. It is left out, and the write after the batch
    fills it in from the pod that actually ran it.
    """
    from robovast.common.campaign_data import image_identifies_bytes
    from robovast.execution.cluster_execution.kubernetes_backend import _planned_images

    images = _planned_images(_runner([
        _container("roqsim", "ghcr.io/o/roqsim:latest", roles=("simulation",)),
        _container("robovast", "ghcr.io/o/rv@sha256:bbb", roles=("scenario",)),
    ]))
    named = {k: v for k, v in images.items() if image_identifies_bytes(v)}

    assert "simulation" not in named
    assert named["scenario"] == "ghcr.io/o/rv@sha256:bbb"
