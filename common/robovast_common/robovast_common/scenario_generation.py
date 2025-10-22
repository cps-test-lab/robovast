
import copy
import math
import os
import random
import tempfile

import numpy as np
import yaml

from .common import load_scenario_config, save_scenario_variants_file
from .file_cache import FileCache
from .floorplan_generation import generate_floorplan_variations
from .navigation import ObstaclePlacer, PathGenerator, WaypointGenerator


class Variation():

    def __init__(self, base_path, parameters, general_parameters, progress_update_callback, output_dir):
        self.base_path = base_path
        self.parameters = parameters
        self.general_parameters = general_parameters
        self.progress_update_callback = progress_update_callback
        self.output_dir = output_dir

    def variation(self, in_variants):
        # vary in_variants and return result
        return None

    def progress_update(self, msg):
        self.progress_update_callback(f"{self.__class__.__name__}: {msg}")

    def get_updated_name(self, variant, name_suffix):
        """Generate updated variant name by appending a suffix."""
        name_suffix = name_suffix.replace("_", "-")
        if 'name' in variant:
            return f"{variant['name']}_{name_suffix}"
        else:
            return name_suffix


class ParameterVariationRandom(Variation):
    """
    Creates variants with random parameter values.

    Expected parameters:
        name: Name of the parameter to vary
        num_variations: Number of random variations to generate
        min: Minimum value (inclusive)
        max: Maximum value (inclusive)
        type: Type to convert values to ('string', 'int', 'float', 'bool')
        seed: Random seed for reproducibility (required)
    """

    def variation(self, in_variants):
        self.progress_update("Running Parameter Variation (Random)...")

        # Extract parameters
        param_name = self.parameters.get("name")
        num_variations = self.parameters.get("num_variations", 1)
        min_val = self.parameters.get("min")
        max_val = self.parameters.get("max")
        value_type = self.parameters.get("type", "float")
        seed = self.parameters.get("seed")

        # Validate required parameters
        if not param_name:
            raise ValueError("Parameter 'name' is required for ParameterVariationRandom")
        if min_val is None or max_val is None:
            raise ValueError("Parameters 'min' and 'max' are required for ParameterVariationRandom")
        if seed is None:
            raise ValueError("Parameter 'seed' is required for ParameterVariationRandom")

        # Set random seed
        random.seed(seed)
        np.random.seed(seed)

        # If no input variants, create initial empty variant
        if not in_variants or len(in_variants) == 0:
            in_variants = [{'variant': {}}]

        # Generate random parameter values once
        random_values = []
        for i in range(num_variations):
            # Generate random value
            if value_type in ['int', 'integer']:
                value = random.randint(int(min_val), int(max_val))
            elif value_type in ['float', 'double', 'number']:
                value = random.uniform(float(min_val), float(max_val))
            elif value_type == 'bool':
                # For bool, min/max are interpreted as probabilities
                value = random.random() < float(max_val)
            else:  # default to string
                # Generate random number and convert to string
                if isinstance(min_val, int) and isinstance(max_val, int):
                    value = str(random.randint(int(min_val), int(max_val)))
                else:
                    value = str(random.uniform(float(min_val), float(max_val)))

            random_values.append(value)
            self.progress_update(f"Generated random value: {param_name}={value}")

        # Apply each random value to all input variants (creating all combinations)
        results = []
        for value in random_values:
            for variant in in_variants:
                new_variant = copy.deepcopy(variant)

                # Ensure variant dict exists
                if 'variant' not in new_variant:
                    new_variant['variant'] = {}

                # Add parameter to variant
                new_variant['variant'][param_name] = value

                # Update variant name
                param_suffix = f"{param_name}{value}"
                new_variant['name'] = self.get_updated_name(new_variant, param_suffix)

                results.append(new_variant)

        return results


class FloorplanVariation(Variation):

    def variation(self, _):
        self.progress_update("Running Floorplan Variation...")

        result = generate_floorplan_variations(self.base_path, self.parameters.get("variation_files"), self.parameters.get(
            "num_variations"), self.parameters.get("floorplan_variation_seed"), self.output_dir, self.progress_update)
        if result is None:
            raise ValueError("Floorplan variation failed, no result returned")

        variants = []
        for root, dirs, _ in os.walk(self.output_dir):
            for dir_name in dirs:
                dir_path = os.path.join(root, dir_name)
                # only accept variants that contain a 'map' or 'maps' subdirectory
                if os.path.isdir(os.path.join(dir_path, 'maps')):
                    variants.append({
                        'name': dir_name.replace("_", "-"),
                        'floorplan_variant_path': os.path.join(self.output_dir, dir_name)
                    })

        return variants


