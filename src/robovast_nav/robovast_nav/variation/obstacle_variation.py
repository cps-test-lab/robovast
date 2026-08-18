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

import itertools
import math
import os
import random
from typing import Optional

import numpy as np
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from rdflib import Namespace

from robovast.common import convert_dataclasses_to_dict
from robovast.common.variation.base_variation import (DestinationConfig, ProvContribution,
                                                      VariationInfeasibleError)

from ..obstacle_placer import ObstaclePlacer
from ..path_generator import PathGenerator
from .. import config_view
from .nav_base_variation import NavVariation

ROBOVAST = Namespace("https://purl.org/robovast/metamodels/")


#: Shapes a placement plugin can compile. ``box`` is what ``roqsim_assets``' ``boxes`` takes;
#: a world gaining another population plugin adds its shape here.
_SUPPORTED_SHAPES = ('box',)


class ObstacleConfig(BaseModel):
    """One obstacle *kind*: how many, how far off the path, and what it is.

    Two vocabularies, and the split is deliberate. ``shape``/``size`` say what the obstacle
    IS -- the neutral fact, in meters, which a simulator that compiles its world needs.
    ``model``/``xacro_arguments`` say how a run-time *spawner* makes one, which is a
    property of that simulator's spawning interface and of the model file being spawned.

    Both are optional because a campaign needs only what its bound slots consume, and
    :meth:`ObstacleVariationConfig.validate_slots_have_inputs` requires the right one:
    ``objects`` (a scenario spawning at run time) needs ``model``; ``instances`` (a
    simulator compiling the placement in) needs ``size``.
    """

    model_config = ConfigDict(extra='forbid')
    amount: Optional[int] = None
    amount_per_m: Optional[float | list[float]] = None
    max_distance: float | list[float]
    model: Optional[str] = None
    #: Passed to the spawner verbatim, EXCEPT for ``{size[i]}`` placeholders, which are
    #: substituted from ``size`` -- so a campaign states the extents once and this renders
    #: them in whatever argument names its own model file happens to use. Substitution is
    #: positional on purpose: naming ``width``/``length``/``height`` here would put one
    #: model file's parameter names into a generic variation, which is what this replaced.
    xacro_arguments: Optional[str] = None
    #: What the obstacle is, for a simulator that compiles rather than spawns. ``box`` is
    #: the only shape a placement plugin takes today; the field exists so a world gaining
    #: cylinders is a new value here rather than a second parallel field.
    shape: str = 'box'
    #: Extents in meters. For ``box``: ``[x, y, z]``.
    size: Optional[list[float]] = None

    @field_validator('size')
    @classmethod
    def validate_size(cls, v):
        if v is not None and len(v) != 3:
            raise ValueError(f"size must have three elements [x, y, z], got {len(v)}")
        return v

    @field_validator('shape')
    @classmethod
    def validate_shape(cls, v):
        if v not in _SUPPORTED_SHAPES:
            raise ValueError(f"shape must be one of {_SUPPORTED_SHAPES}, got '{v}'")
        return v

    def rendered_xacro_arguments(self) -> Optional[str]:
        """``xacro_arguments`` with ``{size[i]}`` filled in from :attr:`size`."""
        if self.xacro_arguments is None:
            return None
        if '{size[' not in self.xacro_arguments:
            return self.xacro_arguments
        if self.size is None:
            raise ValueError(
                "xacro_arguments references {size[...]} but no 'size' is declared: "
                f"{self.xacro_arguments!r}")
        try:
            return self.xacro_arguments.format(size=self.size)
        except (IndexError, KeyError) as exc:
            raise ValueError(
                f"xacro_arguments {self.xacro_arguments!r} does not resolve against "
                f"size={self.size}: {exc}") from exc

    @model_validator(mode='after')
    def validate_amount_exclusive(self):
        has_amount = self.amount is not None
        has_per_m = self.amount_per_m is not None
        if has_amount == has_per_m:  # both set or neither set
            raise ValueError(
                "Exactly one of 'amount' or 'amount_per_m' must be specified in obstacle_configs entry."
            )
        return self

    def to_concrete(self, amount_per_m_value: float = None, max_distance_value: float = None) -> 'ObstacleConfig':
        """Return a copy of this config with concrete scalar values for list fields."""
        updates = {}
        if amount_per_m_value is not None:
            updates['amount_per_m'] = amount_per_m_value
        if max_distance_value is not None:
            updates['max_distance'] = max_distance_value
        return self.model_copy(update=updates)

    def resolve_amount(self, path_length: float) -> int:
        """Return the concrete obstacle count, resolving amount_per_m if necessary."""
        if self.amount is not None:
            return self.amount
        if isinstance(self.amount_per_m, list):
            raise ValueError("resolve_amount called on un-expanded ObstacleConfig with list amount_per_m")
        return max(0, math.floor(self.amount_per_m * path_length))


