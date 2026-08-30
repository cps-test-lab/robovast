#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""What MoveIt is allowed to know about the world around the arm.

MoveIt plans against the ROBOT it was given and nothing else: the URDF describes the arm, and the
bench, the bin and the box exist only in the simulator. Without the obstacles built here, OMPL is
free to route a joint the long way round -- and it does.

MEASURED, first pilot of this cell: with an empty scene the planner returned a path taking
``shoulder_lift`` to +5.00 rad (the wraparound of -1.28), sweeping the arm straight down through the
bench. MuJoCo stopped it, the position servos saturated 5.59 rad from the commanded vector, and the
bridge still reported the trajectory SUCCEEDED, because it ends a trajectory on time rather than on
arrival. Nothing in the stack complained; the box simply never moved.

So this file is not decoration. An empty planning scene is a campaign that runs, passes its own
checks, and measures the wrong thing.
"""

from __future__ import annotations

import rclpy
from geometry_msgs.msg import Pose
from moveit_msgs.msg import AttachedCollisionObject, CollisionObject, PlanningScene
from moveit_msgs.srv import ApplyPlanningScene
from shape_msgs.msg import SolidPrimitive

#: The support surface. Wider than the arm's reach, so no plan can go around it. Its top sits
#: TOP_GAP below the real surface: the arm's own base link stands exactly on the bench, and a slab
#: flush with it would put the START state in collision and fail every plan instead.
BENCH_SIZE = (1.4, 1.0, 0.05)
BENCH_TOP_GAP = 0.005
#: The bin as four thin walls rather than one solid box: a solid one fills the cavity, and the place
#: pose is INSIDE it, so the drop could never be planned.
BIN_WALL_T = 0.010
#: The carried box's id in the scene.
BOX_ID = "parcel"


class Scene:
    """move_group's world model, applied through the service and confirmed.

    The service rather than the ``/planning_scene`` topic: a publish is fire-and-forget, so a scene
    that never arrived looks exactly like one the planner ignored -- and this campaign would then
    quietly measure the empty-scene behaviour described above.
    """

    def __init__(self, node, frame: str, tcp_link: str, gripper_links) -> None:
        self.node = node
        self.frame = frame
        self.tcp = tcp_link
        self.gripper_links = tuple(gripper_links)
        self.srv = node.create_client(ApplyPlanningScene, "/apply_planning_scene")

    def _box(self, oid: str, x: float, y: float, z: float, size) -> CollisionObject:
        """One axis-aligned box, in the planning frame.

        Identity orientation is correct for every object here even though the planning frame is
        yawed 180 deg from the world's: that yaw maps an axis-aligned box onto an axis-aligned box.
        """
        co = CollisionObject()
        co.header.frame_id = self.frame
        co.id = oid
        prim = SolidPrimitive()
        prim.type = SolidPrimitive.BOX
        prim.dimensions = [float(v) for v in size]
        pose = Pose()
        pose.position.x, pose.position.y, pose.position.z = x, y, z
        pose.orientation.w = 1.0
        co.primitives.append(prim)
        co.primitive_poses.append(pose)
        co.operation = CollisionObject.ADD
        return co

    def apply(self, objects=(), attached=()) -> bool:
        """Apply a scene DIFF and report whether move_group accepted it."""
        if not self.srv.wait_for_service(timeout_sec=20.0):
            return False
        scene = PlanningScene()
        scene.is_diff = True
        scene.robot_state.is_diff = True
        scene.world.collision_objects.extend(objects)
        scene.robot_state.attached_collision_objects.extend(attached)
        fut = self.srv.call_async(ApplyPlanningScene.Request(scene=scene))
        rclpy.spin_until_future_complete(self.node, fut, timeout_sec=15.0)
        res = fut.result()
        return bool(res is not None and res.success)

    def add_obstacles(self, binp, half_x: float, half_y: float, rim_z: float) -> bool:
        """The bench and the bin. The bin is placed from TF, not restated as a constant here."""
        objs = [self._box("bench", 0.0, 0.0, -(BENCH_TOP_GAP + BENCH_SIZE[2] / 2.0), BENCH_SIZE)]
        t = BIN_WALL_T
        cz = binp[2] + rim_z / 2.0
        walls = (
            (half_x + t / 2.0, 0.0, t, 2 * half_y + 2 * t),
            (-half_x - t / 2.0, 0.0, t, 2 * half_y + 2 * t),
            (0.0, half_y + t / 2.0, 2 * half_x, t),
            (0.0, -half_y - t / 2.0, 2 * half_x, t),
        )
        for i, (dx, dy, sx, sy) in enumerate(walls):
            objs.append(self._box(f"bin_wall_{i}", binp[0] + dx, binp[1] + dy, cz, (sx, sy, rim_z)))
        return self.apply(objects=objs)

    def attach(self, x: float, y: float, z: float, size) -> bool:
        """Carry the box IN the scene, so the traverse plans around what the arm is holding.

        The box is not in the scene before this point, deliberately: at the grasp pose the jaws are
        closed around it, which is a collision until it is attached, and the alternative is editing
        the allowed-collision matrix to permit exactly what ``touch_links`` already means.
        """
        aco = AttachedCollisionObject()
        aco.link_name = self.tcp
        aco.object = self._box(BOX_ID, x, y, z, size)
        aco.touch_links = list(self.gripper_links)
        return self.apply(attached=[aco])

    def detach(self) -> bool:
        """Let go in the scene as well as in the gripper, and drop the object entirely."""
        aco = AttachedCollisionObject()
        aco.link_name = self.tcp
        aco.object.id = BOX_ID
        aco.object.operation = CollisionObject.REMOVE
        gone = CollisionObject()
        gone.header.frame_id = self.frame
        gone.id = BOX_ID
        gone.operation = CollisionObject.REMOVE
        return self.apply(objects=[gone], attached=[aco])
