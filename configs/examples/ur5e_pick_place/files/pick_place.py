#!/usr/bin/env python3
# Copyright (C) 2026 Frederik Pasch
# SPDX-License-Identifier: Apache-2.0
"""One UR5e pick-and-place trial: read the box's pose from the detector, grasp it, bin it.

**A failed trial is this campaign's RESULT, not its error.** This exits 0 either way: at the wider
noise levels failing IS the measurement. Read `success` with `failure` -- the phase name is half the
diagnosis.

Nothing here trusts a return value. Both bridge actions report success on DELIVERY rather than
arrival, and MoveIt's verdict is about MoveIt -- it can satisfy a goal exactly and still leave the
jaws beside the box. So every motion is checked against MEASURED state (``/joint_states``) and the
verdict is a rigid body's true pose on ``/tf``. See ``wait_gripper_still`` and ``move_to`` for what
each check cost to learn. Planning happens in the arm's own frame (``base``), where the detector
also reports, so no transform sits between perception and planning.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time

import rclpy
from control_msgs.action import GripperCommand
from moveit_msgs.action import MoveGroup
from rclpy.action import ActionClient
from rclpy.node import Node
from sensor_msgs.msg import JointState
from tf2_ros import Buffer, TransformListener
from vision_msgs.msg import Detection3DArray

from grasp_goal import top_down_goal
from planning_scene import Scene

#: The scenario gates on /clock and /joint_states; what is missing here is move_group, which takes
#: most of a minute to load its planners and kinematics plugin.
STACK_WAIT_S = 180.0

GROUP = "arm"
#: Welded to the bench, so the planning frame IS the URDF root link; `--root-link base` made that
#: the string the detector reports in too.
PLANNING_FRAME = "base"
#: The chain ends at the gripper's `pinch` site (`--tip-site`), so goals are about what GRASPS.
TCP = "tcp"
GRIPPER_JOINT = "robotiq_85_left_knuckle_joint"

#: Gripper angles in the units `moveit_controllers.yaml` reports: 0 open, 0.8 closed.
GRIP_OPEN = 0.0
GRIP_CLOSE = 0.8
GRIP_EFFORT = 50.0

APPROACH_CLEARANCE = 0.12   #: 0.12 m clears the 86 mm bin rim and is well inside reach
LIFT = 0.10          #: how far to lift after closing, before traversing
TRAVERSE_Z = 0.26    #: carry height, chosen to clear the bin rim (0.094 m above its floor)
RELEASE_Z = 0.16     #: where the box is released above the bin's floor

#: Velocity/acceleration scaling on every plan. Slow because the servo shapes the achieved profile,
#: and a plan it lags behind throws a held box out of the jaws.
VEL_SCALE = 0.15

#: Replans before giving up: planning fails outright now and then, and in a SWEEP that reads as the
#: grasp failure being measured. (move_group logs "Planning attempt 1 of at most 1" regardless.)
PLAN_TRIES = 3
#: Worth replanning: the planner found nothing, so nothing executed and the arm has not moved. Any
#: other code (an aborted execution, an invalid start state) is a different fault, and is reported.
PLAN_RETRY_CODES = frozenset({-1, -2, -31, 99999})

#: A pose counts as reached within this of the commanded joint vector; the servo converges
#: asymptotically, so waiting for zero never returns.
JOINT_TOL = 0.02
#: ...and this long to get there. Arm error is spent from the SAME ~13 mm budget as the noise: at
#: 4.0 s the descend left 0.037 rad, ~17 mm at this radius.
SETTLE_S = 12.0
PICKED_MIN_RISE = 0.030   #: below this the box never left the bench: closed on air, not a mis-drop
#: The bin's cavity -- world/drop_bin.xml's numbers, telling "in" from "on the rim".
BIN_HALF_X = 0.093
BIN_HALF_Y = 0.143
BIN_RIM_Z = 0.094

#: Links allowed to touch the box once attached; the export collapses the 2F-85 to these three.
GRIPPER_LINKS = ("robotiq_85_base_link", "robotiq_85_left_knuckle_joint_link", "tcp")

#: How far the tool axis may tip off vertical.
TILT_TOL = 0.25
#: How far the wrist may ROLL about it; BOTH bounds are measured. Y CLOSES and the goal lines it up
#: with the box's 60 mm axis, so the jaws swallow `60*cos(t) + 80*sin(t)` mm of an 87 mm aperture
#: (0.10 -> 67.7 mm, 9.6 per side). FREE they closed across the DIAGONAL and squirted the box out;
#: at 0.05 OMPL failed to solve at all.
ROLL_TOL = 0.10


def _now() -> float:
    return time.monotonic()


class PickPlace(Node):
    """The phase machine, and the measurements that decide whether each phase actually happened."""

    def __init__(self) -> None:
        super().__init__("pick_place")
        self.detections: Detection3DArray | None = None
        self.joints: dict[str, float] = {}

        self.create_subscription(Detection3DArray, "/detections", self._on_detections, 10)
        self.create_subscription(JointState, "/joint_states", self._on_joints, 10)
        # tf2, not a raw /tf subscription: a pose arrives already IN the planning frame, so the
        # bench height and the arm's 180 deg yaw stay facts of the WORLD rather than constants here.
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        self.move = ActionClient(self, MoveGroup, "move_action")
        self.grip = ActionClient(self, GripperCommand, "/gripper_controller/gripper_cmd")
        # What move_group is allowed to know about the bench, the bin and the carried box. See
        # files/planning_scene.py -- an empty scene here plans straight through the bench.
        self.scene = Scene(self, PLANNING_FRAME, TCP, GRIPPER_LINKS)

        # Measurements the verdict is assembled from.
        self.max_rise = 0.0
        self.box_start_z: float | None = None
        self.worst_residual = 0.0
        self.grip_closed_to: float | None = None
        self.detect_delta: tuple[float, float, float] | None = None

    # -- inputs ----------------------------------------------------------------------------------
    def _on_detections(self, msg: Detection3DArray) -> None:
        self.detections = msg

    def _on_joints(self, msg: JointState) -> None:
        for name, pos in zip(msg.name, msg.position, strict=False):
            self.joints[name] = float(pos)

    def at(self, frame: str) -> tuple[float, float, float] | None:
        """``frame``'s origin in the PLANNING frame, or None while the chain is incomplete."""
        try:
            t = self.tf_buffer.lookup_transform(
                PLANNING_FRAME, frame, rclpy.time.Time()
            ).transform.translation
        except Exception:  # noqa: BLE001 -- absent or not yet connected; the caller retries
            return None
        return (t.x, t.y, t.z)

    def track_box(self) -> None:
        """Latch the box's highest point, DURING the lift rather than at the end.

        Read at the end it cannot tell "never picked" from "picked, then dropped", which is the one
        distinction these columns exist to draw.
        """
        box = self.at("graspable_box")
        if box is None:
            return
        if self.box_start_z is None:
            self.box_start_z = box[2]
        self.max_rise = max(self.max_rise, box[2] - self.box_start_z)

    def spin(self, seconds: float) -> None:
        deadline = _now() + seconds
        while _now() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)
            self.track_box()

    def wait_for(self, predicate, timeout: float, what: str) -> bool:
        deadline = _now() + timeout
        while _now() < deadline:
            rclpy.spin_once(self, timeout_sec=0.1)
            self.track_box()
            if predicate():
                return True
        self.get_logger().error(f"timed out waiting for {what}")
        return False

    # -- motion ----------------------------------------------------------------------------------
    def move_to(self, phase: str, x: float, y: float, z: float, tol: float = 0.008) -> str | None:
        """Plan and execute. Returns None on success, else a `<phase>:<reason>` string."""
        goal = top_down_goal(GROUP, PLANNING_FRAME, TCP, x, y, z, tol=tol,
                             tilt_tol=TILT_TOL, roll_tol=ROLL_TOL, scale=VEL_SCALE)
        for attempt in range(PLAN_TRIES):
            fut = self.move.send_goal_async(goal)
            rclpy.spin_until_future_complete(self, fut, timeout_sec=40.0)
            handle = fut.result()
            if handle is None or not handle.accepted:
                return f"{phase}:goal_rejected"
            rf = handle.get_result_async()
            rclpy.spin_until_future_complete(self, rf, timeout_sec=120.0)
            res = rf.result()
            if res is None:
                return f"{phase}:no_result"
            code = res.result.error_code.val
            if code == 1:
                break
            if code not in PLAN_RETRY_CODES or attempt == PLAN_TRIES - 1:
                return f"{phase}:moveit_error_{code}"
            self.get_logger().warning(f"{phase}: planner returned {code}, replanning")

        # The action has returned; the arm has not necessarily arrived. Wait on the JOINTS against
        # the trajectory's own last waypoint, which is what "arrived" means here.
        traj = res.result.planned_trajectory.joint_trajectory
        if traj.points:
            want = dict(zip(traj.joint_names, traj.points[-1].positions, strict=False))

            def gap() -> float:
                """Worst |measured - commanded| over the trajectory's own joints, in RADIANS.

                A joint the controller reports under another name would silently read as a huge
                gap, so a missing one raises rather than defaulting: in the result column the two
                look alike, and one is a broken measurement rather than a failed grasp.
                """
                missing = [j for j in want if j not in self.joints]
                if missing:
                    raise KeyError(f"joints absent from /joint_states: {sorted(missing)}")
                return max((abs(self.joints[j] - v) for j, v in want.items()), default=0.0)

            self.wait_for(lambda: gap() <= JOINT_TOL, SETTLE_S, f"{phase} to settle")
            self.worst_residual = max(self.worst_residual, gap())
        return None

    def set_gripper(self, position: float, phase: str) -> str | None:
        goal = GripperCommand.Goal()
        goal.command.position = position
        goal.command.max_effort = GRIP_EFFORT
        fut = self.grip.send_goal_async(goal)
        rclpy.spin_until_future_complete(self, fut, timeout_sec=20.0)
        handle = fut.result()
        if handle is None or not handle.accepted:
            return f"{phase}:gripper_rejected"
        rf = handle.get_result_async()
        rclpy.spin_until_future_complete(self, rf, timeout_sec=30.0)
        # The RESULT infers a grasp from a stall, and this one stalls while still closing: wait
        # for the JAWS.
        self.wait_gripper_still()
        return None

    def wait_gripper_still(self, timeout: float = 10.0, quiet: float = 0.5) -> None:
        """Wait for the jaws to start moving and then stop. NOT a fixed pause.

        MEASURED: the action returned and a 1.5 s pause after it STILL expired before the jaws moved
        -- the knuckle read -0.0005 a second before it began closing and 0.78 half a second later. A
        fixed wait therefore read the gripper OPEN and recorded that as the grasp.
        """
        start = self.joints.get(GRIPPER_JOINT)
        last, still_since, moved = start, None, False
        deadline = _now() + timeout
        while _now() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)
            cur = self.joints.get(GRIPPER_JOINT)
            if cur is None:
                continue
            if start is not None and abs(cur - start) > 5e-3:
                moved = True
            if last is not None and abs(cur - last) < 1e-4:
                still_since = _now() if still_since is None else still_since
                if moved and _now() - still_since >= quiet:
                    return
            else:
                still_since = None
            last = cur

    # -- the trial -------------------------------------------------------------------------------
    def run(self) -> tuple[bool, bool, str | None]:
        """(picked, placed, failure). Never raises for a trial outcome."""
        if not self.move.wait_for_server(timeout_sec=STACK_WAIT_S):
            return False, False, "startup:no_move_action"
        if not self.grip.wait_for_server(timeout_sec=60.0):
            return False, False, "startup:no_gripper_action"
        if not self.wait_for(lambda: self.detections is not None, 30.0, "/detections"):
            return False, False, "startup:no_detections"
        if not self.wait_for(lambda: self.at("graspable_box"), 30.0, "the box in TF"):
            return False, False, "startup:no_box_tf"
        if not self.wait_for(lambda: self.at("drop_bin"), 30.0, "the bin in TF"):
            return False, False, "startup:no_bin_tf"

        det = self.detections
        if det.header.frame_id != PLANNING_FRAME:
            # Detection and planning must share a frame; nothing here transforms between them.
            return False, False, f"startup:detections_in_{det.header.frame_id}"
        if not det.detections:
            return False, False, "startup:nothing_detected"
        c = det.detections[0].bbox.center.position
        bx, by, bz = c.x, c.y, c.z

        # What the noise cost THIS run: |detected - true|, both already in the planning frame.
        truth = self.at("graspable_box")
        self.detect_delta = (bx - truth[0], by - truth[1], bz - truth[2])

        # From TF, so the world YAML stays the only place the bin's position is written down.
        binp = self.at("drop_bin")
        # Before the first plan: an obstacle added later cannot un-plan a path through it.
        if not self.scene.add_obstacles(binp, BIN_HALF_X, BIN_HALF_Y, BIN_RIM_Z):
            return False, False, "startup:planning_scene_rejected"

        if (f := self.move_to("approach", bx, by, bz + APPROACH_CLEARANCE)) is not None:
            return False, False, f
        if (f := self.set_gripper(GRIP_OPEN, "open")) is not None:
            return False, False, f
        if (f := self.move_to("descend", bx, by, bz)) is not None:
            return False, False, f
        if (f := self.set_gripper(GRIP_CLOSE, "close")) is not None:
            return False, False, f

        # Where the jaws stopped: the commanded 0.8 means they closed on NOTHING, short means
        # something is between them.
        self.grip_closed_to = self.joints.get(GRIPPER_JOINT)
        if self.grip_closed_to is not None and self.grip_closed_to >= GRIP_CLOSE - 0.02:
            return False, False, "close:jaws_closed_fully_empty"

        # Now it is held, so the scene has to know the hand is bigger than the hand.
        # LATERALLY from the tool (where the jaws closed), VERTICALLY from geometry (it rests on the
        # bench): the detection modelled a low box in the bench, the tool's height the settle error.
        bs = det.detections[0].bbox.size
        held = self.at(TCP) or (bx, by, bz)
        if not self.scene.attach(held[0], held[1], bs.z / 2.0, (bs.x, bs.y, bs.z)):
            return False, False, "close:attach_rejected"

        if (f := self.move_to("lift", bx, by, bz + LIFT)) is not None:
            return False, False, f
        self.spin(1.0)
        if self.max_rise < PICKED_MIN_RISE:
            return False, False, "lift:box_did_not_rise"
        picked = True

        if (f := self.move_to("traverse", bx, by, TRAVERSE_Z, tol=0.03)) is not None:
            return picked, False, f
        if (f := self.move_to("over_bin", binp[0], binp[1], TRAVERSE_Z, tol=0.03)) is not None:
            return picked, False, f
        if (f := self.move_to("lower", binp[0], binp[1], RELEASE_Z, tol=0.03)) is not None:
            return picked, False, f
        if (f := self.set_gripper(GRIP_OPEN, "release")) is not None:
            return picked, False, f
        self.scene.detach()

        # Let it land before judging where it landed.
        self.spin(2.5)
        box = self.at("graspable_box")
        # The cavity is axis-aligned and the frames differ only by a yaw of 180 deg, so |dx| and
        # |dy| are the same test in either.
        inside = (
            abs(box[0] - binp[0]) <= BIN_HALF_X
            and abs(box[1] - binp[1]) <= BIN_HALF_Y
            and box[2] <= binp[2] + BIN_RIM_Z
        )
        if not inside:
            return picked, False, "place:box_not_in_bin"
        return picked, True, None

    def place_error(self) -> float | None:
        box, binp = self.at("graspable_box"), self.at("drop_bin")
        if box is None or binp is None:
            return None
        return math.dist(box[:2], binp[:2])


