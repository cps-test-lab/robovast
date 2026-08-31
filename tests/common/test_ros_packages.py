# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""``ros_packages``: the third way a container gets code, for the packages the other two cannot.

A ROS package with a ``source:`` entry and no ``release:`` block in ``ros/rosdistro`` has no
Debian on any distro and no PyPI distribution -- ``px4_msgs`` is one -- so neither
``system_packages`` nor ``python_packages`` can express it. What is asserted here is that
declaring one is enough on its own: it is validated offline, it makes the container build an
image, and it reaches the build spec whichever block the author wrote it in.
"""

import os

import pytest
import yaml

from robovast.common.config import validate_config
from robovast.common.config_validation import validate_project_file
from robovast.common.containers import (containers_without_a_resolvable_image, plan_containers,
                                        ros_repo_name)
from robovast.common.execution import IMAGE_TIER_BUILT, image_provenance_tier
from robovast.service.image_build import extract_build_specs
from robovast.service.retrigger import _builds_an_image

PX4_MSGS = {"git": "https://github.com/PX4/px4_msgs.git",
            "ref": "598c7aad7b2386f9406ebd2a2f841619fddc3c78"}


def _cfg(**containers):
    return {"version": 3, "execution": {"containers": containers, "runs": 1}}


def _vast(tmp_path, containers, name="c.vast"):
    path = os.path.join(tmp_path, name)
    doc = {"version": 3, "scenario": "s.osc",
           "execution": {"containers": containers, "runs": 1}}
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(doc, f)
    open(os.path.join(tmp_path, "s.osc"), "w", encoding="utf-8").close()
    return path


# -- the schema --------------------------------------------------------------------

def test_a_repo_and_a_pin_is_the_whole_declaration():
    """``packages:`` is optional because colcon discovers what a repo contains."""
    c = validate_config(_cfg(scenario={"image": "base:1", "ros_packages": [PX4_MSGS]}))
    entry = c.execution.containers["scenario"].ros_packages[0]
    assert entry.git == PX4_MSGS["git"] and entry.packages is None


def test_a_branch_is_refused_as_a_ref():
    """A layer's cache key is its command text, so a branch is read once and served forever."""
    with pytest.raises(ValueError, match="looks like a branch"):
        validate_config(_cfg(scenario={"image": "b:1",
                                       "ros_packages": [{"git": "https://h/r.git",
                                                         "ref": "main"}]}))


def test_a_missing_ref_is_refused():
    with pytest.raises(ValueError):
        validate_config(_cfg(scenario={"image": "b:1",
                                       "ros_packages": [{"git": "https://h/r.git"}]}))


def test_a_missing_git_is_refused():
    with pytest.raises(ValueError):
        validate_config(_cfg(scenario={"image": "b:1", "ros_packages": [{"ref": "v1.0"}]}))


# -- offline validation ------------------------------------------------------------

def test_validation_reports_a_missing_ref_and_a_duplicate_repo(tmp_path):
    """Everything checkable without the network, reported at submit rather than mid-build."""
    path = _vast(str(tmp_path), {"scenario": {"image": "b:1", "ros_packages": [
        {"git": "https://github.com/example/a.git"},
        {"git": "https://github.com/example/b.git", "ref": "v1"},
        {"git": "https://github.com/example/b.git", "ref": "v2"},
    ]}})
    messages = " ".join(p["message"] for p in validate_project_file(path)["problems"])
    assert "no 'ref'" in messages
    assert "declared twice" in messages


def test_two_repos_with_one_basename_cannot_share_the_workspace(tmp_path):
    path = _vast(str(tmp_path), {"scenario": {"image": "b:1", "ros_packages": [
        {"git": "https://github.com/one/msgs.git", "ref": "v1"},
        {"git": "https://github.com/two/msgs.git", "ref": "v2"},
    ]}})
    messages = " ".join(p["message"] for p in validate_project_file(path)["problems"])
    assert "src/msgs" in messages


def test_nothing_is_fetched_to_validate(tmp_path):
    """A well-formed declaration validates offline; the repository is never contacted."""
    path = _vast(str(tmp_path), {"scenario": {"image": "b:1", "ros_packages": [PX4_MSGS]}})
    assert [p for p in validate_project_file(path)["problems"]
            if p["field"] and "ros_packages" in p["field"]] == []


def test_the_repo_directory_is_the_basename_without_dot_git():
    assert ros_repo_name("https://github.com/PX4/px4_msgs.git") == "px4_msgs"
    assert ros_repo_name("https://github.com/PX4/px4_msgs") == "px4_msgs"


# -- it must trigger a derived build, on its own -----------------------------------

def test_ros_packages_alone_makes_the_container_build():
    c = validate_config(_cfg(scenario={"image": "base:1", "ros_packages": [PX4_MSGS]}))
    assert c.execution.containers["scenario"].builds_image()
    plan = plan_containers({"containers": {"scenario": {"image": "base:1",
                                                        "ros_packages": [PX4_MSGS]}}})
    assert plan.main.builds
    assert extract_build_specs(c)["scenario"].ros_packages[0]["git"] == PX4_MSGS["git"]


def test_ros_packages_alone_is_a_built_image_tier():
    """Robovast builds it, so its inputs are recorded and no ``provenance:`` is owed."""
    tier, _ = image_provenance_tier("sut", {"image": "vendor/nav2:humble",
                                            "ros_packages": [PX4_MSGS]})
    assert tier == IMAGE_TIER_BUILT


def test_ros_packages_alone_supplies_a_container_its_image():
    assert containers_without_a_resolvable_image(
        {"containers": {"scenario": {}, "sut": {"ros_packages": [PX4_MSGS]}}}) == []


def test_a_retrigger_knows_the_campaign_builds_images():
    assert _builds_an_image(
        {"execution": {"containers": {"sut": {"ros_packages": [PX4_MSGS]}}}})


def test_an_empty_packages_list_is_refused():
    """Omission means "everything"; an empty list would silently mean "nothing"."""
    with pytest.raises(ValueError, match="omit it"):
        validate_config(_cfg(scenario={"image": "b:1",
                                       "ros_packages": [{**PX4_MSGS, "packages": []}]}))


def test_a_plain_cmake_project_is_as_declarable_as_an_ament_one(tmp_path):
    """Nothing here requires a ``package.xml``, an ament build type or a rosdistro entry.

    colcon identifies a package by what it finds -- ``colcon-cmake`` discovers a CMake project
    carrying a ``colcon.pkg`` and no ``package.xml`` -- and the Micro XRCE-DDS Agent, the second
    entry the first campaign using this key declares, is exactly that shape. A validation rule
    that assumed ROS metadata would refuse builds that work; the name follows the ROS-workspace
    idiom, it does not describe a limit.
    """
    agent = {"git": "https://github.com/eProsima/Micro-XRCE-DDS-Agent.git",
             "ref": "155cfaaf8b7abac2e85d4a62d3649b09ace0be55"}
    path = _vast(str(tmp_path), {"scenario": {"image": "b:1",
                                              "ros_packages": [PX4_MSGS, agent]}})
    assert [p for p in validate_project_file(path)["problems"]
            if p["field"] and "ros_packages" in p["field"]] == []
