# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""Two placed obstacles do not intersect -- including two placed by different variations.

The separation used to be ``robot_diameter * 1.5``, a number derived from the robot. It answers
"can the robot get between them", which is a different question from "are they the same lump of
geometry", and for anything bigger than about a 0.37 m box it is the smaller of the two. On top of
that each variation placed its own population with its own placer and an empty history, so two
populations placed near the same path never saw each other at all.

The rule is the real OUTLINE at the yaw the obstacle is placed at. A circle around the box is easy
and wrong in the expensive direction: two 0.5 m boxes side by side occupy 0.5 m, while the circles
containing them demand 0.71 m, and every placement refused for that is a configuration the search
was asked to try and never got.

Both together put a 0.5 m box inside another 0.5 m box, at every level of the search: the campaign
recorded the obstacle count it asked for, the two populations each recorded a valid layout, and only
the simulator disagreed -- audibly, because one of the two was the movable one.
"""

import math
import random

import numpy as np
import pytest

from robovast_nav.data_model import Position
from robovast_nav.obstacle_placer import Footprint, ObstaclePlacer, footprint_of


# -- the outline --------------------------------------------------------------------------------

BOX = [0.5, 0.5, 1.0]


def _box(x, y, yaw=0.0, size=None):
    return footprint_of('box', size or BOX, Position(x=x, y=y), yaw)


def test_a_box_outline_is_its_half_extents_at_its_yaw():
    f = _box(1.0, 2.0, yaw=math.pi / 2)
    assert (f.half_x, f.half_y) == (0.25, 0.25)
    assert f.radius == pytest.approx(math.hypot(0.25, 0.25))


def test_no_declared_geometry_has_no_outline():
    """`size` is optional, and a rule invented from nothing would be worse than the
    robot-derived floor that was already there."""
    assert footprint_of('box', None, Position(x=0.0, y=0.0)) is None
    assert footprint_of('sphere', BOX, Position(x=0.0, y=0.0)) is None


# -- separation is the outlines, not their circles ----------------------------------------------

def test_two_aligned_boxes_may_stand_almost_touching():
    """The point of using the real shape. Their circles would demand 0.71 m between centres;
    aligned, they need the width of two boxes and the margin, which is 0.55 m."""
    a = _box(0.0, 0.0)
    assert not a.overlaps(_box(0.5 + ObstaclePlacer.OBSTACLE_MARGIN_M + 1e-9, 0.0),
                          ObstaclePlacer.OBSTACLE_MARGIN_M)
    # And 0.71 m apart -- what the circumradius rule demanded -- is of course also fine.
    assert not a.overlaps(_box(0.71, 0.0), ObstaclePlacer.OBSTACLE_MARGIN_M)


def test_two_aligned_boxes_that_interpenetrate_are_refused():
    assert _box(0.0, 0.0).overlaps(_box(0.4, 0.0), ObstaclePlacer.OBSTACLE_MARGIN_M)


def test_the_yaw_is_part_of_the_answer():
    """The same two centres, one rotation apart: apart when aligned, touching when turned."""
    gap = 0.60
    assert not _box(0.0, 0.0).overlaps(_box(gap, 0.0), 0.0)
    assert _box(0.0, 0.0, yaw=math.pi / 4).overlaps(_box(gap, 0.0, yaw=math.pi / 4), 0.0)


def test_the_margin_is_what_keeps_them_from_touching():
    """Exactly abutting is not a placement anyone means: a lidar reads two obstacles in contact
    as one, and a planner's inflation closes the seam."""
    abutting = _box(0.5, 0.0)
    assert not _box(0.0, 0.0).overlaps(abutting, 0.0)
    assert _box(0.0, 0.0).overlaps(abutting, ObstaclePlacer.OBSTACLE_MARGIN_M)


# -- the separation test ------------------------------------------------------------------------

def _valid(placer, footprint, existing, robot_diameter=0.35):
    return placer._is_valid_obstacle_position(
        Position(x=footprint.x, y=footprint.y), [], 0.0, existing, robot_diameter, footprint)


def test_boxes_that_do_not_overlap_are_accepted():
    placer = ObstaclePlacer()
    assert _valid(placer, _box(0.56, 0.0), [_box(0.0, 0.0)])


def test_boxes_that_overlap_are_refused():
    placer = ObstaclePlacer()
    assert not _valid(placer, _box(0.30, 0.0), [_box(0.0, 0.0)])


