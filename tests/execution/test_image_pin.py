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