class PathVariation(Variation):

    def _generate_path_for_variant(self, cache_path, variant, path_index, seed):
        """Generate a single path for a variant. This method is executed in a thread pool."""
        variant_name = f"{variant['name']}-p{path_index + 1}"

        if "floorplan_variant_path" not in variant:
            raise ValueError("Expected variant to contain 'floorplan_variant_path' field")

        map_file_basename = os.path.basename(variant["floorplan_variant_path"]).rsplit("_", 1)[0]
        rel_map_path = os.path.join('maps', map_file_basename + '.yaml')
        rel_mesh_path = os.path.join('3d-mesh', map_file_basename + '.stl')
        map_file_path = os.path.join(variant["floorplan_variant_path"], rel_map_path)
        if not os.path.exists(map_file_path):
            raise ValueError(f"File {map_file_path} does not exist.")

        file_cache = FileCache()
        cache_file_name = f"path_generation_{variant_name}_{seed}"
        file_cache.set_current_data_directory(cache_path)
        strings_for_hash = [str(path_index), str(seed)]
        cached_attempt = file_cache.get_cached_file([map_file_path], cache_file_name, strings_for_hash=strings_for_hash)
        if cached_attempt:
            attempt = int(cached_attempt)
            self.progress_update(f"Using cached attempt {attempt}")
        else:
            attempt = 0

        path_generator = PathGenerator(map_file_path)

        max_attempts = 1000  # Maximum attempts to find a valid path
        path_found = False

        while attempt < max_attempts and not path_found:
            current_seed = attempt + (max_attempts * path_index) + seed

            np.random.seed(current_seed)
            waypoint_generator = WaypointGenerator(map_file_path)
            self.progress_update(
                f"Generating {variant_name} - Attempt {attempt}/{max_attempts}"
            )
            # Use user-defined robot diameter
            waypoints = waypoint_generator.generate_waypoints(
                num_waypoints=2,  # Generate 2 waypoints beyond start
                robot_diameter=self.general_parameters.get('robot_diameter'),
                min_distance=self.parameters.get('min_distance'),  # Minimum distance between waypoints
            )
            start_pose = waypoints[0] if waypoints else None
            goal_poses = waypoints[1:] if len(waypoints) > 1 else []

            if start_pose and goal_poses:
                # Generate path considering any existing static objects
                path = path_generator.generate_path(waypoints, [])

                if not path:
                    attempt += 1
                    continue

                # Enforce path length tolerance
                length = sum(
                    math.hypot(
                        path[i].x - path[i - 1].x, path[i].y - path[i - 1].y
                    )
                    for i in range(1, len(path))
                )
                if abs(length - self.parameters.get('path_length')) > self.parameters.get('path_length_tolerance'):
                    attempt += 1
                    continue

                # Path found and valid
                path_found = True

        if not path_found:
            self.progress_update(
                f"Failed to generate {variant_name} after {max_attempts} attempts"
            )
            return None

        self.progress_update(f"Found path: {start_pose} -> {goal_poses}")
        # updated_variant_data = copy.deepcopy(variant_data)
        # updated_variant_data.name = variant_name
        # updated_variant_data.planned_path = path
        # updated_variant_data.variant.start_pose = start_pose
        # updated_variant_data.variant.goal_poses = goal_poses
        file_cache.save_file_to_cache(
            input_files=[map_file_path],
            file_name=cache_file_name,
            file_content=str(attempt),
            strings_for_hash=strings_for_hash)

        variant['variant_name'] = variant_name
        if "variant" not in variant:
            variant['variant'] = {}
        variant['variant']['start_pose'] = start_pose
        variant['variant']['goal_poses'] = goal_poses
        variant['path'] = path
        variant['variant']['map_file'] = rel_map_path  # os.path.relpath(map_file_path, variant["floorplan_variant_path"])
        variant['variant']['mesh_file'] = rel_mesh_path

        return variant

    def variation(self, in_variants):
        self.progress_update("Running Path Variation...")

        # Create a list of tasks for the thread pool
        tasks = []
        for variant in in_variants:
            for path_index in range(self.parameters.get("num_paths")):
                tasks.append((variant, path_index, self.parameters.get("path_generation_seed")))

        # # Process tasks in parallel using ThreadPoolExecutor
        results = []
        # with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        #     # Submit all tasks
        #     future_to_task = {
        #         executor.submit(self._generate_path_for_variant, variant, path_index, seed): (variant, path_index, seed)
        #         for variant, path_index, seed in tasks
        #     }

        #     # Collect results as they complete
        #     for future in as_completed(future_to_task):
        #         variant, path_index, _ = future_to_task[future]
        #         try:
        #             result = future.result()
        #             if result is not None:
        #                 results.append(result)
        #         except Exception as e:
        #             self.progress_update(f"Error generating path for {variant['name']}-p{path_index + 1}: {e}")

        for task in tasks:
            result = self._generate_path_for_variant(self.output_dir, task[0], task[1], task[2])
            if not result:
                return []
            results.append(result)

        return results


