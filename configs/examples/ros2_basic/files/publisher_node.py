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
Minimal ROS 2 "server" node.

Advertises a std_srvs/srv/Trigger service on 'trigger'. When triggered it
answers the service request immediately, then publishes a std_msgs/msg/UInt32
counter on 'counter' three times and shuts down cleanly.

Usage (no package installation required):
    python3 publisher_node.py
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import UInt32
from std_srvs.srv import Trigger

PUBLISH_COUNT = 3
PUBLISH_PERIOD = 0.5  # seconds between publications


class PublisherNode(Node):
    """Answers a Trigger service, then publishes a UInt32 counter 3 times."""

    def __init__(self) -> None:
        super().__init__("publisher_node")

        self._publisher = self.create_publisher(UInt32, "counter", 10)
        self._service = self.create_service(Trigger, "trigger", self._on_trigger)

        self._count = 0
        self._timer = None
        self.get_logger().info("Ready — waiting for a call on the 'trigger' service.")

    def _on_trigger(self, request, response):
        """Answer the service call, then start publishing the counter topic."""
        self.get_logger().info("Trigger received — starting to publish.")
        response.success = True
        response.message = "publishing started"

        # Start publishing only after the response is returned to the caller.
        if self._timer is None:
            self._timer = self.create_timer(PUBLISH_PERIOD, self._publish_once)
        return response

    def _publish_once(self) -> None:
        msg = UInt32()
        msg.data = self._count
        self._publisher.publish(msg)
        self.get_logger().info(f"Published counter = {self._count}")
        self._count += 1

        if self._count >= PUBLISH_COUNT:
            self._timer.cancel()
            self.get_logger().info(
                f"Published {PUBLISH_COUNT} messages — shutting down."
            )
            raise SystemExit


def main() -> None:
    rclpy.init()
    node = PublisherNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
