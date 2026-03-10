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

"""ObstacleVariationWithDistanceTrigger — obstacle placement with a computed spawn trigger point.

Obstacles are placed at path positions at least *trigger_distance* arc-length ahead of the
robot's start.  A *spawn_trigger_point* scenario parameter is also computed: the position on
the planned path that is exactly *trigger_distance* before the earliest placed obstacle.
When the robot reaches that point it can trigger the dynamic spawning of the obstacles.

trigger_distance can be a single float or a list of floats.  When a list is provided, one
output configuration is produced per value (multiplied with the normal count/in_configs fan-out).
"""

import copy
import math
import random
from typing import List, Optional, Union

import numpy as np
from pydantic import ConfigDict, field_validator

from ..data_model import Orientation, Pose, Position
from .obstacle_variation import ObstacleVariation, ObstacleVariationConfig, ObstacleVariationGuiRenderer


# ---------------------------------------------------------------------------
# Path geometry helpers
# ---------------------------------------------------------------------------

def _arc_length_of_point(path: List[Position], point: Position) -> float:
    """Return the arc-length from path start of the projection of *point* onto *path*."""
    best_arc = 0.0
    best_dist = float('inf')
    cum = 0.0
    for i in range(1, len(path)):
        dx = path[i].x - path[i - 1].x
        dy = path[i].y - path[i - 1].y
        seg = math.hypot(dx, dy)
        if seg > 0:
            t = max(0.0, min(1.0, (
                (point.x - path[i - 1].x) * dx + (point.y - path[i - 1].y) * dy
            ) / (seg * seg)))
        else:
            t = 0.0
        proj_x = path[i - 1].x + t * dx
        proj_y = path[i - 1].y + t * dy
        dist = math.hypot(point.x - proj_x, point.y - proj_y)
        if dist < best_dist:
            best_dist = dist
            best_arc = cum + t * seg
        cum += seg
    return best_arc


def _position_at_arc_length(path: List[Position], arc: float) -> Position:
    """Return the interpolated position on *path* at the given arc-length from its start."""
    arc = max(0.0, arc)
    cum = 0.0
    for i in range(1, len(path)):
        seg = math.hypot(path[i].x - path[i - 1].x, path[i].y - path[i - 1].y)
        if cum + seg >= arc:
            t = (arc - cum) / seg if seg > 0 else 0.0
            t = max(0.0, min(1.0, t))
            return Position(
                x=path[i - 1].x + t * (path[i].x - path[i - 1].x),
                y=path[i - 1].y + t * (path[i].y - path[i - 1].y),
            )
        cum += seg
    return path[-1]


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------

class ObstacleVariationWithDistanceTriggerConfig(ObstacleVariationConfig):
    """Configuration for ObstacleVariationWithDistanceTrigger.

    Inherits all fields from ObstacleVariationConfig and adds:
    - spawn_trigger_point: scenario parameter name to store the computed trigger position
    - trigger_distance:    arc-length (m) from start_pose before obstacles may be placed.
                           A single float or a list of floats (one config per value).
    - start_pose:          optional explicit start pose (dict); falls back to config['config']['start_pose']
    - goal_pose:           optional explicit goal pose (dict); falls back to config['config']['goal_pose']
    """

    model_config = ConfigDict(extra='forbid')

    spawn_trigger_point: str
    trigger_distance: Union[float, List[float]]
    start_pose: Optional[dict] = None
    goal_pose: Optional[dict] = None

    @field_validator('trigger_distance', mode='before')
    @classmethod
    def normalise_trigger_distance(cls, v):
        """Accept a single float or a list; always store as list[float]."""
        if isinstance(v, (int, float)):
            return [float(v)]
        return [float(x) for x in v]


# ---------------------------------------------------------------------------
# GUI renderer
# ---------------------------------------------------------------------------

class ObstacleVariationWithDistanceTriggerGuiRenderer(ObstacleVariationGuiRenderer):
    """Renders path, obstacles, and the spawn trigger point on the map."""

    def update_gui(self, config, path):
        # Draw path
        nav_path = config.get('_path', None)
        if nav_path:
            plain_path = [(p.x, p.y) for p in nav_path]
            self.gui_object.draw_path(plain_path,
                                      color='red', linewidth=2.0,
                                      alpha=0.8, label='Path',
                                      show_endpoints=True)

        # Draw obstacles via parent renderer
        super().update_gui(config, path)

        # Draw spawn trigger point
        tp = config.get('_spawn_trigger_point')
        if tp:
            x, y = tp['x'], tp['y']
            self.gui_object.draw_circle(x, y, radius=0.25,
                                        color='orange', alpha=0.9,
                                        label='Spawn Trigger Point')
            self.gui_object.map_visualizer.ax.annotate(
                'trigger', xy=(x, y), fontsize=7,
                color='orange', ha='center', va='bottom'
            )
            self.gui_object.canvas.draw()


