# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""The PX4 protocol, in one place. The scenario owns *when*; this owns *what PX4 requires*.

Every px4_msgs message this campaign sends is sent from here -- the two continuous streams and all
four one-shot commands. The scenario is the sequencer: it publishes a phase name and a course leg
and waits, exactly as ``../drone_flight_envelope/scenario.osc`` published a pose and waited.

**Why the split is drawn here and not somewhere more even.** Offboard is a streaming protocol with
a mode machine in front of it, and almost every rule in it is about *how* a message is formed or
*how often* it is sent rather than about when the trial wants it:

* ``OffboardControlMode`` on ``/fmu/in/offboard_control_mode`` is a heartbeat. PX4 requires it at
  better than 2 Hz; if it stops for longer than about half a second the vehicle leaves Offboard and
  falls back to its failsafe. It must also *already* be running before the mode switch is
  commanded, or the switch is rejected.
* ``TrajectorySetpoint`` on ``/fmu/in/trajectory_setpoint`` is the command, restated every cycle. A
  setpoint published once is not a setpoint PX4 will fly to; it is one sample of a stream that
  stopped.
* ``VehicleCommand`` carries a ``timestamp`` in microseconds, ``from_external`` must be true for
  PX4 to treat it as coming from a companion at all, and its ``target_*``/``source_*`` fields have
  to be filled. Those are protocol facts, not trial facts.

That last one is the reason the commands moved here rather than staying as readable
``topic_publish`` lines in the scenario. A ``topic_publish`` cannot ask the ROS clock for the
current time, so it can only send a literal -- and a literal ``timestamp`` is either wrong or a bet
that PX4 ignores the field. An unresolved protocol bet inside a thrust-margin campaign is the worst
possible one: if PX4 does reject a stale command, the aircraft never arms, and "never armed" is
exactly what a run at T/W <= 1 looks like. So the campaign would have reported the result it was
built to measure, from a bug. One component owning the protocol removes the question.

scenario-execution has no action that publishes at a rate either, which is what forced a node in
the first place. Its ROS library offers ``topic_publish``, ``topic_monitor``, ``service_call``,
``action_call``, the ``wait_for_*`` family, ``bag_record``, ``ros_launch`` and ``ros_run``; the only
repetition in the language is ``osc.helpers``' ``repeat`` modifier, whose period is whatever the
behaviour tree costs that tick rather than a control loop's -- and an unsteady heartbeat is
precisely what PX4 fails closed on. Inventing an action was not on the table.

**NED.** ``TrajectorySetpoint.position`` is [north, east, DOWN] in metres, so 3 m above the takeoff
point is ``z = -3.0``. This node does no conversion at all: the scenario states its course in NED
and this passes it through. That is deliberate -- a frame conversion hidden in a relay is how a
sign error survives review, and a sign error on z flies the aircraft into the floor at full thrust.

Started by ``scenario.osc``::

    run_process(command: 'python3 /config/files/offboard_stream.py', wait_for_shutdown: false)
