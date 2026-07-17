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
Minimal ROS 2 "client" node.

Subscribes to the std_msgs/msg/UInt32 topic 'counter', then calls the
std_srvs/srv/Trigger service 'trigger'. Once it has received the service
response and three messages on 'counter', it shuts down cleanly.

Usage (no package installation required):
    python3 trigger_client_node.py
"""

import rclpy
from rclpy.node import Node
from std_msgs.msg import UInt32
from std_srvs.srv import Trigger

EXPECTED_MESSAGES = 3


class TriggerClientNode(Node):
    """Calls the Trigger service and exits after receiving 3 counter messages."""

    def __init__(self) -> None:
        super().__init__("trigger_client_node")

        self._received = 0
        self._response_ok = False

        self._subscription = self.create_subscription(
            UInt32, "counter", self._on_counter, 10
        )
        self._client = self.create_client(Trigger, "trigger")

        self.get_logger().info("Waiting for the 'trigger' service...")
        while not self._client.wait_for_service(timeout_sec=1.0):
            self.get_logger().info("still waiting for the 'trigger' service...")

        self.get_logger().info("Calling the 'trigger' service.")
        future = self._client.call_async(Trigger.Request())
        future.add_done_callback(self._on_response)

    def _on_response(self, future) -> None:
        response = future.result()
        self._response_ok = response.success
        self.get_logger().info(
            f"Trigger response: success={response.success} message='{response.message}'"
        )
        self._maybe_shutdown()

    def _on_counter(self, msg: UInt32) -> None:
        self._received += 1
        self.get_logger().info(
            f"Received counter = {msg.data} ({self._received}/{EXPECTED_MESSAGES})"
        )
        self._maybe_shutdown()

    def _maybe_shutdown(self) -> None:
        if self._response_ok and self._received >= EXPECTED_MESSAGES:
            self.get_logger().info(
                "Got service response and 3 messages — shutting down."
            )
            raise SystemExit


def main() -> None:
    rclpy.init()
    node = TriggerClientNode()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
