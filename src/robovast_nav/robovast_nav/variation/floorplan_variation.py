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

import os

from pydantic import BaseModel, ConfigDict, field_validator

from ..floorplan_generation import generate_floorplan_variations
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
        for value in floorplan_names:
            for config in in_configs:
                base_name = os.path.basename(value).split('_')[0]
                map_file_path = os.path.join(self.output_dir, value, 'maps', base_name + '.yaml')
                mesh_file_path = os.path.join(self.output_dir, value, '3d-mesh', base_name + '.stl')

                if not os.path.exists(map_file_path):
                    raise FileNotFoundError(f"Warning: Map file not found: {map_file_path}")
                if not os.path.exists(mesh_file_path):
                    raise FileNotFoundError(f"Warning: Mesh file not found: {mesh_file_path}")
                rel_map_yaml_path = os.path.join('maps', base_name + '.yaml')
                rel_map_pgm_path = os.path.join('maps', base_name + '.pgm')
                rel_mesh_path = os.path.join('3d-mesh', base_name + '.stl')
                new_config = self.update_config(config, {
                    map_file_parameter_name: rel_map_yaml_path,
                    mesh_file_parameter_name: rel_mesh_path
                },
                    config_files=[
                    (rel_map_yaml_path, map_file_path),
                    (rel_map_pgm_path, os.path.join(self.output_dir, value, 'maps', base_name + '.pgm')),
                    (rel_mesh_path, mesh_file_path)
                ],
                    other_values={
                        '_map_file': map_file_path,
                }
                )
                results.append(new_config)

        return results
