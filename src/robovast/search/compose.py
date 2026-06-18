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
path: each :class:`ParamSet` is turned into one ``configuration`` block, then the
existing ``generate_scenario_variations`` chain runs to produce
``campaign_data["configs"]`` — exactly the structure the packer and launchers
already consume. No rewrite of the variation plugins is required.

How a sampled value reaches a config:

* **Variation template** — the ``search:`` block may carry a ``variations:`` (and
  ``parameters:``) template, identical in shape to a batch ``configuration``
  block. It fixes most variation parameters inline and references the *searched*
  ones with a ``$name`` / ``${name}`` marker naming a ``search_space`` dimension.
  Compose deep-copies the template per param set and substitutes each marker with
  the sampled value (preserving its native type). This is disjoint from the
  ``@name`` *scenario-parameter* reference resolved inside the variation plugins.
* **Direct scenario parameter (fallback)** — any ``search_space`` dimension *not*
  referenced anywhere in the template is set directly as a scenario parameter
  (the simple-sweep case: no ``variations:`` ⇒ every dim is a scenario param).

A variation in the template must collapse to **exactly one** config per param set
(search relies on a 1:1 paramset→config mapping); Compose enforces this and
reports a clear error if a variation expands combinatorially.
"""

import copy
import logging
import os
import tempfile
from collections import defaultdict
from typing import Any

import yaml

from robovast.common.common import load_config
from robovast.common.config import match_var_marker
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


def _substitute_vars(node: Any, values: dict[str, Any], used: set[str]) -> Any:
    """Deep-copy ``node`` replacing every ``$name`` / ``${name}`` marker leaf with
    ``values[name]`` (verbatim, so the value keeps its native type).

    Records each consumed variable name in ``used``. A leading ``$$`` is an
    escaped literal ``$``. Strings that are not whole-value markers (including
    ``@name`` references and file paths) pass through unchanged. Raises
    ``ValueError`` for a marker that names no declared variable.
    """
    if isinstance(node, dict):
        return {k: _substitute_vars(v, values, used) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_substitute_vars(v, values, used) for v in node]
    if isinstance(node, str):
        name = match_var_marker(node)
        if name is not None:
            if name not in values:
                raise ValueError(
                    f"variations template references '{node}', which is not a "
                    f"search_space variable; declared: {sorted(values)}")
            used.add(name)
            return copy.deepcopy(values[name])
        if node.startswith("$$"):
            return node[1:]  # collapse leading $$ to a literal $
        return node
    return node


class Compose:
    """Turns parameter sets into ``campaign_data`` using a base ``.vast``."""

    def __init__(self, vast_file: str):
        self.vast_file = os.path.abspath(vast_file)
        self.vast_dir = os.path.dirname(self.vast_file)
        self.base = load_config(self.vast_file)
        # The variation/parameter template lives in the search: block. Each param
        # set fills it in; unreferenced search dims fall back to scenario params.
        search = self.base.get("search") or {}
        self.variations_template = search.get("variations")
        self.fixed_parameters = search.get("parameters")

    def compose(self, param_sets: list[ParamSet], output_dir: str) -> tuple[dict, dict]:
        """Generate configs for ``param_sets``.

        Returns ``(campaign_data, name_by_id)`` where ``name_by_id`` maps each
        ``ParamSet.id`` to its config (result-dir) name.
        """
        blocks = []
        id_by_block = {}
        for ps in param_sets:
            used: set[str] = set()
            block_name = config_name_for(ps)
            block: dict = {"name": block_name}
            if self.fixed_parameters is not None:
                block["parameters"] = _substitute_vars(
                    self.fixed_parameters, ps.values, used)
            if self.variations_template is not None:
                block["variations"] = _substitute_vars(
                    self.variations_template, ps.values, used)
            # Any search dim not consumed by the template is a direct scenario
            # parameter (the simple-sweep case, e.g. the quadrotor example).
            for key, value in ps.values.items():
                if key not in used:
                    _set_scenario_param(block.setdefault("parameters", []), key, value)
            blocks.append(block)
            id_by_block[block_name] = ps.id

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

        name_by_id = self._resolve_names(campaign_data, id_by_block)

        logger.debug("Composed %d param set(s) into %d config(s)",
                     len(param_sets), len(campaign_data.get("configs", [])))
        return campaign_data, name_by_id

    @staticmethod
    def _resolve_names(campaign_data: dict, id_by_block: dict) -> dict:
        """Map each ``ParamSet.id`` to its single produced config name, enforcing
        the search 1:1 contract.

        A variation in ``search.variations`` renames its output (``c<id>-1``) and
        may expand a block combinatorially (e.g. ``num_paths > 1`` or a
        list-valued ``path_length``) while ``_config_name`` stays the parent
        block name. Search looks up results by the produced config name, so each
        block must yield exactly one config; an expansion (or an empty result) is
        a configuration error — fail early and clearly.
        """
        produced: dict[str, list] = defaultdict(list)
        for c in campaign_data.get("configs", []):
            produced[c.get("_config_name")].append(c.get("name"))
        name_by_id = {}
        for block_name, ps_id in id_by_block.items():
            got = produced.get(block_name, [])
            if len(got) == 1:
                name_by_id[ps_id] = got[0]
                continue
            if not got:
                raise ValueError(
                    f"Search variation produced no config for param set "
                    f"'{block_name}'. A variation in search.variations filtered "
                    f"everything out — check its parameters (e.g. an impossible "
                    f"path/obstacle constraint).")
            raise ValueError(
                f"Search variation expanded param set '{block_name}' into "
                f"{len(got)} configs ({got}). Each search param set must map to "
                f"exactly one config. Make every expanding parameter scalar: "
                f"PathVariationRandom num_paths=1 and scalar path_length/"
                f"num_goal_poses_per_m; ObstacleVariation count=1 and one amount/"
                f"max_distance per obstacle_configs entry; FloorplanVariation "
                f"num_variations=1.")
        return name_by_id
