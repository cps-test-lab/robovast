# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A start pose crossing onto the ``sim`` channel is written the way a simulator states one.

A path variation's start pose can land on either channel. A scenario parameter is a ``pose_3d`` and
takes this model's own pose; the ``sim`` channel is the simulator's ground, where a pose is a
``geometry_msgs/Pose`` -- the shape ``SpawnEntity.srv`` gives its ``initial_pose``. Binding ``start``
there is what lets a campaign compile the robot where the path begins instead of moving it once the
trial is running.

The two spellings share the key ``orientation`` and disagree about its contents, which is why the
conversion has to be explicit: a yaw dumped as-is is a valid-looking quaternion with every component
defaulted to zero.
"""

import math

import pytest

from robovast_nav.data_model import Orientation, Pose, Position, pose_to_message


def _pose(x=1.5, y=-2.0, yaw=0.0):
    return Pose(Position(x, y), Orientation(yaw))


def test_the_orientation_becomes_a_quaternion():
    msg = pose_to_message(_pose(yaw=math.pi / 2))
    assert msg["orientation"] == pytest.approx(
        {"x": 0.0, "y": 0.0, "z": math.sin(math.pi / 4), "w": math.cos(math.pi / 4)}
    )


def test_the_yaw_key_does_not_survive():
    """The whole point. `{yaw: ...}` under `orientation` is a quaternion message with x, y, z and w
    all defaulted to zero, so a simulator reading it would place the entity at a rotation nobody
    asked for -- from a line that reads correctly."""
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
    """Read back the way a receiver does, so the test fails if either half changes its convention."""
    q = pose_to_message(_pose(yaw=yaw))["orientation"]
    back = math.atan2(2.0 * q["w"] * q["z"], 1.0 - 2.0 * q["z"] * q["z"])
    assert back == pytest.approx(yaw)


def test_the_result_is_plain_data():
    """It is dumped into a world document, so nothing in it may be a dataclass the YAML writer
    would have to know about."""
    msg = pose_to_message(_pose(yaw=0.5))
    assert isinstance(msg, dict)
    assert all(isinstance(v, dict) for v in msg.values())
    assert all(isinstance(n, float) for v in msg.values() for n in v.values())
