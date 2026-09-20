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
#: No `reads:`. The producer bound the slots; repeating the names here would be the campaign
#: writing one fact twice, in two places that can drift.
CONSUMES = {"scenario": {"objects": "static_objects"}}
#: What a configuration states when there is no producer to inherit from.
CONSUMES_STATED = {"scenario": {"objects": "static_objects"},
                   "reads": {"start": "start_pose", "goal": "goal_poses"}}

SCENARIO_DECLARES = [{"name": n} for n in ("start_pose", "goal_poses", "static_objects")]


def _check(config, *variations):
    _check_declared_contracts(config, list(variations), SCENARIO_DECLARES, {}, ".")


def _config(**scenario):
    return {"name": "cfg", "parameters": {"scenario": scenario}}


def test_a_producer_ahead_of_the_consumer_passes():
    """And says nothing about names: the consumer inherits the producer's binding."""
    _check(_config(), (Producer, PRODUCES), (Consumer, CONSUMES))


def test_poses_the_campaign_stated_itself_pass():
    """The other valid answer, and the one this check exists to stop calling an error."""
    _check(_config(start_pose={}, goal_poses=[{}]), (Consumer, CONSUMES_STATED))


def test_neither_is_refused_before_anything_runs():
    with pytest.raises(ValueError) as exc:
        _check(_config(), (Consumer, CONSUMES))
    message = str(exc.value)
    assert "Consumer reads 'start'" in message
    assert "cfg" in message
    assert "reads: {start: <parameter>}" in message


def test_a_producer_listed_after_the_consumer_is_refused():
    """Order is the whole contract: the same two variations, listed the other way round, is
    a campaign where nothing has written the poses by the time they are read."""
    with pytest.raises(ValueError, match="Consumer reads 'start'"):
        _check(_config(), (Consumer, CONSUMES), (Producer, PRODUCES))


def test_a_stated_input_the_campaign_never_set_is_refused():
    with pytest.raises(ValueError) as exc:
        _check(_config(start_pose={}), (Consumer, CONSUMES_STATED))
    assert "reads 'goal' from 'goal_poses'" in str(exc.value)


def test_the_message_lists_what_was_available_instead():
    with pytest.raises(ValueError) as exc:
        _check(_config(start_pose={}), (Consumer, CONSUMES_STATED))
    assert "Available here: ['start_pose']" in str(exc.value)


def test_the_inherited_name_is_the_one_the_producer_chose():
    """The reason inheriting is safe: a campaign that renames the parameter renames it once,
    on the producer, and the consumer follows without being told."""
    produces = {"scenario": {"start": "robot_start", "goal": "waypoints"}}
    declares = [{"name": n} for n in ("robot_start", "waypoints", "static_objects")]
    _check_declared_contracts(_config(), [(Producer, produces), (Consumer, CONSUMES)],
                              declares, {}, ".")


def test_a_consumer_bound_to_a_name_nobody_writes_is_refused():
    """`reads:` is still checked where it IS given -- it overrides, so a stale one would
    otherwise read a parameter that does not exist."""
    consumes = {"scenario": {"objects": "static_objects"},
                "reads": {"start": "robot_start"}}
    with pytest.raises(ValueError, match=r"reads 'start' from 'robot_start'"):
        _check(_config(), (Producer, PRODUCES), (Consumer, consumes))


def test_a_variation_declaring_no_inputs_is_not_asked():
    """`{}` means undeclared, the escape that keeps a third-party plugin working."""
    class _Undeclared(Variation):
        pass

    _check(_config(), (_Undeclared, {"scenario": "goal_pose"}))
