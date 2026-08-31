# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What each container's image was built FROM -- the half a digest cannot answer.

A digest identifies bytes and is reproducible exactly as long as the registry keeps them. The
Dockerfile already pins everything a rebuild needs -- the base by digest, both apt archives to a
dated snapshot so a rebuild resolves the same versions rather than whatever is current -- but
those are build ARGs, invisible from outside the build. The labels are what carry them out.

This block was written by nobody and read by nobody on the cluster lane: it reads labels with
`docker inspect`, and the controller pod that writes execution.yaml ships no docker CLI, so
every lookup returned "". Its docstring said absent means "not knowable here", which was true
and useless -- the answer was one registry read away, and the protocol check was already making
that read.
"""

from robovast.common.execution import _BUILD_REF_LABELS, image_build_refs

BASE = "ros:jazzy-ros-base@sha256:" + "1" * 64


def _labels(**over):
    full = {
        "org.opencontainers.image.revision": "abc1234",
        "org.opencontainers.image.source": "https://github.com/cps-test-lab/robovast",
        "org.robovast.base-image": BASE,
        "org.robovast.ubuntu-snapshot": "20260819T003043Z",
        "org.robovast.ros-snapshot": "2026-06-18",
    }
    full.update(over)
    return full


def test_supplied_labels_are_used_where_no_docker_exists():
    """The cluster lane's whole problem: the answer exists, the probe cannot reach it."""
    refs = image_build_refs({"sut": {"image": BASE}}, {"sut": BASE},
                            labels_by_role={"sut": _labels()})
    assert refs["sut"]["base_image"] == BASE
    assert refs["sut"]["ubuntu_snapshot"] == "20260819T003043Z"
    assert refs["sut"]["ros_snapshot"] == "2026-06-18"
    assert refs["sut"]["revision"] == "abc1234"


def test_a_role_with_no_supplied_labels_records_nothing_rather_than_guessing():
    """Absent must stay "not knowable", never "nothing was used" -- a rebuild would follow it."""
    refs = image_build_refs({"sut": {"image": BASE}}, {"sut": BASE},
                            labels_by_role={"sut": {}})
    assert "sut" not in refs


def test_the_recipe_covers_every_pin_the_dockerfile_makes():
    """The set is the point: a rebuild needs the base AND both dated archives.

    Recording two of the three would produce a rebuild that starts from the right base and then
    installs whatever the archives hold today -- reproducing the shape and not the software.
    """
    for key in ("base_image", "ubuntu_snapshot", "ros_snapshot"):
        assert key in _BUILD_REF_LABELS, f"{key} is not collected"
    refs = image_build_refs({"sut": {}}, {"sut": BASE}, labels_by_role={"sut": _labels()})
    assert {"base_image", "ubuntu_snapshot", "ros_snapshot"} <= set(refs["sut"])


def test_a_declared_provenance_still_wins_over_the_image():
    """Unchanged: the author is describing an image robovast did not build, so a label found on
    it was put there by somebody else and may describe a base rather than this image."""
    refs = image_build_refs(
        {"sut": {"image": BASE, "provenance": {"source": "https://example.com/theirs",
                                               "revision": "theirs"}}},
        {"sut": BASE}, labels_by_role={"sut": _labels()})
    assert refs["sut"]["source"] == "https://example.com/theirs"
    assert refs["sut"]["revision"] == "theirs"
    assert refs["sut"]["declared"] is True