# ---------------------------------------------------------------------------
# Variation class
# ---------------------------------------------------------------------------

class ObstacleVariationWithDistanceTrigger(ObstacleVariation):
    """Obstacle placement where obstacles are guaranteed to be at least *trigger_distance*
    arc-length ahead of the robot start, together with a computed *spawn_trigger_point*.

    The spawn_trigger_point is the path position exactly *trigger_distance* before the
    first (closest-to-start) placed obstacle.  It is written as a scenario parameter of
    type position_3d so that an OSC scenario can trigger dynamic spawning when the robot
    reaches that point.

    When *trigger_distance* is a list, one output configuration is produced per value.
    Seeds are offset by the list index for reproducibility across values.
    """

    CONFIG_CLASS = ObstacleVariationWithDistanceTriggerConfig
    GUI_RENDERER_CLASS = ObstacleVariationWithDistanceTriggerGuiRenderer

    def variation(self, in_configs):
        self.progress_update("Running ObstacleVariationWithDistanceTrigger...")
        results = []
        for config in in_configs:
            for idx, td in enumerate(self.parameters.trigger_distance):
                self._current_trigger_distance = td
                seed = self.parameters.seed + idx
                np.random.seed(seed)
                random.seed(seed)
                effective_config = self._inject_poses(config)
                for _ in range(self.parameters.count):
                    result = self._generate_obstacles_for_config(
                        self.base_path, effective_config, self.parameters.obstacle_configs
                    )
                    # Propagate spawn trigger point to a private key for GUI access
                    for r in result:
                        tp = r['config'].get(self.parameters.spawn_trigger_point)
                        if tp:
                            r['_spawn_trigger_point'] = tp
                    results.extend(result)
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _dict_to_pose(d) -> Pose:
        """Convert a pose dict (from YAML parameters) to a Pose dataclass.

        Accepts dicts of the form::

            {'position': {'x': 1.0, 'y': 2.0}}          # no orientation
            {'position': {'x': 1.0, 'y': 2.0},
             'orientation': {'yaw': 0.5}}                # with orientation

        If *d* is already a Pose instance it is returned unchanged.
        """
        if isinstance(d, Pose):
            return d
        pos = d['position']
        orientation_dict = d.get('orientation', {})
        return Pose(
            position=Position(x=float(pos['x']), y=float(pos['y'])),
            orientation=Orientation(yaw=float(orientation_dict.get('yaw', 0.0))),
        )

    def _inject_poses(self, config):
        """Return a deep copy of *config* with start_pose / goal_pose converted to
        Pose objects and injected into config['config']. Explicit variation parameters
        take priority; otherwise the existing config values are used (and converted if
        they are still in dict form from YAML parameters)."""
        effective = copy.deepcopy(config)

        # Determine source poses (variation parameters override config)
        raw_start = self.parameters.start_pose or effective['config'].get('start_pose')
        raw_goal = self.parameters.goal_pose or (
            effective['config'].get('goal_pose')
            or (effective['config'].get('goal_poses') or [None])[0]
        )

        if raw_start is not None:
            effective['config']['start_pose'] = self._dict_to_pose(raw_start)
        if raw_goal is not None:
            effective['config']['goal_pose'] = self._dict_to_pose(raw_goal)
            # Remove goal_poses so the base class derives it from goal_pose alone,
            # avoiding unknown parameter errors in the OSC scenario.
            effective['config'].pop('goal_poses', None)

        return effective

    # ------------------------------------------------------------------
    # Hooks (override ObstacleVariation base hooks)
    # ------------------------------------------------------------------

    def _min_arc_length_for_config(self, obstacle_config_index: int) -> float:
        """Keep all obstacles at least trigger_distance ahead on the path."""
        return self._current_trigger_distance

    def _post_process(self, obstacle_objects, obstacle_anchors, path) -> dict:
        """Compute and return the spawn_trigger_point scenario parameter.

        The trigger point is the path position exactly *trigger_distance* before the
        earliest (closest-to-start) obstacle anchor.  If no anchors are available,
        the start of the path is used as a safe fallback.
        """
        if not path:
            return {}

        if obstacle_anchors:
            # Find the arc-length of each anchor on the full path
            anchor_arcs = [_arc_length_of_point(path, a) for a in obstacle_anchors]
            # Use the anchor closest to start — that determines the earliest trigger
            min_anchor_arc = min(anchor_arcs)
            trigger_arc = max(0.0, min_anchor_arc - self._current_trigger_distance)
        else:
            trigger_arc = 0.0

        trigger_pos = _position_at_arc_length(path, trigger_arc)
        return {
            self.parameters.spawn_trigger_point: {
                'x': trigger_pos.x,
                'y': trigger_pos.y,
                'z': 0.0,
            }
        }
