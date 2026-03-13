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

"""ObstacleVariationWithDistanceTrigger — single-obstacle placement with a distance-based spawn trigger.

Exactly **one** obstacle is placed at a path position at least *trigger_distance* arc-length
ahead of the robot's start.

Two scenario parameters are written:

* *spawn_trigger_point*  — the spawn pose position of the single placed obstacle.
* *spawn_trigger_threshold* — the trigger distance (arc-length in meters) that was used.

trigger_distance can be a single float or a list of floats.  When a list is provided, one
output configuration is produced per value (multiplied with the normal count/in_configs fan-out).
"""

import copy
import random
from typing import List, Optional, Union

import numpy as np
from pydantic import ConfigDict, field_validator, model_validator

from robovast.common import convert_dataclasses_to_dict
from ..data_model import Orientation, Pose, Position
from .obstacle_variation import ObstacleVariation, ObstacleVariationConfig, ObstacleVariationGuiRenderer


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------

class ObstacleVariationWithDistanceTriggerConfig(ObstacleVariationConfig):
    """Configuration for ObstacleVariationWithDistanceTrigger.

    Inherits all fields from ObstacleVariationConfig and adds:
    - spawn_trigger_point:     scenario parameter name to store the obstacle position.
    - spawn_trigger_threshold: scenario parameter name to store the trigger distance.
    - trigger_distance:        arc-length (m) before the obstacle; a single float or a list
                               of floats (one output config per value).
    - start_pose:              optional explicit start pose (dict).
    - goal_pose:               optional explicit goal pose (dict).

    Exactly one obstacle must be configured (i.e. a single ObstacleConfig entry with amount=1).
    """

    model_config = ConfigDict(extra='forbid')

    spawn_trigger_point: str
    spawn_trigger_threshold: str
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

    @model_validator(mode='after')
    def validate_single_obstacle(self):
        """Raise an error if the total obstacle amount is not exactly 1."""
        total = sum(c.amount for c in self.obstacle_configs)
        if total != 1:
            raise ValueError(
                f"ObstacleVariationWithDistanceTrigger only supports a single obstacle "
                f"(total amount must be 1), but got {total}."
            )
        return self


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
    """Places exactly one obstacle at a position at least *trigger_distance* arc-length ahead of the robot's start along the planned path.

    Two scenario parameters are written for use in the scenario script.

    Expected parameters:

    - ``name``: Name of the parameter to store the placed obstacle.
    - ``spawn_trigger_point``: Scenario parameter name to receive the obstacle's spawn
      pose position.
    - ``spawn_trigger_threshold``: Scenario parameter name to receive the trigger
      distance value that was used.
    - ``trigger_distance``: Arc-length in metres from the start to the obstacle.
      Accepts a single float or a list of floats — one output configuration is produced
      per value.
    - ``obstacle_configs``: List of obstacle configurations (same format as
      :class:`ObstacleVariation`).  Total ``amount`` across all entries must equal
      exactly 1.
    - ``seed``: Seed for random number generation to ensure reproducibility.
    - ``robot_diameter``: Diameter of the robot for collision checking in metres.
    - ``map_file``: Optional map file path (uses scenario default if omitted).
    - ``count``: Number of obstacle configurations to generate (default: ``1``).
    - ``start_pose``: Optional explicit start pose (dict with ``x``, ``y``, ``yaw``).
    - ``goal_pose``: Optional explicit goal pose (dict with ``x``, ``y``, ``yaw``).

    Generated outputs:

    - ``<name>``: Placed obstacle with spawn pose and model information.
    - ``<spawn_trigger_point>``: Position of the placed obstacle.
    - ``<spawn_trigger_threshold>``: The trigger distance value that was applied.

    Example:

    .. code-block:: yaml

        - ObstacleVariationWithDistanceTrigger:
            name: dynamic_objects
            spawn_trigger_point: spawn_trigger_point
            spawn_trigger_threshold: spawn_trigger_threshold
            trigger_distance: [1.0, 2.0]
            obstacle_configs:
            - amount: 1
              max_distance: [0.0, 0.3]
              model: file:///config/files/models/box.sdf.xacro
              xacro_arguments: width:=0.5, length:=0.5, height:=1.0
            seed: 42
            robot_diameter: 0.35
            count: 2
    """

    CONFIG_CLASS = ObstacleVariationWithDistanceTriggerConfig
    GUI_RENDERER_CLASS = ObstacleVariationWithDistanceTriggerGuiRenderer

    def variation(self, in_configs):
        self.progress_update("Running ObstacleVariationWithDistanceTrigger...")
        all_expanded = self._expand_obstacle_configs(self.parameters.obstacle_configs)
        n_expanded = len(all_expanded)
        results = []
        for config in in_configs:
            for td_idx, td in enumerate(self.parameters.trigger_distance):
                self._current_trigger_distance = td
                for exp_idx, expanded_configs in enumerate(all_expanded):
                    seed = self.parameters.seed + td_idx * n_expanded + exp_idx
                    np.random.seed(seed)
                    random.seed(seed)
                    effective_config = self._inject_poses(config)
                    for _ in range(self.parameters.count):
                        result = self._generate_obstacles_for_config(
                            self.base_path, effective_config, list(expanded_configs)
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
        """Return spawn_trigger_point (obstacle position) and spawn_trigger_threshold.

        * spawn_trigger_point     — the spawn pose position of the single placed obstacle.
        * spawn_trigger_threshold — the current trigger distance value.
        """
        if not obstacle_objects:
            return {}

        obj_dict = convert_dataclasses_to_dict([obstacle_objects[0]])[0]
        pos = obj_dict['spawn_pose']['position']
        return {
            self.parameters.spawn_trigger_point: {
                'x': pos['x'],
                'y': pos['y'],
                'z': 0.0,
            },
            self.parameters.spawn_trigger_threshold: self._current_trigger_distance,
        }
