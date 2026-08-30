#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""The one motion goal this trial ever asks for: put the grasp point HERE, pointing down.

Kept apart from the trial because it is pure construction -- no node, no state, no ROS calls -- so
the phase machine next door reads as a sequence of poses rather than as message plumbing.

The orientation is the load-bearing part. In the 2F-85 the pads separate along the gripper's own Y
and the `pinch` site adds no rotation of its own, so in the TCP frame Z approaches and **Y closes**.
A half-turn about X therefore points the tool down AND lines the closing axis up with the box's
60 mm side; how far the planner may then roll away from that is `roll_tol`, and it is the difference
between grasping the 60 mm axis and grasping the 100 mm diagonal.
"""

from __future__ import annotations

from geometry_msgs.msg import PoseStamped
from moveit_msgs.action import MoveGroup
from moveit_msgs.msg import Constraints, OrientationConstraint, PositionConstraint
from shape_msgs.msg import SolidPrimitive


def top_down_goal(group: str, frame: str, link: str, x: float, y: float, z: float, *,
                  tol: float, tilt_tol: float, roll_tol: float, scale: float) -> MoveGroup.Goal:
    """A MoveGroup goal putting `link`'s origin within `tol` of (x, y, z) in `frame`, tool down."""
    g = MoveGroup.Goal()
    g.request.group_name = group
    g.request.num_planning_attempts = 6
    g.request.allowed_planning_time = 15.0
    # Slow, and an execution property rather than a preference: the servo shapes the achieved
    # profile, and a plan it lags behind throws a held box out of the jaws.
    g.request.max_velocity_scaling_factor = scale
    g.request.max_acceleration_scaling_factor = scale

    c = Constraints()
    pc = PositionConstraint()
    pc.header.frame_id = frame
    pc.link_name = link
    pc.weight = 1.0
    prim = SolidPrimitive()
    prim.type = SolidPrimitive.SPHERE
    prim.dimensions = [tol]
    pc.constraint_region.primitives.append(prim)
    p = PoseStamped().pose
    p.position.x, p.position.y, p.position.z = x, y, z
    p.orientation.w = 1.0
    pc.constraint_region.primitive_poses.append(p)
    c.position_constraints.append(pc)

    oc = OrientationConstraint()
    oc.header.frame_id = frame
    oc.link_name = link
    # Tool axis pointing down at the bench: a half-turn about x from identity.
    oc.orientation.x, oc.orientation.w = 1.0, 0.0
    oc.absolute_x_axis_tolerance = tilt_tol
    oc.absolute_y_axis_tolerance = tilt_tol
    oc.absolute_z_axis_tolerance = roll_tol
    oc.weight = 1.0
    c.orientation_constraints.append(oc)
    g.request.goal_constraints.append(c)
    return g
