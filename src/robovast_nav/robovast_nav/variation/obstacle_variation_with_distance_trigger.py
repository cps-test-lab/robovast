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

* *trigger_point*     — the spawn pose position of the single placed obstacle.
* *trigger_threshold* — the trigger distance (arc-length in meters) that was used.

trigger_distance can be a single float or a list of floats.  When a list is provided, one
output configuration is produced per value (multiplied with the normal count/in_configs fan-out).
"""

import copy
import random
from typing import List, Optional, Union

import numpy as np
from pydantic import ConfigDict, field_validator, model_validator

from robovast.common import convert_dataclasses_to_dict

from .. import config_view
from ..data_model import Orientation, Pose, Position
from .obstacle_variation import ObstacleVariation, ObstacleVariationConfig

# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------

class ObstacleVariationWithDistanceTriggerConfig(ObstacleVariationConfig):
    """Configuration for ObstacleVariationWithDistanceTrigger.

    Inherits all fields from ObstacleVariationConfig and adds:
    - the ``trigger_point`` / ``trigger_threshold`` output slots, bound by the campaign.
    - trigger_distance:        arc-length (m) before the obstacle; a single float or a list
                               of floats (one output config per value).
    - start_pose:              optional explicit start pose (dict).
    - goal_pose:               optional explicit goal pose (dict); only for scenarios
                               declaring the singular parameter.

    Exactly one obstacle must be configured (i.e. a single ObstacleConfig entry with amount=1).
    """

    model_config = ConfigDict(extra='forbid')

    #: Two further outputs, so the same binding form covers them: where the obstacle sits and
    #: how far along the path the trial should act on it. They used to be config keys whose
    #: *values* were parameter names, which is a slot binding without saying so.
    SLOTS = ("objects", "trigger_point", "trigger_threshold")

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

class ObstacleVariationWithDistanceTrigger(ObstacleVariation):
    """Places exactly one obstacle at a position at least *trigger_distance* arc-length ahead of the robot's start along the planned path.

    Two scenario parameters are written for use in the scenario script.

    Expected parameters:

    - ``name``: Name of the parameter to store the placed obstacle.
    - ``trigger_point`` (slot): receives the obstacle's spawn
      pose position.
    - ``trigger_threshold`` (slot): receives the trigger
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
      Applies to scenarios declaring the singular ``goal_pose`` parameter; a
      scenario taking ``goal_poses`` gets its list from the config unchanged.

    Generated outputs:

    - ``<name>``: Placed obstacle with spawn pose and model information.
    - ``trigger_point``: Position of the placed obstacle.
    - ``trigger_threshold``: The trigger distance value that was applied.

    Example:

    .. code-block:: yaml

        - ObstacleVariationWithDistanceTrigger:
            scenario:
              objects: dynamic_objects
              trigger_point: spawn_trigger_point
              trigger_threshold: spawn_trigger_threshold
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

    @classmethod
    def config_view_data(cls, config, base_path):
        """The obstacles, plus the trigger point that spawns them."""
        del base_path
        return config_view.trigger_contribution(config)

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
                        # Propagate spawn trigger point to a private key for GUI access.
                        # Read back from the destination the campaign BOUND the slot to, the
                        # same way ObstacleVariation resolves `objects`. It used to be
                        # `self.parameters.spawn_trigger_point` -- a config key whose value was
                        # a parameter name, which is what output slots replaced, so the
                        # attribute no longer exists and every campaign using this variation
                        # failed at generation.
                        trigger_point_name = self.parameters.binding('trigger_point')[1]
                        for r in result:
                            tp = r['config'].get(trigger_point_name)
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
        """Return a deep copy of *config* with its poses converted to Pose objects.

        Conversion is in place: whichever spelling the config already carries
        (``goal_pose`` or ``goal_poses``) is the one written back. Neither key is
        added nor removed — the scenario's own parameter set decides which exists,
        and injecting the other spelling makes the OSC reject an undeclared
        parameter at startup. Explicit variation parameters override the config
        values; YAML-sourced poses arrive as dicts and must be converted, since the
        base class plans the path itself when no path variation ran before it.
        """
        effective = copy.deepcopy(config)

        raw_start = self.parameters.start_pose or effective['config'].get('start_pose')
        if raw_start is not None:
            effective['config']['start_pose'] = self._dict_to_pose(raw_start)

        raw_goal = self.parameters.goal_pose or effective['config'].get('goal_pose')
        if raw_goal is not None:
            effective['config']['goal_pose'] = self._dict_to_pose(raw_goal)

        raw_goals = effective['config'].get('goal_poses')
        if raw_goals:
            effective['config']['goal_poses'] = [self._dict_to_pose(g) for g in raw_goals]

        return effective

    # ------------------------------------------------------------------
    # Hooks (override ObstacleVariation base hooks)
    # ------------------------------------------------------------------

    def _min_arc_length_for_config(self, obstacle_config_index: int) -> float:
        """Keep all obstacles at least trigger_distance ahead on the path."""
        return self._current_trigger_distance

    def _post_process(self, obstacle_objects, obstacle_anchors, path) -> dict:
        """The two extra outputs, by SLOT -- the campaign names their destinations.

        * ``trigger_point``     — the spawn pose position of the single placed obstacle.
        * ``trigger_threshold`` — the current trigger distance value.
        """
        if not obstacle_objects:
            return {}

        obj_dict = convert_dataclasses_to_dict([obstacle_objects[0]])[0]
        pos = obj_dict['spawn_pose']['position']
        return {
            'trigger_point': {
                'x': pos['x'],
                'y': pos['y'],
                'z': 0.0,
            },
            'trigger_threshold': self._current_trigger_distance,
        }
