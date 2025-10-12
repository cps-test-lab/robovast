#!/usr/bin/env python3
"""
Data models for the Variation Editor application.

This module contains all the data classes and structures used throughout
the application for representing scenarios and variants.
"""

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import yaml


@dataclass
class Position:
    """Represents a 2D position with x and y coordinates."""

    x: float
    y: float


@dataclass
class Pose:
    """Represents a pose with position and yaw orientation."""

    position: Position
    yaw: float = 0.0  # Yaw angle in radians


@dataclass
class StaticObject:
    """Represents a static object with name, model, pose, and optional xacro arguments."""

    name: str
    model: str
    pose: Pose
    xacro_arguments: str = ""


@dataclass
class Variant:
    """Represents a navigation scenario with map, mesh, start and goal poses, and static objects."""

    mesh_file: str
    map_file: str
    start_pose: Pose
    goal_poses: List[Pose]
    static_objects: List[StaticObject] = None
    laserscan_random_drop_percentage: float = 0.0
    laserscan_gaussian_noise_std_deviation: float = 0.0

    def __post_init__(self):
        if self.static_objects is None:
            self.static_objects = []


@dataclass
class VariantData:
    """Represents a complete variant with name and file path."""

    name: str
    floorplan_variation: str
    floorplan_variant_name: str
    variant: Variant
    planned_path: List[Position] = None

    def __post_init__(self):
        if self.planned_path is None:
            self.planned_path = []


@dataclass
class PathGenerationSettings:
    """Settings for path generation page."""

    path_length: float = 10.0
    num_paths: int = 1
    robot_diameter: float = 0.5
    path_length_tolerance: float = 0.5
    min_distance: float = 1.0
    path_generation_seed: int = None


@dataclass
class ObstaclePlacementSettings:
    """Settings for obstacle placement page."""

    obstacle_configs: List[Dict] = None
    obstacle_placement_seed: int = None

    def __post_init__(self):
        if self.obstacle_configs is None:
            self.obstacle_configs = []


@dataclass
class SensorNoiseSettings:
    """Settings for sensor noise configuration page."""

    noise_configs: List[Dict] = None
    skip_sensor_noise: bool = False
    sensor_noise_seed: int = None

    def __post_init__(self):
        if self.noise_configs is None:
            self.noise_configs = []


@dataclass
class FloorplanVariationSettings:
    """Settings for floorplan variation generation page."""

    variation_files: List[str] = None
    num_variations: int = 0
    floorplan_variation_seed: int = None

    def __post_init__(self):
        if self.variation_files is None:
            self.variation_files = []


@dataclass
class GeneralParameters:
    """General parameters for the robot navigation."""

    robot_diameter: float = 0.3


@dataclass
class VariationData:
    """Represents complete variation data with variants and page settings."""

    variants: List[VariantData] = None
    path_generation_settings: PathGenerationSettings = None
    obstacle_placement_settings: ObstaclePlacementSettings = None
    sensor_noise_settings: SensorNoiseSettings = None
    floorplan_variation_settings: FloorplanVariationSettings = None
    general_parameters: GeneralParameters = None

    def __post_init__(self):
        if self.variants is None:
            self.variants = []
        if self.path_generation_settings is None:
            self.path_generation_settings = PathGenerationSettings()
        if self.obstacle_placement_settings is None:
            self.obstacle_placement_settings = ObstaclePlacementSettings()
        if self.sensor_noise_settings is None:
            self.sensor_noise_settings = SensorNoiseSettings()
        if self.floorplan_variation_settings is None:
            self.floorplan_variation_settings = FloorplanVariationSettings()
        if self.general_parameters is None:
            self.general_parameters = GeneralParameters()


