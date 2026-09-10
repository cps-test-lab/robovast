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
from dataclasses import dataclass
from typing import List

import numpy as np

from .data_model import Orientation, Pose, Position, StaticObject
from .map_loader import load_map

logger = logging.getLogger(__name__)

#: Shapes :func:`footprint_of` knows, as a function from ``size`` to plan-view half-extents.
#: A shape absent from here has no footprint, so a caller gets ``None`` and falls back to the
#: robot-derived floor rather than a made-up outline.
_HALF_EXTENTS = {
    'box': lambda size: (size[0] / 2.0, size[1] / 2.0),
}


@dataclass(frozen=True)
class Footprint:
    """An obstacle's plan-view outline AT A POSE: the thing that actually has to fit.

    A rectangle at a yaw, not the circle around it. The circle is easy and wrong in the direction
    that costs the most: two 0.5 m boxes side by side occupy 0.5 m of corridor, while the circles
    containing them demand 0.71 m. In a corridor that is the difference between a layout the
    campaign asked for and one the placer could not find, and every rejected sample is a
    configuration the search does not get to try.

    The pose is part of the footprint because the yaw is part of the answer: the same two boxes
    are 0.5 m apart when aligned and 0.71 m apart corner-to-corner, and only a posed outline can
    tell those apart.
    """

    x: float
    y: float
    yaw: float
    half_x: float
    half_y: float

    @property
    def radius(self) -> float:
        """The circle that contains it. Only a cheap pre-filter now, never the rule."""
        return math.hypot(self.half_x, self.half_y)

    def _axes(self):
        """The rectangle's own two unit axes: the only directions that can separate rectangles."""
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        return (c, s), (-s, c)

    def corners(self, grow: float = 0.0):
        """The four corners, optionally with the outline grown by *grow* on every side."""
        (ux, uy), (vx, vy) = self._axes()
        hx, hy = self.half_x + grow, self.half_y + grow
        return [
            (self.x + sx * hx * ux + sy * hy * vx, self.y + sx * hx * uy + sy * hy * vy)
            for sx, sy in ((1, 1), (1, -1), (-1, -1), (-1, 1))
        ]

    def clearance_to(self, point: Position) -> float:
        """How much room is left between *point* and this outline. ``0.0`` when inside it.

        The distance to the RECTANGLE, not to its centre. A centre distance answers a question
        about where the obstacle was placed; what a waypoint needs to know is how much floor is
        left beside the obstacle, and those differ by the extents -- by half a metre for a wide
        one, which is the whole margin a start pose has.
        """
        (ux, uy), (vx, vy) = self._axes()
        dx, dy = point.x - self.x, point.y - self.y
        # In the rectangle's own frame, the nearest point on it is the offset clamped to the
        # half-extents, so what is left over on each axis is the gap.
        gap_u = max(abs(dx * ux + dy * uy) - self.half_x, 0.0)
        gap_v = max(abs(dx * vx + dy * vy) - self.half_y, 0.0)
        return math.hypot(gap_u, gap_v)

    def overlaps(self, other: "Footprint", margin: float = 0.0) -> bool:
        """Do the two outlines touch, with *margin* of clear space required between them?

        Separating-axis test. Two convex shapes are apart exactly when some axis separates their
        projections, and for rectangles only the four edge normals can be that axis -- so this is
        exact, not a bound. The margin is applied by growing each outline by half of it, which is
        slightly strict at a corner and simple everywhere.
        """
        grow = margin / 2.0
        mine, theirs = self.corners(grow), other.corners(grow)
        for axis in (*self._axes(), *other._axes()):
            a = [px * axis[0] + py * axis[1] for px, py in mine]
            b = [px * axis[0] + py * axis[1] for px, py in theirs]
            # `<=`: outlines that exactly abut are separated, not overlapping. The gap between
            # them is the margin's job to enforce, and leaving the boundary to this test would
            # decide a placement on the last bit of a float.
            if max(a) <= min(b) or max(b) <= min(a):
                return False  # this axis separates them, so they do not overlap
        return True

    def fits(self, map_obj) -> bool:
        """Does this outline stand entirely in free space on *map_obj*?

        The other half of "an obstacle goes where it fits". Separation from the obstacles a
        campaign placed is not enough on its own: the path a placement is offset from was planned
        for the ROBOT's radius, so a wider obstacle pushed far enough sideways reaches into a wall
        that nothing else here would notice.

        Occupancy is the same grid the planner uses, so "free" means what it means to the stack
        under test. **Off the map counts as not fitting**: an obstacle outside the surveyed area
        is not known to be clear, and reading unknown as free is how a placement ends up somewhere
        the map cannot vouch for.
        """
        if map_obj is None:
            return True

        res = map_obj.resolution
        xs = [c[0] for c in self.corners()]
        ys = [c[1] for c in self.corners()]
        # Grid indices spanned by the outline's bounding box, via the map's own conversion so the
        # y-flip is applied in exactly one place.
        gx0, gy0 = map_obj.world_to_grid(min(xs), max(ys))
        gx1, gy1 = map_obj.world_to_grid(max(xs), min(ys))
        if not (0 <= gx0 and gx1 < map_obj.width and 0 <= gy0 and gy1 < map_obj.height):
            return False

        window = map_obj.occupancy_grid[gy0 : gy1 + 1, gx0 : gx1 + 1]
        if not window.any():
            return True  # nothing occupied anywhere near it

        # Which of those cells the outline actually covers. A cell is a square of side `res`, so
        # testing its CENTRE against the outline grown by the cell's own reach along each of the
        # outline's axes counts every cell the rectangle clips, not only those it swallows.
        rows, cols = np.nonzero(window)
        world = [map_obj.grid_to_world(int(gx0 + c), int(gy0 + r)) for r, c in zip(rows, cols)]
        (ux, uy), (vx, vy) = self._axes()
        reach = (res / 2.0) * (abs(math.cos(self.yaw)) + abs(math.sin(self.yaw)))
        for wx, wy in world:
            dx, dy = wx - self.x, wy - self.y
            if (abs(dx * ux + dy * uy) <= self.half_x + reach
                    and abs(dx * vx + dy * vy) <= self.half_y + reach):
                return False
        return True


