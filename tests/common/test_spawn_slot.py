# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""``spawn``: the simulator's view of a path variation's start pose.

A drawn start pose is one fact that both sides of the compile boundary need. The stack under test
is told where it starts (``start``, a scenario parameter); the simulator compiles the robot there
(``spawn``, a world's spawn pose). They are separate slots because they are separate artifacts --
a ``pose_3d`` and a ``geometry_msgs/Pose`` -- so the shape follows the slot rather than being
inferred from the channel it landed on.

The pattern is ``ObstacleVariation``'s, where the trial's view of an obstacle and the simulator's
view of the same placement are likewise written from one call.
"""

import math

import pytest

from robovast_nav.data_model import Orientation, Pose, Position, pose_to_message


def _pose(x=1.5, y=-2.0, yaw=0.0):
    return Pose(Position(x, y), Orientation(yaw))


# -- the shape a world reads ------------------------------------------------------------------

def test_the_orientation_becomes_a_quaternion():
    msg = pose_to_message(_pose(yaw=math.pi / 2))
    assert msg["orientation"] == pytest.approx(
        {"x": 0.0, "y": 0.0, "z": math.sin(math.pi / 4), "w": math.cos(math.pi / 4)}
    )


def test_the_yaw_key_does_not_survive():
    """`{yaw: ...}` under `orientation` is a quaternion message with x, y, z and w all defaulted to
    zero, so a simulator reading it would place the entity at a rotation nobody asked for -- from a
    line that reads correctly."""
    assert "yaw" not in pose_to_message(_pose(yaw=1.0))["orientation"]


def test_the_position_carries_no_z():
    """An omitted z means the entity's own resting height, which is what a wheeled robot needs: a
    base authored at the origin with its wheels below it is buried by a literal zero."""
    assert set(pose_to_message(_pose())["position"]) == {"x", "y"}


def test_the_position_is_the_pose_s_own():
    msg = pose_to_message(_pose(x=3.25, y=-4.5))
    assert (msg["position"]["x"], msg["position"]["y"]) == pytest.approx((3.25, -4.5))


@pytest.mark.parametrize("yaw", [-3.0, -1.5, 0.0, 0.75, 3.0])
def test_the_heading_survives_the_conversion(yaw):
    """Read back the way a receiver does, so this fails if either side changes its convention."""
    q = pose_to_message(_pose(yaw=yaw))["orientation"]
    back = math.atan2(2.0 * q["w"] * q["z"], 1.0 - 2.0 * q["z"] * q["z"])
    assert back == pytest.approx(yaw)


def test_the_result_is_plain_data():
    """It is written into a world document, so nothing in it may be a dataclass the YAML writer
    would have to know about."""
    msg = pose_to_message(_pose(yaw=0.5))
    assert isinstance(msg, dict)
    assert all(isinstance(v, dict) for v in msg.values())
    assert all(isinstance(n, float) for v in msg.values() for n in v.values())


# -- the slot ---------------------------------------------------------------------------------

def _config(**bindings):
    from robovast_nav.variation.path_variation import PathVariationRandomConfig

    return PathVariationRandomConfig(
        path_length=5.0, num_paths=1, min_distance=1.0, seed=1, robot_diameter=0.35, **bindings
    )


def test_the_spawn_slot_is_optional():
    """Every campaign that binds only `start` keeps working, which is what lets this go in."""
    cfg = _config(scenario={"start": "start_pose", "goal": "goal_pose"})
    assert cfg.is_bound("start") and not cfg.is_bound("spawn")


def test_both_views_of_one_pose_may_be_bound_at_once():
    cfg = _config(
        scenario={"start": "start_pose", "goal": "goal_pose"},
        sim={"spawn": "components.robot.pose"},
    )
    assert cfg.binding("start") == ("scenario", "start_pose")
    assert cfg.binding("spawn") == ("sim", "components.robot.pose")


def test_the_spawn_does_not_stand_in_for_the_start():
    """`spawn` is an addition, not a substitute: a variation that produces a start pose must still
    say where it goes, so a campaign cannot bind only the simulator's half and leave the pose the
    trial was drawn to run from unbound."""
    import pytest as _pytest

    with _pytest.raises(Exception, match="unbound: start"):
        _config(scenario={"goal": "goal_pose"}, sim={"spawn": "components.robot.pose"})


def test_the_declared_outputs_name_both_destinations():
    """What validation, preview and the rendered docs read."""
    cfg = _config(
        scenario={"start": "start_pose", "goal": "goal_pose"},
        sim={"spawn": "components.robot.pose"},
    )
    outputs = cfg.outputs()
    assert outputs["scenario"] == ["start_pose", "goal_pose"]
    assert outputs["sim"] == ["components.robot.pose"]


def test_the_spawn_value_is_the_start_pose_in_the_world_s_shape():
    """One drawn pose, written twice -- the trial's as it stands, the simulator's converted."""
    from robovast_nav.variation.path_variation import StartGoalSlots

    class _Slots(StartGoalSlots):
        def __init__(self, parameters):
            self.parameters = parameters

    pose = _pose(x=2.0, y=3.0, yaw=0.5)
    values = _Slots(
        _config(scenario={"start": "start_pose", "goal": "goal_pose"},
                sim={"spawn": "components.robot.pose"})
    )._placement(pose)
    assert values["start"] is pose
    assert values["spawn"] == pose_to_message(pose)


def test_nothing_is_written_for_a_spawn_nobody_bound():
    from robovast_nav.variation.path_variation import StartGoalSlots

    class _Slots(StartGoalSlots):
        def __init__(self, parameters):
            self.parameters = parameters

    values = _Slots(_config(scenario={"start": "start_pose", "goal": "goal_pose"}))._placement(_pose())
    assert set(values) == {"start"}
