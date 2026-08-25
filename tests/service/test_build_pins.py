# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Every input an image build starts from is pinned, and pinned to one point in time.

A campaign a year old can only be rebuilt if the archives it installed from still serve those
versions, which the rolling Ubuntu and ROS archives do not: they drop superseded versions
continuously, so `apt-get install pkg=<ver>` against them fails long before the campaign is
retriggered. What keeps a rebuild possible is a dated archive, and what keeps *that* honest is
that no rolling source survives beside it -- apt prefers whichever version is higher, so one
leftover source silently undoes the pin.

Deliberately textual, for the reason `test_controller_image_installs_every_lane` gives: building
these images needs Docker, a registry and minutes, while reading their pins needs neither and
catches a regression where it is introduced. The build-time counterpart -- asserting on apt's own
view of its sources -- lives in the Dockerfile itself, because only a real build can check it.
"""

import pathlib
import re

import pytest

CONTAINER = pathlib.Path(__file__).resolve().parents[2] / "container"
#: Every Dockerfile whose FROMs must be digest-pinned. The snapshot probe is excluded: it exists
#: to *try* candidate dates against live archives, so a pin there would defeat its purpose.
DOCKERFILES = sorted(p for p in CONTAINER.rglob("Dockerfile*")
                     if "pins" not in p.parts)
ROBOVAST_DOCKERFILE = CONTAINER / "robovast" / "Dockerfile"

_FROM = re.compile(r"^FROM\s+(?P<ref>\S+)", re.MULTILINE)


def test_the_dockerfile_set_is_what_this_module_thinks_it_is():
    """A new Dockerfile must not be able to arrive unpinned and untested."""
    assert {p.name for p in DOCKERFILES} == {"Dockerfile", "Dockerfile.roqsim"}
    assert len(DOCKERFILES) == 4, [str(p) for p in DOCKERFILES]


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_from_is_pinned_by_digest(path):
    """A tag resolves to whatever it points at on the day of the build.

    `scratch` and `${VAR}` refs are the two honest exceptions: the first has no content to pin,
    and the second is resolved by the caller -- `ARG ROS_BASE_DIGEST` and `ARG BASE_IMAGE` carry
    those pins instead, which the next test checks.
    """
    for match in _FROM.finditer(path.read_text(encoding="utf-8")):
        ref = match.group("ref")
        if ref == "scratch" or "${" in ref:
            continue
        assert "@sha256:" in ref, f"{path}: unpinned FROM {ref}"


def test_the_ros_base_digest_is_pinned():
    text = ROBOVAST_DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ARG ROS_BASE_DIGEST=sha256:[0-9a-f]{64}$", text, re.MULTILINE)


def test_the_apt_archives_are_dated():
    """Both snapshots pinned, and the Ubuntu one NOT tied to the ROS date.

    Tying them together is the obvious thing and it is wrong -- see the Dockerfile's own comment.
    `osrf/ros` is rebuilt daily so its Ubuntu packages are current, while the newest ROS snapshot
    can be months old; a Ubuntu snapshot older than the base offers `udev 255.4-1ubuntu8.16`
    against a base carrying `libudev1 8.17`, and every install pulling udev in dies on "held
    broken packages". The Ubuntu stamp therefore comes from the base image, so the only thing
    assertable here is that it is at or after the ROS date, never before it.
    """
    text = ROBOVAST_DOCKERFILE.read_text(encoding="utf-8")
    ros = re.search(r"^ARG ROS_SNAPSHOT=(\d{4}-\d{2}-\d{2})$", text, re.MULTILINE)
    ubuntu = re.search(r"^ARG UBUNTU_SNAPSHOT=(\d{8})T\d{6}Z$", text, re.MULTILINE)
    assert ros and ubuntu
    assert ubuntu.group(1) >= ros.group(1).replace("-", ""), \
        "a Ubuntu snapshot older than the ROS one cannot satisfy the base image's own apt state"


def test_the_snapshot_key_is_a_full_fingerprint():
    """A 64-bit key id is cheap to collide, and this key decides what every ROS package is
    trusted against. The Dockerfile also verifies it after import -- pinning a fingerprint it
    never checks would only look careful."""
    text = ROBOVAST_DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ARG ROS_SNAPSHOT_FPR=[0-9A-F]{40}$", text, re.MULTILINE)
    assert "--with-fingerprint" in text, "the pinned fingerprint is never verified"


def test_rolling_archives_are_removed_and_the_removal_is_checked():
    """Both halves, because the removal alone has already been wrong once.

    `grep -r` skips symlinks it meets while recursing, and the base ships `ros2.sources` as a
    symlink into /usr/share/ros-apt-source/ -- so `-r` left the rolling ROS repo in place, apt
    kept preferring it, and the snapshot pin did nothing at all. The build-time assertion on
    `apt-cache policy` is what turns that class of mistake from silent into loud.
    """
    text = ROBOVAST_DOCKERFILE.read_text(encoding="utf-8")
    assert "grep -Rl" in text, "grep -r skips the symlinked ros2.sources"
    assert "grep -rl -e archive" not in text
    for host in ("archive.ubuntu.com", "security.ubuntu.com", "packages.ros.org"):
        assert host in text, f"{host} is never removed"
    assert "a rolling apt archive survived" in text, "the removal is never verified"