class ObstacleVariation(Variation):

    def _generate_obstacles_for_variant(self, base_path, variant, obstacle_configs, seed):
        self.progress_update(f"Generating obstacles for {variant['name']}, {obstacle_configs}, {seed}...")

        np.random.seed(seed)
        resulting_variants = []
        for obstacle_config in obstacle_configs:
            if obstacle_config["amount"] > 0:
                max_attempts = 10
                attempt = 0
                navigable_variant_found = False

                result_variant = None
                while (
                    attempt < max_attempts
                    and not navigable_variant_found
                ):
                    attempt += 1

                    # Reset variant for this attempt
                    result_variant = copy.deepcopy(variant)

                    # Create obstacle placer without setting seed (use
                    # global random state)
                    placer = ObstaclePlacer()

                    waypoints = [
                        variant["variant"]["start_pose"],
                    ] + variant["variant"]["goal_poses"]

                    robot_diameter = float(self.general_parameters["robot_diameter"])
                    try:
                        obstacle_objects = placer.place_obstacles(
                            variant["path"],
                            obstacle_config["max_distance"],
                            obstacle_config["amount"],
                            obstacle_config["model"],
                            obstacle_config.get("xacro_arguments", ""),
                            robot_diameter=robot_diameter,
                            waypoints=waypoints,
                        )
                    except Exception as e:
                        self.progress_update(f"Error placing obstacles: {e}")
                        obstacle_objects = []

                    # Add static objects to variant
                    if obstacle_objects:
                        if 'variant' not in result_variant:
                            result_variant['variant'] = {}
                        result_variant["variant"]["static_objects"] = obstacle_objects
                    else:
                        navigable_variant_found = True

                    # Validate navigation with the placed obstacles
                    if obstacle_objects and variant['variant']["map_file"]:
                        map_path = os.path.join(variant["floorplan_variant_path"],
                                                variant['variant']["map_file"])
                        print(f"Validating navigation on map {map_path} with {len(obstacle_objects)} obstacles")
                        if os.path.exists(map_path):
                            try:
                                generator = PathGenerator(
                                    map_path, robot_diameter
                                )

                                # Check if navigation is still possible
                                path = generator.generate_path(
                                    waypoints,
                                    result_variant['variant']["static_objects"],
                                )

                                if path:
                                    # Success! Navigation is still possible
                                    variant["planned_path"] = path
                                    navigable_variant_found = True
                                    self.progress_update(
                                        f"Successfully placed {obstacle_config['amount']} obstacles for variant"
                                    )
                                else:
                                    self.progress_update(
                                        f"Try to set obstacles {
                                            attempt}/{max_attempts}: obstacles block navigation, retrying..."
                                    )

                            except Exception as e:
                                self.progress_update(
                                    f"Attempt {attempt}/{max_attempts}: validation error: {str(e)}, retrying..."
                                )
                        else:
                            print(f"Warning: Map file not found: {map_path}")

                    # If we couldn't find a navigable configuration after
                    # all attempts
                    if not navigable_variant_found:
                        if obstacle_config["amount"] > 0:
                            self.progress_update(
                                f"Warning: Could not place {obstacle_config['amount']} obstacles for variant while maintaining navigation"
                            )
                            raise ValueError("Could not place obstacles while maintaining navigation")

                # Update variant name to include obstacle info
                short_model_name = os.path.basename(obstacle_config["model"]).replace(
                    ".sdf.xacro", ""
                ).replace(".sdf", "")
                result_variant["name"] = (
                    f"{result_variant["name"]}-o{obstacle_config['amount']}-{short_model_name}"
                )

                resulting_variants.append(result_variant)

        return resulting_variants

    def variation(self, in_variants):
        self.progress_update("Running Obstacle Variation...")

        # Create a list of tasks for the thread pool
        tasks = []

        for variant in in_variants:
            tasks.append((variant, self.parameters.get("obstacle_configs"), self.parameters.get("obstacle_placement_seed")))

        # Process tasks in parallel using ThreadPoolExecutor
        results = []
        # with ThreadPoolExecutor(max_workers=os.cpu_count()) as executor:
        #     # Submit all tasks
        #     future_to_task = {
        #         executor.submit(self._generate_obstacles_for_variant, variant, obstacle_configs, idx, seed): (variant, obstacle_config, idx, seed)
        #         for variant, obstacle_configs, idx, seed in tasks
        #     }

        #     # Collect results as they complete
        #     for future in as_completed(future_to_task):
        #         variant, obstacle_config, idx, _ = future_to_task[future]
        #         try:
        #             result = future.result()
        #             if result is not None:
        #                 results.append(result)
        #         except Exception as e:
        #             self.progress_update(f"Error generating path for {variant['name']} {obstacle_config}: {e}")

        for task in tasks:
            result = self._generate_obstacles_for_variant(self.base_path, task[0], task[1], task[2])
            if not result:
                return []
            results.extend(result)

        return results


