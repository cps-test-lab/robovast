# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""``describe_world``: what a campaign's world offers, and the image that can say so.

The bug this defends against is a *silent* one. A world ref resolves to whatever is installed,
so a campaign shipping its own world package has worlds that exist only in its built image;
described against a fixed base image the ref does not resolve, the simulator answers nothing,
and the override pre-check passed for exactly the campaigns most in need of it -- at debug
level, so nothing said so.
"""

import logging

import pytest

from robovast.common.config_generation import (WorldQueryUnavailable,
                                               describe_world_payload)
from robovast_sim_robosito.backend import (DEFAULT_COMBINED_IMAGE,
                                           DEFAULT_SIM_IMAGE, RobositoBackend,
                                           RobositoConfig)


def _cfg(config="rst_scenes:depot", **kw):
    return RobositoConfig(config=config, **kw)


# -- which image answers ------------------------------------------------------------------
def test_the_campaigns_own_image_is_asked_not_a_default():
    """The whole bug: a campaign's world may exist only in the image the campaign runs."""
    pinned = "harbor.example/robovast_robosito@sha256:abc"
    query = RobositoBackend().describe_query(
        _cfg(), {"mode": "base", "containers": {"scenario": {"image": pinned}}})
    assert query.spec.image == pinned


def test_a_ros_campaign_is_asked_in_its_simulation_container():
    pinned = "harbor.example/sim:pinned"
    query = RobositoBackend().describe_query(
        _cfg(), {"mode": "ros2", "containers": {"simulation": {"image": pinned}}})
    assert query.spec.image == pinned


@pytest.mark.parametrize("mode, expected", [("ros2", DEFAULT_SIM_IMAGE),
                                            ("base", DEFAULT_COMBINED_IMAGE)])
def test_a_campaign_that_pins_nothing_falls_back_to_the_shape_default(mode, expected):
    query = RobositoBackend().describe_query(_cfg(), {"mode": mode})
    assert query.spec.image == expected


def test_input_files_asks_the_same_image():
    """Same reasoning, same bug class: what a world extends is resolved by what is installed."""
    pinned = "harbor.example/sim:pinned"
    query = RobositoBackend().input_files(
        _cfg(config="/config/world.yaml"),
        {"mode": "ros2", "containers": {"simulation": {"image": pinned}}})
    # A path world that extends a campaign file is the case that needs a container at all.
    assert getattr(query, "spec", None) is None or query.spec.image == pinned


# -- what is asked for --------------------------------------------------------------------
def test_targets_and_entities_are_opt_in_because_each_costs_a_model_build():
    backend = RobositoBackend()
    plain = backend.describe_query(_cfg(), {"mode": "ros2"}).command
    assert "--entities" not in plain and "--overridable" not in plain

    asked = backend.describe_query(_cfg(), {"mode": "ros2"},
                                   entities=True, targets="gripper_right*").command
    assert "--entities" in asked
    assert asked[asked.index("--overridable") + 1] == "gripper_right*"


# -- when nothing can answer --------------------------------------------------------------
def test_an_unbuilt_image_is_reported_rather_than_silently_skipped():
    """`build:<tag>` is not an image name. Saying so beats a check that quietly passes."""
    with pytest.raises(WorldQueryUnavailable, match="does not exist yet"):
        describe_world_payload(
            {"mode": "base",
             "containers": {"simulation": {"backend": "robosito"},
                            "scenario": {"image": "build:scenario"}}},
            {"config": "rst_scenes:depot"}, ".")


def test_no_backend_is_a_reason_not_a_shrug():
    with pytest.raises(WorldQueryUnavailable, match="no simulator backend"):
        describe_world_payload({}, {}, ".")


def test_the_pre_check_warns_when_it_could_not_check(caplog, monkeypatch):
    """It used to log this at debug and carry on, which is indistinguishable from a clean check."""
    from robovast.common import config_generation
    from robovast.common.config_generation import _check_sim_against_world

    def _unavailable(*_a, **_kw):
        raise WorldQueryUnavailable("the world could not be described in image X")

    monkeypatch.setattr(config_generation, "describe_world_payload", _unavailable)
    with caplog.at_level(logging.WARNING):
        _check_sim_against_world(
            {"containers": {"simulation": {"backend": "robosito", "config": "w.yaml"}}},
            [{"sim": {"overrides": {"plugins": {"floorplan": {"size": 3.0}}}}}], ".")
    assert "were not pre-checked" in caplog.text
    assert "could not be described" in caplog.text