def _expand_obstacle_configs(obstacle_configs: list) -> list:
    """Expand obstacle_configs with list field values into a list of concrete
    obstacle_config lists via cartesian product.

    Example: [{amount_per_m: [0, 0.2], max_distance: [0.0, 0.3]}, {amount: 3}]
    → all combinations of amount_per_m × max_distance for each entry,
      then cartesian product across entries.
    """
    per_entry_options = []
    for oc in obstacle_configs:
        # Expand amount_per_m
        apm_values = oc.amount_per_m if isinstance(oc.amount_per_m, list) else [None]
        # Expand max_distance
        md_values = oc.max_distance if isinstance(oc.max_distance, list) else [None]

        alternatives = []
        for apm in apm_values:
            for md in md_values:
                alternatives.append(oc.to_concrete(
                    amount_per_m_value=apm,
                    max_distance_value=md,
                ))
        per_entry_options.append(alternatives)

    return [list(combo) for combo in itertools.product(*per_entry_options)]


class ObstacleVariationConfig(DestinationConfig):
    model_config = ConfigDict(extra='forbid')

    #: Where the placed obstacles go. ``objects`` is the trial's view of them -- the list a
    #: scenario spawns or drives, on the ``scenario`` channel.
    SLOTS = ("objects",)

    #: ``instances`` is the *simulator's* view of the same placement: ``pos`` / ``size`` /
    #: ``yaw`` per obstacle, shaped for a list-valued placement plugin::
    #:
    #:     scenario: {objects: static_objects}
    #:     sim:      {instances: plugins.obstacles.instances}
    #:
    #: Both, from one call, because they are one fact: MuJoCo does not recompile mid-run and
    #: ``sim_interfaces`` serves no ``SpawnEntity``, so an obstacle the trial drives must be
    #: one the world compiled. Optional because a simulator that spawns at run time (Gazebo)
    #: needs only the first, and requiring it would make every such campaign bind a
    #: destination it has none for.
    OPTIONAL_SLOTS = ("instances",)

    obstacle_configs: list[ObstacleConfig]
    seed: int
    robot_diameter: float
    map_file: Optional[str] = None
    count: int = 1
    #: Stem of the generated entity names (``<prefix>_<i>``), which the trial drives them by
    #: and -- when the ``instances`` slot is bound -- a simulator compiles them under. A
    #: campaign placing two populations must give them distinct stems: two ``obstacle_0``s in
    #: one compiled model is a duplicate-name failure, and a scenario naming one addresses
    #: whichever the simulator resolved first.
    entity_prefix: str = 'obstacle'

    @model_validator(mode='after')
    def validate_obstacle_inputs_for_bound_slots(self):
        """Each bound slot needs the vocabulary it is written from.

        Checked here rather than at placement time because the answer is knowable from the
        campaign alone: a bound ``instances`` with no ``size`` used to yield an obstacle
        whose extents the placement plugin invented, which is a wrong number in a result
        set rather than an error.
        """
        for i, oc in enumerate(self.obstacle_configs):
            if self.is_bound('objects') and not oc.model:
                raise ValueError(
                    f"obstacle_configs[{i}]: 'model' is required when the 'objects' slot is "
                    "bound -- a scenario spawning at run time has nothing to spawn without it")
            if self.is_bound('instances') and not oc.size:
                raise ValueError(
                    f"obstacle_configs[{i}]: 'size' is required when the 'instances' slot is "
                    "bound -- a simulator compiling this placement needs the extents, and "
                    "they are no longer inferred from 'xacro_arguments'")
            # Raises if the template does not resolve, so a bad placeholder is refused at
            # composition rather than reaching a spawner as a literal '{size[0]}'.
            oc.rendered_xacro_arguments()
        return self

    @field_validator('seed')
    @classmethod
    def validate_seed(cls, v):
        if v is None:
            raise ValueError('seed is required and cannot be None')
        return v

    @field_validator('robot_diameter')
    @classmethod
    def validate_robot_diameter(cls, v):
        if v <= 0.:
            raise ValueError('robot_diameter is required and cannot be None')
        return v


