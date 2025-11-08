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

from robovast.common.variation import Variation

from ..floorplan_generation import generate_floorplan_variations


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


class FloorplanVariation(Variation):
    """Create floorplan variation."""

    CONFIG_CLASS = FloorplanVariationConfig

    def variation(self, in_variants):
        self.progress_update("Running Floorplan Variation...")

        # If no input variants, create initial empty variant
        if not in_variants or len(in_variants) == 0:
            in_variants = [{'variant': {}, 'variant_files': []}]

        floorplan_names = generate_floorplan_variations(self.base_path,
                                                        self.parameters.variation_files,
                                                        self.parameters.num_variations,
                                                        self.parameters.seed,
                                                        self.output_dir,
                                                        self.progress_update)

        if not floorplan_names:
            raise ValueError("Floorplan variation failed, no result returned")
        if len(floorplan_names) != self.parameters.num_variations * len(self.parameters.variation_files):
            raise ValueError(f"Floorplan variation returned unexpected number ({len(floorplan_names)}) of variants. Expected {
                             self.parameters.num_variations * len(self.parameters.variation_files)}")

        map_file_parameter_name = self.parameters.name[0]
        mesh_file_parameter_name = self.parameters.name[1]

        results = []
        for value in floorplan_names:
            for variant in in_variants:
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
                new_variant = self.update_variant(variant, {
                    map_file_parameter_name: rel_map_yaml_path,
                    mesh_file_parameter_name: rel_mesh_path
                },
                    variant_files=[rel_map_yaml_path, rel_map_pgm_path, rel_mesh_path],
                    other_values={
                        'variant_file_path': value,
                        'floorplan_variant_path': value
                }
                )
                results.append(new_variant)

        return results
