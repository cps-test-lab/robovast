# Copyright (C) 2026 Frederik Pasch
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

"""Compose sampled parameter sets into runnable configs.

This is the bridge from search to the existing generation/packing/execution
path: each :class:`ParamSet` is turned into one ``configuration`` block by
*overriding* a base block with the sampled values, then the existing
``generate_scenario_variations`` chain runs to produce
``campaign_data["configs"]`` — exactly the structure the packer and launchers
already consume. No rewrite of the variation plugins is required: a search dim
that drives a list-style variation simply collapses it to one concrete value,
yielding one config per param set.

Override key convention (a ``search_space`` key is a dotted path):

* ``variations.<ClassName>.<param>[.<sub>...]`` -> set ``param`` inside the
  variation whose single key is ``<ClassName>`` (creating it if absent).
* ``parameters.<name>`` or a bare ``<name>`` -> set scenario parameter ``name``.
"""

import copy
import logging
import os
import tempfile
from typing import Any

import yaml

from robovast.common.common import load_config
from robovast.common.config_generation import generate_scenario_variations

from .types import ParamSet

logger = logging.getLogger(__name__)


def config_name_for(param_set: ParamSet) -> str:
    """Deterministic, schema-valid config/result-dir name for a param set.

    Prefixed with a letter so the name is always lowercase-cased (a pure-digit
    hash would fail the ``configuration.name`` validator).
    """
    return f"c{param_set.id}"


def _set_scenario_param(params: list, name: str, value: Any) -> None:
    """Set scenario parameter ``name`` in a list of single-key dicts."""
    for entry in params:
        if isinstance(entry, dict) and name in entry:
            entry[name] = value
            return
    params.append({name: value})


def _deep_set(d: dict, path: list[str], value: Any) -> None:
    for key in path[:-1]:
        nxt = d.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            d[key] = nxt
        d = nxt
    d[path[-1]] = value


def _find_variation(variations: list, class_name: str) -> dict | None:
    for entry in variations:
        if isinstance(entry, dict) and class_name in entry:
            return entry
    return None


def apply_override(block: dict, key: str, value: Any) -> None:
    """Apply one ``search_space`` override into a configuration block in place."""
    parts = key.split(".")
    head = parts[0]

    if head == "variations":
        if len(parts) < 3:
            raise ValueError(
                f"search_space key '{key}' must be 'variations.<ClassName>.<param>'"
            )
        class_name, nested = parts[1], parts[2:]
        variations = block.setdefault("variations", [])
        entry = _find_variation(variations, class_name)
        if entry is None:
            entry = {class_name: {}}
            variations.append(entry)
        if entry[class_name] is None:
            entry[class_name] = {}
        _deep_set(entry[class_name], nested, value)
        return

    name = parts[1] if head == "parameters" else key
    if head == "parameters" and len(parts) != 2:
        raise ValueError(f"search_space key '{key}' must be 'parameters.<name>'")
    _set_scenario_param(block.setdefault("parameters", []), name, value)


class Compose:
    """Turns parameter sets into ``campaign_data`` using a base ``.vast``."""

    def __init__(self, vast_file: str):
        self.vast_file = os.path.abspath(vast_file)
        self.vast_dir = os.path.dirname(self.vast_file)
        self.base = load_config(self.vast_file)
        # Search synthesizes its configurations from the search space; there is no
        # override template. Config validation enforces that a `search:` section
        # is not paired with a `configuration:` block, so the base is always empty.
        self.base_block: dict = {}

    def compose(self, param_sets: list[ParamSet], output_dir: str) -> tuple[dict, dict]:
        """Generate configs for ``param_sets``.

        Returns ``(campaign_data, name_by_id)`` where ``name_by_id`` maps each
        ``ParamSet.id`` to its config (result-dir) name.
        """
        blocks = []
        name_by_id = {}
        for ps in param_sets:
            block = copy.deepcopy(self.base_block)
            block["name"] = config_name_for(ps)
            for key, value in ps.values.items():
                apply_override(block, key, value)
            blocks.append(block)
            name_by_id[ps.id] = block["name"]

        params = copy.deepcopy(self.base)
        params["configuration"] = blocks
        # Drop any closed-loop block so the generated temp campaign is a plain
        # batch of the composed configs.
        params.pop("search", None)

        # Write the temp .vast in the original dir so relative paths
        # (scenario_file, run_files, …) resolve identically.
        fd, temp_vast = tempfile.mkstemp(
            prefix=".robovast_search_", suffix=".vast", dir=self.vast_dir
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(params, f, sort_keys=False)
            campaign_data, _ = generate_scenario_variations(
                variation_file=temp_vast,
                output_dir=output_dir,
                use_cache=False,
            )
            # Repoint "vast" at the persistent original (same dir, so relative
            # scenario_file/run_files still resolve) so downstream consumers that
            # read or copy the file (e.g. prepare_campaign_configs) don't depend
            # on the temp file we are about to delete.
            campaign_data["vast"] = self.vast_file
        finally:
            try:
                os.remove(temp_vast)
            except OSError:
                pass

        logger.debug("Composed %d param set(s) into %d config(s)",
                     len(param_sets), len(campaign_data.get("configs", [])))
        return campaign_data, name_by_id