def main() -> int:
    ap = argparse.ArgumentParser(description="One UR5e pick-and-place trial.")
    # REQUIRED, not defaulted: a hardcoded /out writes a campaign's results nowhere, silently.
    ap.add_argument("--out", required=True, help="CSV to write the single result row to")
    args = ap.parse_args()

    rclpy.init()
    node = PickPlace()
    try:
        picked, placed, failure = node.run()
    except Exception as err:  # noqa: BLE001 -- a crash is still a trial outcome, not a broken run
        node.get_logger().error(f"trial raised: {err}")
        picked, placed, failure = False, False, f"exception:{type(err).__name__}"

    d = node.detect_delta
    row = {
        "success": placed,
        "picked": picked,
        "placed": placed,
        "place_error_m": node.place_error(),
        "detect_error_m": None if d is None else math.dist(d, (0.0, 0.0, 0.0)),
        # Split by AXIS: they fail differently and |error| cannot tell them apart. `y` crosses the
        # jaws (roll is pinned) and spends the ~13 mm clearance; `z` is the approach axis, where too
        # much shuts the jaws above or below the box at a tiny `y`.
        "detect_error_y_m": None if d is None else abs(d[1]),
        "detect_error_z_m": None if d is None else abs(d[2]),
        "max_rise_m": node.max_rise,
        "grip_closed_to": node.grip_closed_to,
        # RADIANS, joint-space: the worst gap between commanded and measured. This is the part of
        # the error budget that is NOT perception, so it is what calibrates the noise levels.
        "worst_arm_residual_rad": node.worst_residual,
        "failure": failure or "",
    }
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row))
        w.writeheader()
        w.writerow(row)
    node.get_logger().info(f"result: {row}")

    node.destroy_node()
    rclpy.shutdown()
    # ALWAYS 0. See the module docstring: at the wider noise levels, failing is the measurement.
    return 0


if __name__ == "__main__":
    sys.exit(main())