def save_variation_data_to_file(variation_data: VariationData, file_path: str):
    """Save VariationData to scenario.variants file, excluding planned_path."""
    # Ensure the directory exists
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    # Create the complete data structure with variants and settings
    data_to_save = {
        "settings": {
            "path_generation": {
                "path_length": variation_data.path_generation_settings.path_length,
                "num_paths": variation_data.path_generation_settings.num_paths,
                "robot_diameter": variation_data.path_generation_settings.robot_diameter,
                "path_length_tolerance": variation_data.path_generation_settings.path_length_tolerance,
                "min_distance": variation_data.path_generation_settings.min_distance,
                "path_generation_seed": variation_data.path_generation_settings.path_generation_seed,
            },
            "obstacle_placement": {
                "obstacle_configs": variation_data.obstacle_placement_settings.obstacle_configs,
                "obstacle_placement_seed": variation_data.obstacle_placement_settings.obstacle_placement_seed,
            },
            "sensor_noise": {
                "noise_configs": variation_data.sensor_noise_settings.noise_configs,
                "skip_sensor_noise": variation_data.sensor_noise_settings.skip_sensor_noise,
                "sensor_noise_seed": variation_data.sensor_noise_settings.sensor_noise_seed,
            },
            "floorplan_variation": {
                "variation_files": variation_data.floorplan_variation_settings.variation_files,
                "num_variations": variation_data.floorplan_variation_settings.num_variations,
                "floorplan_variation_seed": variation_data.floorplan_variation_settings.floorplan_variation_seed,
            },
            "general_parameters": {
                "robot_diameter": variation_data.general_parameters.robot_diameter,
            },
        }
    }

    # Convert variants to the expected format - each variant is a direct entry
    variants_list = []
    for variant_data in variation_data.variants:
        # Create the variant structure matching the expected format
        variant_dict = {
            "name": variant_data.name,
            "floorplan_variation": variant_data.floorplan_variation,
            "floorplan_variant_name": variant_data.floorplan_variant_name,
            "variant": {
                "nav_scenario": {
                    "mesh_file": variant_data.variant.mesh_file,
                    "map_file": variant_data.variant.map_file,
                    "start_pose": {
                        "position": {
                            "x": variant_data.variant.start_pose.position.x,
                            "y": variant_data.variant.start_pose.position.y,
                        },
                        "orientation": {"yaw": variant_data.variant.start_pose.yaw},
                    },
                    "goal_poses": [
                        {
                            "position": {
                                "x": goal_pose.position.x,
                                "y": goal_pose.position.y,
                            },
                            "orientation": {"yaw": goal_pose.yaw},
                        }
                        for goal_pose in variant_data.variant.goal_poses
                    ],
                    # Add sensor noise parameters from variant data
                    "laserscan_random_drop_percentage": str(
                        variant_data.variant.laserscan_random_drop_percentage
                    ),
                    "laserscan_gaussian_noise_std_deviation": str(
                        variant_data.variant.laserscan_gaussian_noise_std_deviation
                    ),
                }
            },
        }

        # Add static objects if they exist
        if variant_data.variant.static_objects:
            static_objects_list = []
            for static_obj in variant_data.variant.static_objects:
                obj_dict = {
                    "entity_name": static_obj.name,
                    "spawn_pose": {
                        "position": {
                            "x": static_obj.pose.position.x,
                            "y": static_obj.pose.position.y,
                            "z": 0.5,  # Default z value
                        },
                        "orientation": {"yaw": static_obj.pose.yaw},
                    },
                    "model": static_obj.model,
                }
                if static_obj.xacro_arguments:
                    obj_dict["xacro_arguments"] = static_obj.xacro_arguments

                static_objects_list.append(obj_dict)

            variant_dict["variant"]["nav_scenario"][
                "static_objects"
            ] = static_objects_list

        variants_list.append(variant_dict)

    # Write settings at the top, then variants
    with open(file_path, "w") as f:
        # Write settings as the first document
        yaml.dump(data_to_save, f, default_flow_style=False)
        f.write("---\n")

        # Write each variant as a separate YAML document
        for idx, variant_dict in enumerate(variants_list):
            yaml.dump(variant_dict, f, default_flow_style=False)
            # separate documents with '---'
            if idx < len(variants_list) - 1:
                f.write("---\n")


