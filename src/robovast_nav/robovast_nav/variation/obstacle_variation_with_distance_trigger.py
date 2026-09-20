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

import random
from typing import List, Union

import numpy as np
from pydantic import ConfigDict, field_validator, model_validator

from robovast.common import convert_dataclasses_to_dict

from .. import config_view
from .obstacle_variation import ObstacleVariation, ObstacleVariationConfig, resting_z

# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------

class ObstacleVariationWithDistanceTriggerConfig(ObstacleVariationConfig):
    """Configuration for ObstacleVariationWithDistanceTrigger.

    Inherits all fields from ObstacleVariationConfig and adds:
    - the ``trigger_point`` / ``trigger_threshold`` output slots, bound by the campaign.
    - trigger_distance:        arc-length (m) before the obstacle; a single float or a list
                               of floats (one output config per value).

    Exactly one obstacle must be configured (i.e. a single ObstacleConfig entry with amount=1).
    """

    model_config = ConfigDict(extra='forbid')

    #: Two further outputs, so the same binding form covers them: where the obstacle sits and
    #: how far along the path the trial should act on it. As config keys whose *values*
    #: were parameter names they would be slot bindings without saying so.
    OUTPUT_SLOTS = ("objects", "trigger_point", "trigger_threshold")

    trigger_distance: Union[float, List[float]]

    @field_validator('trigger_distance', mode='before')
    @classmethod
    def normalise_trigger_distance(cls, v):
        """Accept a single float or a list; always store as list[float]."""
        if isinstance(v, (int, float)):
            return [float(v)]
        return [float(x) for x in v]

    @model_validator(mode='after')
    def validate_single_obstacle(self):
        """Raise an error if the total obstacle amount is not exactly 1.

        ``amount_per_m`` is refused rather than summed. It is a density, resolved as
        ``floor(amount_per_m x path_length)`` once a path exists, so at config time it
        states no number at all -- and the one number this variation accepts is 1, which
        a density can only reach by accident of the path it lands on.
        """
        per_m = [c for c in self.obstacle_configs if c.amount_per_m is not None]
        if per_m:
            raise ValueError(
                "ObstacleVariationWithDistanceTrigger places a single obstacle at a "
                "trigger distance, so it needs an obstacle_configs entry that states one: "
                "write 'amount: 1'. 'amount_per_m' is a density resolved against the path "
                "length, which is not known here and which no single value of it fixes at 1."
            )
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

    - ``reads`` (optional): Which parameter each input is read from, as
      ``{start: <parameter>, goal: <parameter>}`` -- see :class:`ObstacleVariation`.
    - ``trigger_point`` (slot): receives the obstacle's spawn
      pose position.
    - ``trigger_threshold`` (slot): receives the trigger
      distance value that was used.
    - ``trigger_distance``: Arc-length in meters from the start to the obstacle.
      Accepts a single float or a list of floats — one output configuration is produced
      per value.
    - ``obstacle_configs``: List of obstacle configurations (same format as
      :class:`ObstacleVariation`).  Total ``amount`` across all entries must equal
      exactly 1.
    - ``seed``: Seed for random number generation to ensure reproducibility.
    - ``robot_diameter``: Diameter of the robot for collision checking in meters.
    - ``map_file``: Optional map file path (uses scenario default if omitted).
    - ``count``: Number of obstacle configurations to generate (default: ``1``).

    Generated outputs:

    - ``objects``: Placed obstacle with spawn pose and model information.
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

    #: ``driven``: this obstacle is REVEALED partway through the run -- parked out of the way and
    #: teleported in when the robot comes within the trigger distance -- so it needs a pose the
    #: trial can write, which welded scenery has not. ``driven`` is that and nothing more: the
    #: body has no degrees of freedom, so the solver never owns its pose.
    #:
    #: Not ``physics``, whose free body IS owned by the solver from the next step. The pose is the
    #: campaign's variable here, and a solver-owned obstacle stops holding it in two ways: the
    #: robot that reaches it pushes it off the placement the search selected, and a placement that
    #: overlaps other geometry is answered by ejecting it at speed. Those look like results.
    SIM_INSTANCES_MOTION = "driven"

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
                    for _ in range(self.parameters.count):
                        result = self._generate_obstacles_for_config(
                            self.base_path, config, list(expanded_configs)
                        )
                        # Propagate spawn trigger point to a private key for GUI access.
                        # Read back from the destination the campaign BOUND the slot to, the
                        # same way ObstacleVariation resolves `objects`. Not
                        # `self.parameters.spawn_trigger_point`: that is a config key whose
                        # value is a parameter name, which is what output slots express, and
                        # no such attribute exists.
                        trigger_point_name = self.parameters.binding('trigger_point')[1]
                        for r in result:
                            tp = r['config'].get(trigger_point_name)
                            if tp:
                                r['_spawn_trigger_point'] = tp
                        results.extend(result)
        return results

    # ------------------------------------------------------------------
    # Hooks (override ObstacleVariation base hooks)
    # ------------------------------------------------------------------

    def _min_arc_length_for_config(self, obstacle_config_index: int) -> float:
        """Keep all obstacles at least trigger_distance ahead on the path."""
        return self._current_trigger_distance

    def _post_process(self, obstacle_objects, obstacle_anchors, path, obstacle_geometry) -> dict:
        """The two extra outputs, by SLOT -- the campaign names their destinations.

        * ``trigger_point``     — the spawn pose position of the single placed obstacle.
        * ``trigger_threshold`` — the current trigger distance value.

        ``trigger_point`` is a whole POSITION, z included, because a scenario revealing the
        obstacle has to state one: the distance test that fires the trigger is planar and reads
        only x and y, but the teleport that follows it places a body. Reporting z as 0.0 -- a
        height the obstacle is never at -- left the scenario to invent one, and the value near
        to hand is the robot's, which seats a floor-standing obstacle inside the floor.
        """
        if not obstacle_objects:
            return {}

        obj_dict = convert_dataclasses_to_dict([obstacle_objects[0]])[0]
        pos = obj_dict['spawn_pose']['position']
        _, size = obstacle_geometry[0] if obstacle_geometry else (None, None)
        return {
            'trigger_point': {
                'x': pos['x'],
                'y': pos['y'],
                'z': resting_z(size),
            },
            'trigger_threshold': self._current_trigger_distance,
        }
