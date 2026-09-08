# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0

"""A path variation's start pose, on both sides of the compile boundary.

A drawn start pose is ONE value both sides need: the stack under test is told where it starts,
and the simulator can compile the robot there rather than have the trial move it once the run
is going -- with nav2 and AMCL coming up around a robot that was somewhere else a moment ago.

So it is one output bound twice, not two outputs. The binding mechanism itself is covered in
test_output_slots.py; what is pinned here is that this variation's start pose can use it, and
that what reaches a world through it is a pose a world reads.
"""

from robovast.common import convert_dataclasses_to_dict
from robovast_nav.data_model import Orientation, Pose, Position

import pytest


def _pose(x=1.5, y=-2.0, yaw=0.0):
    return Pose(Position(x, y), Orientation(yaw))


def _config(**bindings):
    from robovast_nav.variation.path_variation import PathVariationRandomConfig

    return PathVariationRandomConfig(
        path_length=5.0, num_paths=1, min_distance=1.0, seed=1, robot_diameter=0.35, **bindings
    )


def test_the_start_pose_may_name_a_destination_on_each_channel():
    cfg = _config(
        scenario={"start": "start_pose", "goal": "goal_pose"},
        sim={"start": "components.robot.pose"},
    )
    assert cfg.bindings("start") == (("scenario", "start_pose"),
                                     ("sim", "components.robot.pose"))


def test_a_campaign_that_binds_only_the_trial_is_unchanged():
    """What lets this go in: the simulator's destination is an addition nobody has to name."""
    cfg = _config(scenario={"start": "start_pose", "goal": "goal_pose"})
    assert cfg.bindings("start") == (("scenario", "start_pose"),)


def test_the_start_must_still_be_bound_somewhere():
    with pytest.raises(Exception, match="unbound: start"):
        _config(scenario={"goal": "goal_pose"})


def test_both_destinations_are_declared():
    """What validation, preview and the rendered docs read -- so a world path is checked
    against the backend just as the scenario parameter is checked against the .osc."""
    cfg = _config(
        scenario={"start": "start_pose", "goal": "goal_pose"},
        sim={"start": "components.robot.pose"},
    )
    outputs = cfg.outputs()
    assert outputs["scenario"] == ["start_pose", "goal_pose"]
    assert outputs["sim"] == ["components.robot.pose"]


def test_the_pose_a_world_receives_is_one_a_world_reads():
    """No conversion on the way to the simulator, because none is needed.

    A world's ``pose:`` states orientation as a quaternion OR as Euler angles, and tells the
    two apart by the keys present rather than by a mode flag -- so ``{yaw: ...}`` is read as a
    heading, not as a quaternion with its components defaulted. This pose carries exactly that,
    which is why it can go to a world as it stands.

    ``position`` deliberately has no ``z``: omitted means the model's own resting height, and a
    wheeled base authored at the origin with its wheels below it is buried by a literal zero.
    """
    assert convert_dataclasses_to_dict(_pose(x=1.5, y=2.0, yaw=0.785)) == {
        "position": {"x": 1.5, "y": 2.0},
        "orientation": {"yaw": 0.785},
    }
