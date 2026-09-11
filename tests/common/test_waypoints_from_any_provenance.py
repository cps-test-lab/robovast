# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A pose reads the same whether the campaign wrote it or a variation did.

One config key carries both spellings: a campaign stating poses in its `parameters:` block
leaves the mapping YAML parsed, while a variation that generated them leaves `Pose` objects. A
consumer that assumed the second worked only after a path variation had run -- and failed on
attribute access, deep in composition, for the campaign that had simply stated its own poses.
"""

import pytest

from robovast_nav.data_model import Orientation, Pose, Position
from robovast_nav.variation.nav_base_variation import NavVariation
from robovast_nav.variation.obstacle_variation import ObstacleVariationConfig


# -- the coercion -------------------------------------------------------------------------

def test_a_mapping_becomes_a_pose():
    pose = Pose.from_any({'position': {'x': 1.0, 'y': 2.0}, 'orientation': {'yaw': 0.5}})
    assert pose == Pose(position=Position(x=1.0, y=2.0), orientation=Orientation(yaw=0.5))


def test_a_pose_passes_through_unchanged():
    pose = Pose(position=Position(x=1.0, y=2.0), orientation=Orientation(yaw=0.5))
    assert Pose.from_any(pose) is pose


def test_an_absent_orientation_is_zero_yaw():
    """A 2-D placement states where; a campaign that does not care which way the robot faces
    should not have to write it."""
    assert Pose.from_any({'position': {'x': 1.0, 'y': 2.0}}).orientation.yaw == 0.0


def test_a_null_orientation_is_zero_yaw():
    assert Pose.from_any({'position': {'x': 0.0, 'y': 0.0}, 'orientation': None}
                         ).orientation.yaw == 0.0


def test_integers_are_accepted_as_coordinates():
    """YAML gives an int for `x: 2`, and a pose is no less stated for it."""
    assert Pose.from_any({'position': {'x': 2, 'y': -3}}).position == Position(x=2.0, y=-3.0)


@pytest.mark.parametrize('value', [
    None, 42, 'start_pose', {}, {'x': 1.0, 'y': 2.0}, {'position': {'x': 1.0}},
])
def test_what_is_not_a_pose_says_so_naming_itself(value):
    with pytest.raises(ValueError, match='not a pose'):
        Pose.from_any(value)


# -- reading them through the slots -------------------------------------------------------

def _reader(**binding):
    """A NavVariation with nothing but its parameters -- `get_waypoints` needs no more."""
    # pylint: disable-next=no-value-for-parameter
    reader = NavVariation.__new__(NavVariation)
    reader.parameters = ObstacleVariationConfig(
        scenario={'objects': 'static_objects'},
        reads=binding or {'start': 'start_pose', 'goal': 'goal_poses'},
        obstacle_configs=[{'amount': 1, 'max_distance': 0.0, 'model': 'box.sdf.xacro'}],
        seed=1, robot_diameter=0.35)
    return reader


DICT_START = {'position': {'x': 0.0, 'y': 0.0}}
DICT_GOAL = {'position': {'x': 3.0, 'y': 4.0}}
POSE_START = Pose(position=Position(x=0.0, y=0.0), orientation=Orientation(yaw=0.0))
POSE_GOAL = Pose(position=Position(x=3.0, y=4.0), orientation=Orientation(yaw=1.0))


def _config(**params):
    return {'name': 'cfg', 'config': params}


def test_poses_the_campaign_stated_are_read():
    waypoints = _reader().get_waypoints(
        _config(start_pose=DICT_START, goal_poses=[DICT_GOAL]))
    assert waypoints == [POSE_START, Pose(position=Position(x=3.0, y=4.0),
                                          orientation=Orientation(yaw=0.0))]


def test_poses_a_variation_produced_are_read():
    waypoints = _reader().get_waypoints(
        _config(start_pose=POSE_START, goal_poses=[POSE_GOAL]))
    assert waypoints == [POSE_START, POSE_GOAL]


def test_both_provenances_give_the_same_waypoints():
    """The whole point: a consumer never asks which wrote them."""
    stated = _reader().get_waypoints(_config(start_pose=DICT_START, goal_poses=[DICT_GOAL]))
    produced = _reader().get_waypoints(_config(
        start_pose=POSE_START,
        goal_poses=[Pose(position=Position(x=3.0, y=4.0), orientation=Orientation(yaw=0.0))]))
    assert stated == produced


def test_a_goal_bound_to_a_singular_parameter_still_gives_a_list():
    waypoints = _reader(start='start_pose', goal='goal_pose').get_waypoints(
        _config(start_pose=DICT_START, goal_pose=DICT_GOAL))
    assert len(waypoints) == 2


def test_several_goals_arrive_in_order():
    far = {'position': {'x': 9.0, 'y': 0.0}}
    waypoints = _reader().get_waypoints(
        _config(start_pose=DICT_START, goal_poses=[DICT_GOAL, far]))
    assert [w.position.x for w in waypoints] == [0.0, 3.0, 9.0]


def test_the_bound_name_is_the_one_read():
    """A campaign that named its parameters differently is read at those names."""
    waypoints = _reader(start='robot_start', goal='waypoints').get_waypoints(
        _config(robot_start=DICT_START, waypoints=[DICT_GOAL]))
    assert waypoints[0] == POSE_START


def test_the_conventional_name_is_not_read_when_something_else_is_bound():
    """No fallback: binding means the bound name, or nothing."""
    with pytest.raises(ValueError, match="robot_start"):
        _reader(start='robot_start', goal='waypoints').get_waypoints(
            _config(start_pose=DICT_START, goal_poses=[DICT_GOAL]))


@pytest.mark.parametrize('missing,params', [
    ('start_pose', {'goal_poses': [DICT_GOAL]}),
    ('goal_poses', {'start_pose': DICT_START}),
])
def test_an_absent_pose_is_refused_naming_it_and_both_remedies(missing, params):
    """Loud, and it names the OTHER valid answer: stating the pose outright is as good as
    running a path variation, which the old message did not say."""
    with pytest.raises(ValueError) as exc:
        _reader().get_waypoints(_config(**params))
    assert missing in str(exc.value)
    assert "parameters.scenario" in str(exc.value)
    assert "PathVariationRandom" in str(exc.value)
