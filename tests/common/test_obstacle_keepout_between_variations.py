# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""What one obstacle variation placed, the next one has to place around.

A campaign that wants static scenery AND an obstacle revealed on cue runs two variations. Each
builds its own placer, each places near the SAME path with a similar lateral offset, and each began
from an empty history -- so nothing in the chain ever compared the two populations. The dynamic
obstacle landed inside a static one, and both variations reported the layout they were asked for.

The circles travel on ``_placed_obstacles``, the same private channel ``_path`` uses, because it is
a fact a later variation needs and cannot recompute: a population's own slot value states poses in
the trial's vocabulary, while a placer asks which discs are occupied.
"""

import math
import random

import pytest

from robovast_nav.data_model import Orientation, Pose, Position, StaticObject
from robovast_nav.obstacle_placer import footprint_radius
from robovast_nav.variation.obstacle_variation import ObstacleVariation, ObstacleVariationConfig

BOX = [0.5, 0.5, 1.0]
R = footprint_radius('box', BOX)


class _Path:
    """A straight corridor and a planner that always finds a way down it."""

    def __init__(self, *_args, **_kwargs):
        pass

    def generate_path(self, *_args, **_kwargs):
        return [Position(x=float(i) * 0.25, y=0.0) for i in range(60)]


@pytest.fixture
def variation(monkeypatch, tmp_path):
    import robovast_nav.variation.obstacle_variation as mod

    monkeypatch.setattr(mod, 'PathGenerator', _Path)
    map_file = tmp_path / 'map.yaml'
    map_file.write_text('image: map.pgm\n')

    # pylint: disable-next=no-value-for-parameter
    v = ObstacleVariation.__new__(ObstacleVariation)
    v.parameters = ObstacleVariationConfig(
        scenario={'objects': 'static_objects'},
        sim={'instances': 'components.static_obstacles.instances'},
        obstacle_configs=[{'amount': 1, 'max_distance': 0.7, 'model': 'file:///box.sdf.xacro',
                           'size': BOX}],
        seed=42, robot_diameter=0.35)
    v._config_child_indices = {}
    v.progress_update = lambda *_a, **_k: None
    v.get_map_file = lambda *_a, **_k: str(map_file)
    return v


def _config(**extra):
    return {
        'name': 'cfg',
        'config': {
            'start_pose': Pose(position=Position(x=0.0, y=0.0), orientation=Orientation(yaw=0.0)),
            'goal_poses': [Pose(position=Position(x=14.0, y=0.0),
                                orientation=Orientation(yaw=0.0))],
        },
        **extra,
    }


def _placed(result):
    """The obstacle poses this variation wrote, from the slot the campaign bound."""
    return [(o['spawn_pose']['position']['x'], o['spawn_pose']['position']['y'])
            for o in result['config']['static_objects']]


def test_the_placement_is_published_for_the_next_variation(variation):
    """Not the poses -- the occupied CIRCLES, which is the question the next placer asks."""
    result = variation._generate_obstacles_for_config([], _config(), variation.parameters
                                                      .obstacle_configs)[0]
    circles = result['_placed_obstacles']
    assert len(circles) == 1
    position, radius = circles[0]
    assert radius == pytest.approx(R)
    assert (position.x, position.y) == pytest.approx(_placed(result)[0])


def test_a_second_variation_places_clear_of_the_first(variation):
    """The defect, at the scale it happened: two populations near one path.

    Repeated rather than sampled once, and seeded so the repetition is the same every run: the
    placements are random, so a single draw that happens not to overlap says nothing. Both
    populations sit within 0.7 m of one corridor, which is why the unguarded version put one
    inside the other in every configuration of the batch that found this.
    """
    random.seed(20260909)
    for _ in range(40):
        first = variation._generate_obstacles_for_config([], _config(), variation.parameters
                                                         .obstacle_configs)[0]
        second = variation._generate_obstacles_for_config(
            [], _config(_placed_obstacles=first['_placed_obstacles']),
            variation.parameters.obstacle_configs)[0]

        gap = math.dist(_placed(first)[0], _placed(second)[0])
        assert gap >= 2 * R, f"two 0.5 m boxes {gap:.3f} m apart intersect"


def test_the_published_circles_accumulate(variation):
    """A third variation must clear both of the first two, not only the most recent."""
    first = variation._generate_obstacles_for_config([], _config(), variation.parameters
                                                     .obstacle_configs)[0]
    second = variation._generate_obstacles_for_config(
        [], _config(_placed_obstacles=first['_placed_obstacles']),
        variation.parameters.obstacle_configs)[0]
    assert len(second['_placed_obstacles']) == 2


def test_a_configuration_with_no_room_left_is_refused_rather_than_overlapped(variation):
    """Failing loudly is the point: a placement that cannot clear what is already there is a
    configuration the campaign has to hear about, not one quietly placed on top."""
    from robovast.common.variation.base_variation import VariationInfeasibleError

    occupied = [(Position(x=float(i) * 0.25, y=0.0), 20.0) for i in range(60)]
    with pytest.raises(VariationInfeasibleError) as raised:
        variation._generate_obstacles_for_config(
            [], _config(_placed_obstacles=occupied), variation.parameters.obstacle_configs)

    # It says WHICH constraint bound, because the two ask for different keys. Reporting a
    # placement that never found room as "blocked the path" sends the reader to widen a corridor
    # that was never the constraint -- and stricter separation makes this the common failure.
    message = str(raised.value)
    assert "already standing" in message
    assert "footprint radius" in message
    assert "blocked the path" not in message


def test_the_obstacle_is_still_placed_when_nothing_is_in_the_way(variation):
    """The keepout narrows placement; it must not prevent it."""
    result = variation._generate_obstacles_for_config(
        [], _config(_placed_obstacles=[(Position(x=-50.0, y=-50.0), R)]),
        variation.parameters.obstacle_configs)[0]
    assert len(_placed(result)) == 1


def test_the_static_population_is_welded_and_carries_its_size(variation):
    """The instances channel is what a simulator COMPILES, and it is unaffected by the keepout."""
    result = variation._generate_obstacles_for_config([], _config(), variation.parameters
                                                      .obstacle_configs)[0]
    instances = result['sim']['components.static_obstacles.instances']
    assert len(instances) == 1
    assert instances[0]['motion'] == 'static'
    assert instances[0]['size'] == BOX


def test_a_campaign_without_size_still_places(monkeypatch, variation):
    """`size` is optional, and the robot-derived floor is what a placement without it gets."""
    variation.parameters = variation.parameters.model_copy(update={
        'obstacle_configs': [type(variation.parameters.obstacle_configs[0])(
            amount=1, max_distance=0.7, model='file:///box.sdf.xacro')]})
    result = variation._generate_obstacles_for_config([], _config(), variation.parameters
                                                      .obstacle_configs)[0]
    assert len(_placed(result)) == 1
    assert result['_placed_obstacles'][0][1] is None


def test_an_unused_static_object_import_stays_referenced():
    """Keeps the data-model import honest: the placer returns these, and a rename must break here."""
    obj = StaticObject(entity_name='obstacle_0', model='file:///box.sdf.xacro',
                       spawn_pose=Pose(position=Position(x=1.0, y=2.0),
                                       orientation=Orientation(yaw=0.0)))
    assert obj.spawn_pose.position.x == 1.0