def footprint_of(shape: str, size, position: Position, yaw: float = 0.0):
    """The posed outline of an obstacle, or ``None`` when the campaign declared no extents.

    ``None`` is not a failure: ``size`` is optional, and a placement rule invented from no
    geometry would be a worse answer than the robot-derived floor that was already there.
    """
    if not size or len(size) < 2:
        return None
    half = _HALF_EXTENTS.get(shape)
    if half is None:
        return None
    half_x, half_y = half(size)
    return Footprint(x=position.x, y=position.y, yaw=float(yaw),
                     half_x=float(half_x), half_y=float(half_y))


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
        shape: str = 'box',
        size=None,
        keepout: List = None,
        map_obj=None,
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
            shape: What is being placed, for its outline (:func:`footprint_of`).
            size: Its extents in meters, so obstacles are separated by their real outlines at the
                yaw they are placed at rather than by a number derived from the robot. ``None``
                keeps the robot-derived floor, which is all a campaign declaring no size supports.
            keepout: Per obstacle ALREADY placed for this configuration -- including by an
                earlier variation -- a :class:`Footprint`, or a ``Position`` where no extents
                were declared. Distinct populations are placed by separate calls near the SAME
                path, so without this each is blind to the others and can put its obstacle
                inside one of theirs.
            map_obj: The world's occupancy, so an obstacle is placed only where its outline fits
                (:meth:`Footprint.fits`). ``None`` skips that check.

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
        # The outlines of what this call has placed, kept beside the results rather than rebuilt
        # from them each attempt: the return shape is the caller's contract and stays as it was.
        placed_footprints: List = []
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
            # The yaw is drawn BEFORE the check, because it is part of what is being checked:
            # two boxes are 0.5 m apart aligned and 0.71 m apart corner-to-corner, so a validity
            # test run before the rotation is known can only answer for the worst case.
            yaw = random.uniform(-math.pi, math.pi)  # Random rotation from -180° to +180°
            footprint = footprint_of(shape, size, obstacle_pos, yaw)

            # Everything already standing in this configuration: what an earlier variation
            # placed near the same path, plus what this call has placed so far.
            standing = list(keepout or []) + placed_footprints
            if self._is_valid_obstacle_position(
                obstacle_pos,
                waypoint_positions,
                waypoint_clearance,
                standing,
                robot_diameter,
                footprint,
                map_obj,
            ):
                name = f"{entity_prefix}_{len(obstacle_objects)}"

                obstacle = StaticObject(
                    entity_name=name,
                    model=model,
                    spawn_pose=Pose(position=obstacle_pos, orientation=Orientation(yaw=yaw)),
                    xacro_arguments=xacro_arguments,
                )

                obstacle_objects.append((obstacle, path_point))
                placed_footprints.append(footprint if footprint is not None else obstacle_pos)
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
        shape: str = 'box',
        size=None,
        keepout: List = None,
    ) -> List[StaticObject]:
        """Place obstacles randomly on the map as StaticObject instances.

        Args:
            map_file: Path to the map YAML file
            amount: Number of obstacles to place
            model: Name of the obstacle model to use
            xacro_arguments: Optional xacro arguments string for the model
            robot_diameter: Diameter of the robot in meters (default: 0.354m for TurtleBot4)
            waypoints: List of Pose objects to avoid placing obstacles near (e.g., start/goal poses)
            shape: What is being placed, for its outline (:func:`footprint_of`)
            size: Its extents in meters, so obstacles are separated by their real outlines at the
                yaw they are placed at (:meth:`_is_valid_obstacle_position`)
            keepout: Per obstacle already placed for this configuration, including by an earlier
                variation: a :class:`Footprint`, or a ``Position`` where no extents were declared

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
        placed_footprints: List = []
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
            # Drawn before the check, because the yaw is part of what is being checked.
            yaw = np.random.uniform(-math.pi, math.pi)  # Random rotation from -180° to +180°
            footprint = footprint_of(shape, size, obstacle_pos, yaw)

            if self._is_valid_obstacle_position(
                obstacle_pos,
                waypoint_positions,
                waypoint_clearance,
                list(keepout or []) + placed_footprints,
                robot_diameter,
                footprint,
                # It sampled a FREE CELL, which says the obstacle's centre is clear and nothing
                # about its extents; the outline still has to fit around that centre.
                map_obj,
            ):
                name = f"{entity_prefix}_{len(obstacle_objects)}"

                obstacle = StaticObject(
                    entity_name=name,
                    model=model,
                    spawn_pose=Pose(position=obstacle_pos, orientation=Orientation(yaw=yaw)),
                    xacro_arguments=xacro_arguments,
                )

                obstacle_objects.append(obstacle)
                placed_footprints.append(footprint if footprint is not None else obstacle_pos)

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
        existing_obstacles: List,
        robot_diameter: float,
        footprint=None,
        map_obj=None,
    ) -> bool:
        """Can the obstacle go here, given the world, the waypoints and what already stands?

        Three constraints, answering three different questions.

        *map_obj* is the world: the outline must stand in free space. The path a placement is
        offset from was planned for the ROBOT's radius, so a wider obstacle pushed far enough
        sideways reaches into a wall nothing else here would notice.

        *waypoint_clearance* keeps an obstacle off the start and the goal -- a trial that begins
        or ends inside one measures nothing. Measured from the obstacle's OUTLINE where there is
        one: as a centre distance it ignores the extents, so an obstacle wide enough to cover the
        start pose passes it, and the trial then fails on a collision before it has begun.

        Obstacle against obstacle is the two OUTLINES not touching, with a small margin. Not
        centre distance and not their circles: two boxes side by side occupy the width of two
        boxes, while the circles containing them demand forty percent more, and refusing that
        placement costs the search a configuration it was asked to try.

        ``robot_diameter * 1.5`` between centres remains the rule only where there is no outline
        to use -- a campaign that declared no ``size`` -- so such a campaign behaves exactly as
        it always has. It is not applied on top of the outline test: whether the robot can get
        between two obstacles is decided by the navigability check the caller already runs with
        the whole population in place, which answers it about the real layout rather than by
        proxy.

        Args:
            obstacle_pos: Position to validate
            waypoints: List of waypoint positions to avoid
            waypoint_clearance: Minimum distance from waypoints
            existing_obstacles: Per obstacle already placed, a :class:`Footprint` or, where the
                campaign declared no extents, a ``Position``
            robot_diameter: Diameter of the robot
            footprint: The posed outline being placed, or ``None`` when no extents were declared
            map_obj: Occupancy the outline must stand clear of, or ``None`` not to check it

        Returns:
            True if position is valid, False otherwise
        """
        # Cheapest first, and the one a corridor rejects most often.
        if footprint is not None and not footprint.fits(map_obj):
            return False

        for waypoint in waypoints:
            if footprint is not None:
                # The room left BESIDE the obstacle, which is what a robot at the waypoint needs.
                # A centre distance says where the obstacle was PUT and nothing about how far it
                # reaches, so a wide one swallows a start pose while passing that test.
                #
                # Half the centre-distance rule, because that rule was two robot diameters between
                # centres and this one is measured from the outline: one whole diameter of floor,
                # which is the robot standing there plus its own width again to turn and leave.
                if footprint.clearance_to(waypoint) < waypoint_clearance / 2.0:
                    return False
            elif self._distance(obstacle_pos, waypoint) < waypoint_clearance:
                return False

        floor = robot_diameter * 1.5
        for existing in existing_obstacles:
            if isinstance(existing, Footprint) and footprint is not None:
                if footprint.overlaps(existing, self.OBSTACLE_MARGIN_M):
                    return False
                continue
            # One side or the other has no outline: fall back to centres, which is all that can
            # be compared, and to the separation that was the rule before outlines existed.
            other = existing if isinstance(existing, Position) else Position(
                x=existing.x, y=existing.y)
            if self._distance(obstacle_pos, other) < floor:
                return False

        return True