"""

from __future__ import annotations

import sys

import rclpy
from geometry_msgs.msg import PointStamped
from rclpy.node import Node
from rclpy.qos import (DurabilityPolicy, HistoryPolicy, QoSProfile,
                       ReliabilityPolicy)
from std_msgs.msg import String

try:
    from px4_msgs.msg import (OffboardControlMode, TrajectorySetpoint,
                              VehicleCommand)
except ImportError as exc:  # pragma: no cover - an environment problem, not a code path
    # Loud, and with the cause named. px4_msgs has no released Debian for any ROS 2 distribution
    # (ros/rosdistro jazzy/distribution.yaml, 2026-08-31: a `source:` entry and no `release:`), so
    # the campaign builds it from source: see the `ros_packages:` block on the `scenario` container
    # in px4_envelope.vast. Reaching here means the container is not this campaign's derived image,
    # and a silent fallback would leave the aircraft on the pad and score it as a thrust-margin
    # failure, which is the one wrong answer this campaign can give.
    raise SystemExit(
        "offboard_stream: px4_msgs is not importable in this container "
        f"({exc}). px4_envelope.vast declares it under the scenario container's "
        "ros_packages:; this container was not built from that declaration."
    )

#: How fast both streams are published. PX4 requires the heartbeat above 2 Hz; 20 Hz is the rate
#: PX4's own ROS 2 offboard example uses and leaves an order of magnitude of margin for a container
#: that is briefly descheduled. Higher buys nothing: the position controller runs at its own rate
#: and a setpoint is a step, not a trajectory.
STREAM_HZ = 20.0

#: Where the scenario writes the current leg, in NED metres. A PointStamped rather than a PX4
#: message so that the course reads as three numbers in the .osc instead of a 12-field struct.
SETPOINT_TOPIC = "/course/ned_setpoint"

#: Where the scenario names the phase it wants. A bare string, because the scenario's whole
#: contribution here is the *order* of four words and how long it waits between them.
PHASE_TOPIC = "/course/phase"

#: MAVLink command ids and their parameters, as PX4's own ROS 2 offboard example sends them
#: (px4_ros_com ``src/examples/offboard/offboard_control.cpp``), read rather than remembered.
#: Values are (command, param1, param2).
PHASES = {
    # VEHICLE_CMD_DO_SET_MODE. param1 = 1 (custom mode enabled), param2 = 6 (PX4_CUSTOM_MAIN_MODE
    # _OFFBOARD). Sent before arming: PX4 will not fly an armed aircraft in a mode it is not in.
    "offboard": (176, 1.0, 6.0),
    # VEHICLE_CMD_COMPONENT_ARM_DISARM, param1 = 1 to arm.
    "arm": (400, 1.0, 0.0),
    # VEHICLE_CMD_NAV_LAND. Not a descending setpoint: PX4 has a landing mode with a descent
    # profile, ground detection and auto-disarm, so touchdown is flown by the same logic that flies
    # it on the vehicle. It also takes PX4 out of Offboard, which is why the streams below are
    # harmless from that point on -- PX4 stops reading them.
    "land": (21, 0.0, 0.0),
    # param1 = 0 to disarm. A backstop for the aircraft that never got airborne (T/W <= 1), where
    # PX4's land detector has nothing to detect.
    "disarm": (400, 0.0, 0.0),
}


class OffboardStream(Node):

    def __init__(self) -> None:
        super().__init__("offboard_stream")

        # Reliable + transient-local for both scenario-facing topics, so that a phase or a leg
        # published a moment before this node's subscription matched is still delivered. Without
        # durability the FIRST message of the flight is the one most likely to be lost, and losing
        # it looks like an aircraft that ignored its first command.
        latched = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )
        self._mode_pub = self.create_publisher(
            OffboardControlMode, "/fmu/in/offboard_control_mode", 10)
        self._setpoint_pub = self.create_publisher(
            TrajectorySetpoint, "/fmu/in/trajectory_setpoint", 10)
        self._command_pub = self.create_publisher(
            VehicleCommand, "/fmu/in/vehicle_command", 10)
        self.create_subscription(PointStamped, SETPOINT_TOPIC, self._on_setpoint, latched)
        self.create_subscription(String, PHASE_TOPIC, self._on_phase, latched)

        # The pad, in NED: on the ground at the takeoff point. Streaming this from t=0 rather than
        # waiting for the first leg is what lets the scenario switch to Offboard before it has
        # commanded anything -- PX4 rejects the mode switch if no setpoint stream is already
        # running, so "nothing to fly to yet" has to be expressed as a setpoint, not as silence.
        self._target = (0.0, 0.0, 0.0)
        self._yaw = 0.0

        self.create_timer(1.0 / STREAM_HZ, self._tick)
        self.get_logger().info(
            f"streaming OffboardControlMode + TrajectorySetpoint at {STREAM_HZ:g} Hz; "
            f"legs from {SETPOINT_TOPIC} (NED), phases from {PHASE_TOPIC}")

    # -- what the scenario asks for ------------------------------------------------------------

    def _on_setpoint(self, msg: PointStamped) -> None:
        self._target = (msg.point.x, msg.point.y, msg.point.z)
        # Logged with the sign intact: "D=-3.00" in the log beside "3 m up" in the scenario is what
        # makes a frame mistake visible without reading any code.
        self.get_logger().info(
            f"leg -> N={msg.point.x:.2f} E={msg.point.y:.2f} D={msg.point.z:.2f} (NED)")

    def _on_phase(self, msg: String) -> None:
        phase = msg.data.strip()
        if phase not in PHASES:
            # Refused rather than ignored. A typo in the scenario would otherwise skip a phase in
            # silence, and a trial that never armed reads as a thrust-margin failure.
            self.get_logger().error(
                f"unknown phase '{phase}'; expected one of {sorted(PHASES)}")
            return
        command, param1, param2 = PHASES[phase]
        self._send_command(command, param1, param2)
        self.get_logger().info(f"phase '{phase}' -> VehicleCommand {command} "
                               f"param1={param1} param2={param2}")

    # -- what PX4 requires ---------------------------------------------------------------------

    def _stamp(self) -> int:
        """PX4 timestamps are microseconds. Taken from this node's clock, which is the simulator's
        under use_sim_time -- so a run that is not realtime still stamps consistently with the bag,
        and every VehicleCommand carries a current time rather than a literal.
        """
        return self.get_clock().now().nanoseconds // 1000

    def _send_command(self, command: int, param1: float, param2: float) -> None:
        msg = VehicleCommand()
        msg.timestamp = self._stamp()
        msg.command = command
        msg.param1 = param1
        msg.param2 = param2
        msg.target_system = 1
        msg.target_component = 1
        msg.source_system = 1
        msg.source_component = 1
        # Without this PX4 treats the command as internal and ignores it.
        msg.from_external = True
        self._command_pub.publish(msg)

    def _tick(self) -> None:
        stamp = self._stamp()

        mode = OffboardControlMode()
        mode.timestamp = stamp
        # Position control: PX4 flies to the setpoint with its own velocity and acceleration
        # limits. Exactly one of these may be true, and which one decides which fields of
        # TrajectorySetpoint PX4 reads at all.
        mode.position = True
        mode.velocity = False
        mode.acceleration = False
        mode.attitude = False
        mode.body_rate = False
        self._mode_pub.publish(mode)

        setpoint = TrajectorySetpoint()
        setpoint.timestamp = stamp
        # NED. The third component is DOWN: negative is up. See the module docstring.
        setpoint.position = [float(self._target[0]), float(self._target[1]), float(self._target[2])]
        setpoint.yaw = float(self._yaw)
        self._setpoint_pub.publish(setpoint)


def main() -> int:
    rclpy.init(args=sys.argv)
    node = OffboardStream()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
