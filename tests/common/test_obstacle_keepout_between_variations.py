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

import random

import pytest

from robovast_nav.data_model import Orientation, Pose, Position, StaticObject
from robovast_nav.map_loader import load_map
from robovast_nav.obstacle_placer import footprint_of
from robovast_nav.variation.obstacle_variation import ObstacleVariation, ObstacleVariationConfig

BOX = [0.5, 0.5, 1.0]
R = 0.25  # half-extent of the box, per side


class _Path:
    """A straight corridor and a planner that always finds a way down it."""

    def __init__(self, *_args, **_kwargs):
        pass

    def generate_path(self, *_args, **_kwargs):
        return [Position(x=float(i) * 0.25, y=0.0) for i in range(60)]


RESOLUTION = 0.05
ORIGIN = [-1.0, -2.0, 0.0]


def _corridor_map(tmp_path, half_width_m):
    """A straight corridor of free space along y = 0, walled beyond +/- *half_width_m*.

    A real map rather than a stub, because the placement rule under test is about occupancy: the
    path is planned for the ROBOT's radius, so what makes an obstacle not fit is its own extents
    against the wall the robot passes comfortably.
    """
    import numpy as np
    from PIL import Image

    width, height = 340, 80
    rows = np.arange(height)
    world_y = (height - rows) * RESOLUTION + ORIGIN[1]
    free = np.abs(world_y) <= half_width_m
    image = np.where(free[:, None], 255, 0).astype(np.uint8).repeat(width, axis=1)
    Image.fromarray(image, mode='L').save(tmp_path / 'map.pgm')

    map_file = tmp_path / 'map.yaml'
    map_file.write_text(
        f"image: map.pgm\nresolution: {RESOLUTION}\norigin: {ORIGIN}\n")
    return map_file


