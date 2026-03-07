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

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..common import get_scenario_parameters
from ..config import get_validated_config

logger = logging.getLogger(__name__)

# Module-level counter for generating short, unique config indexes.
# All variation classes can call `get_config_index()` to obtain a new
# sequential index. Call `reset_config_index()` to reset back to 0
# (the next `get_config_index()` will return 1).
_config_index = 0  # pylint: disable=invalid-name


def reset_config_index():
    """Reset the shared config index back to zero.

    This should be called whenever a new Variation instance (or new
    variation run) starts so generated short names begin at
    `config1` again.
    """
    global _config_index  # pylint: disable=global-statement
    _config_index = 0


def get_config_index():
    """Return the next unique config index (1-based).

    Thread-safe.
    """
    global _config_index  # pylint: disable=global-statement
    _config_index += 1
    return _config_index


def _to_cache_jsonable(value):
    """Convert value to JSON-serializable form for cache key hashing."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, dict):
        return {k: _to_cache_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_cache_jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "model_dump") and callable(getattr(value, "model_dump")):
        return _to_cache_jsonable(value.model_dump())
    return str(value)


@dataclass
class ProvContribution:
    """Domain-specific PROV-O graph contributions from a variation plugin.

    Returned by :meth:`Variation.collect_prov_metadata` to inject
    domain-specific provenance nodes into the campaign's PROV-O graph.

    Attributes:
        graph_nodes: Extra PROV graph node dicts (entities, activities)
            to append to the ``@graph`` list.
        scenario_properties: Properties to merge onto the concrete
            scenario entity node for this configuration.
        run_used_iris: IRIs of entities that each run activity should
            declare as ``used``.
    """

    graph_nodes: List[Dict[str, Any]] = field(default_factory=list)
    scenario_properties: Dict[str, Any] = field(default_factory=dict)
    run_used_iris: List[str] = field(default_factory=list)


class Variation():

    CONFIG_CLASS = None  # Pydantic model class for config validation
    GUI_CLASS = None  # Could be set to a GUI class for editing
    GUI_RENDERER_CLASS = None  # Could be set to a GUI renderer class
    CACHE_ID = None  # Subclasses set to enable caching (e.g. "robovast_mt_generation_")

    def __init__(self, base_path, parameters, general_parameters, progress_update_callback, scenario_file, output_dir):
        # Reset shared config index for each new Variation instance so
        # generated short names start from 1 for this variation run.
        reset_config_index()
        self.base_path = base_path
        if self.CONFIG_CLASS is not None:
            self.parameters = get_validated_config(parameters, self.CONFIG_CLASS)
        else:
            self.parameters = parameters
        self.general_parameters = general_parameters
        self.progress_update_callback = progress_update_callback
        self.scenario_file = scenario_file
        self.output_dir = output_dir
        # Track the next index for each parent config name
        self._config_child_indices = {}

    def variation(self, in_configs):
        # vary in_configs and return result
        return None

    def get_cache_input_files(self, in_configs):
        """Return file paths that affect variation output. Override when using CACHE_ID."""
        return []

    def get_input_files(self):
        """Return relative file paths (relative to base_path) required as input.

        Override in subclasses to report files that this variation consumes from
        the source directory. These files will be copied into the campaign
        ``_config/`` directory to make the campaign self-contained.

        Returns:
            list[str]: Relative file paths (relative to ``self.base_path``).
        """
        return []

    def get_campaign_transient_files(self):
        """Return intermediate files to be placed in the campaign-level ``_transient/`` directory.

        Override in subclasses to report files created as intermediate artifacts
        during the variation step that are campaign-wide (not specific to a single
        config).  These files will be copied into ``campaign-<id>/_transient/``.

        Must be called after :meth:`variation` has been executed.

        Returns:
            list[tuple[str, str]]: List of ``(relative_path, absolute_path)`` tuples.
                ``relative_path`` is the destination inside ``_transient/``.
        """
        return []

    def progress_update(self, msg):
        self.progress_update_callback(f"{self.__class__.__name__}: {msg}")

    def update_config(self, config, scenario_values, config_files: list = None, other_values=None):
        new_config = copy.deepcopy(config)

        # Ensure config dict exists
        if 'config' not in new_config:
            new_config['config'] = {}

        # Add parameters to config
        for key, val in scenario_values.items():
            new_config['config'][key] = val

        # Add other parameters to config
        if other_values:
            for key, val in other_values.items():
                new_config[key] = val

        # Ensure config_files list exists
        if '_config_files' not in new_config:
            new_config['_config_files'] = []

        new_config['_config_files'].extend(config_files or [])

        # Update config name with automatic per-parent indexing
        parent_name = config['name']
        # Automatically track index per parent config
        if parent_name not in self._config_child_indices:
            self._config_child_indices[parent_name] = 1
        local_index = self._config_child_indices[parent_name]
        self._config_child_indices[parent_name] += 1

        new_config['name'] = f"{parent_name}-{local_index}"
        return new_config

    @classmethod
    def collect_config_metadata(cls, config_entry: dict, config_dir, campaign_dir) -> dict:
        """Return additional metadata fields for a configuration entry.

        Called during metadata generation for each configuration that used
        this variation.  Override in subclasses to attach domain-specific
        metadata (e.g. map or mesh information).

        Args:
            config_entry: The configuration dict from ``configurations.yaml``.
            config_dir: :class:`~pathlib.Path` to
                ``<campaign>/<config-name>/``.
            campaign_dir: :class:`~pathlib.Path` to ``campaign-<id>/``.

        Returns:
            Dictionary of fields to merge into the configuration's metadata
            entry, or an empty dict.
        """
        return {}

    @classmethod
    def collect_prov_metadata(
        cls,
        config_entry: dict,
        campaign_namespace,
        config_namespace,
        gen_activity_id: str,
    ) -> Optional["ProvContribution"]:
        """Return domain-specific PROV-O graph contributions.

        Called during PROV-O generation for each configuration that used
        this variation.  Override in subclasses to contribute
        domain-specific provenance nodes (e.g. map entities, generation
        activities).

        Args:
            config_entry: The configuration metadata dict.
            campaign_namespace: :class:`rdflib.Namespace` for the campaign.
            config_namespace: :class:`rdflib.Namespace` for this config.
            gen_activity_id: IRI of the config-generation activity.

        Returns:
            A :class:`ProvContribution`, or ``None`` to contribute nothing.
        """
        return None

    def check_scenario_parameter_reference(self, reference_name):
        """Check if a scenario parameter reference exists."""
        parameters = get_scenario_parameters(self.scenario_file)
        if not isinstance(parameters, dict) or not len(parameters) == 1:
            raise ValueError("Unexpected scenario parameters format.")

        parameters = next(iter(parameters.values()))
        for param in parameters:
            if param.get('name') == reference_name:
                return
        raise ValueError(f"Scenario parameter reference '{reference_name}' not found in scenario parameters.")
