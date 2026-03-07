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

import logging
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from ..floorplan_generation import (_create_config_for_floorplan,
                                    generate_floorplan_artifacts,
                                    generate_floorplan_variations,
                                    get_scenery_builder_version)
from .nav_base_variation import NavVariation

logger = logging.getLogger(__name__)


# Custom YAML loader that keeps timestamps as strings
class _NoDatetimeLoader(yaml.SafeLoader):
    pass


if hasattr(_NoDatetimeLoader, 'yaml_implicit_resolvers'):
    _NoDatetimeLoader.yaml_implicit_resolvers = {
        k: [r for r in v if r[0] != 'tag:yaml.org,2002:timestamp']
        for k, v in _NoDatetimeLoader.yaml_implicit_resolvers.items()
    }


def _collect_floorplan_transient_files(output_dir, floorplan_name):
    """Collect intermediate files (json-ld, fpm) for a specific floorplan.

    Returns:
        list[tuple[str, str]]: (relative_path, absolute_path) tuples where
            relative_path is relative to ``output_dir/<floorplan_name>/``
            (i.e. starts with ``json-ld/`` or ``fpm/``).
    """
    transient_files = []
    floorplan_dir = os.path.join(output_dir, floorplan_name)
    if not os.path.isdir(floorplan_dir):
        return transient_files
    for subdir in ('json-ld', 'fpm'):
        subdir_path = os.path.join(floorplan_dir, subdir)
        if not os.path.isdir(subdir_path):
            continue
        for dirpath, _dirnames, filenames in os.walk(subdir_path):
            for filename in filenames:
                abs_path = os.path.join(dirpath, filename)
                rel_path = os.path.relpath(abs_path, floorplan_dir)
                transient_files.append((rel_path, abs_path))
    return transient_files


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

    @classmethod
    def collect_config_metadata(cls, config_entry: dict, config_dir: Path, campaign_dir: Path) -> dict:
        """Load map and mesh YAML metadata for this floorplan configuration.

        Reads the map file (``.yaml``) and mesh sidecar (``.stl.yaml``) that
        were generated by this variation and adds their contents to the metadata
        under the same key as the configuration parameter (e.g. ``map_file``,
        ``mesh_file``).
        """
        extra: dict = {}
        config_params = config_entry.get("config", {})
        if not isinstance(config_params, dict):
            return extra

        config_name = config_entry.get("name", "")
        config_files = config_entry.get("config_files", [])

        # Retrieve derived_from_files recorded in the _variations entry for this class
        # and prefix each path with <config-name>/_transient/ to make it campaign-relative.
        derived_from_files = []
        for v in config_entry.get("variations", []):
            if v.get("name") == cls.__name__:
                derived_from_files = [
                    f"{config_name}/_transient/{rel}"
                    for rel in v.get("derived_from_files", [])
                ]
                v.pop("derived_from_files", None)
                break

        # Overwrite derived_from with the recorded transient file paths

        def overwrite_derived_from(data, files):
            if isinstance(data, dict):
                if 'derived_from' in data:
                    data['derived_from'] = files
                for v in data.values():
                    overwrite_derived_from(v, files)
            elif isinstance(data, list):
                for item in data:
                    overwrite_derived_from(item, files)

        for key, value in config_params.items():
            if not isinstance(value, str):
                continue

            candidate = os.path.join(config_name, "_config", value)
            if candidate not in config_files:
                continue

            if value.endswith(".yaml"):
                # Map file — the .yaml itself is the metadata
                yaml_path = campaign_dir / candidate
            elif value.endswith(".stl"):
                # Mesh file — load the .stl.yaml sidecar
                yaml_path = campaign_dir / (candidate + ".yaml")
            else:
                continue

            if yaml_path.exists():
                try:
                    with open(yaml_path, "r", encoding="utf-8") as f:
                        loaded_data = yaml.load(f, Loader=_NoDatetimeLoader)
                        if derived_from_files:
                            overwrite_derived_from(loaded_data, derived_from_files)
                        extra[key] = loaded_data
                except Exception as e:
                    logger.warning(
                        "Failed to load metadata YAML %s: %s", yaml_path, e
                    )

        return extra

    def get_input_files(self):
        return list(self.parameters.floorplans)

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

        scenery_builder_image = get_scenery_builder_version()

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
        for floorplan_idx, floorplan_name in enumerate(floorplan_names):
            transient = _collect_floorplan_transient_files(self.output_dir, floorplan_name)
            fpm_file = self.parameters.floorplans[floorplan_idx]
            for config in in_configs:
                new_config = _create_config_for_floorplan(
                    floorplan_name,
                    self.output_dir,
                    config,
                    map_file_parameter_name,
                    mesh_file_parameter_name,
                    self.update_config
                )
                if not transient:
                    raise FileNotFoundError(
                        f"No generated artifacts found for floorplan '{floorplan_name}' in expected location. "
                    )

                new_config.setdefault('_config_transient_files', []).extend(transient)
                # Also expose the relative paths in the _variations entry via extras
                derived_from_files = [rel for rel, _abs in transient]
                extras = {
                    'derived_from_files': derived_from_files,
                    'fpm_file': f'_config/{fpm_file}'
                }
                if scenery_builder_image:
                    extras['scenery_builder_image'] = scenery_builder_image
                new_config['_variation_entry_extras'] = extras
                results.append(new_config)

        return results


class FloorplanVariation(NavVariation):
    """Create floorplan variation."""

    CONFIG_CLASS = FloorplanVariationConfig
    # GUI_CLASS = FloorplanVariationGui

    def get_input_files(self):
        return list(self.parameters.variation_files)

    def variation(self, in_configs):
        self.progress_update("Running Floorplan Variation...")

        scenery_builder_image = get_scenery_builder_version()

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
        floorplan_idx = 0
        for _, variation_file in enumerate(self.parameters.variation_files):
            for _ in range(self.parameters.num_variations):
                floorplan_name = floorplan_names[floorplan_idx]
                transient = _collect_floorplan_transient_files(self.output_dir, floorplan_name)
                for config in in_configs:
                    new_config = _create_config_for_floorplan(
                        floorplan_name,
                        self.output_dir,
                        config,
                        map_file_parameter_name,
                        mesh_file_parameter_name,
                        self.update_config
                    )
                    if transient:
                        new_config.setdefault('_config_transient_files', []).extend(transient)
                        # Also expose the relative paths in the _variations entry via extras
                        derived_from_files = [rel for rel, _abs in transient]
                        extras = {
                            'derived_from_files': derived_from_files,
                            'variation_file': f'_config/{variation_file}'
                        }
                    else:
                        extras = {
                            'variation_file': f'_config/{variation_file}'
                        }
                    if scenery_builder_image:
                        extras['scenery_builder_image'] = scenery_builder_image
                    new_config['_variation_entry_extras'] = extras
                    results.append(new_config)
                floorplan_idx += 1

        return results