@pytest.fixture
def variation(monkeypatch, tmp_path):
    import robovast_nav.variation.obstacle_variation as mod

    monkeypatch.setattr(mod, 'PathGenerator', _Path)
    # Wide enough that a 0.5 m box fits well off the path, so the keepout tests exercise
    # obstacle-to-obstacle separation rather than the wall.
    map_file = _corridor_map(tmp_path, half_width_m=1.6)

    # pylint: disable-next=no-value-for-parameter
    v = ObstacleVariation.__new__(ObstacleVariation)
    v.parameters = ObstacleVariationConfig(
        scenario={'objects': 'static_objects'},
        reads={'start': 'start_pose', 'goal': 'goal_poses'},
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
    """Not the poses -- the occupied OUTLINES, which is the question the next placer asks, and
    which carries the yaw the poses alone could not."""
    result = variation._generate_obstacles_for_config([], _config(), variation.parameters
                                                      .obstacle_configs)[0]
    outlines = result['_placed_obstacles']
    assert len(outlines) == 1
    assert (outlines[0].half_x, outlines[0].half_y) == (R, R)
    assert (outlines[0].x, outlines[0].y) == pytest.approx(_placed(result)[0])


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

        # The keepout accumulates, so the second result carries both populations' outlines.
        # None of them may overlap any other -- that is the whole property.
        outlines = second['_placed_obstacles']
        assert len(outlines) == 2
        for i, a in enumerate(outlines):
            for b in outlines[i + 1:]:
                assert not a.overlaps(b), (
                    f"two 0.5 m boxes overlap: {(a.x, a.y, a.yaw)} and {(b.x, b.y, b.yaw)}")


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

    occupied = [footprint_of('box', [40.0, 40.0, 1.0], Position(x=float(i) * 0.25, y=0.0))
                for i in range(60)]
    with pytest.raises(VariationInfeasibleError) as raised:
        variation._generate_obstacles_for_config(
            [], _config(_placed_obstacles=occupied), variation.parameters.obstacle_configs)

    # It says WHICH constraint bound, because the two ask for different keys. Reporting a
    # placement that never found room as "blocked the path" sends the reader to widen a corridor
    # that was never the constraint -- and stricter separation makes this the common failure.
    message = str(raised.value)
    assert "already standing" in message
    assert "of size 0.5 x 0.5 m" in message
    assert "blocked the path" not in message


def test_the_obstacle_is_still_placed_when_nothing_is_in_the_way(variation):
    """The keepout narrows placement; it must not prevent it."""
    result = variation._generate_obstacles_for_config(
        [], _config(_placed_obstacles=[footprint_of('box', BOX, Position(x=-50.0, y=-50.0))]),
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
    # No extents declared, so what is published is a bare position: all a later placer can
    # compare against, and what the robot-derived floor is for.
    assert isinstance(result['_placed_obstacles'][0], Position)


def test_an_unused_static_object_import_stays_referenced():
    """Keeps the data-model import honest: the placer returns these, and a rename must break here."""
    obj = StaticObject(entity_name='obstacle_0', model='file:///box.sdf.xacro',
                       spawn_pose=Pose(position=Position(x=1.0, y=2.0),
                                       orientation=Orientation(yaw=0.0)))
    assert obj.spawn_pose.position.x == 1.0


# -- an obstacle goes where it FITS -------------------------------------------------------------

def _narrow(monkeypatch, tmp_path, half_width_m):
    """The same variation, against a corridor of a stated width."""
    import robovast_nav.variation.obstacle_variation as mod

    monkeypatch.setattr(mod, 'PathGenerator', _Path)
    map_file = _corridor_map(tmp_path, half_width_m=half_width_m)

    # pylint: disable-next=no-value-for-parameter
    v = ObstacleVariation.__new__(ObstacleVariation)
    v.parameters = ObstacleVariationConfig(
        scenario={'objects': 'static_objects'},
        reads={'start': 'start_pose', 'goal': 'goal_poses'},
        sim={'instances': 'components.static_obstacles.instances'},
        obstacle_configs=[{'amount': 1, 'max_distance': 0.7, 'model': 'file:///box.sdf.xacro',
                           'size': BOX}],
        seed=42, robot_diameter=0.35)
    v._config_child_indices = {}
    v.progress_update = lambda *_a, **_k: None
    v.get_map_file = lambda *_a, **_k: str(map_file)
    return v, load_map(str(map_file))


def test_an_obstacle_is_never_placed_reaching_into_a_wall(monkeypatch, tmp_path):
    """The half the obstacle-to-obstacle rule cannot cover.

    The corridor is 1.2 m wide and the path runs down its middle, so the robot (0.35 m) passes
    with room to spare and the planner is content. A 0.5 m box may be offset up to 0.7 m, which
    puts its far corner 1.05 m off the path -- well inside the wall. The placement has to refuse
    that on the obstacle's own extents, because nothing about the ROBOT's clearance notices it.
    """
    random.seed(20260910)
    variation, map_obj = _narrow(monkeypatch, tmp_path, half_width_m=0.6)

    for _ in range(25):
        result = variation._generate_obstacles_for_config(
            [], _config(), variation.parameters.obstacle_configs)[0]
        for outline in result['_placed_obstacles']:
            assert outline.fits(map_obj), (
                f"a 0.5 m box at y={outline.y:.3f}, yaw={outline.yaw:.2f} reaches into the wall")


def test_a_corridor_too_narrow_for_the_obstacle_is_refused(monkeypatch, tmp_path):
    """No amount of retrying makes a 0.5 m box fit a 0.4 m gap, and the campaign hears so."""
    from robovast.common.variation.base_variation import VariationInfeasibleError

    variation, _ = _narrow(monkeypatch, tmp_path, half_width_m=0.2)
    with pytest.raises(VariationInfeasibleError) as raised:
        variation._generate_obstacles_for_config(
            [], _config(), variation.parameters.obstacle_configs)
    assert "fits" in str(raised.value)
    assert "world's own geometry" in str(raised.value)
