#!/usr/bin/env python3
# Copyright (C) 2025 Frederik Pasch
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions
# and limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""
Standalone ROS 2 node that reads a 'test_parameter' from a YAML params file,
logs its value to rosout, then exits cleanly after 3 seconds.

Usage (no package installation required):
    ros2 run --  # not needed; run directly:
    python3 ros2_basic_node.py --ros-args --params-file params.yaml
"""

import rclpy
from rclpy.node import Node


class BasicNode(Node):
    """A minimal ROS 2 node that reads one parameter, logs it, and shuts down."""

    def __init__(self) -> None:
        super().__init__("basic_node")

        # Declare the parameter with a default fallback value so the node
        # still works when no params file is supplied.
        self.declare_parameter("test_parameter", "default_value")

        value = self.get_parameter("test_parameter").get_parameter_value().string_value
        self.get_logger().info(f"test_parameter = '{value}'")

        # Timer fires once after 3 seconds and triggers a clean shutdown.
        self._shutdown_timer = self.create_timer(3.0, self._shutdown_callback)
        self.get_logger().info("Node started — will exit in 3 seconds.")

    def _shutdown_callback(self) -> None:
        self._shutdown_timer.cancel()
        self.get_logger().info("3 seconds elapsed — shutting down.")
        raise SystemExit


def main() -> None:
    rclpy.init()
    node = BasicNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