def test_small_obstacles_may_stand_close_together():
    """No robot-derived floor on top of the outline rule. Whether the robot can pass between two
    obstacles is settled by the navigability check the caller runs over the whole population,
    which answers it about the real layout instead of by proxy."""
    placer = ObstaclePlacer()
    tiny = [0.05, 0.05, 1.0]
    assert _valid(placer, _box(0.2, 0.0, size=tiny), [_box(0.0, 0.0, size=tiny)])


def test_a_campaign_declaring_no_size_keeps_the_robot_derived_floor():
    """Nothing to compare outlines with, so the rule is the one that was always there."""
    placer = ObstaclePlacer()
    assert not placer._is_valid_obstacle_position(
        Position(x=0.4, y=0.0), [], 0.0, [Position(x=0.0, y=0.0)], 0.35, None)
    assert placer._is_valid_obstacle_position(
        Position(x=0.6, y=0.0), [], 0.0, [Position(x=0.0, y=0.0)], 0.35, None)


# -- fitting the world --------------------------------------------------------------------------

class _Map:
    """A 4 m x 4 m room, free inside a stated half-width band around y = 0."""

    resolution = 0.05
    width = 80
    height = 80

    def __init__(self, half_width_m=10.0):
        rows = np.arange(self.height)
        world_y = (self.height - rows) * self.resolution - 2.0
        free = np.abs(world_y) <= half_width_m
        self.occupancy_grid = ~free[:, None].repeat(self.width, axis=1)

    def world_to_grid(self, x, y):
        return int((x + 2.0) / self.resolution), int(self.height - (y + 2.0) / self.resolution)

    def grid_to_world(self, grid_x, grid_y):
        return grid_x * self.resolution - 2.0, (self.height - grid_y) * self.resolution - 2.0


def test_an_outline_clear_of_everything_fits():
    assert _box(0.0, 0.0).fits(_Map())


def test_an_outline_reaching_into_a_wall_does_not_fit():
    """The centre is in free space and the obstacle still does not fit -- which is the point:
    a free cell says nothing about the extents around it."""
    room = _Map(half_width_m=0.6)
    assert not _box(0.0, 0.45).fits(room)


def test_the_yaw_decides_whether_it_fits_a_narrow_gap():
    """A 0.9 x 0.3 m box fits a 0.5 m corridor lengthwise and not across it. A circle around it
    could never express that: it would refuse both."""
    plank = [0.9, 0.3, 1.0]
    room = _Map(half_width_m=0.25)
    assert _box(0.0, 0.0, yaw=0.0, size=plank).fits(room)
    assert not _box(0.0, 0.0, yaw=math.pi / 2, size=plank).fits(room)


def test_off_the_map_does_not_fit():
    """Unknown is not free: an obstacle outside the surveyed area is not known to be clear."""
    assert not _box(50.0, 0.0).fits(_Map())


def test_no_map_is_not_a_refusal():
    """A campaign whose placement channel names no map keeps the behaviour it always had."""
    assert _box(0.0, 0.0).fits(None)


# -- the keepout --------------------------------------------------------------------------------

def _path():
    return [Position(x=float(i) * 0.25, y=0.0) for i in range(60)]


def _place(keepout=None, amount=1, seed=0):
    random.seed(seed)
    return ObstaclePlacer().place_obstacles(
        _path(), 0.7, amount, 'file:///box.sdf.xacro',
        robot_diameter=0.35, shape='box', size=BOX, keepout=keepout or [])


def test_a_placement_clears_obstacles_it_did_not_place():
    """What the two variations could not do: place around a population placed by another call."""
    wall = [_box(float(i) * 0.25, 0.0, size=[40.0, 40.0, 1.0]) for i in range(60)]
    assert _place(keepout=wall) == []
    assert len(_place(keepout=[_box(-50.0, -50.0)])) == 1


def test_obstacles_placed_in_one_call_clear_each_other():
    placed = _place(amount=3)
    assert len(placed) == 3
    outlines = [
        footprint_of('box', BOX, o.spawn_pose.position, o.spawn_pose.orientation.yaw)
        for o, _ in placed
    ]
    for i, a in enumerate(outlines):
        for b in outlines[i + 1:]:
            assert not a.overlaps(b, ObstaclePlacer.OBSTACLE_MARGIN_M)


def test_a_placed_obstacle_reports_the_yaw_it_was_validated_at():
    """The yaw is drawn before the check, so what was validated is what is written out."""
    placed = _place(amount=1)
    obstacle = placed[0][0]
    assert isinstance(
        footprint_of('box', BOX, obstacle.spawn_pose.position,
                     obstacle.spawn_pose.orientation.yaw), Footprint)
