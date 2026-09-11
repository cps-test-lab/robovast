# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Input slots: how a variation says which parameters it READS.

The mirror of test_output_slots.py. A variation that consumes what an earlier one produced used
to name the parameter itself, in the plugin -- so a campaign binding the producer to a name of
its own choosing left the consumer reading a key nobody wrote, and nothing could tell until the
composition ran. Declaring the read makes the coupling a statement in the `.vast`, which a
reader and a validator can both see.
"""

import pytest
from pydantic import ValidationError

from robovast.common.variation.base_variation import SCENARIO_CHANNEL, DestinationConfig


class Producer(DestinationConfig):
    """Writes a start and a goal -- the PathVariationRandom shape."""

    OUTPUT_SLOTS = ("start", "goal")


class Consumer(DestinationConfig):
    """Reads a start and a goal, writes obstacles -- the ObstacleVariation shape."""

    OUTPUT_SLOTS = ("objects",)
    INPUT_SLOTS = ("start", "goal")


class ReadsNothing(DestinationConfig):
    """Declares no inputs, which is every variation that needs none."""


# -- binding ------------------------------------------------------------------------------

def test_an_input_is_read_from_the_parameter_the_campaign_bound():
    cfg = Consumer(scenario={"objects": "static_objects"},
                   reads={"start": "start_pose", "goal": "goal_poses"})
    assert cfg.input_binding("start") == "start_pose"
    assert cfg.input_binding("goal") == "goal_poses"


def test_the_campaign_may_bind_a_name_of_its_own():
    """The point of binding: a scenario declaring `robot_start` is read, not `start_pose`."""
    cfg = Consumer(scenario={"objects": "static_objects"},
                   reads={"start": "robot_start", "goal": "waypoints"})
    assert cfg.input_binding("start") == "robot_start"


def test_a_goal_slot_may_name_a_singular_parameter():
    """One pose or a list is the scenario's choice, and binding is how it says so -- rather
    than the consumer trying both spellings and taking whichever it finds."""
    cfg = Consumer(scenario={"objects": "static_objects"},
                   reads={"start": "start_pose", "goal": "goal_pose"})
    assert cfg.input_binding("goal") == "goal_pose"


# -- what is refused ----------------------------------------------------------------------

def test_every_declared_input_must_be_bound():
    with pytest.raises(ValidationError, match="unbound: goal"):
        Consumer(scenario={"objects": "static_objects"}, reads={"start": "start_pose"})


def test_an_input_left_entirely_unbound_is_refused():
    """No conventional default: a consumer reading whatever happens to sit under a familiar
    name is the implied coupling this exists to remove."""
    with pytest.raises(ValidationError, match="every input must be bound"):
        Consumer(scenario={"objects": "static_objects"})


def test_an_unknown_input_is_refused_naming_the_real_ones():
    with pytest.raises(ValidationError, match="its inputs are: start, goal"):
        Consumer(scenario={"objects": "static_objects"},
                 reads={"start": "start_pose", "goal": "goal_poses", "map": "map_file"})


def test_a_variation_that_declares_no_inputs_refuses_reads():
    with pytest.raises(ValidationError, match="declares none"):
        ReadsNothing(scenario="goal_pose", reads={"start": "start_pose"})


def test_asking_for_an_input_that_is_not_one_says_so():
    cfg = Consumer(scenario={"objects": "static_objects"},
                   reads={"start": "start_pose", "goal": "goal_poses"})
    with pytest.raises(KeyError, match="its inputs are: start, goal"):
        cfg.input_binding("map")


# -- what the pre-run check reads ---------------------------------------------------------

def test_the_declared_inputs_are_the_bound_parameters():
    cfg = Consumer(scenario={"objects": "static_objects"},
                   reads={"start": "start_pose", "goal": "goal_poses"})
    assert cfg.inputs() == {SCENARIO_CHANNEL: ["start_pose", "goal_poses"]}


def test_a_variation_reading_nothing_declares_nothing():
    """`{}` means undeclared, the escape a third-party plugin keeps working through."""
    assert Producer(scenario={"start": "start_pose", "goal": "goal_poses"}).inputs() == {
        SCENARIO_CHANNEL: []}