def load_variation_data_from_file(file_path: str) -> Optional[VariationData]:
    """Load VariationData from scenario.variants file."""
    info = ""
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "r") as file:
            content = file.read()

        # Parse YAML documents separated by ---
        yaml_docs = content.split("---\n")

        # Initialize with defaults
        variation_data = VariationData()

        for doc in yaml_docs:
            doc = doc.strip()
            if doc:
                try:
                    data = yaml.safe_load(doc)
                    if data is None:
                        continue

                    if "settings" in data:
                        settings = data["settings"]

                        pg_settings = settings["path_generation"]

                        variation_data.path_generation_settings = (
                            PathGenerationSettings(
                                path_length=pg_settings.get("path_length", 10.0),
                                num_paths=pg_settings.get("num_paths", 1),
                                robot_diameter=pg_settings.get(
                                    "robot_diameter", 0.5
                                ),  # Kept for backward compatibility
                                path_length_tolerance=pg_settings.get(
                                    "path_length_tolerance", 0.5
                                ),
                                min_distance=pg_settings.get("min_distance", 1.0),
                                path_generation_seed=pg_settings.get(
                                    "path_generation_seed"
                                ),
                            )
                        )

                        op_settings = settings["obstacle_placement"]
                        variation_data.obstacle_placement_settings = (
                            ObstaclePlacementSettings(
                                obstacle_configs=op_settings.get(
                                    "obstacle_configs", []
                                ),
                                obstacle_placement_seed=op_settings.get(
                                    "obstacle_placement_seed"
                                ),
                            )
                        )

                        sn_settings = settings["sensor_noise"]
                        variation_data.sensor_noise_settings = SensorNoiseSettings(
                            noise_configs=sn_settings.get("noise_configs", []),
                            skip_sensor_noise=sn_settings.get(
                                "skip_sensor_noise", False
                            ),
                            sensor_noise_seed=sn_settings.get("sensor_noise_seed"),
                        )
                        try:
                            fv_settings = settings["floorplan_variation"]
                            variation_data.floorplan_variation_settings = (
                                FloorplanVariationSettings(
                                    variation_files=fv_settings.get(
                                        "variation_files"
                                    ),
                                    num_variations=fv_settings["num_variations"],
                                    floorplan_variation_seed=fv_settings.get(
                                        "floorplan_variation_seed"),
                                )
                            )
                        except (KeyError, TypeError):
                            info += ("Invalid settings for floorplan variation. Using default.\n")
                            print(info)
                            variation_data.floorplan_variation_settings = (
                                FloorplanVariationSettings([], 1, 1))


                        gp_settings = settings["general_parameters"]
                        variation_data.general_parameters = GeneralParameters(
                            robot_diameter=gp_settings.get("robot_diameter", 0.3),
                        )

                    # Check if this is a variant document
                    if "name" in data and "variant" in data:
                        variant = _parse_variant_data(data)
                        if variant:
                            variation_data.variants.append(variant)

                except yaml.YAMLError as e:
                    print(f"Error parsing YAML document: {e}")
                    continue

        if info:
            print(info)
        return variation_data, info

    except Exception as e:
        info += (f"Error loading variation data: {e}")
        print(info)
        return None, info


def _parse_variant_data(data: Dict) -> Optional[VariantData]:
    """Parse variant data from dictionary."""
    try:
        nav_data = data["variant"]["nav_scenario"]

        # Parse start pose
        start_pos_data = nav_data["start_pose"]["position"]
        start_yaw = nav_data["start_pose"].get("orientation", {}).get("yaw", 0.0)
        # Support both formats: direct yaw or nested in orientation
        if "yaw" in nav_data["start_pose"]:
            start_yaw = nav_data["start_pose"]["yaw"]
        start_pose = Pose(Position(start_pos_data["x"], start_pos_data["y"]), start_yaw)

        # Parse goal poses
        goal_poses = []
        for goal_data in nav_data.get("goal_poses", []):
            goal_pos_data = goal_data["position"]
            goal_yaw = goal_data.get("orientation", {}).get("yaw", 0.0)
            # Support both formats: direct yaw or nested in orientation
            if "yaw" in goal_data:
                goal_yaw = goal_data["yaw"]
            goal_pose = Pose(Position(goal_pos_data["x"], goal_pos_data["y"]), goal_yaw)
            goal_poses.append(goal_pose)

        # Parse static objects (if present)
        static_objects = []
        if "static_objects" in nav_data:
            for obj_data in nav_data["static_objects"]:
                obj_name = obj_data["entity_name"]
                obj_model = obj_data["model"]
                obj_spawn_pose = obj_data["spawn_pose"]
                obj_pos = obj_spawn_pose["position"]
                obj_yaw = 0.0  # Default yaw for spawn_pose format

                obj_pose = Pose(Position(obj_pos["x"], obj_pos["y"]), obj_yaw)
                xacro_args = obj_data.get("xacro_arguments", "")
                static_objects.append(
                    StaticObject(obj_name, obj_model, obj_pose, xacro_args)
                )

        # Get sensor noise parameters (as strings in file, convert to float)
        laserscan_drop = float(nav_data.get("laserscan_random_drop_percentage", "0.0"))
        laserscan_noise = float(
            nav_data.get("laserscan_gaussian_noise_std_deviation", "0.0")
        )

        # Create variant
        variant = Variant(
            mesh_file=nav_data["mesh_file"],
            map_file=nav_data["map_file"],
            start_pose=start_pose,
            goal_poses=goal_poses,
            static_objects=static_objects,
            laserscan_random_drop_percentage=laserscan_drop,
            laserscan_gaussian_noise_std_deviation=laserscan_noise,
        )

        return VariantData(
            name=data["name"],
            floorplan_variation=data["floorplan_variation"],
            floorplan_variant_name=data["floorplan_variant_name"],
            variant=variant,
        )

    except Exception as e:
        print(f"Error parsing variant data: {e}")
        return None
