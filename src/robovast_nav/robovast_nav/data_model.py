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

import math
from dataclasses import dataclass


@dataclass
class Position:
    """Represents a 2D position with x and y coordinates."""

    x: float
    y: float

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Position):
            return NotImplemented
        return math.isclose(self.x, other.x) and math.isclose(self.y, other.y)


@dataclass
class Orientation:
    """Represents an orientation in radians."""

    yaw: float

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Orientation):
            return NotImplemented
        return math.isclose(self.yaw, other.yaw)


@dataclass
class Pose:
    """Represents a pose with position and orientation."""

    position: Position
    orientation: Orientation

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Pose):
            return NotImplemented
        return self.position == other.position and self.orientation == other.orientation


@dataclass
class StaticObject:
    """Represents a static object with name, model, pose, and optional xacro arguments."""

    entity_name: str
    model: str
    spawn_pose: Pose
    xacro_arguments: str = ""
