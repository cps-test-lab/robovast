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

from pydantic import BaseModel, ConfigDict, field_validator

from ..floorplan_generation import (
    _create_config_for_floorplan,
    generate_floorplan_variations,
    generate_floorplan_artifacts
)
from .nav_base_variation import NavVariation


class FloorplanVariationConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: list[str]
    variation_files: list[str]
    num_variations: int
    seed: int

    @field_validator('name')
    @classmethod
    def validate_name_list(cls, v):
        if not v or len(v) != 2:
            raise ValueError('name must contain exactly two elements, 1. for map file, 2. for mesh file')
        return v

    @field_validator('variation_files')
    @classmethod
    def validate_variation_files(cls, v):
        if not v or len(v) == 0:
            raise ValueError('variation_files must contain at least one file')
        return v

    @field_validator('num_variations')
    @classmethod
    def validate_num_variations(cls, v):
        if v < 1:
            raise ValueError('num_variations must be at least 1')
        return v


class FloorplanGenerationConfig(BaseModel):
    """Configuration for FloorplanGeneration.
    
    Attributes:
        name: List with exactly two elements: [map_file_param, mesh_file_param].
              These names will be used as parameter keys in the generated configs.
        floorplans: List of paths to .fpm floorplan files to generate artifacts for.
                    Paths are relative to the base configuration directory.
    """
    model_config = ConfigDict(extra='forbid')
    name: list[str]
    floorplans: list[str]

    @field_validator('name')
    @classmethod
    def validate_name_list(cls, v):
        if not v or len(v) != 2:
            raise ValueError('name must contain exactly two elements, 1. for map file, 2. for mesh file')
        return v

    @field_validator('floorplans')
    @classmethod
    def validate_floorplans(cls, v):
        if not v or len(v) == 0:
            raise ValueError('floorplans must contain at least one file')
        return v


class FloorplanGeneration(NavVariation):
    """Generate floorplan artifacts from existing floorplan files.
    
    This variation takes existing .fpm (floorplan) files and generates the necessary
    artifacts for navigation testing:
    - Occupancy grid maps (.yaml and .pgm files)
    - 3D meshes (.stl files)
    
    Unlike FloorplanVariation which creates multiple variations from .variation files,
    FloorplanGeneration processes floorplan files directly without creating variations.
    It generates exactly one configuration per input floorplan file.
    
    Example configuration:
        - FloorplanGeneration:
            name:
            - map_file
            - mesh_file
            floorplans:
            - floorplans/rooms/rooms.fpm
            - floorplans/hallways/hallways.fpm
    
    This will generate map and mesh artifacts for each floorplan and create
    configurations with the map_file and mesh_file parameters set appropriately.
    """

    CONFIG_CLASS = FloorplanGenerationConfig

    def variation(self, in_configs):
        """Generate artifacts for each floorplan and create configurations.
        
        Args:
            in_configs: List of input configurations to extend. If empty, a default
                       empty configuration is created.
        
        Returns:
            List of configurations, one per input floorplan per input config.
            Each configuration includes map_file and mesh_file parameters pointing
            to the generated artifacts.
        
        Raises:
            ValueError: If artifact generation fails or returns unexpected number of results.
            FileNotFoundError: If floorplan files or generated artifacts are not found.
        """
        self.progress_update("Running Floorplan Generation...")

        # If no input configs, create initial empty config
        if not in_configs or len(in_configs) == 0:
            in_configs = [{'config': {}, '_config_files': []}]

        floorplan_names = generate_floorplan_artifacts(
            self.base_path,
            self.parameters.floorplans,
            self.output_dir,
            self.progress_update
        )

        if not floorplan_names:
            raise ValueError("Floorplan generation failed, no result returned")
        if len(floorplan_names) != len(self.parameters.floorplans):
            raise ValueError(
                f"Floorplan generation returned unexpected number ({len(floorplan_names)}) of configs. "
                f"Expected {len(self.parameters.floorplans)}"
            )

        map_file_parameter_name = self.parameters.name[0]
        mesh_file_parameter_name = self.parameters.name[1]

        results = []
        for floorplan_name in floorplan_names:
            for config in in_configs:
                new_config = _create_config_for_floorplan(
                    floorplan_name,
                    self.output_dir,
                    config,
                    map_file_parameter_name,
                    mesh_file_parameter_name,
                    self.update_config
                )
                results.append(new_config)

        return results


class FloorplanVariation(NavVariation):
    """Create floorplan variation."""

    CONFIG_CLASS = FloorplanVariationConfig
    # GUI_CLASS = FloorplanVariationGui

    def variation(self, in_configs):
        self.progress_update("Running Floorplan Variation...")

        # If no input configs, create initial empty config
        if not in_configs or len(in_configs) == 0:
            in_configs = [{'config': {}, '_config_files': []}]

        floorplan_names = generate_floorplan_variations(self.base_path,
                                                        self.parameters.variation_files,
                                                        self.parameters.num_variations,
                                                        self.parameters.seed,
                                                        self.output_dir,
                                                        self.progress_update)

        if not floorplan_names:
            raise ValueError("Floorplan variation failed, no result returned")
        if len(floorplan_names) != self.parameters.num_variations * len(self.parameters.variation_files):
            raise ValueError(f"Floorplan variation returned unexpected number ({len(floorplan_names)}) of configs. Expected {
                             self.parameters.num_variations * len(self.parameters.variation_files)}")

        map_file_parameter_name = self.parameters.name[0]
        mesh_file_parameter_name = self.parameters.name[1]

        results = []
        for floorplan_name in floorplan_names:
            for config in in_configs:
                new_config = _create_config_for_floorplan(
                    floorplan_name,
                    self.output_dir,
                    config,
                    map_file_parameter_name,
                    mesh_file_parameter_name,
                    self.update_config
                )
                results.append(new_config)

        return results
