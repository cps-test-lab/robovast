# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Two placed obstacles do not intersect -- including two placed by different variations.

The separation used to be ``robot_diameter * 1.5``, a number derived from the robot. It answers
"can the robot get between them", which is a different question from "are they the same lump of
geometry", and for anything bigger than about a 0.37 m box it is the smaller of the two. On top of
that each variation placed its own population with its own placer and an empty history, so two
populations placed near the same path never saw each other at all.

Both together put a 0.5 m box inside another 0.5 m box, at every level of the search: the campaign
recorded the obstacle count it asked for, the two populations each recorded a valid layout, and only
the simulator disagreed -- audibly, because one of the two was the movable one.
"""

import math

import pytest

from robovast_nav.data_model import Position
from robovast_nav.obstacle_placer import ObstaclePlacer, footprint_radius


# -- the radius ---------------------------------------------------------------------------------

def test_a_box_radius_contains_it_at_every_yaw():
    """Obstacles are placed at a random yaw, so half a side is not the separation they need."""
    assert footprint_radius('box', [0.5, 0.5, 1.0]) == pytest.approx(0.5 * math.sqrt(2) / 2)
    assert footprint_radius('box', [2.0, 0.5, 1.0]) == pytest.approx(math.hypot(1.0, 0.25))


def test_no_declared_geometry_has_no_radius():
    """`size` is optional, and a placement rule invented from nothing would be worse than the
    robot-derived floor that was already there."""
    assert footprint_radius('box', None) is None
    assert footprint_radius('box', []) is None
    assert footprint_radius('sphere', [0.5, 0.5, 1.0]) is None


# -- the separation -----------------------------------------------------------------------------

def _valid(placer, pos, existing, radius, robot_diameter=0.35):
    return placer._is_valid_obstacle_position(pos, [], 0.0, existing, robot_diameter, radius)


def test_two_boxes_closer_than_their_diagonals_are_refused():
    """0.53 m apart passed the old robot-derived test and puts one 0.5 m box inside another."""
    placer = ObstaclePlacer()
    r = footprint_radius('box', [0.5, 0.5, 1.0])  # 0.354
    assert not _valid(placer, Position(x=0.53, y=0.0), [(Position(x=0.0, y=0.0), r)], r)


def test_two_boxes_clear_of_each_other_are_accepted():
    placer = ObstaclePlacer()
    r = footprint_radius('box', [0.5, 0.5, 1.0])
    just_clear = 2 * r + ObstaclePlacer.OBSTACLE_MARGIN_M + 1e-6
    assert _valid(placer, Position(x=just_clear, y=0.0), [(Position(x=0.0, y=0.0), r)], r)


def test_the_robot_derived_floor_still_applies_to_small_obstacles():
    """Two obstacles the robot cannot pass between are still refused, whatever their size."""
    placer = ObstaclePlacer()
    r = footprint_radius('box', [0.05, 0.05, 1.0])  # 0.035: their own extents ask for almost nothing
    assert not _valid(placer, Position(x=0.4, y=0.0), [(Position(x=0.0, y=0.0), r)], r)
    assert _valid(placer, Position(x=0.6, y=0.0), [(Position(x=0.0, y=0.0), r)], r)


def test_a_campaign_declaring_no_size_behaves_as_before():
    """No radius on either side: the rule is the floor, which is what it always was."""
    placer = ObstaclePlacer()
    assert not _valid(placer, Position(x=0.4, y=0.0), [(Position(x=0.0, y=0.0), None)], None)
    assert _valid(placer, Position(x=0.6, y=0.0), [(Position(x=0.0, y=0.0), None)], None)


# -- the keepout --------------------------------------------------------------------------------

def _path():
    return [Position(x=float(i) * 0.25, y=0.0) for i in range(60)]


def _place(keepout=None, amount=1, seed=0):
    import random
    random.seed(seed)
    return ObstaclePlacer().place_obstacles(
        _path(), 0.7, amount, 'file:///box.sdf.xacro',
        robot_diameter=0.35,
        obstacle_radius=footprint_radius('box', [0.5, 0.5, 1.0]),
        keepout=keepout or [],
    )


def test_a_placement_clears_obstacles_it_did_not_place():
    """What the two variations could not do: place around a population placed by another call.

    Every position on this path is inside the keepout circle, so a placer that honours it can place
    nothing -- and the variation's own retry-then-VariationInfeasibleError path reports that. A
    placer that ignores it places an obstacle straight into the occupied space.
    """
    r = footprint_radius('box', [0.5, 0.5, 1.0])
    wall = [(Position(x=float(i) * 0.25, y=0.0), 20.0) for i in range(60)]
    assert _place(keepout=wall) == []
    assert len(_place(keepout=[(Position(x=-50.0, y=-50.0), r)])) == 1


def test_obstacles_placed_in_one_call_clear_each_other():
    """The intra-population case, which the floor alone under-served for a 0.5 m box."""
    placed = _place(amount=3)
    assert len(placed) == 3
    r = footprint_radius('box', [0.5, 0.5, 1.0])
    for i, (a, _) in enumerate(placed):
        for b, _ in placed[i + 1:]:
            gap = math.dist((a.spawn_pose.position.x, a.spawn_pose.position.y),
                            (b.spawn_pose.position.x, b.spawn_pose.position.y))
            assert gap >= 2 * r + ObstaclePlacer.OBSTACLE_MARGIN_M
