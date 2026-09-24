# Copyright (C) 2026 Frederik Pasch
#
# SPDX-License-Identifier: Apache-2.0

"""Everything PX4's Offboard protocol requires, in one node: the OffboardControlMode heartbeat and
the TrajectorySetpoint stream at 20 Hz, and the VehicleCommands with a current timestamp. The
scenario sequences it by publishing a phase name (/course/phase) and the current leg
(/course/ned_setpoint, NED metres, passed through unconverted).

Started by scenario.osc: run_process(command: 'python3 /config/files/offboard_stream.py')
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
except ImportError as exc:  # pragma: no cover
    # px4_msgs has no Debian; the .vast's ros_packages builds it into the derived image.
    raise SystemExit(
        "offboard_stream: px4_msgs is not importable in this container "
        f"({exc}). px4_envelope.vast declares it under the scenario container's "
        "ros_packages:; this container was not built from that declaration."
    ) from exc

# PX4 leaves Offboard if the heartbeat drops below 2 Hz; 20 Hz is PX4's own example rate.
STREAM_HZ = 20.0

SETPOINT_TOPIC = "/course/ned_setpoint"
PHASE_TOPIC = "/course/phase"

# (command, param1, param2), as px4_ros_com's offboard_control.cpp sends them.
PHASES = {
    "offboard": (176, 1.0, 6.0),   # DO_SET_MODE: custom mode, PX4_CUSTOM_MAIN_MODE_OFFBOARD
    "arm": (400, 1.0, 0.0),        # COMPONENT_ARM_DISARM
    "land": (21, 0.0, 0.0),        # NAV_LAND: PX4's landing mode, takes it out of Offboard
    "disarm": (400, 0.0, 0.0),     # backstop for an aircraft that never got airborne
}


class OffboardStream(Node):

    def __init__(self) -> None:
        super().__init__("offboard_stream")

        # Transient-local, so a phase or leg published before the subscription matched still
        # arrives. The scenario's publishers must offer the same durability.
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

        # The pad, streamed from t=0: PX4 rejects the switch to Offboard without a running stream.
        self._target = (0.0, 0.0, 0.0)
        self._yaw = 0.0

        self.create_timer(1.0 / STREAM_HZ, self._tick)
        self.get_logger().info(
            f"streaming OffboardControlMode + TrajectorySetpoint at {STREAM_HZ:g} Hz; "
            f"legs from {SETPOINT_TOPIC} (NED), phases from {PHASE_TOPIC}")

    def _on_setpoint(self, msg: PointStamped) -> None:
        self._target = (msg.point.x, msg.point.y, msg.point.z)
        self.get_logger().info(
            f"leg -> N={msg.point.x:.2f} E={msg.point.y:.2f} D={msg.point.z:.2f} (NED)")

    def _on_phase(self, msg: String) -> None:
        phase = msg.data.strip()
        if phase not in PHASES:
            self.get_logger().error(
                f"unknown phase '{phase}'; expected one of {sorted(PHASES)}")
            return
        command, param1, param2 = PHASES[phase]
        self._send_command(command, param1, param2)
        self.get_logger().info(f"phase '{phase}' -> VehicleCommand {command} "
                               f"param1={param1} param2={param2}")

    def _stamp(self) -> int:
        """Microseconds, from this node's clock (the simulator's under use_sim_time)."""
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
        msg.from_external = True   # otherwise PX4 treats the command as internal and ignores it
        self._command_pub.publish(msg)

    def _tick(self) -> None:
        stamp = self._stamp()

        mode = OffboardControlMode()
        mode.timestamp = stamp
        mode.position = True
        mode.velocity = False
        mode.acceleration = False
        mode.attitude = False
        mode.body_rate = False
        self._mode_pub.publish(mode)

        setpoint = TrajectorySetpoint()
        setpoint.timestamp = stamp
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
