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
Obstacle placement module for generating obstacle positions near navigation paths.
"""

import logging
import math
import random
from typing import List

import numpy as np

from .data_model import Orientation, Pose, Position, StaticObject
from .map_loader import load_map

#: Shapes :func:`footprint_radius` knows. A shape absent from here has no radius, so a caller
#: gets ``None`` and falls back to the robot-derived floor rather than a made-up number.
_FOOTPRINT_RADIUS = {
    # Half the diagonal of the footprint rectangle: obstacles are placed at a RANDOM yaw, so the
    # circle that contains the box at every yaw is the only separation an unrotated half-extent
    # would not cover.
    'box': lambda size: math.hypot(size[0] / 2.0, size[1] / 2.0),
}


def footprint_radius(shape: str, size) -> float | None:
    """The radius of the disc that contains this obstacle's footprint at any yaw.

    Two obstacles whose centres are closer than the sum of their radii INTERSECT. That is the
    separation an obstacle population needs, and it is a fact about the obstacles -- not about the
    robot that has to drive between them, which is what the placer used to test against.

    ``None`` when the campaign declared no extents, or a shape this does not know: the placer then
    keeps its robot-derived floor, because a placement rule invented from no geometry would be a
    worse answer than the one that was already there.
    """
    if not size or len(size) < 2:
        return None
    fn = _FOOTPRINT_RADIUS.get(shape)
    return None if fn is None else float(fn(size))

logger = logging.getLogger(__name__)


class ObstaclePlacer:
    """Class for placing obstacles near navigation paths."""

    #: Gap left between two obstacles' footprint circles, beyond merely not intersecting. Touching
    #: is not a placement anyone means, and it leaves the run's geometry on the wrong side of every
    #: rounding: a lidar reads two obstacles in contact as one, and a planner's inflation closes the
    #: seam. Small on purpose -- it is a tie-break, not a clearance rule.
    OBSTACLE_MARGIN_M = 0.05

    def place_obstacles(
        self,
        path: List[Position],
        max_distance: float,
        amount: int,
        model: str,
        xacro_arguments: str = "",
        robot_diameter: float = 0.354,
        waypoints: List[Pose] = None,
        min_arc_length: float = 0.0,
        entity_prefix: str = "obstacle",
        obstacle_radius: float = None,
        keepout: List[tuple] = None,
    ) -> List[tuple]:
        """Place obstacles near a navigation path as StaticObject instances.

        Args:
            path: List of positions defining the navigation path
            max_distance: Maximum distance from path for obstacle placement (in meters)
            amount: Number of obstacles to place
            model: Name of the obstacle model to use
            xacro_arguments: Optional xacro arguments string for the model
            robot_diameter: Diameter of the robot in meters (default: 0.354m for TurtleBot4)
            waypoints: List of Pose objects to avoid placing obstacles near (e.g., start/goal poses)
            min_arc_length: Minimum arc-length from the path start before obstacles can be placed.
                Segments before this distance are excluded from sampling.
            entity_prefix: Stem of the generated entity names (``<prefix>_<i>``). A campaign
                placing more than one population needs distinct stems: the names travel to a
                simulator that COMPILES the placement, where two populations both called
                ``obstacle_0`` are a duplicate-name model-compilation failure.
            obstacle_radius: Footprint radius of what is being placed
                (:func:`footprint_radius`), so two obstacles are separated by their own extents
                rather than by a number derived from the robot. ``None`` keeps the robot-derived
                floor, which is all a campaign declaring no ``size`` supports.
            keepout: ``(Position, radius_or_None)`` per obstacle ALREADY placed for this
                configuration -- including by an earlier variation. Distinct populations are
                placed by separate calls near the SAME path, so without this each one is blind to
                the others and can put its obstacle inside one of theirs.

        Returns:
            List of (StaticObject, path_point) tuples where path_point is the
            anchor position on the path from which the obstacle was offset.
        """
        if not path or len(path) < 2:
            return []

        # Trim path to start at min_arc_length
        effective_path = self._trim_path_to_arc_length(path, min_arc_length)
        if not effective_path or len(effective_path) < 2:
            return []

        obstacle_objects: List[tuple] = []  # List of (StaticObject, path_point)
        # Define minimum clearance around waypoints (robot diameter + safety
        # margin)
        waypoint_clearance = robot_diameter * 2.0  # 2x robot diameter for safety
        if waypoints is None:
            waypoint_positions: List[Position] = []
        else:
            waypoint_positions = [pose.position for pose in waypoints]
        # Calculate path segments and their lengths
        path_segments = []
        total_length = 0.0

        for i in range(len(effective_path) - 1):
            start = effective_path[i]
            end = effective_path[i + 1]
            length = self._distance(start, end)
            path_segments.append({"start": start, "end": end, "length": length})
            total_length += length
        # Place obstacles with collision avoidance
        max_attempts = amount * 100  # Allow multiple attempts per obstacle
        attempts = 0

        while len(obstacle_objects) < amount and attempts < max_attempts:
            logger.debug(
                "Attempting to place obstacle: %d/%d, try %d/%d",
                len(obstacle_objects) + 1, amount, attempts + 1, max_attempts)
            attempts += 1
            # Select a random segment based on length (longer segments get more
            # obstacles)
            segment = self._select_random_segment(path_segments, total_length)
            # Find a random point along the segment
            t = random.random()  # Random parameter between 0 and 1
            path_point = Position(
                x=segment["start"].x + t * (segment["end"].x - segment["start"].x),
                y=segment["start"].y + t * (segment["end"].y - segment["start"].y),
            )
            # Generate obstacle position near the path point
            obstacle_pos = self._generate_obstacle_position(
                path_point, segment["start"], segment["end"], max_distance
            )
            # Check if obstacle is too close to waypoints, to what this call has already placed,
            # or to what an earlier variation placed near the same path.
            existing_circles = list(keepout or []) + [
                (obj.spawn_pose.position, obstacle_radius) for obj, _ in obstacle_objects
            ]
            if self._is_valid_obstacle_position(
                obstacle_pos,
                waypoint_positions,
                waypoint_clearance,
                existing_circles,
                robot_diameter,
                obstacle_radius,
            ):
                # Generate random yaw angle (rotation) for the obstacle
                yaw = random.uniform(
                    -math.pi, math.pi
                )  # Random rotation from -180° to +180°
                name = f"{entity_prefix}_{len(obstacle_objects)}"

                obstacle = StaticObject(
                    entity_name=name,
                    model=model,
                    spawn_pose=Pose(position=obstacle_pos, orientation=Orientation(yaw=yaw)),
                    xacro_arguments=xacro_arguments,
                )

                obstacle_objects.append((obstacle, path_point))
        return obstacle_objects

    def place_obstacles_random(
        self,
        map_file,
        amount: int,
        model: str,
        xacro_arguments: str = "",
        robot_diameter: float = 0.354,
        waypoints: List[Pose] = None,
        entity_prefix: str = "obstacle",
        obstacle_radius: float = None,
        keepout: List[tuple] = None,
    ) -> List[StaticObject]:
        """Place obstacles randomly on the map as StaticObject instances.

        Args:
            map_file: Path to the map YAML file
            amount: Number of obstacles to place
            model: Name of the obstacle model to use
            xacro_arguments: Optional xacro arguments string for the model
            robot_diameter: Diameter of the robot in meters (default: 0.354m for TurtleBot4)
            waypoints: List of Pose objects to avoid placing obstacles near (e.g., start/goal poses)
            obstacle_radius: Footprint radius of what is being placed, so two obstacles are
                separated by their own extents (:meth:`_is_valid_obstacle_position`)
            keepout: ``(Position, radius_or_None)`` per obstacle already placed for this
                configuration, including by an earlier variation

        Returns:
            List of StaticObject instances for obstacles
        """

        # Load map using map_loader
        map_obj = load_map(map_file)

        # Find free space using the map's occupancy grid
        # Invert occupancy_grid (True = occupied) to get free space (True = free)
        free_space_mask = ~map_obj.occupancy_grid

        # Get coordinates of free space (y, x format from numpy)
        free_coords = np.argwhere(free_space_mask)

        if len(free_coords) == 0:
            return []

        obstacle_objects: List[StaticObject] = []
        waypoint_clearance = robot_diameter * 2.0  # 2x robot diameter for safety

        if waypoints is None:
            waypoint_positions: List[Position] = []
        else:
            waypoint_positions = [pose.position for pose in waypoints]

        # Place obstacles with collision avoidance
        max_attempts = amount * 1000  # Allow multiple attempts per obstacle
        attempts = 0

        while len(obstacle_objects) < amount and attempts < max_attempts:
            logger.debug(
                "Attempting to place obstacle: %d/%d, try %d/%d",
                len(obstacle_objects) + 1, amount, attempts + 1, max_attempts)
            attempts += 1

            # Select random free space coordinate
            random_idx = np.random.randint(0, len(free_coords))
            grid_y, grid_x = free_coords[random_idx]

            # Convert grid coordinates to world coordinates using map_loader's conversion
            world_x, world_y = map_obj.grid_to_world(grid_x, grid_y)
            obstacle_pos = Position(x=world_x, y=world_y)

            # Check if obstacle position is valid
            if self._is_valid_obstacle_position(
                obstacle_pos,
                waypoint_positions,
                waypoint_clearance,
                list(keepout or [])
                + [(obj.spawn_pose.position, obstacle_radius) for obj in obstacle_objects],
                robot_diameter,
                obstacle_radius,
            ):
                # Generate random yaw angle (rotation) for the obstacle
                yaw = np.random.uniform(-math.pi, math.pi)  # Random rotation from -180° to +180°
                name = f"{entity_prefix}_{len(obstacle_objects)}"

                obstacle = StaticObject(
                    entity_name=name,
                    model=model,
                    spawn_pose=Pose(position=obstacle_pos, orientation=Orientation(yaw=yaw)),
                    xacro_arguments=xacro_arguments,
                )

                obstacle_objects.append(obstacle)

        return obstacle_objects

    def _trim_path_to_arc_length(self, path: List[Position], min_arc_length: float) -> List[Position]:
        """Return the sub-path starting at the given arc-length from the path start.

        If min_arc_length is 0 or negative, returns the original path unchanged.
        The first point of the returned path is interpolated exactly at min_arc_length.
        """
        if min_arc_length <= 0.0:
            return path
        cum = 0.0
        for i in range(len(path) - 1):
            seg_len = self._distance(path[i], path[i + 1])
            if cum + seg_len >= min_arc_length:
                t = (min_arc_length - cum) / seg_len if seg_len > 0 else 0.0
                trim_point = Position(
                    x=path[i].x + t * (path[i + 1].x - path[i].x),
                    y=path[i].y + t * (path[i + 1].y - path[i].y),
                )
                return [trim_point] + list(path[i + 1:])
            cum += seg_len
        # min_arc_length exceeds total path length
        return []

    def _distance(self, p1: Position, p2: Position) -> float:
        """Calculate Euclidean distance between two positions."""
        return math.sqrt((p2.x - p1.x) ** 2 + (p2.y - p1.y) ** 2)

    def _select_random_segment(self, segments: List[dict], total_length: float) -> dict:
        """Select a random segment weighted by length."""
        if not segments:
            return segments[0]

        # Generate random value between 0 and total_length
        random_length = random.random() * total_length

        # Find the segment corresponding to this length
        current_length = 0.0
        for segment in segments:
            current_length += segment["length"]
            if random_length <= current_length:
                return segment

        # Fallback to last segment
        return segments[-1]

    def _generate_obstacle_position(
        self, path_point: Position, start: Position, end: Position, max_distance: float
    ) -> Position:
        """Generate obstacle position near a path point.

        Args:
            path_point: Point on the path
            start: Start of the path segment
            end: End of the path segment
            max_distance: Maximum distance from path

        Returns:
            Obstacle position
        """
        # Calculate path direction vector
        path_dx = end.x - start.x
        path_dy = end.y - start.y
        path_length = math.sqrt(path_dx**2 + path_dy**2)

        if path_length == 0:
            # Degenerate case - place obstacle randomly around point
            angle = random.random() * 2 * math.pi
            distance = random.random() * max_distance
            return Position(
                x=path_point.x + distance * math.cos(angle),
                y=path_point.y + distance * math.sin(angle),
            )

        # Normalize path direction
        path_dx /= path_length
        path_dy /= path_length

        # Calculate perpendicular direction (normal to path)
        normal_dx = -path_dy
        normal_dy = path_dx

        # Choose random side (left or right of path)
        side = random.choice([-1, 1])

        # Choose random distance from path
        distance = random.random() * max_distance

        # Add some randomness along the path direction as well
        along_path_offset = (random.random() - 0.5) * min(
            max_distance, path_length * 0.3
        )

        # Calculate obstacle position
        obstacle_x = (
            path_point.x + side * distance * normal_dx + along_path_offset * path_dx
        )
        obstacle_y = (
            path_point.y + side * distance * normal_dy + along_path_offset * path_dy
        )

        return Position(x=obstacle_x, y=obstacle_y)

    def validate_obstacle_placement(
        self, obstacles: List[Position], min_obstacle_distance: float = 0.5
    ) -> List[Position]:
        """Validate and filter obstacle positions to avoid overlaps.

        Args:
            obstacles: List of obstacle positions
            min_obstacle_distance: Minimum distance between obstacles

        Returns:
            Filtered list of obstacle positions
        """
        if not obstacles:
            return obstacles

        validated_obstacles = [obstacles[0]]  # Always keep the first obstacle

        for obstacle in obstacles[1:]:
            # Check if this obstacle is too close to any existing obstacle
            is_valid = True
            for existing in validated_obstacles:
                if self._distance(obstacle, existing) < min_obstacle_distance:
                    is_valid = False
                    break

            if is_valid:
                validated_obstacles.append(obstacle)

        return validated_obstacles

    def _is_valid_obstacle_position(
        self,
        obstacle_pos: Position,
        waypoints: List[Position],
        waypoint_clearance: float,
        existing_obstacles: List[tuple],
        robot_diameter: float,
        obstacle_radius: float = None,
    ) -> bool:
        """Is this a position the obstacle can go, given the waypoints and what is already placed?

        Two separations, answering two different questions.

        *waypoint_clearance* keeps an obstacle off the start and the goal -- a trial that begins or
        ends inside one measures nothing.

        The obstacle-to-obstacle separation keeps two obstacles from INTERSECTING, which is a fact
        about their extents: ``r_a + r_b`` is where they touch. ``robot_diameter * 1.5`` remains
        the floor, so a population the robot cannot pass between is still refused and a campaign
        that declared no ``size`` behaves exactly as before -- but it is a floor, not the rule.
        Using it AS the rule is how a 0.5 m box population came to be placed 0.1 m apart: the
        number is derived from the robot, and says nothing about how big the obstacles are.

        Args:
            obstacle_pos: Position to validate
            waypoints: List of waypoint positions to avoid
            waypoint_clearance: Minimum distance from waypoints
            existing_obstacles: ``(Position, radius_or_None)`` per obstacle already placed --
                including ones placed by an earlier variation, which is why this takes circles
                rather than reading them back off this call's own results
            robot_diameter: Diameter of the robot
            obstacle_radius: Footprint radius of the obstacle being placed, or ``None`` when the
                campaign declared no extents

        Returns:
            True if position is valid, False otherwise
        """
        # Check distance from waypoints
        for waypoint in waypoints:
            if self._distance(obstacle_pos, waypoint) < waypoint_clearance:
                return False

        # Check distance from existing obstacles (prevent overlap)
        floor = robot_diameter * 1.5  # the robot still has to get between them
        for existing, existing_radius in existing_obstacles:
            required = floor
            if obstacle_radius is not None and existing_radius is not None:
                required = max(floor, obstacle_radius + existing_radius + self.OBSTACLE_MARGIN_M)
            if self._distance(obstacle_pos, existing) < required:
                return False

        return True
