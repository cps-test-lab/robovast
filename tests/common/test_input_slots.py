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

from robovast.common.variation.base_variation import DestinationConfig


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

WROTE = {"_slot_bindings": {"start": "start_pose", "goal": "goal_poses"}}


def test_an_input_is_inherited_from_the_variation_that_wrote_the_slot():
    """The common case, and the reason `reads:` is not required: the producer already bound
    `start` to a parameter, in this same file. Restating it would let the two disagree."""
    cfg = Consumer(scenario={"objects": "static_objects"})
    assert cfg.input_binding("start", WROTE) == "start_pose"
    assert cfg.input_binding("goal", WROTE) == "goal_poses"


def test_an_inherited_binding_follows_a_name_the_campaign_chose():
    """No conventional name is assumed anywhere: what is inherited is what was written."""
    wrote = {"_slot_bindings": {"start": "robot_start", "goal": "waypoints"}}
    assert Consumer(scenario={"objects": "static_objects"}).input_binding("start", wrote) \
        == "robot_start"


def test_reads_says_where_to_look_when_nothing_wrote_the_slot():
    """A configuration that states its poses in its own `parameters:` block has no earlier
    variation to inherit from, which is the case `reads:` exists for."""
    cfg = Consumer(scenario={"objects": "static_objects"},
                   reads={"start": "start_pose", "goal": "goal_poses"})
    assert cfg.input_binding("start", {}) == "start_pose"


def test_reads_wins_over_what_was_written():
    cfg = Consumer(scenario={"objects": "static_objects"}, reads={"start": "other_pose"})
    assert cfg.input_binding("start", WROTE) == "other_pose"
    assert cfg.input_binding("goal", WROTE) == "goal_poses"   # untouched, still inherited


def test_a_goal_slot_may_name_a_singular_parameter():
    """One pose or a list is the scenario's choice, and binding is how it says so -- rather
    than the consumer trying both spellings and taking whichever it finds."""
    cfg = Consumer(scenario={"objects": "static_objects"},
                   reads={"start": "start_pose", "goal": "goal_pose"})
    assert cfg.input_binding("goal", {}) == "goal_pose"


# -- what is refused ----------------------------------------------------------------------

def test_an_input_that_is_neither_written_nor_bound_says_both_remedies():
    cfg = Consumer(scenario={"objects": "static_objects"})
    with pytest.raises(KeyError) as exc:
        cfg.input_binding("start", {})
    assert "nothing has written the 'start' input" in str(exc.value)
    assert "reads:" in str(exc.value)


def test_a_partial_reads_is_accepted_and_the_rest_inherited():
    """Binding one input does not oblige the campaign to restate the others."""
    cfg = Consumer(scenario={"objects": "static_objects"}, reads={"start": "start_pose"})
    assert cfg.input_binding("goal", WROTE) == "goal_poses"


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
        cfg.input_binding("map", WROTE)


# -- what the pre-run check reads ---------------------------------------------------------

def test_the_declared_inputs_are_the_slots_and_what_was_said_about_them():
    """`None` is not "nothing": it is "resolve this from whoever wrote the slot", which only
    the walk over the variation list can do."""
    cfg = Consumer(scenario={"objects": "static_objects"}, reads={"start": "start_pose"})
    assert cfg.inputs() == {"start": "start_pose", "goal": None}


def test_a_variation_reading_nothing_declares_nothing():
    """`{}` means undeclared, the escape a third-party plugin keeps working through."""
    assert Producer(scenario={"start": "start_pose", "goal": "goal_poses"}).inputs() == {}