def _instances_for_sim(obstacle_objects, obstacle_geometry) -> list:
    """The placement as *geometry*: what a list-valued placement plugin compiles.

    Deliberately pos/size/yaw and nothing else. The scenario's view of an obstacle carries a
    model reference and spawner arguments -- one simulator's spawning vocabulary -- while what
    has to exist in a compiled model is a shape at a pose.

    The extents come from what the campaign DECLARED (``ObstacleConfig.size``), never from
    reading them back out of a spawner's argument string. Inferring them meant this generic
    variation knew one model file's parameter names (``width``/``length``/``height``), and --
    worse than the naming -- anything it could not parse yielded no size at all, so the
    placement plugin fell back to its own default: a cylinder, or a xacro spelling its
    parameters differently, compiled a differently-sized box against the real thing on the
    other simulator, silently, with every other cross-check passing.
    """
    instances = []
    for obj, (shape, size) in zip(obstacle_objects or [], obstacle_geometry or []):
        entry = convert_dataclasses_to_dict([obj])[0]
        position = entry.get('spawn_pose', {}).get('position', {})
        instance = {'pos': [position.get('x', 0.0), position.get('y', 0.0)]}
        # The SAME name the trial uses. Without it a placement plugin invents its own
        # (`boxes_0`), so a scenario driving `obstacle_0` by name would address nothing --
        # the two channels would agree on how many obstacles exist and where, and disagree
        # about what they are called, which is the one mismatch nothing else would catch.
        if entry.get('entity_name'):
            instance['name'] = entry['entity_name']
        yaw = entry.get('spawn_pose', {}).get('orientation', {}).get('yaw')
        if yaw:
            instance['yaw'] = yaw
        if size:
            instance['size'] = list(size)
        if shape and shape != 'box':
            instance['shape'] = shape
        instances.append(instance)
    return instances