def progress_update(msg):
    print(msg)


def execute_variation(base_dir, variants, variation_class, parameters, general_parameters, progress_update_callback, output_dir=None):
    variation = variation_class(base_dir, parameters, general_parameters, progress_update_callback, output_dir)
    try:
        variants = variation.variation(copy.deepcopy(variants))
    except Exception as e:
        progress_update_callback(f"Variation failed. {variation_class.__name__}: {e}")
        return []

    # Check if variants is None and return empty list
    if variants is None:
        progress_update_callback(f"Variation failed. {variation_class.__name__}: No variants returned")
        return []

    # progress_update(f"Current variants {variants}")
    for variant in variants:
        print('-' * 40)
        print(f"{variant['name']
                 }: start: {variant.get('start_pose').position if 'start_pose' in variant else "✗"
                            }, goal: {len(variant.get('goal_poses', [])) if 'goal_poses' in variant else "✗"
                                      }, obstacles: {len(variant.get('static_objects', [])) if 'static_objects' in variant else "✗"
                                                     }, path: {len(variant.get('path', [])) if 'path' in variant else "✗"
                                                               }, floorplan_variant_path: {variant.get('floorplan_variant_path', '✗')}")
        print('-' * 40)
    return variants


def _read_variation_classes_from_file(variation_file):
    """
    Read variation class names from the variation file settings.

    Returns a list of variation class objects in the order they appear in the file.
    Reads from settings.variation list.
    """
    if not os.path.exists(variation_file):
        return []

    with open(variation_file, 'r') as f:
        try:
            # Load YAML document
            documents = list(yaml.safe_load_all(f))
            if not documents:
                return []

            config = documents[0]
            settings = config.get('settings', {})

            if not settings:
                return []

            # Get the variation list from settings
            variation_list = settings.get('variation', [])

            if not variation_list or not isinstance(variation_list, list):
                return []

            # Map class names to actual class objects
            available_classes = {
                'ParameterVariationRandom': ParameterVariationRandom,
                'FloorplanVariation': FloorplanVariation,
                'PathVariation': PathVariation,
                'ObstacleVariation': ObstacleVariation,
            }

            # Extract variation class names from the list
            variation_classes = []
            for item in variation_list:
                if isinstance(item, dict):
                    # Each item in the list should be a dict with one key (the class name)
                    for class_name in item.keys():
                        if class_name in available_classes:
                            variation_classes.append((available_classes[class_name], item[class_name]))
                        else:
                            print(f"Warning: Unknown variation class '{class_name}' found in variation file")

            return variation_classes

        except yaml.YAMLError as e:
            print(f"Error parsing variation file: {e}")
            return []
        except Exception as e:
            print(f"Error reading variation classes from file: {e}")
            return []


def generate_scenario_variations(variation_file, progress_update_callback, variation_classes=None, output_dir=None):
    progress_update_callback("Start generating variants.")

    parameters = load_scenario_config(variation_file)

    if output_dir is None:
        temp_path = tempfile.TemporaryDirectory(prefix="robovast_variation_")
        output_dir = temp_path.name

    if variation_classes is None:
        # Read variation classes from the variation file
        variation_classes_and_parameters = _read_variation_classes_from_file(variation_file)
    else:
        raise NotImplementedError("Passing variation_classes is not implemented yet")

    general_parameters = parameters.get('general', {})
    variants = []
    for variation_class, parameters in variation_classes_and_parameters:
        result = execute_variation(os.path.dirname(variation_file), variants, variation_class,
                                   parameters, general_parameters, progress_update_callback, output_dir)
        if result is None or len(result) == 0:
            # If a variation step fails or produces no results, stop the pipeline
            progress_update_callback(f"Variation pipeline stopped at {variation_class.__name__} - no variants to process")
            variants = []
            break
        variants = result

    if variants:
        save_scenario_variants_file(variants, os.path.join(output_dir, 'scenario.variants'))

    return variants
