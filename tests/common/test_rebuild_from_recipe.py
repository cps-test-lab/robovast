# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""Rebuilding an image from the recipe it recorded, and comparing the software.

The recipe is a claim: that the pins it names are the *complete* set of inputs. `check_recipe`
asks two weaker questions -- are the pins present, do the archives still answer -- and neither
can notice something reaching the network unpinned. Only a rebuild can, which is what this is.

The two halves worth testing without a build are the ones that decide whether the answer means
anything: what the rebuild is *given*, and what counts as a difference.
"""

import sys
from pathlib import Path

# `tools/` is scripts, not an installed package -- the sibling checkers are reached the same
# way. The import has to follow the path insertion, hence the disables.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

# pylint: disable=wrong-import-position
from rebuild_from_recipe import build_command, diff_locks, parse_lock  # noqa: E402

BASE = "ros:jazzy-ros-base@sha256:" + "1" * 64
RECIPE = {
    "org.robovast.base-image": BASE,
    "org.robovast.ubuntu-snapshot": "20260819T003043Z",
    "org.robovast.ros-snapshot": "2026-06-18",
    "org.robovast.scenario-execution-ref": "abc123",
}


def _args(command):
    return {command[i + 1] for i, a in enumerate(command) if a == "--build-arg"}


def test_every_recorded_pin_is_handed_back_to_the_build():
    """A pin recorded but not passed reproduces the shape and not the software."""
    given = _args(build_command(RECIPE, tag="t", dockerfile="D", context="."))
    assert "UBUNTU_SNAPSHOT=20260819T003043Z" in given
    assert "ROS_SNAPSHOT=2026-06-18" in given
    assert "SCENARIO_EXECUTION_REF=abc123" in given


def test_the_base_is_split_into_the_two_args_the_dockerfile_interpolates():
    """`FROM ros:${ROS_DISTRO}-ros-base@${ROS_BASE_DIGEST}` needs both halves, and the recipe
    records the whole ref -- so the distro has to be read back out of it."""
    given = _args(build_command(RECIPE, tag="t", dockerfile="D", context="."))
    assert "ROS_BASE_DIGEST=sha256:" + "1" * 64 in given
    assert "ROS_DISTRO=jazzy" in given


def test_a_recipe_without_a_base_digest_passes_neither_half():
    """Rather than inventing a distro from a ref that does not name one."""
    given = _args(build_command({"org.robovast.ubuntu-snapshot": "x"},
                                tag="t", dockerfile="D", context="."))
    assert not any(a.startswith("ROS_BASE_DIGEST") or a.startswith("ROS_DISTRO") for a in given)


def test_an_identical_rebuild_is_no_difference_at_all():
    """The claim, stated as a test: same inputs, same software."""
    lock = {"apt": {"tree": "2.1.1-2"}, "pip": {"numpy": "1.26.4"}}
    assert diff_locks(lock, dict(lock)) == []


def test_a_version_that_moved_is_the_finding_this_exists_for():
    """An input the recipe named but did not actually pin."""
    [found] = diff_locks({"apt": {"tree": "2.1.1-2"}}, {"apt": {"tree": "2.2.0-1"}})
    assert found == {"kind": "apt", "name": "tree", "how": "changed",
                     "was": "2.1.1-2", "now": "2.2.0-1"}


def test_gone_and_arrived_are_reported_apart_because_they_mean_different_things():
    found = diff_locks({"apt": {"a": "1"}}, {"apt": {"b": "2"}})
    assert {d["how"] for d in found} == {"missing", "added"}


def test_the_lock_is_parsed_the_way_the_image_writes_it():
    """apt uses `=`, pip uses `==`, vcs uses `->` -- one format each, and a wrong separator
    would silently produce an empty lock, which reads as "nothing changed"."""
    assert parse_lock("apt", "tree=2.1.1-2\nzlib1g=1:1.3\n") == {"tree": "2.1.1-2",
                                                                "zlib1g": "1:1.3"}
    assert parse_lock("pip", "numpy==1.26.4\n") == {"numpy": "1.26.4"}
    assert parse_lock("vcs", "main -> deadbeef\n") == {"main": "deadbeef"}


def test_a_blank_lock_is_empty_rather_than_a_row_of_junk():
    assert parse_lock("apt", "") == {}
    assert parse_lock("apt", "\n  \nnot-a-pair\n") == {}