class ObstacleVariation(NavVariation):
    """Places random obstacles in the environment based on configured obstacle types.

    Expected parameters:

    - ``name``: Name of the parameter to store static objects.
    - ``obstacle_configs``: List of obstacle configurations, each containing:

      - ``amount``: Number of obstacles to place.  Mutually exclusive with
        ``amount_per_m``.
      - ``amount_per_m``: Obstacles per meter of path length (computed as
        ``floor(amount_per_m × path_length)``).  Accepts a single float or a list of
        floats — each value produces a separate variation.  Mutually exclusive with
        ``amount``.
      - ``max_distance``: Maximum distance from the path for obstacle placement.
        Accepts a single float or a list of floats — each value produces a separate
        variation.
      - ``model``: Model name/path for the obstacle.
      - ``xacro_arguments``: Arguments to pass to xacro for model generation. ``{size[i]}``
        placeholders are substituted from ``size``, so the extents are stated once and the
        argument names stay with the model file that defines them.
      - ``size``: Extents in meters (``[x, y, z]`` for a box). Required when the ``instances``
        slot is bound, i.e. when a simulator has to compile the placement rather than spawn it.
      - ``shape``: What the obstacle is, for that simulator. ``box`` (default).
    - ``entity_prefix`` (optional): stem of the generated entity names, ``obstacle`` by
      default. Give two populations in one campaign distinct stems.

    - ``seed``: Seed for random number generation to ensure reproducibility.
    - ``robot_diameter``: Diameter of the robot for collision checking.
    - ``map_file``: Optional map file path (can be omitted if provided by a previous
      variation).
    - ``count``: Number of obstacle configurations to generate (default: ``1``).

    Generated outputs:

    - List of static objects with spawn poses and model information.
    """

    CONFIG_CLASS = ObstacleVariationConfig

    @classmethod
    def config_view_data(cls, config, base_path):
        """One marker per placed obstacle, sized from what the campaign declared."""
        del base_path
        return config_view.obstacle_contribution(config)

    @classmethod
    def collect_prov_metadata(cls, config_entry, campaign_namespace, config_namespace, gen_activity_id, vast_id):
        """Contribute obstacle count to the PROV scenario node."""
        config_cfg = config_entry.get("config", {})
        objects_parameter_name = config_entry.get("_objects_parameter_name", "")
        objects_list = config_cfg.get(objects_parameter_name, [])
        return ProvContribution(
            scenario_properties={ROBOVAST["n_obstacles"]: len(objects_list)}
        )

    @staticmethod
    def _expand_obstacle_configs(obstacle_configs):
        """Return a list of concrete obstacle-config tuples, expanding any list-valued
        max_distance fields into one entry per value (cartesian product across configs)."""
        options = []
        for oc in obstacle_configs:
            distances = oc.max_distance if isinstance(oc.max_distance, list) else [oc.max_distance]
            options.append([oc.model_copy(update={'max_distance': d}) for d in distances])
        return list(itertools.product(*options))

    def variation(self, in_configs):
        self.progress_update("Running Obstacle Variation...")

        # Expand obstacle_configs: list amount_per_m values produce separate variations
        expanded_configs_list = _expand_obstacle_configs(self.parameters.obstacle_configs)

        results = []
        for config in in_configs:
            np.random.seed(self.parameters.seed)
            random.seed(self.parameters.seed)
            for expanded_obstacle_configs in expanded_configs_list:
                for _ in range(self.parameters.count):
                    result = self._generate_obstacles_for_config(self.base_path, config, expanded_obstacle_configs)
                    results.extend(result)
        return results

    def _generate_obstacles_for_config(self, base_path, config, obstacle_configs):
        resulting_configs = []

        placer = ObstaclePlacer()

        try:
            map_file_path = self.get_map_file(self.parameters.map_file, config)
        except Exception as e:  # pylint: disable=broad-except
            raise ValueError(f"Error determining map file for config {config['name']}: {e}") from e

        # Get start and goal poses from config (set by previous variations)
        start_pose = config['config'].get('start_pose')
        goal_poses = config['config'].get('goal_poses', [])
        goal_pose = config['config'].get('goal_pose')

        # Handle both legacy goal_pose (singular) and current goal_poses (plural, from PathVariationRandom)
        if goal_pose and not goal_poses:
            goal_poses = [goal_pose]

        if not start_pose or not goal_poses:
            raise ValueError(
                f"start_pose and goal_pose(s) are required for path-dependent obstacle placement. "
                f"Config '{config['name']}' missing: "
                f"{'start_pose ' if not start_pose else ''}"
                f"{'goal_pose(s) ' if not goal_poses else ''}"
                f"Make sure a path variation (like PathVariationRandom) runs before ObstacleVariation."
            )

        self.progress_update(f"Placing obstacles along path from start_pose to {len(goal_poses)} goal_pose(s)...")

        waypoints = [start_pose] + goal_poses

        # Check if path is already available from previous variation
        if '_path' in config:
            path = config['_path']
            self.progress_update("Using pre-generated path from previous variation")
        else:
            # Generate path if not available
            path_generator = PathGenerator(map_file_path, self.parameters.robot_diameter)
            path = path_generator.generate_path(waypoints, [])
            self.progress_update("Generated new path for obstacle placement")

        # Resolve path length for amount_per_m computation.
        # Must be set by a previous variation (e.g. PathVariationRandom) via _path_length.
        if any(oc.amount_per_m is not None for oc in obstacle_configs):
            if '_path_length' not in config:
                raise ValueError(
                    "obstacle_configs contains 'amount_per_m' but '_path_length' is not set in the config. "
                    "Make sure a path variation (e.g. PathVariationRandom) runs before ObstacleVariation, "
                    "or use 'amount' instead of 'amount_per_m'."
                )
            path_length = config['_path_length']
        else:
            path_length = 0.0  # not needed when all configs use fixed 'amount'

        obstacle_objects = []  # List[StaticObject]
        obstacle_anchors = []  # List[Position] — path anchors for placed obstacles
        # The geometry of each placed object, in the order it was placed, so the `instances`
        # slot can say what a simulator must COMPILE. Carried beside the spawn objects rather
        # than read back off them: a spawn object holds a model reference and spawner
        # arguments, which is a different question from what shape exists at that pose.
        obstacle_geometry = []  # List[(shape, size)]
        for i, obstacle_config in enumerate(obstacle_configs):
            effective_amount = obstacle_config.resolve_amount(path_length)
            if effective_amount > 0:
                max_attempts = 10
                attempt = 0
                navigable_config_found = False

                while (
                    attempt < max_attempts
                    and not navigable_config_found
                ):
                    attempt += 1

                    try:
                        placed_pairs = placer.place_obstacles(
                            path,
                            obstacle_config.max_distance,
                            effective_amount,
                            obstacle_config.model,
                            obstacle_config.rendered_xacro_arguments(),
                            entity_prefix=self.parameters.entity_prefix,
                            robot_diameter=self.parameters.robot_diameter,
                            waypoints=waypoints,
                            min_arc_length=self._min_arc_length_for_config(i),
                        )
                    except Exception as e:
                        self.progress_update(f"Error placing obstacles: {e}")
                        placed_pairs = []

                    placed_obstacles = [obj for obj, _ in placed_pairs]
                    placed_anchor_pts = [anchor for _, anchor in placed_pairs]

                    # Check if we got the expected number of obstacles
                    if len(placed_obstacles) == effective_amount:
                        # Test with all obstacles so far (existing + new ones)
                        test_obstacles = obstacle_objects + placed_obstacles

                        # Validate navigation with the combined obstacle set
                        self.progress_update(f"Validating navigation on map {map_file_path} with {len(test_obstacles)} total obstacles")
                        if os.path.exists(map_file_path):
                            try:
                                generator = PathGenerator(
                                    map_file_path, self.parameters.robot_diameter
                                )

                                # Check if navigation is still possible with all obstacles
                                validation_path = generator.generate_path(
                                    waypoints,
                                    test_obstacles,
                                )

                                if validation_path:
                                    # Success! Add these obstacles to our collection
                                    obstacle_objects.extend(placed_obstacles)
                                    obstacle_anchors.extend(placed_anchor_pts)
                                    obstacle_geometry.extend(
                                        [(obstacle_config.shape, obstacle_config.size)]
                                        * len(placed_obstacles))
                                    navigable_config_found = True
                                    self.progress_update(
                                        f"Successfully placed {obstacle_config.amount} obstacles for config"
                                    )
                                else:
                                    self.progress_update(
                                        f"Attempt {attempt}/{max_attempts}: obstacles block navigation, retrying..."
                                    )

                            except Exception as e:
                                self.progress_update(
                                    f"Attempt {attempt}/{max_attempts}: validation error: {str(e)}, retrying..."
                                )
                        else:
                            raise FileNotFoundError(f"Map file not found: {map_file_path}")
                    else:
                        self.progress_update(
                            f"Attempt {attempt}/{max_attempts}: only placed {len(placed_obstacles)
                                                                             }/{effective_amount} obstacles, retrying..."
                            )

                # If we couldn't find a navigable configuration after all attempts
                if not navigable_config_found:
                    self.progress_update(
                        f"Warning: Could not place {effective_amount} obstacles while maintaining navigation"
                    )
                    # `path_length` above is only resolved when an `amount_per_m` config needs
                    # it; `_path_length` is set by a preceding path variation (e.g.
                    # PathVariationRandom) whenever one ran, regardless of which obstacle_config
                    # style is in use, so prefer it here for an accurate diagnostic.
                    reported_path_length = config.get('_path_length', path_length)
                    raise VariationInfeasibleError(
                        f"Could not place {effective_amount} obstacles while maintaining navigation "
                        f"after {max_attempts} attempts (path_length={reported_path_length:.2f}, "
                        f"max_distance={obstacle_config.max_distance})"
                    )

        # Always create variation with parameter, even if obstacle_objects is empty
        # This ensures consistent naming and parameters in scenario.config
        objects_parameter_name = self.parameters.binding("objects")[1]
        values = {
            'objects': convert_dataclasses_to_dict(obstacle_objects) if obstacle_objects else [],
            **self._post_process(obstacle_objects, obstacle_anchors, path),
        }
        # The same placement, described for the simulator: what must be COMPILED IN so the
        # trial has something to drive. Written in the same call as the trial's view, because
        # a world carrying fewer obstacles than the scenario names is a run that fails on a
        # service call, not a configuration anyone can fix afterwards.
        if self.parameters.is_bound('instances'):
            values['instances'] = _instances_for_sim(obstacle_objects, obstacle_geometry)
        result_config = self.update_slots(
            config, values,
            other_values={'_map_file': map_file_path, '_path': path,
                          '_objects_parameter_name': objects_parameter_name})

        resulting_configs.append(result_config)

        return resulting_configs

    # ------------------------------------------------------------------
    # Hooks for subclasses
    # ------------------------------------------------------------------

    def _min_arc_length_for_config(self, obstacle_config_index: int) -> float:
        """Return the minimum arc-length from path start before obstacles can be placed.

        Called once per obstacle_config entry (indexed by *obstacle_config_index*).
        Base implementation returns 0.0 (no restriction)."""
        return 0.0

    def _post_process(self, obstacle_objects, obstacle_anchors, path) -> dict:
        """Return additional scenario parameters to merge after obstacle placement.

        Called after all obstacle_configs have been placed successfully.
        *obstacle_objects*: List[StaticObject]
        *obstacle_anchors*: List[Position] — path anchors matching each obstacle
        *path*: full planned path (List[Position])

        Base implementation returns an empty dict."""
        return {}
