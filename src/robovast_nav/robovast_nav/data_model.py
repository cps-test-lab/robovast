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

    @classmethod
    def from_any(cls, value) -> "Pose":
        """A pose however it arrived: already a :class:`Pose`, or the mapping YAML gives.

        One config key carries both. A campaign stating a pose in its ``parameters:`` block gets
        the mapping through untouched, while a variation that produced the pose writes this
        class -- so whoever reads the key second sees whichever the campaign happened to use.
        Coercing here rather than in each reader is what lets a consumer stop caring.

        ``orientation`` is optional and defaults to zero yaw: a 2-D placement states where, and
        a campaign that does not care which way the robot faces should not have to write it.

        .. code-block:: python

            Pose.from_any({'position': {'x': 1.0, 'y': 2.0}})
            Pose.from_any({'position': {'x': 1.0, 'y': 2.0}, 'orientation': {'yaw': 0.5}})
        """
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict) or 'position' not in value:
            raise ValueError(
                f"not a pose: {value!r}. Expected a mapping with a 'position' of 'x' and 'y', "
                "and optionally an 'orientation' of 'yaw'")
        position = value['position']
        orientation = value.get('orientation') or {}
        try:
            return cls(
                position=Position(x=float(position['x']), y=float(position['y'])),
                orientation=Orientation(yaw=float(orientation.get('yaw', 0.0))),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"not a pose: {value!r} ({exc})") from exc


@dataclass
class StaticObject:
    """Represents a static object with name, model, pose, and optional xacro arguments."""

    entity_name: str
    model: str
    spawn_pose: Pose
    xacro_arguments: str = ""
