# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What a variation reads has to have been written, and the check knows in which order.

Variations run in the order the `.vast` lists them, and the coupling between them is otherwise
invisible: a consumer reading a parameter no earlier variation writes was discovered by the
composition, one `AttributeError` deep, after the campaign had already been accepted. Walking
the list once -- carrying what the campaign stated plus what each variation writes as it goes --
is the only way to answer it, and it answers before anything runs.
"""

import pytest

from robovast.common.config_generation import _check_declared_contracts
from robovast.common.variation import Variation
from robovast.common.variation.base_variation import DestinationConfig


class _ProducerConfig(DestinationConfig):
    OUTPUT_SLOTS = ("start", "goal")


class Producer(Variation):
    """Writes a start and a goal -- the PathVariationRandom shape."""

    CONFIG_CLASS = _ProducerConfig


class _ConsumerConfig(DestinationConfig):
    OUTPUT_SLOTS = ("objects",)
    INPUT_SLOTS = ("start", "goal")


class Consumer(Variation):
    """Reads a start and a goal -- the ObstacleVariation shape."""

    CONFIG_CLASS = _ConsumerConfig


PRODUCES = {"scenario": {"start": "start_pose", "goal": "goal_poses"}}
CONSUMES = {"scenario": {"objects": "static_objects"},
            "reads": {"start": "start_pose", "goal": "goal_poses"}}

SCENARIO_DECLARES = [{"name": n} for n in ("start_pose", "goal_poses", "static_objects")]


def _check(config, *variations):
    _check_declared_contracts(config, list(variations), SCENARIO_DECLARES, {}, ".")


def _config(**scenario):
    return {"name": "cfg", "parameters": {"scenario": scenario}}


def test_a_producer_ahead_of_the_consumer_passes():
    _check(_config(), (Producer, PRODUCES), (Consumer, CONSUMES))


def test_poses_the_campaign_stated_itself_pass():
    """The other valid answer, and the one this check exists to stop calling an error."""
    _check(_config(start_pose={}, goal_poses=[{}]), (Consumer, CONSUMES))


def test_neither_is_refused_before_anything_runs():
    with pytest.raises(ValueError) as exc:
        _check(_config(), (Consumer, CONSUMES))
    message = str(exc.value)
    assert "Consumer reads ['start_pose', 'goal_poses']" in message
    assert "cfg" in message
    assert "parameters.scenario" in message


def test_a_producer_AFTER_the_consumer_is_refused():
    """Order is the whole contract: the same two variations, listed the other way round, is
    a campaign where nothing has written the poses by the time they are read."""
    with pytest.raises(ValueError, match="Consumer reads"):
        _check(_config(), (Consumer, CONSUMES), (Producer, PRODUCES))


def test_a_partially_supplied_input_names_only_what_is_missing():
    with pytest.raises(ValueError) as exc:
        _check(_config(start_pose={}), (Consumer, CONSUMES))
    assert "['goal_poses']" in str(exc.value)


def test_the_message_lists_what_was_available_instead():
    with pytest.raises(ValueError) as exc:
        _check(_config(start_pose={}), (Consumer, CONSUMES))
    assert "Available here: ['start_pose']" in str(exc.value)


def test_a_consumer_reading_a_name_the_campaign_bound_is_followed():
    """Binding is what makes the check possible at all: the producer writes `robot_start`, so
    that is the name the consumer must be bound to, and the check compares the two."""
    produces = {"scenario": {"start": "robot_start", "goal": "goal_poses"}}
    consumes = {"scenario": {"objects": "static_objects"},
                "reads": {"start": "robot_start", "goal": "goal_poses"}}
    declares = [{"name": n} for n in ("robot_start", "goal_poses", "static_objects")]
    _check_declared_contracts(_config(), [(Producer, produces), (Consumer, consumes)],
                              declares, {}, ".")


def test_a_consumer_bound_to_a_name_nobody_writes_is_refused():
    consumes = {"scenario": {"objects": "static_objects"},
                "reads": {"start": "robot_start", "goal": "goal_poses"}}
    with pytest.raises(ValueError, match=r"reads \['robot_start'\]"):
        _check(_config(), (Producer, PRODUCES), (Consumer, consumes))


def test_a_variation_declaring_no_inputs_is_not_asked():
    """`{}` means undeclared, the escape that keeps a third-party plugin working."""
    class _Undeclared(Variation):
        pass

    _check(_config(), (_Undeclared, {"scenario": "goal_pose"}))
